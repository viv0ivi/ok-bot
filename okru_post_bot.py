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

# Система блокировки для многопользовательского использования
bot_busy = False
current_user = None
bot_lock = threading.Lock()

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

# Функции для работы с блокировкой бота
def is_bot_busy():
    return bot_busy

def set_bot_busy(user_id, busy=True):
    global bot_busy, current_user
    with bot_lock:
        if busy:
            if bot_busy and current_user != user_id:
                return False  # Бот занят другим пользователем
            bot_busy = True
            current_user = user_id
        else:
            bot_busy = False
            current_user = None
        return True

def get_current_user():
    return current_user

# Функция для отправки уведомлений в Telegram
async def send_telegram_notification(message):
    try:
        await application.bot.send_message(chat_id=TELEGRAM_USER_ID, text=message)
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")

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
            asyncio.create_task(send_telegram_notification("✅ Личность подтверждена"))
            time.sleep(1)
        except:
            asyncio.create_task(send_telegram_notification("ℹ️ Подтверждение личности не требуется"))

    def wait_for_sms_code(self, timeout=120):
        global waiting_for_sms, sms_code_received
        waiting_for_sms = True
        sms_code_received = None
        
        asyncio.create_task(send_telegram_notification("📱 Жду SMS-код..."))
        deadline = time.time() + timeout
        
        while time.time() < deadline:
            if sms_code_received is not None:
                code = sms_code_received
                sms_code_received = None
                waiting_for_sms = False
                return code
            time.sleep(1)
        
        waiting_for_sms = False
        raise TimeoutException("SMS-код не получен")

    def try_sms_verification(self):
        try:
            # Проверяем, авторизованы ли уже
            data_l = self.driver.find_element(By.TAG_NAME,'body').get_attribute('data-l') or ''
            if 'userMain' in data_l and 'anonymMain' not in data_l:
                asyncio.create_task(send_telegram_notification("✅ Уже авторизован"))
                return True
                
            asyncio.create_task(send_telegram_notification("📱 Требуется SMS"))
            btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                "//input[@type='submit' and @value='Get code']"
            )))
            btn.click()
            time.sleep(1)
            
            body_text = self.driver.find_element(By.TAG_NAME,'body').text.lower()
            if 'too often' in body_text:
                asyncio.create_task(send_telegram_notification("⏰ Лимит SMS! Попробуйте позже"))
                return False
                
            inp = self.wait.until(EC.presence_of_element_located((By.XPATH,
                "//input[@id='smsCode' or contains(@name,'smsCode')]"
            )))
            
            code = self.wait_for_sms_code()
            
            inp.clear()
            inp.send_keys(code)
            next_btn = self.driver.find_element(By.XPATH,
                "//input[@type='submit' and @value='Next']"
            )
            next_btn.click()
            
            asyncio.create_task(send_telegram_notification("✅ SMS подтвержден"))
            return True
        except Exception as e:
            asyncio.create_task(send_telegram_notification(f"❌ Ошибка SMS: {str(e)[:50]}"))
            return False

    def authenticate(self):
        try:
            asyncio.create_task(send_telegram_notification(f"🚀 Авторизация {self.person_name}"))
            self.init_driver()
            asyncio.create_task(send_telegram_notification("🌐 Открываю OK.ru"))
            self.driver.get("https://ok.ru/")
            
            asyncio.create_task(send_telegram_notification("📝 Ввод данных"))
            self.wait.until(EC.presence_of_element_located((By.NAME,'st.email'))).send_keys(self.email)
            self.driver.find_element(By.NAME,'st.password').send_keys(self.password)
            self.driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
            time.sleep(2)
            
            self.try_confirm_identity()
            
            if self.try_sms_verification():
                self.authenticated = True
                asyncio.create_task(send_telegram_notification("🎉 Авторизация успешна!"))
                return True
            else:
                asyncio.create_task(send_telegram_notification("❌ Авторизация провалена"))
                return False
        except Exception as e:
            asyncio.create_task(send_telegram_notification(f"💥 Ошибка: {str(e)[:50]}"))
            return False

    def wait_for_groups(self):
        global waiting_for_groups, groups_received
        waiting_for_groups = True
        groups_received = None
        
        asyncio.create_task(send_telegram_notification("📋 Жду список групп"))
        while groups_received is None:
            time.sleep(1)
        
        groups = groups_received
        groups_received = None
        waiting_for_groups = False
        return groups

    def wait_for_post_info(self):
        global waiting_for_post, post_info_received
        waiting_for_post = True
        post_info_received = None
        
        asyncio.create_task(send_telegram_notification("📝 Жду информацию для поста"))
        while post_info_received is None:
            time.sleep(1)
        
        post_info = post_info_received
        post_info_received = None
        waiting_for_post = False
        return post_info

    def post_to_group(self, group_url, video_url, text):
        self.driver.get(group_url.rstrip('/') + '/post')
        field = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
            "div[contenteditable='true']"
        )))
        field.click()
        field.clear()
        # 1) вставляем ссылку
        field.send_keys(video_url)
        # 2) ждём карточку
        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR,
            "div.vid-card.vid-card__xl"
        )))
        # 3) по строкам вставляем текст
        for line in text.splitlines():
            field.send_keys(line)
            field.send_keys(Keys.SHIFT, Keys.ENTER)
            time.sleep(5)
        # 4) публикуем
        btn = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
            "button.js-pf-submit-btn[data-action='submit']"
        )))
        btn.click()
        time.sleep(1)

    def start_posting_workflow(self):
        try:
            groups = self.wait_for_groups()
            video_url, post_text = self.wait_for_post_info()
            
            asyncio.create_task(send_telegram_notification(f"📤 Публикую в {len(groups)} групп"))
            
            for i, g in enumerate(groups, 1):
                asyncio.create_task(send_telegram_notification(f"📌 Группа {i}/{len(groups)}"))
                self.post_to_group(g, video_url, post_text)
                
            asyncio.create_task(send_telegram_notification("🎯 Все посты опубликованы!"))
        except Exception as e:
            asyncio.create_task(send_telegram_notification(f"❌ Ошибка постинга: {str(e)[:50]}"))
            
    def close(self):
        if self.driver:
            self.driver.quit()

