#!/usr/bin/env python3
import os
import re
import logging
import asyncio

from flask import Flask, request, jsonify
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

# --- Логирование ---
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Загрузка профилей из ENV ---
PROFILES = []
for i in range(1, 10):
    email = os.getenv(f"OK_EMAIL_{i}")
    password = os.getenv(f"OK_PASSWORD_{i}")
    person = os.getenv(f"OK_PERSON_{i}")
    if email and password and person:
        PROFILES.append({"email": email, "password": password, "person": person})
# fallback без номера
if not PROFILES:
    email = os.getenv("OK_EMAIL")
    password = os.getenv("OK_PASSWORD")
    person = os.getenv("OK_PERSON")
    if email and password and person:
        PROFILES.append({"email": email, "password": password, "person": person})

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # например: my-service.onrender.com

if not TELEGRAM_TOKEN or not PROFILES or not RENDER_EXTERNAL_URL:
    raise RuntimeError("Необходимо задать TELEGRAM_BOT_TOKEN, RENDER_EXTERNAL_URL и хотя бы один профиль OK")

# --- Conversation states ---
SELECT_PROFILE, SMS_CODE, GROUPS, POST_INFO = range(4)

# --- Класс для работы с OK.ru через Selenium ---
class OKSession:
    def __init__(self, email: str, password: str, person: str):
        self.email = email
        self.password = password
        self.person = person
        self.driver = None
        self.wait = None

    def init_driver(self):
        opts = uc.ChromeOptions()
        opts.add_argument('--headless=new')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--disable-gpu')
        opts.add_argument('--window-size=1920,1080')
        self.driver = uc.Chrome(options=opts)
        self.wait = WebDriverWait(self.driver, 20)

    def login(self) -> bool:
        try:
            self.init_driver()
            self.driver.get("https://ok.ru/")
            # Ввод email и пароля
            self.wait.until(EC.presence_of_element_located((By.NAME, 'st.email'))).send_keys(self.email)
            self.driver.find_element(By.NAME, 'st.password').send_keys(self.password)
            self.driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
            asyncio.sleep(2)

            # Подтверждение личности, если нужно
            try:
                btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                    "//button[contains(text(),'Yes, confirm')] | //button[contains(text(),'Да, это я')]"
                )))
                btn.click(); asyncio.sleep(1)
                logger.info("Identity confirmed for %s", self.person)
            except TimeoutException:
                logger.info("No identity confirmation page for %s", self.person)

            # Проверяем, нужен ли SMS-код
            body = self.driver.find_element(By.TAG_NAME, 'body').text.lower()
            if 'sms' in body or self.driver.find_elements(By.ID, 'smsCode'):
                logger.info("SMS verification required for %s", self.person)
                return True

            logger.info("Logged in without SMS for %s", self.person)
            return False
        except WebDriverException as e:
            logger.error("Login error for %s: %s", self.person, e)
            self.close()
            raise

    def submit_sms(self, code: str) -> bool:
        try:
            inp = self.wait.until(EC.presence_of_element_located((By.XPATH,
                "//input[@id='smsCode' or contains(@name,'smsCode')]"
            )))
            inp.clear(); inp.send_keys(code)
            self.driver.find_element(By.CSS_SELECTOR, "input[type='submit'][value='Next']").click()
            asyncio.sleep(2)
            data_l = self.driver.find_element(By.TAG_NAME, 'body').get_attribute('data-l') or ''
            success = 'userMain' in data_l
            if success:
                logger.info("SMS confirmed for %s", self.person)
            else:
                logger.error("SMS verification failed for %s", self.person)
            return success
        except Exception as e:
            logger.error("Error submitting SMS for %s: %s", self.person, e)
            return False

    def post_to_group(self, group_url: str, video_url: str, text: str):
        self.driver.get(group_url.rstrip('/') + '/post')
        field = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "div[contenteditable='true']")))
        field.click(); field.clear(); field.send_keys(video_url)
        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.vid-card.vid-card__xl")))
        for line in text.splitlines():
            field.send_keys(line); field.send_keys(Keys.SHIFT, Keys.ENTER); asyncio.sleep(1)
        btn = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
            "button.js-pf-submit-btn[data-action='submit']"
        )))
        btn.click(); asyncio.sleep(1)
        logger.info("Posted to %s for %s", group_url, self.person)

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass

