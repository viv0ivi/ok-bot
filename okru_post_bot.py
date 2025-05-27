import os
import time
import re
import logging
import sys
import threading
import asyncio
import base64
import io
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
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Например: https://your-app.onrender.com/webhook
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() == "true"

# Проверка переменной окружения
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана обязательная переменная окружения: TELEGRAM_BOT_TOKEN")

if not TELEGRAM_USER_ID:
    raise RuntimeError("Не задана обязательная переменная окружения: TELEGRAM_USER_ID")

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

# Функция для отправки скриншота в Telegram
async def send_screenshot_to_telegram(driver, caption="Screenshot", person_name=""):
    try:
        # Делаем скриншот
        screenshot = driver.get_screenshot_as_png()
        
        # Конвертируем в байты для отправки
        screenshot_bytes = io.BytesIO(screenshot)
        screenshot_bytes.name = f'screenshot_{person_name}_{int(time.time())}.png'
        
        # Отправляем в Telegram
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_photo(
            chat_id=TELEGRAM_USER_ID,
            photo=screenshot_bytes,
            caption=f"📸 {caption}\n👤 Профиль: {person_name}\n⏰ Время: {time.strftime('%H:%M:%S')}"
        )
        logger.info(f"📸 Скриншот отправлен в Telegram для {person_name}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка отправки скриншота: {str(e)}")

# Функция для получения всех профилей из переменных окружения
def get_profiles():
    profiles = {}
    i = 1
    while True:
        person = os.getenv(f"OK_PERSON_{i}")
        email = os.getenv(f"OK_EMAIL_{i}")
        password = os.getenv(f"OK_PASSWORD_{i}")
        
        if not person or not email or not password:
            break
            
        profiles[i] = {
            'person': person,
            'email': email,
            'password': password
        }
        i += 1
    
    # Если нет пронумерованных, проверяем без номера
    if not profiles:
        person = os.getenv("OK_PERSON")
        email = os.getenv("OK_EMAIL")
        password = os.getenv("OK_PASSWORD")
        
        if person and email and password:
            profiles[1] = {
                'person': person,
                'email': email,
                'password': password
            }
    
    return profiles

