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
from selenium.common.exceptions import TimeoutException, WebDriverException
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
bot_running = True

# Flask app
app = Flask(__name__)

# Telegram приложение (глобальная переменная)
application = None

# Функция для отправки сообщений в Telegram
async def send_telegram_message(text):
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=text)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения в Telegram: {e}")

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
        
    async def send_status(self, message):
        """Отправляет статус авторизации в Telegram"""
        status_message = f"👤 {self.person_name}\n{message}"
        await send_telegram_message(status_message)
        
    def init_driver(self):
        """Инициализация драйвера с несколькими попытками"""
        max_attempts = 3
        
        for attempt in range(max_attempts):
            try:
                logger.info(f"Попытка {attempt + 1} инициализации Chrome драйвера")
                
                opts = uc.ChromeOptions()
                opts.add_argument('--headless=new')
                opts.add_argument('--no-sandbox')
                opts.add_argument('--disable-dev-shm-usage')
                opts.add_argument('--disable-gpu')
                opts.add_argument('--window-size=1920,1080')
                opts.add_argument('--disable-web-security')
                opts.add_argument('--allow-running-insecure-content')
                opts.add_argument('--disable-extensions')
                opts.add_argument('--disable-plugins')
                opts.add_argument('--disable-images')
                opts.add_argument('--disable-javascript')
                opts.add_argument('--disable-default-apps')
                opts.add_argument('--disable-background-timer-throttling')
                opts.add_argument('--disable-backgrounding-occluded-windows')
                opts.add_argument('--disable-renderer-backgrounding')
                opts.add_argument('--disable-features=TranslateUI')
                opts.add_argument('--disable-ipc-flooding-protection')
                
                # Попробуем разные способы инициализации
                if attempt == 0:
                    # Первая попытка - автоматическое управление версиями
                    self.driver = uc.Chrome(options=opts, version_main=None)
                elif attempt == 1:
                    # Вторая попытка - использовать установленный Chrome
                    self.driver = uc.Chrome(options=opts, use_subprocess=False)
                else:
                    # Третья попытка - принудительное скачивание совместимой версии
                    self.driver = uc.Chrome(options=opts, driver_executable_path=None)
                
                self.wait = WebDriverWait(self.driver, 20)
                logger.info("Chrome драйвер успешно инициализирован")
                return True
                
            except WebDriverException as e:
                logger.warning(f"Попытка {attempt + 1} неудачна: {str(e)}")
                if attempt == max_attempts - 1:
                    logger.error("Все попытки инициализации Chrome драйвера провалились")
                    raise e
                time.sleep(2)
            except Exception as e:
                logger.error(f"Неожиданная ошибка при инициализации драйвера: {e}")
                if attempt == max_attempts - 1:
                    raise e
                time.sleep(2)
        
        return False
        
    async def try_confirm_identity(self):
        try:
            btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                "//input[@value='Yes, confirm']"
                " | //button[contains(text(),'Yes, confirm')]"
                " | //button[contains(text(),'Да, это я')]"
            )))
            btn.click()
            await self.send_status("✅ Личность подтверждена")
            time.sleep(1)
        except:
            await self.send_status("ℹ️ Подтверждение не требуется")

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

    async def try_sms_verification(self):
        try:
            await self.send_status("🔍 Проверяю статус...")
            data_l = self.driver.find_element(By.TAG_NAME,'body').get_attribute('data-l') or ''
            if 'userMain' in data_l and 'anonymMain' not in data_l:
                await self.send_status("✅ Уже авторизован!")
                return True
                
            await self.send_status("📱 Нужна SMS-верификация")
            btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                "//input[@type='submit' and @value='Get code']"
            )))
            btn.click()
            time.sleep(1)
            
            body_text = self.driver.find_element(By.TAG_NAME,'body').text.lower()
            if 'too often' in body_text:
                await self.send_status("⏰ Слишком часто! Попробуйте позже")
                return False
                
            await self.send_status("⌛ Жду SMS-код...")
            inp = self.wait.until(EC.presence_of_element_located((By.XPATH,
                "//input[@id='smsCode' or contains(@name,'smsCode')]"
            )))
            
            code = self.wait_for_sms_code()
            
            await self.send_status("🔢 Ввожу код...")
            inp.clear()
            inp.send_keys(code)
            next_btn = self.driver.find_element(By.XPATH,
                "//input[@type='submit' and @value='Next']"
            )
            next_btn.click()
            
            await self.send_status("✅ SMS подтвержден!")
            return True
        except Exception as e:
            await self.send_status(f"❌ Ошибка SMS: {str(e)[:50]}...")
            return False

    async def authenticate(self):
        try:
            await self.send_status("🚀 Начинаю авторизацию...")
            
            # Инициализация драйвера с обработкой ошибок
            if not self.init_driver():
                await self.send_status("❌ Не удалось инициализировать браузер")
                return False
            
            await self.send_status("🌐 Открываю OK.ru...")
            self.driver.get("https://ok.ru/")
            
            await self.send_status("📝 Ввожу данные...")
            self.wait.until(EC.presence_of_element_located((By.NAME,'st.email'))).send_keys(self.email)
            self.driver.find_element(By.NAME,'st.password').send_keys(self.password)
            self.driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
            time.sleep(2)
            
            await self.try_confirm_identity()
            
            if await self.try_sms_verification():
                self.authenticated = True
                await self.send_status("🎉 Авторизация успешна!")
                return True
            else:
                await self.send_status("❌ Авторизация провалена")
                return False
                
        except WebDriverException as e:
            error_msg = str(e)
            if "chrome" in error_msg.lower() and "version" in error_msg.lower():
                await self.send_status("❌ Проблема с версией Chrome. Попробуйте обновить браузер или ChromeDriver")
            else:
                await self.send_status(f"❌ Ошибка браузера: {error_msg[:50]}...")
            return False
        except Exception as e:
            await self.send_status(f"💥 Ошибка: {str(e)[:50]}...")
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

    async def post_to_group(self, group_url, video_url, text):
    post_url = group_url.rstrip('/') + '/post'
    logger.info("🚀 Открываю страницу постинга")
    self.driver.get(post_url)
    
    # Ждем загрузки поля для ввода
    box = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
        "div[contenteditable='true']"
    )))
    box.click()
    box.clear()
    
    # 1) Вставляем ссылку и пробел для загрузки превью
    box.send_keys(video_url)
    box.send_keys(Keys.SPACE)  # Критически важно для загрузки превью!
    logger.info("✍️ Ссылка вставлена и пробел отправлен")
    
    # 2) Ждём появление карточки превью с несколькими селекторами
    logger.info("⏳ Жду видео-карточку...")
    attached = False
    for _ in range(10):  # 10 секунд ожидания
        # Проверяем различные типы карточек превью
        if self.driver.find_elements(By.CSS_SELECTOR, "div.vid-card.vid-card__xl"):
            attached = True
            break
        if self.driver.find_elements(By.CSS_SELECTOR, "div.mediaPreview, div.mediaFlex, div.preview_thumb"):
            attached = True
            break
        time.sleep(1)
    
    if attached:
        logger.info("✅ Видео-карта появилась")
    else:
        logger.warning(f"⚠️ Не дождался карточки видео за 10 сек на {group_url}")
    
    # 3) Вставляем текст одной строкой (не построчно!)
    box.send_keys(" " + text)  # Пробел + весь текст сразу
    logger.info("✍️ Текст вставлен")
    
    # 4) Публикуем
    btn = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
        "button.js-pf-submit-btn[data-action='submit']"
    )))
    btn.click()
    logger.info("✅ Пост опубликован")
    await self.send_status("📝 Пост опубликован в группе")
    time.sleep(1)
    
    async def start_posting_workflow(self):
        try:
            await self.send_status("⏳ Жду команду #группы...")
            groups = self.wait_for_groups()
            await self.send_status("⏳ Жду команду #пост...")
            video_url, post_text = self.wait_for_post_info()
            await self.send_status(f"🚀 Начинаю постинг в {len(groups)} групп...")
            for i, g in enumerate(groups, 1):
                await self.post_to_group(g, video_url, post_text)
                await self.send_status(f"✅ {i}/{len(groups)} групп обработано")
            await self.send_status("🎉 Все задачи выполнены!")
        except Exception as e:
            await self.send_status(f"❌ Ошибка: {str(e)[:50]}...")
            
    def close(self):
        if self.driver:
            try:
                self.driver.quit()
                logger.info("Сессия закрыта")
            except Exception as e:
                logger.error(f"Ошибка при закрытии драйвера: {e}")

