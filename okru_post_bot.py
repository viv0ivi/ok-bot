import os
import re
import logging
import asyncio
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# Логи
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ENV
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PROFILES = []
for i in range(1, 10):
    email = os.getenv(f"OK_EMAIL_{i}")
    passwd = os.getenv(f"OK_PASSWORD_{i}")
    person = os.getenv(f"OK_PERSON_{i}")
    if email and passwd and person:
        PROFILES.append({"email": email, "password": passwd, "person": person})
# fallback без номера
if not PROFILES:
    email = os.getenv("OK_EMAIL")
    passwd = os.getenv("OK_PASSWORD")
    person = os.getenv("OK_PERSON")
    if email and passwd and person:
        PROFILES.append({"email": email, "password": passwd, "person": person})

if not TELEGRAM_TOKEN or not PROFILES:
    raise RuntimeError("Необходимы TELEGRAM_BOT_TOKEN и хотя бы один профиль OK")

# Состояния ConversationHandler
SELECT_PROFILE, SMS_CODE, GROUPS, POST_INFO = range(4)

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
            # Ввод creds
            self.wait.until(EC.presence_of_element_located((By.NAME, 'st.email'))).send_keys(self.email)
            self.driver.find_element(By.NAME, 'st.password').send_keys(self.password)
            self.driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
            asyncio.sleep(2)
            # Подтверждение личности
            try:
                btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                    "//button[contains(text(),'Yes, confirm')] | //button[contains(text(),'Да, это я')]"
                )))
                btn.click()
                asyncio.sleep(1)
                logger.info("Identity confirmed")
            except TimeoutException:
                logger.info("No identity confirmation page")
            # Проверка на SMS
            body = self.driver.find_element(By.TAG_NAME, 'body').text.lower()
            if 'sms' in body or self.driver.find_elements(By.ID, 'smsCode'):
                return True  # требуется SMS
            return False  # авторизован без SMS
        except WebDriverException as e:
            logger.error(f"Login error: {e}")
            self.close()
            raise

    def submit_sms(self, code: str) -> bool:
        try:
            inp = self.wait.until(EC.presence_of_element_located((By.XPATH,
                "//input[@id='smsCode' or contains(@name,'smsCode')]"
            )))
            inp.clear()
            inp.send_keys(code)
            self.driver.find_element(By.CSS_SELECTOR, "input[type='submit'][value='Next']").click()
            asyncio.sleep(2)
            # Проверка успешности
            body = self.driver.find_element(By.TAG_NAME, 'body').get_attribute('data-l') or ''
            auth_ok = 'userMain' in body
            if not auth_ok:
                logger.error("SMS verification failed or not authorized")
            return auth_ok
        except Exception as e:
            logger.error(f"Error submitting SMS code: {e}")
            return False

    def post_to_group(self, group_url: str, video_url: str, text: str):
        self.driver.get(group_url.rstrip('/') + '/post')
        field = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "div[contenteditable='true']")))
        field.click()
        field.clear()
        field.send_keys(video_url)
        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.vid-card.vid-card__xl")))
        for line in text.splitlines():
            field.send_keys(line)
            field.send_keys(Keys.SHIFT, Keys.ENTER)
            asyncio.sleep(1)
        btn = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
            "button.js-pf-submit-btn[data-action='submit']"
        )))
        btn.click()
        asyncio.sleep(1)

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(profile['person'], callback_data=str(i))]
                for i, profile in enumerate(PROFILES)]
    reply = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите профиль:", reply_markup=reply)
    return SELECT_PROFILE

async def select_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data)
    profile = PROFILES[idx]
    session = OKSession(profile['email'], profile['password'], profile['person'])
    context.user_data['session'] = session
    # Запуск login в фоне
    needs_sms = await asyncio.get_event_loop().run_in_executor(None, session.login)
    if needs_sms:
        await query.edit_message_text("Введите SMS-код:")
        return SMS_CODE
    else:
        await query.edit_message_text("Авторизация прошла успешно! Введите список групп через #группы")
        return GROUPS

async def sms_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = re.match(r"#код\s*(\d{4,6})", update.message.text)
    if not code:
        return SMS_CODE
    session: OKSession = context.user_data['session']
    ok = await asyncio.get_event_loop().run_in_executor(None, session.submit_sms, code.group(1))
    if ok:
        await update.message.reply_text("✅ SMS подтверждён. Введите список групп через #группы")
        return GROUPS
    else:
        await update.message.reply_text("❌ Неверный код. Попробуйте ещё раз:")
        return SMS_CODE

async def groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = re.match(r"#группы\s+(.+)", update.message.text)
    if not match:
        return GROUPS
    urls = re.findall(r"https?://ok\.ru/group/\d+" , match.group(1))
    if not urls:
        await update.message.reply_text("Не нашёл ссылок на группы. Попробуйте ещё раз:")
        return GROUPS
    context.user_data['groups'] = urls
    await update.message.reply_text("Список групп сохранён. Теперь отправьте #пост <ссылка> <текст поста>")
    return POST_INFO

async def post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = re.match(r"#пост\s+(https?://\S+)\s+([\s\S]+)", update.message.text)
    if not match:
        return POST_INFO
    video_url, text = match.group(1), match.group(2)
    session: OKSession = context.user_data['session']
    groups = context.user_data['groups']
    await update.message.reply_text("Публикую пост... Это может занять время.")
    def do_post():
        for g in groups:
            session.post_to_group(g, video_url, text)
        session.close()
    await asyncio.get_event_loop().run_in_executor(None, do_post)
    await update.message.reply_text("✅ Пост опубликован во всех группах.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'session' in context.user_data:
        context.user_data['session'].close()
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECT_PROFILE: [CallbackQueryHandler(select_profile)],
            SMS_CODE: [MessageHandler(filters.Regex(r'^#код'), sms_code_handler)],
            GROUPS: [MessageHandler(filters.Regex(r'^#группы'), groups_handler)],
            POST_INFO: [MessageHandler(filters.Regex(r'^#пост'), post_handler)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(conv)
    logger.info("Запуск бота...")
    app.run_polling()