# Функция для запуска авторизации в отдельном потоке
def start_auth_thread(profile_data, profile_id, user_id):
    global current_session, current_profile
    
    session = OKSession(profile_data['email'], profile_data['password'], profile_data['person'])
    
    if session.authenticate():
        current_session = session
        current_profile = profile_id
        # После успешной авторизации запускаем рабочий процесс
        session.start_posting_workflow()
    else:
        session.close()
    
    # Освобождаем бота после завершения работы
    set_bot_busy(user_id, False)

# Обработчик текстовых сообщений
async def handle_message(update, context):
    global waiting_for_sms, waiting_for_groups, waiting_for_post
    global sms_code_received, groups_received, post_info_received
    
    user_id = str(update.message.chat.id)
    text = update.message.text.strip()
    
    # Обработка SMS-кода
    if waiting_for_sms:
        if current_user and current_user != user_id:
            await update.message.reply_text("⚠️ Бот занят другим пользователем")
            return
            
        sms_match = re.match(r"^(?:#код\s*)?(\d{4,6})$", text, re.IGNORECASE)
        if sms_match:
            sms_code_received = sms_match.group(1)
            await update.message.reply_text("✅ SMS-код получен!")
            return
    
    # Обработка команды #группы
    if text.lower().startswith("#группы"):
        if current_user and current_user != user_id:
            await update.message.reply_text("⚠️ Бот занят другим пользователем")
            return
            
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
        if current_user and current_user != user_id:
            await update.message.reply_text("⚠️ Бот занят другим пользователем")
            return
            
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
    user_id = str(update.message.chat.id)
    
    # Проверяем статус бота
    if is_bot_busy():
        if current_user == user_id:
            status_msg = "🔄 Вы уже используете бота"
        else:
            status_msg = "⚠️ Бот занят другим пользователем\nПопробуйте позже"
            
        inline_keyboard = [
            [InlineKeyboardButton("🔄 Обновить статус", callback_data='refresh_status')]
        ]
        reply_markup = InlineKeyboardMarkup(inline_keyboard)
        
        await update.message.reply_text(status_msg, reply_markup=reply_markup)
        return
    
    inline_keyboard = [
        [InlineKeyboardButton("🌿 Розгалуджувати", callback_data='branch')]
    ]
    reply_markup = InlineKeyboardMarkup(inline_keyboard)
    
    await update.message.reply_text(
        "Вітаю! Оберіть дію:",
        reply_markup=reply_markup
    )