# --- Handlers для ConversationHandler ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(p['person'], callback_data=str(i))] for i, p in enumerate(PROFILES)]
    await update.message.reply_text("Выберите профиль:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_PROFILE

async def select_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    idx = int(query.data); profile = PROFILES[idx]
    session = OKSession(profile['email'], profile['password'], profile['person'])
    context.user_data['session'] = session
    needs_sms = await asyncio.get_event_loop().run_in_executor(None, session.login)
    if needs_sms:
        await query.edit_message_text("Введите SMS-код в виде: #код 123456")
        return SMS_CODE
    await query.edit_message_text("Авторизация успешна! Введите список групп через #группы")
    return GROUPS

async def sms_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"#код\s*(\d{4,6})", update.message.text)
    if not m:
        await update.message.reply_text("Неверный формат. Повторите: #код 123456")
        return SMS_CODE
    code = m.group(1)
    session: OKSession = context.user_data['session']
    ok = await asyncio.get_event_loop().run_in_executor(None, session.submit_sms, code)
    if ok:
        await update.message.reply_text("✅ SMS подтверждён. Введите список групп через #группы")
        return GROUPS
    await update.message.reply_text("❌ Код неверен. Попробуйте снова:")
    return SMS_CODE

async def groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"#группы\s+(.+)", update.message.text)
    if not m:
        await update.message.reply_text("Укажите группы так: #группы https://ok.ru/group/123 ...")
        return GROUPS
    urls = re.findall(r"https?://ok\.ru/group/\d+", m.group(1))
    if not urls:
        await update.message.reply_text("Не нашёл ссылок. Попробуйте снова:")
        return GROUPS
    context.user_data['groups'] = urls
    await update.message.reply_text("Группы сохранены. Теперь отправьте #пост <ссылка> <текст>")
    return POST_INFO

async def post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"#пост\s+(https?://\S+)\s+([\s\S]+)", update.message.text)
    if not m:
        await update.message.reply_text("Неверный формат. Используйте: #пост <ссылка> <текст>")
        return POST_INFO
    video_url, text = m.group(1), m.group(2)
    session: OKSession = context.user_data['session']
    groups = context.user_data['groups']
    await update.message.reply_text("Публикую во всех группах...")
    await asyncio.get_event_loop().run_in_executor(None, lambda: [session.post_to_group(g, video_url, text) for g in groups])
    session.close()
    await update.message.reply_text("✅ Пост опубликован во всех группах.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'session' in context.user_data:
        context.user_data['session'].close()
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# --- Flask-приложение для health-check и webhook ---
flask_app = Flask(__name__)

@flask_app.route('/health', methods=['GET'])
def health():
    return jsonify(status="ok"), 200

@flask_app.route(f'/webhook/{TELEGRAM_TOKEN}', methods=['POST'])
async def telegram_webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return '', 200

# --- Инициализация Telegram Application & Handlers ---
bot = Bot(TELEGRAM_TOKEN)
application = Application.builder().bot(bot).build()
conv = ConversationHandler(
    entry_points=[CommandHandler('start', start)],
    states={
        SELECT_PROFILE: [CallbackQueryHandler(select_profile)],
        SMS_CODE: [MessageHandler(filters.Regex(r'^#код'), sms_code_handler)],
        GROUPS: [MessageHandler(filters.Regex(r'^#группы'), groups_handler)],
        POST_INFO: [MessageHandler(filters.Regex(r'^#пост'), post_handler)],
    },
    fallbacks=[CommandHandler('cancel', cancel)],
)
application.add_handler(conv)

if __name__ == '__main__':
    # Регистрируем Webhook у Telegram
    webhook_url = f"https://{RENDER_EXTERNAL_URL}/webhook/{TELEGRAM_TOKEN}"
    bot.delete_webhook(drop_pending_updates=True)
    bot.set_webhook(webhook_url)
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Starting Flask on 0.0.0.0:{port}, webhook={webhook_url}")
    flask_app.run(host='0.0.0.0', port=port)