# Функция для запуска авторизации в отдельном потоке
def start_auth_thread(profile_data, profile_id):
    global current_session, current_profile, bot_running
    
    async def auth_process():
        global current_session, current_profile
        logger.info(f"🔄 Создаю сессию для {profile_data['person']}")
        session = OKSession(profile_data['email'], profile_data['password'], profile_data['person'])
        
        logger.info(f"🚀 Запускаю процесс авторизации для {profile_data['person']}")
        if await session.authenticate():
            current_session = session
            current_profile = profile_id
            logger.info(f"🎯 Сессия активна для {profile_data['person']}. Готов к получению команд!")
            # После успешной авторизации запускаем рабочий процесс
            logger.info(f"▶️ Запускаю рабочий процесс для {profile_data['person']}")
            await session.start_posting_workflow()
        else:
            logger.error(f"🚫 АВТОРИЗАЦИЯ ПРОВАЛЕНА для {profile_data['person']}")
            session.close()
    
    # Запускаем асинхронную функцию в новом event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(auth_process())
    finally:
        loop.close()

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

async def show_control_panel(update, context, profile_name):
    """Показывает панель управления после выбора профиля"""
    message = f"✅ Обрано профіль: {profile_name}\n"
    message += "🔄 Виконується авторизація...\n\n"
    message += "📱 Якщо потрібен SMS-код, надішліть його:\n"
    message += "#код 123456\n\n"
    message += "Або використовуйте кнопки нижче:"
    
    inline_keyboard = [
        [InlineKeyboardButton("📋 Додати #группы", callback_data='add_groups')],
        [InlineKeyboardButton("📝 Додати #пост", callback_data='add_post')],
        [InlineKeyboardButton("🛑 Зупинити бота", callback_data='stop_bot')],
        [InlineKeyboardButton("🔙 Обрати інший профіль", callback_data='branch')],
        [InlineKeyboardButton("🏠 На початок", callback_data='back_to_start')]
    ]
    reply_markup = InlineKeyboardMarkup(inline_keyboard)
    
    await update.callback_query.edit_message_text(message, reply_markup=reply_markup)

