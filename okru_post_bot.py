import os
import time
import re
import logging
import threading
import asyncio
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from flask import Flask, request, jsonify

# Настройки
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_IDS = os.getenv("TELEGRAM_CHAT_IDS", "")
ALLOWED_CHAT_IDS = [cid.strip() for cid in TELEGRAM_CHAT_IDS.split(",") if cid.strip()]
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() == "true"

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана переменная окружения: TELEGRAM_BOT_TOKEN")
if not ALLOWED_CHAT_IDS:
    raise RuntimeError("Не задана переменная окружения: TELEGRAM_CHAT_IDS (список chat_id через запятую)")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("okru_bot")

# Глобальные состояния
current_session = None
current_profile = None
current_user_chat = None

waiting_for_sms = False
waiting_for_groups = False
waiting_for_post = False
sms_code_received = None
groups_received = None
post_info_received = None

# Flask app для webhook
app = Flask(__name__)

async def send_log_to_telegram(text: str):
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=current_user_chat, text=text)
        logger.info(f"Лог отправлен: {text}")
    except Exception as e:
        logger.error(f"Ошибка отправки лога: {e}")

# Загрузка профилей из ENV
def get_profiles():
    profiles = {}
    i = 1
    while True:
        person = os.getenv(f"OK_PERSON_{i}")
        email = os.getenv(f"OK_EMAIL_{i}")
        password = os.getenv(f"OK_PASSWORD_{i}")
        if not person or not email or not password:
            break
        profiles[i] = {'person': person, 'email': email, 'password': password}
        i += 1
    # fallback без номера
    if not profiles:
        p = os.getenv("OK_PERSON"); e = os.getenv("OK_EMAIL"); pw = os.getenv("OK_PASSWORD")
        if p and e and pw:
            profiles[1] = {'person': p, 'email': e, 'password': pw}
    return profiles