# Класс для работы с OK.ru
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
        # Добавляем дополнительные опции для стабильности
        opts.add_argument('--disable-blink-features=AutomationControlled')
        opts.add_argument('--disable-extensions')
        opts.add_argument('--no-first-run')
        opts.add_argument('--disable-default-apps')
        self.driver = uc.Chrome(options=opts)
        self.wait = WebDriverWait(self.driver, 30)  # Увеличиваем время ожидания
        
    async def send_screenshot(self, caption):
        """Отправка скриншота в Telegram"""
        await send_screenshot_to_telegram(self.driver, caption, self.person_name)
        
    def try_confirm_identity(self):
        try:
            # Отправляем скриншот перед проверкой подтверждения личности
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.send_screenshot("🔍 Проверка подтверждения личности"))
            
            btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                "//input[@value='Yes, confirm']"
                " | //button[contains(text(),'Yes, confirm')]"
                " | //button[contains(text(),'Да, это я')]"
            )))
            btn.click()
            logger.info("Подтверждение личности прошло")
            time.sleep(2)
            
            # Отправляем скриншот после подтверждения
            loop.run_until_complete(self.send_screenshot("✅ После подтверждения личности"))
            
        except Exception as e:
            logger.info("Нет страницы подтверждения личности")
            # Отправляем скриншот текущей страницы
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.send_screenshot("ℹ️ Страница без подтверждения личности"))

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
        raise TimeoutException("SMS-код не получен")

    def try_sms_verification(self):
        try:
            logger.info(f"🔍 Проверяю статус авторизации для {self.person_name}")
            
            # Отправляем скриншот текущего состояния
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.send_screenshot("🔍 Проверка статуса авторизации"))
            
            # Проверяем data-l атрибут
            try:
                body = self.driver.find_element(By.TAG_NAME, 'body')
                data_l = body.get_attribute('data-l') or ''
                logger.info(f"📊 data-l атрибут: {data_l}")
                
                if 'userMain' in data_l and 'anonymMain' not in data_l:
                    logger.info(f"✅ {self.person_name} уже авторизован!")
                    loop.run_until_complete(self.send_screenshot("✅ Успешная авторизация"))
                    return True
            except Exception as e:
                logger.error(f"Ошибка проверки data-l: {str(e)}")
                
            logger.info(f"📱 Требуется SMS-верификация для {self.person_name}")
            
            # Ищем кнопку получения кода
            try:
                logger.info(f"🔄 Ищу кнопку получения SMS-кода для {self.person_name}")
                
                # Пробуем разные варианты селекторов для кнопки получения кода
                get_code_selectors = [
                    "//input[@type='submit' and @value='Get code']",
                    "//input[@type='submit' and contains(@value, 'код')]",  
                    "//button[contains(text(), 'Get code')]",
                    "//button[contains(text(), 'получить код')]",
                    "//input[@type='submit']",
                    "//button[@type='submit']"
                ]
                
                btn = None
                for selector in get_code_selectors:
                    try:
                        btn = self.wait.until(EC.element_to_be_clickable((By.XPATH, selector)))
                        logger.info(f"✅ Найдена кнопка по селектору: {selector}")
                        break
                    except:
                        continue
                
                if not btn:
                    logger.error("❌ Не найдена кнопка получения SMS-кода")
                    loop.run_until_complete(self.send_screenshot("❌ Кнопка получения SMS не найдена"))
                    return False
                
                logger.info(f"🔄 Нажимаю кнопку получения SMS-кода для {self.person_name}")
                btn.click()
                time.sleep(3)
                
                # Отправляем скриншот после нажатия кнопки
                loop.run_until_complete(self.send_screenshot("📱 После нажатия кнопки получения SMS"))
                
                # Проверяем на rate limit
                body_text = self.driver.find_element(By.TAG_NAME,'body').text.lower()
                if 'too often' in body_text or 'слишком часто' in body_text:
                    logger.error(f"⏰ RATE LIMIT на SMS для {self.person_name}! Попробуйте позже")
                    loop.run_until_complete(self.send_screenshot("⏰ Rate limit на SMS"))
                    return False
                    
            except Exception as e:
                logger.error(f"❌ Ошибка при нажатии кнопки получения SMS: {str(e)}")
                loop.run_until_complete(self.send_screenshot(f"❌ Ошибка кнопки SMS: {str(e)}"))
                return False
                
            # Ищем поле для ввода SMS-кода
            try:
                logger.info(f"⌛ Ищу поле для ввода SMS-кода для {self.person_name}")
                
                sms_input_selectors = [
                    "//input[@id='smsCode']",
                    "//input[contains(@name,'smsCode')]",
                    "//input[contains(@name,'code')]",
                    "//input[@type='text' and contains(@placeholder, 'код')]",
                    "//input[@type='text']"
                ]
                
                inp = None
                for selector in sms_input_selectors:
                    try:
                        inp = self.wait.until(EC.presence_of_element_located((By.XPATH, selector)))
                        logger.info(f"✅ Найдено поле ввода SMS по селектору: {selector}")
                        break
                    except:
                        continue
                
                if not inp:
                    logger.error("❌ Не найдено поле для ввода SMS-кода")
                    loop.run_until_complete(self.send_screenshot("❌ Поле SMS не найдено"))
                    return False
                
                # Отправляем скриншот страницы с полем ввода
                loop.run_until_complete(self.send_screenshot("📝 Страница с полем ввода SMS"))
                
            except Exception as e:
                logger.error(f"❌ Ошибка поиска поля SMS: {str(e)}")
                loop.run_until_complete(self.send_screenshot(f"❌ Ошибка поля SMS: {str(e)}"))
                return False
            
            # Ждем SMS-код от пользователя
            logger.info(f"📨 Жду SMS-код от пользователя для {self.person_name}")
            code = self.wait_for_sms_code()
            
            # Вводим SMS-код
            try:
                logger.info(f"🔢 Ввожу SMS-код для {self.person_name}")
                inp.clear()
                inp.send_keys(code)
                time.sleep(1)
                
                # Отправляем скриншот с введенным кодом
                loop.run_until_complete(self.send_screenshot(f"🔢 Введен SMS-код: {code}"))
                
                # Ищем кнопку "Next" или "Далее"
                next_selectors = [
                    "//input[@type='submit' and @value='Next']",
                    "//input[@type='submit' and contains(@value, 'Далее')]",
                    "//button[contains(text(), 'Next')]",
                    "//button[contains(text(), 'Далее')]",
                    "//input[@type='submit']",
                    "//button[@type='submit']"
                ]
                
                next_btn = None
                for selector in next_selectors:
                    try:
                        next_btn = self.driver.find_element(By.XPATH, selector)
                        logger.info(f"✅ Найдена кнопка Next по селектору: {selector}")
                        break
                    except:
                        continue
                
                if not next_btn:
                    logger.error("❌ Не найдена кнопка Next")
                    loop.run_until_complete(self.send_screenshot("❌ Кнопка Next не найдена"))
                    return False
                
                next_btn.click()
                time.sleep(3)
                
                # Отправляем скриншот после нажатия Next
                loop.run_until_complete(self.send_screenshot("✅ После нажатия Next"))
                
                logger.info(f"✅ SMS-верификация успешна для {self.person_name}!")
                return True
                
            except Exception as e:
                logger.error(f"❌ Ошибка ввода SMS-кода для {self.person_name}: {str(e)}")
                loop.run_until_complete(self.send_screenshot(f"❌ Ошибка ввода SMS: {str(e)}"))
                return False
                
        except Exception as e:
            logger.error(f"❌ Ошибка SMS-верификации для {self.person_name}: {str(e)}")
            # Отправляем скриншот при любой ошибке
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.send_screenshot(f"❌ Общая ошибка SMS: {str(e)}"))
            except:
                pass
            return False

    def authenticate(self):
        try:
            logger.info(f"🚀 Начинаю авторизацию для {self.person_name}")
            self.init_driver()
            
            logger.info(f"🌐 Открываю OK.ru для {self.person_name}")
            self.driver.get("https://ok.ru/")
            time.sleep(3)
            
            # Отправляем скриншот главной страницы
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.send_screenshot("🌐 Главная страница OK.ru"))
            
            logger.info(f"📝 Ввожу email для {self.person_name}")
            email_field = self.wait.until(EC.presence_of_element_located((By.NAME,'st.email')))
            email_field.send_keys(self.email)
            
            logger.info(f"🔑 Ввожу пароль для {self.person_name}")
            password_field = self.driver.find_element(By.NAME,'st.password')
            password_field.send_keys(self.password)
            
            # Отправляем скриншот с заполненными полями
            loop.run_until_complete(self.send_screenshot("📝 Заполненная форма входа"))
            
            logger.info(f"✅ Нажимаю кнопку входа для {self.person_name}")
            submit_btn = self.driver.find_element(By.CSS_SELECTOR, "input[type='submit']")
            submit_btn.click()
            time.sleep(5)
            
            # Отправляем скриншот после входа
            loop.run_until_complete(self.send_screenshot("🔐 После нажатия кнопки входа"))
            
            logger.info(f"🔍 Проверяю подтверждение личности для {self.person_name}")
            self.try_confirm_identity()
            
            logger.info(f"📱 Проверяю необходимость SMS-верификации для {self.person_name}")
            if self.try_sms_verification():
                self.authenticated = True
                logger.info(f"🎉 УСПЕШНАЯ АВТОРИЗАЦИЯ для {self.person_name}!")
                loop.run_until_complete(self.send_screenshot("🎉 УСПЕШНАЯ АВТОРИЗАЦИЯ"))
                logger.info(f"✅ {self.person_name} готов к работе. Ожидаю команды...")
                return True
            else:
                logger.error(f"❌ Не удалось пройти SMS-верификацию для {self.person_name}")
                return False
        except Exception as e:
            logger.error(f"💥 ОШИБКА АВТОРИЗАЦИИ для {self.person_name}: {str(e)}")
            # Отправляем скриншот при критической ошибке
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.send_screenshot(f"💥 КРИТИЧЕСКАЯ ОШИБКА: {str(e)}"))
            except:
                pass
            return False

    def wait_for_groups(self):
        global waiting_for_groups, groups_received
        waiting_for_groups = True
        groups_received = None
        
        logger.info("Жду команду #группы")
        while groups_received is None:
            time.sleep(1)
        
        groups = groups_received
        groups_received = None
        waiting_for_groups = False
        logger.info("Список групп получен")
        return groups

    def wait_for_post_info(self):
        global waiting_for_post, post_info_received
        waiting_for_post = True
        post_info_received = None
        
        logger.info("Жду команду #пост")
        while post_info_received is None:
            time.sleep(1)
        
        post_info = post_info_received
        post_info_received = None
        waiting_for_post = False
        logger.info("Инфо для поста получено")
        return post_info

    def post_to_group(self, group_url, video_url, text):
        try:
            self.driver.get(group_url.rstrip('/') + '/post')
            logger.info("Открываю страницу постинга")
            
            # Отправляем скриншот страницы постинга
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.send_screenshot("📝 Страница создания поста"))
            
            field = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
                "div[contenteditable='true']"
            )))
            field.click()
            field.clear()
            
            # 1) вставляем ссылку
            field.send_keys(video_url)
            logger.info("Ссылка вставлена")
            
            # 2) ждём карточку
            logger.info("Жду видео-карточку")
            self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR,
                "div.vid-card.vid-card__xl"
            )))
            
            # Отправляем скриншот с карточкой видео
            loop.run_until_complete(self.send_screenshot("🎥 Карточка видео загружена"))
            
            # 3) по строкам вставляем текст
            logger.info("Вставляю текст по строкам")
            for line in text.splitlines():
                field.send_keys(line)
                field.send_keys(Keys.SHIFT, Keys.ENTER)
                time.sleep(5)
            
            # Отправляем скриншот готового поста
            loop.run_until_complete(self.send_screenshot("📄 Готовый пост перед публикацией"))
            
            # 4) публикуем
            btn = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
                "button.js-pf-submit-btn[data-action='submit']"
            )))
            btn.click()
            logger.info("Пост опубликован")
            time.sleep(3)
            
            # Отправляем скриншот после публикации
            loop.run_until_complete(self.send_screenshot("✅ Пост опубликован"))
            
        except Exception as e:
            logger.error(f"Ошибка публикации поста: {str(e)}")
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.send_screenshot(f"❌ Ошибка публикации: {str(e)}"))
            except:
                pass

    def start_posting_workflow(self):
        try:
            groups = self.wait_for_groups()
            video_url, post_text = self.wait_for_post_info()
            for g in groups:
                self.post_to_group(g, video_url, post_text)
            logger.info("Все задачи выполнены")
        except Exception as e:
            logger.error(f"Ошибка в рабочем процессе: {str(e)}")
            
    def close(self):
        if self.driver:
            self.driver.quit()
            logger.info("Сессия закрыта")

