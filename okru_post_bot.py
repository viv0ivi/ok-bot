import os
import time
import re
import logging
import sys
import threading
import asyncio
from queue import Queue
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
TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID")  # ID группового чата
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() == "true"

# Проверка переменных окружения
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана обязательная переменная окружения: TELEGRAM_BOT_TOKEN")

if not TELEGRAM_GROUP_ID:
    raise RuntimeError("Не задана обязательная переменная окружения: TELEGRAM_GROUP_ID")

# Логирование
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("okru_bot")

# Глобальные переменные
current_session = None
current_profile = None
waiting_for_sms = False
waiting_for_groups = False
waiting_for_post = False
sms_code_received = None
groups_received = None
post_info_received = None
bot_stopped = False

# Простая очередь задач
task_queue = Queue()
current_user = None

# Flask app
app = Flask(__name__)

# Telegram приложение
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

# Функция для отправки сообщений в группу
async def send_to_group(text, reply_markup=None):
    try:
        await application.bot.send_message(
            chat_id=TELEGRAM_GROUP_ID,
            text=text,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Ошибка отправки в группу: {e}")

# Функция для получения всех профилей
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
    def __init__(self, email, password, person_name, user_name):
        self.email = email
        self.password = password
        self.person_name = person_name
        self.user_name = user_name
        self.driver = None
        self.wait = None
        self.authenticated = False
        
    async def log_to_group(self, message):
        """Отправка логов в группу"""
        log_message = f"👤 {self.user_name} | {self.person_name}\n{message}"
        await send_to_group(log_message)
        
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
            time.sleep(1)
        except:
            pass

    def wait_for_sms_code(self, timeout=120):
        global waiting_for_sms, sms_code_received
        waiting_for_sms = True
        sms_code_received = None
        
        deadline = time.time() + timeout
        
        while time.time() < deadline:
            if bot_stopped:
                waiting_for_sms = False
                raise Exception("Бот остановлен")
            if sms_code_received is not None:
                code = sms_code_received
                sms_code_received = None
                waiting_for_sms = False
                return code
            time.sleep(1)
        
        waiting_for_sms = False
        raise TimeoutException("SMS-код не получен")

    async def try_sms_verification(self):
        try:
            data_l = self.driver.find_element(By.TAG_NAME,'body').get_attribute('data-l') or ''
            if 'userMain' in data_l and 'anonymMain' not in data_l:
                await self.log_to_group("✅ Уже авторизован")
                return True
                
            await self.log_to_group("📱 Нужен SMS")
            btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                "//input[@type='submit' and @value='Get code']"
            )))
            btn.click()
            time.sleep(1)
            
            body_text = self.driver.find_element(By.TAG_NAME,'body').text.lower()
            if 'too often' in body_text:
                await self.log_to_group("⏰ Лимит SMS! Попробуйте позже")
                return False
                
            inp = self.wait.until(EC.presence_of_element_located((By.XPATH,
                "//input[@id='smsCode' or contains(@name,'smsCode')]"
            )))
            
            await self.log_to_group("⌛ Жду SMS-код...")
            code = self.wait_for_sms_code()
            
            inp.clear()
            inp.send_keys(code)
            next_btn = self.driver.find_element(By.XPATH,
                "//input[@type='submit' and @value='Next']"
            )
            next_btn.click()
            
            await self.log_to_group("✅ SMS принят")
            return True
        except Exception as e:
            await self.log_to_group(f"❌ Ошибка SMS: {str(e)[:50]}")
            return False

    async def authenticate(self):
        try:
            if bot_stopped:
                return False
                
            await self.log_to_group("🚀 Начинаю авторизацию")
            self.init_driver()
            self.driver.get("https://ok.ru/")
            
            await self.log_to_group("📝 Ввожу данные")
            self.wait.until(EC.presence_of_element_located((By.NAME,'st.email'))).send_keys(self.email)
            self.driver.find_element(By.NAME,'st.password').send_keys(self.password)
            self.driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
            time.sleep(2)
            
            self.try_confirm_identity()
            
            if await self.try_sms_verification():
                self.authenticated = True
                await self.log_to_group("🎉 Авторизация успешна")
                return True
            else:
                await self.log_to_group("❌ Авторизация провалена")
                return False
        except Exception as e:
            await self.log_to_group(f"💥 Ошибка: {str(e)[:50]}")
            return False

    def wait_for_groups(self):
        global waiting_for_groups, groups_received
        waiting_for_groups = True
        groups_received = None
        
        while groups_received is None and not bot_stopped:
            time.sleep(1)
        
        if bot_stopped:
            return None
            
        groups = groups_received
        groups_received = None
        waiting_for_groups = False
        return groups

    def wait_for_post_info(self):
        global waiting_for_post, post_info_received
        waiting_for_post = True
        post_info_received = None
        
        while post_info_received is None and not bot_stopped:
            time.sleep(1)
        
        if bot_stopped:
            return None, None
            
        post_info = post_info_received
        post_info_received = None
        waiting_for_post = False
        return post_info

    async def post_to_group(self, group_url, video_url, text):
        self.driver.get(group_url.rstrip('/') + '/post')
        field = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
            "div[contenteditable='true']"
        )))
        field.click()
        field.clear()
        field.send_keys(video_url)
        
        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR,
            "div.vid-card.vid-card__xl"
        )))
        
        for line in text.splitlines():
            field.send_keys(line)
            field.send_keys(Keys.SHIFT, Keys.ENTER)
            time.sleep(5)
            
        btn = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
            "button.js-pf-submit-btn[data-action='submit']"
        )))
        btn.click()
        time.sleep(1)

    async def start_posting_workflow(self):
        try:
            if bot_stopped:
                return
                
            await self.log_to_group("📋 Жду список групп")
            groups = self.wait_for_groups()
            if not groups or bot_stopped:
                return
                
            await self.log_to_group(f"✅ Получено {len(groups)} групп")
            
            await self.log_to_group("📝 Жду пост")
            video_url, post_text = self.wait_for_post_info()
            if not video_url or bot_stopped:
                return
                
            await self.log_to_group("🚀 Начинаю постинг")
            for i, group in enumerate(groups, 1):
                if bot_stopped:
                    break
                await self.log_to_group(f"📮 Пост {i}/{len(groups)}")
                await self.post_to_group(group, video_url, post_text)
                
            if not bot_stopped:
                await self.log_to_group("🎉 Все посты опубликованы")
        except Exception as e:
            await self.log_to_group(f"❌ Ошибка: {str(e)[:50]}")
            
    def close(self):
        if self.driver:
            self.driver.quit()