# Класс сессии OK.ru без скриншотов, но с логами
class OKSession:
    def __init__(self, email, password, person):
        self.email = email
        self.password = password
        self.person = person
        self.driver = None
        self.wait = None
        self.authenticated = False

    def init_driver(self):
        opts = uc.ChromeOptions()
        opts.add_argument('--headless=new')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--disable-gpu')
        opts.add_argument('--window-size=1920,1080')
        opts.add_argument('--disable-blink-features=AutomationControlled')
        opts.add_argument('--disable-extensions')
        opts.add_argument('--no-first-run')
        opts.add_argument('--disable-default-apps')
        self.driver = uc.Chrome(options=opts)
        self.wait = WebDriverWait(self.driver, 30)

    def log(self, msg: str):
        asyncio.get_event_loop().run_until_complete(
            send_log_to_telegram(f"{self.person}: {msg}"))

    def try_confirm_identity(self):
        try:
            self.log("Проверка подтверждения личности")
            btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                "//input[@value='Yes, confirm'] | //button[contains(text(),'Да, это я')]))"))
            btn.click()
            time.sleep(2)
            self.log("Подтверждение личности прошло")
        except:
            self.log("Нет страницы подтверждения личности")

    def wait_for_sms_code(self, timeout=120):
        global waiting_for_sms, sms_code_received
        waiting_for_sms = True
        sms_code_received = None
        self.log("Ожидаю SMS-код")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if sms_code_received:
                code = sms_code_received
                waiting_for_sms = False
                return code
            time.sleep(1)
        waiting_for_sms = False
        raise TimeoutException("SMS-код не получен")

    def try_sms_verification(self) -> bool:
        try:
            self.log("Проверка статуса авторизации")
            body = self.driver.find_element(By.TAG_NAME, 'body')
            data_l = body.get_attribute('data-l') or ''
            if 'userMain' in data_l and 'anonymMain' not in data_l:
                self.log("Уже авторизован")
                return True
            self.log("Требуется SMS-верификация")
            # Запрос кода
            btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                "//input[@type='submit' and (contains(@value,'код') or contains(@value,'Get code'))] | //button[contains(text(),'код')] | //button[contains(text(),'Get code')]))"))
            btn.click()
            time.sleep(2)
            self.log("Запрошен SMS-код")
            code = self.wait_for_sms_code()
            # Ввод кода
            inp = self.wait.until(EC.presence_of_element_located((By.XPATH,
                "//input[contains(@name,'sms') or contains(@placeholder,'код')]")))
            inp.clear()
            inp.send_keys(code)
            time.sleep(1)
            self.log(f"Введен SMS-код: {code}")
            next_btn = self.driver.find_element(By.XPATH,
                "//input[@type='submit' and (contains(@value,'Next') or contains(@value,'Далее'))] | //button[contains(text(),'Next')] | //button[contains(text(),'Далее')]))"
            )
            next_btn.click()
            time.sleep(3)
            self.log("SMS-верификация успешна")
            return True
        except Exception as e:
            self.log(f"Ошибка SMS-верификации: {e}")
            return False

    def authenticate(self) -> bool:
        try:
            self.log("Начинаю авторизацию")
            self.init_driver()
            self.driver.get("https://ok.ru/")
            time.sleep(3)
            self.log("Главная страница открыта")
            email_field = self.wait.until(EC.presence_of_element_located((By.NAME, 'st.email')))
            email_field.send_keys(self.email)
            password_field = self.driver.find_element(By.NAME, 'st.password')
            password_field.send_keys(self.password)
            self.log("Введены учётные данные")
            self.driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
            time.sleep(5)
            self.log("Нажата кнопка входа")
            self.try_confirm_identity()
            if self.try_sms_verification():
                self.authenticated = True
                self.log("Авторизация успешна")
                return True
            return False
        except Exception as e:
            self.log(f"Критическая ошибка авторизации: {e}")
            return False

    def wait_for_groups(self):
        global waiting_for_groups, groups_received
        waiting_for_groups = True
        groups_received = None
        self.log("Ожидаю #группы")
        while waiting_for_groups:
            time.sleep(1)
        return groups_received

    def wait_for_post_info(self):
        global waiting_for_post, post_info_received
        waiting_for_post = True
        post_info_received = None
        self.log("Ожидаю #пост")
        while waiting_for_post:
            time.sleep(1)
        return post_info_received

    def post_to_group(self, url: str, video_url: str, text: str):
        try:
            self.driver.get(url.rstrip('/') + '/post')
            time.sleep(2)
            self.log("Страница создания поста")
            field = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "div[contenteditable='true']")))
            field.click()
            field.clear()
            field.send_keys(video_url)
            time.sleep(2)
            for line in text.splitlines():
                field.send_keys(line)
                field.send_keys(Keys.SHIFT, Keys.ENTER)
                time.sleep(1)
            self.log("Подготовлен текст поста")
            submit_btn = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.js-pf-submit-btn")))
            submit_btn.click()
            time.sleep(3)
            self.log("Пост опубликован")
        except Exception as e:
            self.log(f"Ошибка публикации: {e}")

    def start_posting_workflow(self):
        groups = self.wait_for_groups()
        video_url, post_text = self.wait_for_post_info()
        for g in groups:
            self.post_to_group(g, video_url, post_text)
        self.log("Все задачи выполнены")

    def close(self):
        if self.driver:
            self.driver.quit()

# Запуск авторизации в потоке
def start_auth_thread(profile, pid, chat_id):
    global current_session, current_profile, current_user_chat
    current_session = None
    current_profile = None
    current_user_chat = chat_id
    session = OKSession(profile['email'], profile['password'], profile['person'])
    if session.authenticate():
        current_session = session
        current_profile = pid
        session.start_posting_workflow()
        session.close()
    else:
        session.close()
    current_session = None
    current_profile = None
    current_user_chat = None