# Функция для запуска авторизации в отдельном потоке
def start_auth_thread(profile_data, profile_id):
    global current_session, current_profile
    logger.info(f"🔄 Создаю сессию для {profile_data['person']}")
    session = OKSession(profile_data['email'], profile_data['password'], profile_data['person'])
    
    logger.info(f"🚀 Запускаю процесс авторизации для {profile_data['person']}")
    if session.authenticate():
        current_session = session
        current_profile = profile_id
        logger.info(f"🎯 Сессия активна для {profile_data['person']}. Готов к получению команд!")
        # После успешной авторизации запускаем рабочий процесс
        logger.info(f"▶️ Запускаю рабочий процесс для {profile_data['person']}")
        session.start_posting_workflow()
    else:
        logger.error(f"🚫 АВТОРИЗАЦИЯ ПРОВАЛЕНА для {profile_data['person']}")
        session.close()

# Обработчик текстовых сообщений
async def handle_message(update, context):
    global waiting_for_sms, waiting_for_groups, waiting_for_post
    global sms_code_received, groups_received, post_info_received
    
    # Проверяем, что сообщение от нужного пользователя
    if str(update.message.chat.id) != TELEGRAM_USER_ID:
        return
    
    text = update.message.text.strip()
    
    # Обработка SMS-кода
    if waiting_for_sms:
        sms_match = re.match(r"^(?:#код\s*)?(\d{4,6})$", text, re.IGNORECASE)
        if sms_match:
            sms_code_received = sms_match.group(1)
            await update.message.reply_text("✅ SMS-код получен!")
            return
    
    # Обработка команды #группы
    if text.lower().startswith("#группы"):
        groups_match = re.match(r"#группы\s+(.+)", text, re.IGNORECASE)
        if groups_match:
            urls = re.findall(r"https?://ok\.ru/group/\d+/?", groups_match.group(1))
            if urls:
                if waiting_for_groups:
                    groups_received = urls
                    await update.message.reply_text(f"✅ Получен список из {len(urls)} групп!")
                else:
                    await update.message.reply_text("❌ Сначала нужно авторизоваться!")
            else:
                await update.message.reply_text("❌ Не найдены корректные ссылки на группы!")
        return
    
    # Обработка команды #пост
    if text.lower().startswith("#пост"):
        post_match = re.match(r"#пост\s+(.+)", text, re.IGNORECASE)
        if post_match:
            rest = post_match.group(1).strip()
            url_match = re.search(r"https?://\S+", rest)
            if url_match:
                video_url = url_match.group(0)
                post_text = rest.replace(video_url, "").strip()
                if waiting_for_post:
                    post_info_received = (video_url, post_text)
                    await update.message.reply_text("✅ Информация для поста получена!")
                else:
                    await update.message.reply_text("❌ Сначала нужно авторизоваться и отправить группы!")
            else:
                await update.message.reply_text("❌ Не найдена ссылка на видео!")
        return