# Функция запуска авторизации
async def start_auth_thread(profile_data, profile_id, user_name):
    global current_session, current_profile, current_user
    
    session = OKSession(profile_data['email'], profile_data['password'], 
                       profile_data['person'], user_name)
    
    if await session.authenticate():
        current_session = session
        current_profile = profile_id
        current_user = user_name
        await session.start_posting_workflow()
    else:
        session.close()

# Проверка прав в группе
def is_group_member(update):
    return str(update.effective_chat.id) == TELEGRAM_GROUP_ID

# Обработчик текстовых сообщений
async def handle_message(update, context):
    global waiting_for_sms, waiting_for_groups, waiting_for_post
    global sms_code_received, groups_received, post_info_received
    
    if not is_group_member(update):
        return
    
    text = update.message.text.strip()
    user_name = update.message.from_user.first_name or "Пользователь"
    
    # SMS-код
    if waiting_for_sms:
        sms_match = re.match(r"^(?:#код\s*)?(\d{4,6})$", text, re.IGNORECASE)
        if sms_match:
            sms_code_received = sms_match.group(1)
            await update.message.reply_text("✅ SMS получен")
            return
    
    # Группы
    if text.lower().startswith("#группы"):
        groups_match = re.match(r"#группы\s+(.+)", text, re.IGNORECASE)
        if groups_match:
            urls = re.findall(r"https?://ok\.ru/group/\d+/?", groups_match.group(1))
            if urls:
                if waiting_for_groups:
                    groups_received = urls
                    await update.message.reply_text(f"✅ {len(urls)} групп получено")
                else:
                    await update.message.reply_text("❌ Сначала авторизуйтесь")
            else:
                await update.message.reply_text("❌ Неверные ссылки на группы")
        return
    
    # Пост
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
                    await update.message.reply_text("✅ Пост получен")
                else:
                    await update.message.reply_text("❌ Сначала отправьте группы")
            else:
                await update.message.reply_text("❌ Нет ссылки на видео")
        return