async def button_callback(update, context):
    global bot_running, current_session
    query = update.callback_query
    await query.answer()
    
    if query.data == 'branch':
        await show_profiles(update, context)
    
    elif query.data.startswith('profile_'):
        profile_id = int(query.data.split('_')[1])
        profiles = get_profiles()
        
        if profile_id in profiles:
            selected_profile = profiles[profile_id]
            
            await show_control_panel(update, context, selected_profile['person'])
            
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
    
    elif query.data == 'add_groups':
        inline_keyboard = [
            [InlineKeyboardButton("🔙 Повернутися до панелі", callback_data='back_to_control_panel')]
        ]
        reply_markup = InlineKeyboardMarkup(inline_keyboard)
        
        await query.edit_message_text(
            "📋 Скопіюйте та відправте:\n\n"
            "`#группы https://ok.ru/group/123456789 https://ok.ru/group/987654321`\n\n"
            "Замініть посилання на ваші групи",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    elif query.data == 'add_post':
        inline_keyboard = [
            [InlineKeyboardButton("🔙 Повернутися до панелі", callback_data='back_to_control_panel')]
        ]
        reply_markup = InlineKeyboardMarkup(inline_keyboard)
        
        await query.edit_message_text(
            "📝 Скопіюйте та відправте:\n\n"
            "`#пост https://www.youtube.com/watch?v=example Ваш текст поста тут`\n\n"
            "Замініть посилання та текст на ваші",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    elif query.data == 'stop_bot':
        bot_running = False
        if current_session:
            current_session.close()
            current_session = None
        
        await query.edit_message_text(
            "🛑 Бот зупинено\n"
            "Всі активні сесії закрито\n\n"
            "Для перезапуску використайте /start"
        )
        
        # Завершаем работу приложения
        os._exit(0)
    
    elif query.data == 'back_to_start':
        await cmd_start_callback(update, context)
    
    elif query.data == 'back_to_control_panel':
        # Возвращаемся к панели управления текущего профиля
        if current_profile is not None:
            profiles = get_profiles()
            if current_profile in profiles:
                profile_name = profiles[current_profile]['person']
                await show_control_panel(update, context, profile_name)
            else:
                await cmd_start_callback(update, context)
        else:
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