# Telegram бот функции
async def cmd_start(update, context):
    inline_keyboard = [
        [InlineKeyboardButton("🌿 Розгалуджувати", callback_data='branch')]
    ]
    reply_markup = InlineKeyboardMarkup(inline_keyboard)
    
    await update.message.reply_text(
        "Вітаю! Оберіть дію:",
        reply_markup=reply_markup
    )

async def show_profiles(update, context):
    profiles = get_profiles()
    
    if not profiles:
        await update.callback_query.edit_message_text(
            "❌ Профілі не знайдені!\nПеревірте налаштування змінних оточення."
        )
        return
    
    inline_keyboard = []
    for profile_id, profile_data in profiles.items():
        button_text = f"👤 {profile_data['person']}"
        callback_data = f"profile_{profile_id}"
        inline_keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
    
    # Добавляем кнопку "Назад"
    inline_keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_start')])
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard)
    
    await update.callback_query.edit_message_text(
        "Оберіть профіль для авторизації:",
        reply_markup=reply_markup
    )

async def button_callback(update, context):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'branch':
        await show_profiles(update, context)
    
    elif query.data.startswith('profile_'):
        profile_id = int(query.data.split('_')[1])
        profiles = get_profiles()
        
        if profile_id in profiles:
            selected_profile = profiles[profile_id]
            
            message = f"✅ Обрано профіль: {selected_profile['person']}\n"
            message += f"📧 Email: {selected_profile['email']}\n"
            message += "🔄 Виконується авторизація...\n\n"
            message += "📱 Якщо потрібен SMS-код, надішліть його у форматі:\n"
            message += "#код 123456\n\n"
            message += "Після авторизації надішліть:\n"
            message += "📋 #группы [список ссылок]\n"
            message += "📝 #пост [ссылка на видео] [текст поста]\n\n"
            message += "📸 Скриншоты процесса будут отправляться автоматически"
            
            # Добавляем кнопку для возврата к выбору профилей
            inline_keyboard = [
                [InlineKeyboardButton("🔙 Обрати інший профіль", callback_data='branch')],
                [InlineKeyboardButton("🏠 На початок", callback_data='back_to_start')]
            ]
            reply_markup = InlineKeyboardMarkup(inline_keyboard)
            
            await query.edit_message_text(message, reply_markup=reply_markup)
            
            # Запускаем авторизацию в отдельном потоке
            auth_thread = threading.Thread(
                target=start_auth_thread, 
                args=(selected_profile, profile_id)
            )
            auth_thread.daemon = True
            auth_thread.start()
            
            logger.info(f"Запущена авторизация для профиля: {selected_profile['person']}")
        else:
            await query.edit_message_text("❌ Профіль не знайдено!")
    
    elif query.data == 'back_to_start':
        await cmd_start_callback(update, context)