# Команда /start
async def cmd_start(update, context):
    if not is_group_member(update):
        return
        
    user_name = update.message.from_user.first_name or "Пользователь"
    
    # Проверяем очередь
    if not task_queue.empty() or current_user:
        queue_size = task_queue.qsize()
        if current_user:
            queue_size += 1
        await update.message.reply_text(f"⏳ В очереди: {queue_size} человек")
        return
    
    inline_keyboard = [
        [InlineKeyboardButton("🌿 Выбрать профиль", callback_data=f'branch_{user_name}')],
        [InlineKeyboardButton("🛑 Остановить бота", callback_data='stop_bot')]
    ]
    reply_markup = InlineKeyboardMarkup(inline_keyboard)
    
    await update.message.reply_text(
        f"Привет, {user_name}! Выберите действие:",
        reply_markup=reply_markup
    )

async def show_profiles(update, context, user_name):
    profiles = get_profiles()
    
    if not profiles:
        await update.callback_query.edit_message_text("❌ Профили не найдены")
        return
    
    inline_keyboard = []
    for profile_id, profile_data in profiles.items():
        button_text = f"👤 {profile_data['person']}"
        callback_data = f"profile_{profile_id}_{user_name}"
        inline_keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
    
    inline_keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f'back_to_start_{user_name}')])
    inline_keyboard.append([InlineKeyboardButton("🛑 Остановить", callback_data='stop_bot')])
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard)
    
    await update.callback_query.edit_message_text(
        "Выберите профиль:",
        reply_markup=reply_markup
    )

async def button_callback(update, context):
    global bot_stopped, current_session, current_user
    
    if not is_group_member(update):
        return
        
    query = update.callback_query
    await query.answer()
    
    # Остановка бота
    if query.data == 'stop_bot':
        bot_stopped = True
        if current_session:
            current_session.close()
            current_session = None
        current_user = None
        
        # Очищаем очередь
        while not task_queue.empty():
            task_queue.get()
            
        await query.edit_message_text("🛑 Бот остановлен")
        await send_to_group("🛑 Бот остановлен всеми задачами")
        return
    
    # Извлекаем имя пользователя из callback_data
    parts = query.data.split('_')
    if len(parts) < 2:
        return
        
    user_name = parts[-1]
    
    if query.data.startswith('branch_'):
        await show_profiles(update, context, user_name)
    
    elif query.data.startswith('profile_'):
        profile_id = int(parts[1])
        profiles = get_profiles()
        
        if profile_id in profiles and not bot_stopped:
            selected_profile = profiles[profile_id]
            
            message = f"✅ Профиль: {selected_profile['person']}\n"
            message += "🔄 Авторизация...\n\n"
            message += "Отправьте:\n"
            message += "📱 SMS: #код 123456\n"
            message += "📋 #группы [ссылки]\n"
            message += "📝 #пост [видео] [текст]"
            
            inline_keyboard = [
                [InlineKeyboardButton("🔙 Другой профиль", callback_data=f'branch_{user_name}')],
                [InlineKeyboardButton("🛑 Стоп", callback_data='stop_bot')]
            ]
            reply_markup = InlineKeyboardMarkup(inline_keyboard)
            
            await query.edit_message_text(message, reply_markup=reply_markup)
            
            # Запускаем авторизацию
            auth_task = asyncio.create_task(
                start_auth_thread(selected_profile, profile_id, user_name)
            )
    
    elif query.data.startswith('back_to_start_'):
        inline_keyboard = [
            [InlineKeyboardButton("🌿 Выбрать профиль", callback_data=f'branch_{user_name}')],
            [InlineKeyboardButton("🛑 Остановить бота", callback_data='stop_bot')]
        ]
        reply_markup = InlineKeyboardMarkup(inline_keyboard)
        
        await query.edit_message_text(
            f"Привет, {user_name}! Выберите действие:",
            reply_markup=reply_markup
        )

# Создание приложения
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Регистрация обработчиков
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CallbackQueryHandler(button_callback))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# Запуск
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    
    if USE_WEBHOOK and WEBHOOK_URL:
        logger.info("🌐 Запуск в режиме Webhook")
        
        async def set_webhook():
            await application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
            logger.info(f"Webhook установлен: {WEBHOOK_URL}/webhook")
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(set_webhook())
        
        logger.info(f"🚀 Flask сервер на порту {port}")
        app.run(host='0.0.0.0', port=port, debug=False)
        
    else:
        logger.info("🤖 Режим Polling")
        
        def run_flask():
            app.run(host='0.0.0.0', port=port, debug=False)
        
        flask_thread = threading.Thread(target=run_flask)
        flask_thread.daemon = True
        flask_thread.start()
        
        try:
            async def clear_webhook():
                await application.bot.delete_webhook()
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(clear_webhook())
            
            application.run_polling()
        finally:
            if current_session:
                current_session.close()
            logger.info("👋 Бот остановлен")