async def show_profiles(update, context):
    user_id = str(update.callback_query.from_user.id)
    
    # Проверяем, занят ли бот
    if is_bot_busy() and current_user != user_id:
        await update.callback_query.edit_message_text(
            "⚠️ Бот занят другим пользователем\nПопробуйте позже"
        )
        return
    
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
    user_id = str(query.from_user.id)
    
    if query.data == 'refresh_status':
        if is_bot_busy():
            if current_user == user_id:
                status_msg = "🔄 Вы уже используете бота"
            else:
                status_msg = "⚠️ Бот все еще занят другим пользователем"
        else:
            status_msg = "✅ Бот свободен!"
            
        inline_keyboard = []
        if not is_bot_busy():
            inline_keyboard.append([InlineKeyboardButton("🌿 Розгалуджувати", callback_data='branch')])
        else:
            inline_keyboard.append([InlineKeyboardButton("🔄 Обновить статус", callback_data='refresh_status')])
            
        reply_markup = InlineKeyboardMarkup(inline_keyboard)
        await query.edit_message_text(status_msg, reply_markup=reply_markup)
        return
    
    if query.data == 'branch':
        await show_profiles(update, context)
    
    elif query.data.startswith('profile_'):
        profile_id = int(query.data.split('_')[1])
        profiles = get_profiles()
        
        if profile_id in profiles:
            # Пытаемся заблокировать бота для этого пользователя
            if not set_bot_busy(user_id, True):
                await query.edit_message_text("⚠️ Бот занят другим пользователем")
                return
                
            selected_profile = profiles[profile_id]
            
            message = f"✅ Обрано профіль: {selected_profile['person']}\n"
            message += f"📧 Email: {selected_profile['email']}\n"
            message += "🔄 Виконується авторизація...\n\n"
            message += "📱 Якщо потрібен SMS-код, надішліть його у форматі:\n"
            message += "#код 123456\n\n"
            message += "Після авторизації надішліть:\n"
            message += "📋 #группы [список ссылок]\n"
            message += "📝 #пост [ссылка на видео] [текст поста]"
            
            # Добавляем кнопку для отмены
            inline_keyboard = [
                [InlineKeyboardButton("❌ Отменить", callback_data='cancel_work')]
            ]
            reply_markup = InlineKeyboardMarkup(inline_keyboard)
            
            await query.edit_message_text(message, reply_markup=reply_markup)
            
            # Запускаем авторизацию в отдельном потоке
            auth_thread = threading.Thread(
                target=start_auth_thread, 
                args=(selected_profile, profile_id, user_id)
            )
            auth_thread.daemon = True
            auth_thread.start()
            
        else:
            await query.edit_message_text("❌ Профіль не знайдено!")
    
    elif query.data == 'cancel_work':
        # Освобождаем бота
        set_bot_busy(user_id, False)
        
        # Закрываем активную сессию если она есть
        global current_session
        if current_session:
            current_session.close()
            current_session = None
            
        await query.edit_message_text("❌ Работа отменена. Бот свободен.")
    
    elif query.data == 'back_to_start':
        await cmd_start_callback(update, context)

async def cmd_start_callback(update, context):
    user_id = str(update.callback_query.from_user.id)
    
    # Проверяем статус бота
    if is_bot_busy() and current_user != user_id:
        await update.callback_query.edit_message_text(
            "⚠️ Бот занят другим пользователем\nПопробуйте позже"
        )
        return
    
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
