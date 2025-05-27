import os
import time
import re
import logging
import sys
import threading
import asyncio
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from flask import Flask, request, jsonify
import json

# Настройки из ENV
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = str(os.getenv("TELEGRAM_USER_ID"))  # Приводим к строке для chat_id
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Например: https://your-app.onrender.com/webhook
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() == "true"

# Проверка переменных окружения
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана обязательная переменная окружения: TELEGRAM_BOT_TOKEN")
if not TELEGRAM_USER_ID:
    raise RuntimeError("Не задана обязательная переменная окружения: TELEGRAM_USER_ID")

# Инициализация глобального Telegram-бота для отправки скриншотов
telegram_bot = Bot(token=TELEGRAM_TOKEN)

# Функция для отправки скриншота через Telegram
def send_screenshot(driver, caption: str):
    """
    Сохраняет скриншот из Selenium и отправляет его в Telegram.
    """
    try:
        png = driver.get_screenshot_as_png()
        telegram_bot.send_photo(
            chat_id=TELEGRAM_USER_ID,
            photo=png,
            caption=caption
        )
    except Exception as e:
        logging.error(f"Не удалось отправить скриншот: {e}")

# Логирование
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("okru_bot")

# Глобальные переменные для сессии и ожидания команд
current_session = None
current_profile = None
waiting_for_sms = False
waiting_for_groups = False
waiting_for_post = False
sms_code_received = None
groups_received = None
post_info_received = None

# Flask app
app = Flask(__name__)

# Telegram приложение (глобальная переменная)
application = None

@app.route('/')
def health_check():
    return jsonify({"status": "ok", "message": "Bot is running"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

@app.route('/webhook', methods=['POST'])
async def webhook():
    """Обработчик webhook от Telegram"""
    if request.content_type == 'application/json':
        try:
            update_data = request.get_json()
            update = Update.de_json(update_data, application.bot)
            await application.process_update(update)
            return jsonify({"status": "ok"})
        except Exception as e:
            logger.error(f"Ошибка обработки webhook: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "error", "message": "Invalid content type"}), 400

# Функция для получения всех профилей из переменных окружения
... # остальной код get_profiles, OKSession и другие без изменений, но обновленный authenticate и try_sms_verification

class OKSession:
    def __init__(self, email, password, person_name):
        self.email = email
        self.password = password
        self.person_name = person_name
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
        self.driver = uc.Chrome(options=opts)
        self.wait = WebDriverWait(self.driver, 20)

    def try_confirm_identity(self):
        try:
            btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                "//input[@value='Yes, confirm']"
                " | //button[contains(text(),'Yes, confirm')]"
                " | //button[contains(text(),'Да, это я')]"
            )))
            btn.click()
            logger.info("Подтверждение личности прошло")
            time.sleep(1)
            send_screenshot(self.driver, f"Подтверждение личности для {self.person_name}")
        except:
            logger.info("Нет страницы подтверждения личности")
            send_screenshot(self.driver, f"Нет подтверждения личности для {self.person_name}")

    def wait_for_sms_code(self, timeout=120):
        global waiting_for_sms, sms_code_received
        waiting_for_sms = True
        sms_code_received = None
        logger.info("Ожидаю SMS-код")
        deadline = time.time() + timeout

        while time.time() < deadline:
            if sms_code_received is not None:
                code = sms_code_received
                sms_code_received = None
                waiting_for_sms = False
                logger.info("SMS-код получен")
                return code
            time.sleep(1)

        waiting_for_sms = False
        logger.error("Не получили SMS-код")
        send_screenshot(self.driver, f"Таймаут ожидания SMS-кода для {self.person_name}")
        raise TimeoutException("SMS-код не получен")

    def try_sms_verification(self):
        try:
            logger.info(f"🔍 Проверяю статус авторизации для {self.person_name}")
            data_l = self.driver.find_element(By.TAG_NAME,'body').get_attribute('data-l') or ''
            if 'userMain' in data_l and 'anonymMain' not in data_l:
                logger.info(f"✅ {self.person_name} уже авторизован!")
                send_screenshot(self.driver, f"Уже авторизован: {self.person_name}")
                return True

            logger.info(f"📱 Требуется SMS-верификация для {self.person_name}")
            logger.info(f"🔄 Запрашиваю SMS-код для {self.person_name}")
            btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                "//input[@type='submit' and @value='Get code']"
            )))
            btn.click()
            time.sleep(1)
            send_screenshot(self.driver, f"Запрошен SMS-код для {self.person_name}")

            inp = self.wait.until(EC.presence_of_element_located((By.XPATH,
                "//input[@id='smsCode' or contains(@name,'smsCode')]"
            )))

            logger.info(f"📨 Жду SMS-код от пользователя для {self.person_name}")
            code = self.wait_for_sms_code()

            logger.info(f"🔢 Ввожу SMS-код для {self.person_name}")
            inp.clear()
            inp.send_keys(code)
            send_screenshot(self.driver, f"Ввели SMS-код {code} для {self.person_name}")

            next_btn = self.driver.find_element(By.XPATH,
                "//input[@type='submit' and @value='Next']"
            )
            next_btn.click()
            time.sleep(1)
            send_screenshot(self.driver, f"После submit SMS-кода для {self.person_name}")

            logger.info(f"✅ SMS-верификация успешна для {self.person_name}!")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка SMS-верификации для {self.person_name}: {e}")
            send_screenshot(self.driver, f"Ошибка SMS-верификации: {e}")
            return False

    def authenticate(self):
        try:
            logger.info(f"🚀 Начинаю авторизацию для {self.person_name}")
            self.init_driver()
            logger.info(f"🌐 Открываю OK.ru для {self.person_name}")
            self.driver.get("https://ok.ru/")
            send_screenshot(self.driver, f"Открыта OK.ru для {self.person_name}")

            logger.info(f"📝 Ввожу email для {self.person_name}")
            self.wait.until(EC.presence_of_element_located((By.NAME,'st.email'))).send_keys(self.email)
            logger.info(f"🔑 Ввожу пароль для {self.person_name}")
            self.driver.find_element(By.NAME,'st.password').send_keys(self.password)
            send_screenshot(self.driver, f"Вводим учетные данные для {self.person_name}")

            logger.info(f"✅ Нажимаю кнопку входа для {self.person_name}")
            self.driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
            time.sleep(2)
            send_screenshot(self.driver, f"Нажали Войти для {self.person_name}")

            logger.info(f"🔍 Проверяю подтверждение личности для {self.person_name}")
            self.try_confirm_identity()

            logger.info(f"📱 Проверяю необходимость SMS-верификации для {self.person_name}")
            if self.try_sms_verification():
                self.authenticated = True
                logger.info(f"🎉 УСПЕШНАЯ АВТОРИЗАЦИЯ для {self.person_name}!")
                send_screenshot(self.driver, f"Авторизация успешна для {self.person_name}")
                return True
            else:
                logger.error(f"❌ Не удалось пройти SMS-верификацию для {self.person_name}")
                send_screenshot(self.driver, f"Провал авторизации для {self.person_name}")
                return False
        except Exception as e:
            logger.error(f"💥 ОШИБКА АВТОРИЗАЦИИ для {self.person_name}: {e}")
            send_screenshot(self.driver, f"Исключение при авторизации: {e}")
            return False

    # Остальные методы без изменений: wait_for_groups, wait_for_post_info, post_to_group, start_posting_workflow, close

# Функция для запуска авторизации в отдельном потоке
... # остальной код запуска и handlers без изменений

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    # Логика запуска webhook или polling без изменений
