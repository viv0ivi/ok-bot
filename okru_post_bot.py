import os
import time
import re
import random
import logging
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler, MessageHandler, Filters

# Настройки из ENV
EMAIL = os.getenv("OK_EMAIL")
PASSWORD = os.getenv("OK_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = int(os.getenv("TELEGRAM_USER_ID", 0))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Проверка переменных
for var_name, val in [("OK_EMAIL", EMAIL), ("OK_PASSWORD", PASSWORD), ("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN), ("TELEGRAM_USER_ID", TELEGRAM_USER_ID), ("WEBHOOK_URL", WEBHOOK_URL)]:
    if not val:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {var_name}")

# Логирование
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("okru_bot")

# WebDriver
def init_driver():
    opts = uc.ChromeOptions()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--window-size=1920,1080')
    return uc.Chrome(options=opts)

driver = None
wait = None

def start_driver():
    global driver, wait
    driver = init_driver()
    wait = WebDriverWait(driver, 20)

def post_to_group(group_url: str, video_url: str, text: str):
    post_url = group_url.rstrip('/') + '/post'
    driver.get(post_url)
    box = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "div[contenteditable='true']")))
    box.click(); box.clear()
    box.send_keys(video_url)
    box.send_keys(Keys.SPACE)
    attached = False
    for _ in range(10):
        if driver.find_elements(By.CSS_SELECTOR, "div.vid-card.vid-card__xl"):
            attached = True
            break
        time.sleep(1)
    if not attached:
        logger.warning(f"Не дождался превью для {group_url}")
    box.send_keys(" " + text)
    btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.js-pf-submit-btn[data-action='submit']")))
    btn.click()
    time.sleep(1)

# Telegram Webhook Bot setup
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(bot, None, workers=0)

# /start command
def cmd_start(update, context):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    keyboard = [
        [InlineKeyboardButton("Указать группы", callback_data="set_groups"),
         InlineKeyboardButton("Указать пост", callback_data="set_post")],
        [InlineKeyboardButton("Запустить публикацию", callback_data="run_post")]
    ]
    update.message.reply_text("Выберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))

# Button handler
def button_handler(update, context):
    query = update.callback_query
    if query.from_user.id != TELEGRAM_USER_ID:
        return
    data = query.data
    query.answer()
    if data == "set_groups":
        query.edit_message_text("Отправьте URL групп (через пробел/новую строку):")
        context.user_data["expect"] = "groups"
    elif data == "set_post":
        query.edit_message_text("Отправьте сообщение вида:\n#пост https://ok.ru/video/... Текст")
        context.user_data["expect"] = "post"
    elif data == "run_post":
        groups = context.user_data.get("groups") or []
        post = context.user_data.get("post")
        if not groups or not post:
            query.edit_message_text("Сначала задайте группы и текст поста!")
            return
        query.edit_message_text("Запускаю публикацию...")
        start_driver()
        driver.get("https://ok.ru/")
        wait.until(EC.presence_of_element_located((By.NAME, "st.email"))).send_keys(EMAIL)
        driver.find_element(By.NAME, "st.password").send_keys(PASSWORD)
        driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
        time.sleep(2)
        video_url, text = post
        for idx, g in enumerate(groups, 1):
            post_to_group(g, video_url, text)
            delay = random.uniform(60, 120)
            bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"Опубликовано в {g} ({idx}/{len(groups)}). Жду {int(delay)} сек.")
            time.sleep(delay)
        bot.send_message(chat_id=TELEGRAM_USER_ID, text="🎉 Публикация завершена.")
        driver.quit()

# Message handler
def message_handler(update, context):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    expect = context.user_data.get("expect")
    txt = update.message.text.strip()
    if expect == "groups":
        urls = re.findall(r"https?://ok\.ru/group/\d+/?", txt)
        context.user_data["groups"] = urls
        update.message.reply_text(f"Сохранено {len(urls)} групп.")
        context.user_data.pop("expect", None)
    elif expect == "post":
        m = re.match(r"#пост\s+(https?://\S+)\s+(.+)", txt, re.IGNORECASE)
        if m:
            context.user_data["post"] = (m.group(1), m.group(2))
            update.message.reply_text("Пост сохранён.")
        else:
            update.message.reply_text("Неверный формат. Используйте: #пост URL Текст")
        context.user_data.pop("expect", None)

# Register handlers
dp.add_handler(CommandHandler("start", cmd_start))
dp.add_handler(CallbackQueryHandler(button_handler))
dp.add_handler(MessageHandler(Filters.text & ~Filters.command, message_handler))

# Flask app and webhook endpoint
app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, bot)
    dp.process_update(update)
    return "OK", 200

if __name__ == "__main__":
    # Set webhook
    bot.setWebhook(WEBHOOK_URL)
    # Run Flask on port provided by Render
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