# Обработчики Telegram
async def cmd_start(update: Update, context):
    chat_id = str(update.effective_chat.id)
    if chat_id not in ALLOWED_CHAT_IDS:
        return
    keyboard = [[InlineKeyboardButton("🌿 Розгалуджувати", callback_data='branch')]]
    await update.message.reply_text("Вітаю! Оберіть дію:", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_profiles(update: Update, context):
    profiles = get_profiles()
    keyboard = []
    for pid, prof in profiles.items():
        keyboard.append([InlineKeyboardButton(prof['person'], callback_data=f"profile_{pid}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back')])
    await update.callback_query.edit_message_text("Выберите профиль:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context):
    global current_session
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    if chat_id not in ALLOWED_CHAT_IDS:
        return
    if query.data == 'branch':
        if current_session:
            await query.edit_message_text("❌ Бот занят. Попробуйте позже.")
        else:
            await show_profiles(update, context)
    elif query.data.startswith('profile_'):
        if current_session:
            await query.edit_message_text("❌ Бот занят. Попробуйте позже.")
            return
        pid = int(query.data.split('_')[1])
        profiles = get_profiles()
        if pid in profiles:
            prof = profiles[pid]
            await query.edit_message_text(f"✅ Выбрано {prof['person']}\nЗапускаю авторизацию...")
            thread = threading.Thread(target=start_auth_thread, args=(prof, pid, chat_id), daemon=True)
            thread.start()
        else:
            await query.edit_message_text("❌ Профиль не найден!")
    elif query.data == 'back':
        await cmd_start(update, context)

async def handle_message(update: Update, context):
    global waiting_for_sms, sms_code_received, waiting_for_groups, groups_received, waiting_for_post, post_info_received
    chat_id = str(update.effective_chat.id)
    if chat_id not in ALLOWED_CHAT_IDS:
        return
    text = update.message.text.strip()
    # SMS код
    if waiting_for_sms:
        m = re.match(r"^(?:#код\s*)?(\d{4,6})$", text, re.IGNORECASE)
        if m:
            sms_code_received = m.group(1)
            waiting_for_sms = False
            await update.message.reply_text("✅ SMS-код получен!")
            return
    # Группы
    if text.lower().startswith("#группы"):
        m = re.match(r"#группы\s+(.+)", text, re.IGNORECASE)
        if m and waiting_for_groups:
            urls = re.findall(r"https?://ok\.ru/group/\d+/?", m.group(1))
            if urls:
                groups_received = urls
                waiting_for_groups = False
                await update.message.reply_text(f"✅ Групп: {len(urls)}")
            else:
                await update.message.reply_text("❌ Некорректные ссылки на группы")
        else:
            await update.message.reply_text("❌ Сначала авторизуйтесь или формат неверен")
        return
    # Пост
    if text.lower().startswith("#пост"):
        m = re.match(r"#пост\s+(.+)", text, re.IGNORECASE)
        if m and waiting_for_post:
            rest = m.group(1).strip()
            u = re.search(r"https?://\S+", rest)
            if u:
                video_url = u.group(0)
                post_text = rest.replace(video_url, "").strip()
                post_info_received = (video_url, post_text)
                waiting_for_post = False
                await update.message.reply_text("✅ Инфо для поста получена")
            else:
                await update.message.reply_text("❌ Не найдена ссылка на видео")
        else:
            await update.message.reply_text("❌ Сначала списки групп или формат неверен")
        return

# Инициализация бота
application = Application.builder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CallbackQueryHandler(button_callback))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# Webhook endpoint
@app.route("/webhook", methods=["POST"])
async def webhook():
    data = request.get_json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    if USE_WEBHOOK and WEBHOOK_URL:
        asyncio.get_event_loop().run_until_complete(
            application.bot.set_webhook(f"{WEBHOOK_URL}/webhook")
        )
        logger.info(f"Webhook установлен: {WEBHOOK_URL}/webhook")
        app.run(host='0.0.0.0', port=port)
    else:
        # Запуск Flask для health check
        threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port), daemon=True).start()
        logger.info("Flask health check запущен")
        application.run_polling()