async def cmd_start_callback(update, context):
    inline_keyboard = [
        [InlineKeyboardButton("🌿 Розгалуджувати", callback_data='branch')]
    ]
    reply_markup = InlineKeyboardMarkup(inline_keyboard)
    
    await update.callback_query.edit_message_text(
        "Вітаю! Оберіть дію:",
        reply_markup=reply_markup
    )

# Создание Telegram приложения
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Регистрация обработчиков
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CallbackQueryHandler(button_callback))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# Запуск бота
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    
    if USE_WEBHOOK and WEBHOOK_URL:
        logger.info("🌐 Запуск в режиме Webhook")
        
        # Настройка webhook
        async def set_webhook():
            await application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
            logger.info(f"Webhook установлен: {WEBHOOK_URL}/webhook")
        
        # Запускаем установку webhook
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(set_webhook())
        
        # Запускаем Flask
        logger.info(f"🚀 Запуск Flask сервера на порту {port}")
        app.run(host='0.0.0.0', port=port, debug=False)
        
    else:
        logger.info("🤖 Запуск в режиме Polling")
        
        # Запускаем Flask в отдельном потоке для health check
        def run_flask():
            app.run(host='0.0.0.0', port=port, debug=False)
        
        flask_thread = threading.Thread(target=run_flask)
        flask_thread.daemon = True
        flask_thread.start()
        
        logger.info("🌐 Flask health check запущен")
        
        try:
            # Сначала удаляем webhook если он был установлен
            async def clear_webhook():
                await application.bot.delete_webhook()
                logger.info("Webhook удален")
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(clear_webhook())
            
            # Запускаем polling
            application.run_polling()
        finally:
            # Закрываем активную сессию при завершении
            if current_session:
                logger.info("🔄 Закрываю активную сессию...")
                current_session.close()
            logger.info("👋 Бот остановлен")
