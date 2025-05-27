import os
import time
import re
import requests
import logging
import sys
import threading
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from flask import Flask, jsonify

# Настройки из ENV
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")

# Проверка переменной окружения
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана обязательная переменная окружения: TELEGRAM_BOT_TOKEN")

if not TELEGRAM_USER_ID:
    raise RuntimeError("Не задана обязательная переменная окружения: TELEGRAM_USER_ID")

# Логирование
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("okru_bot")

# Глобальные переменные для сессии
current_session = None
current_profile = None

# Flask app для health check (нужен для Render)
app = Flask(__name__)

@app.route('/')
def health_check():
    return jsonify({"status": "ok", "message": "Bot is running"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

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
            logger.info("Подтверждение личности прошло")
            time.sleep(1)
        except:
            logger.info("Нет страницы подтверждения личности")

    def retrieve_sms_code(self, timeout=120, poll=5):
        api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        last = None
        try:
            init = requests.get(api, params={'timeout':0}).json()
            if init.get('ok'):
                ids = [u['update_id'] for u in init['result']]
                last = max(ids) + 1 if ids else None
        except:
            pass
        deadline = time.time() + timeout
        logger.info("Ожидаю SMS-код")
        while time.time() < deadline:
            try:
                resp = requests.get(api, params={'timeout':0,'offset': last}).json()
            except:
                time.sleep(poll)
                continue
            if resp.get('ok'):
                for upd in resp['result']:
                    last = upd['update_id'] + 1
                    msg = upd.get('message') or {}
                    if str(msg.get('chat',{}).get('id')) != TELEGRAM_USER_ID:
                        continue
                    txt = msg.get('text','').strip()
                    m = re.match(r"^(?:#код\s*)?(\d{4,6})$", txt, re.IGNORECASE)
                    if m:
                        logger.info("SMS-код получен")
                        return m.group(1)
            time.sleep(poll)
        logger.error("Не получили SMS-код")
        raise TimeoutException("SMS-код не получен")

    def try_sms_verification(self):
        try:
            logger.info(f"🔍 Проверяю статус авторизации для {self.person_name}")
            data_l = self.driver.find_element(By.TAG_NAME,'body').get_attribute('data-l') or ''
            if 'userMain' in data_l and 'anonymMain' not in data_l:
                logger.info(f"✅ {self.person_name} уже авторизован!")
                return True
                
            logger.info(f"📱 Требуется SMS-верификация для {self.person_name}")
            logger.info(f"🔄 Запрашиваю SMS-код для {self.person_name}")
            btn = self.wait.until(EC.element_to_be_clickable((By.XPATH,
                "//input[@type='submit' and @value='Get code']"
            )))
            btn.click()
            time.sleep(1)
            
            body_text = self.driver.find_element(By.TAG_NAME,'body').text.lower()
            if 'too often' in body_text:
                logger.error(f"⏰ RATE LIMIT на SMS для {self.person_name}! Попробуйте позже")
                return False
                
            logger.info(f"⌛ Ожидаю ввод SMS-кода для {self.person_name}...")
            inp = self.wait.until(EC.presence_of_element_located((By.XPATH,
                "//input[@id='smsCode' or contains(@name,'smsCode')]"
            )))
            
            logger.info(f"📨 Жду SMS-код от пользователя для {self.person_name}")
            code = self.retrieve_sms_code()
            
            logger.info(f"🔢 Ввожу SMS-код для {self.person_name}")
            inp.clear()
            inp.send_keys(code)
            next_btn = self.driver.find_element(By.XPATH,
                "//input[@type='submit' and @value='Next']"
            )
            next_btn.click()
            
            logger.info(f"✅ SMS-верификация успешна для {self.person_name}!")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка SMS-верификации для {self.person_name}: {str(e)}")
            return False

    def authenticate(self):
        try:
            logger.info(f"🚀 Начинаю авторизацию для {self.person_name}")
            self.init_driver()
            logger.info(f"🌐 Открываю OK.ru для {self.person_name}")
            self.driver.get("https://ok.ru/")
            
            logger.info(f"📝 Ввожу email для {self.person_name}")
            self.wait.until(EC.presence_of_element_located((By.NAME,'st.email'))).send_keys(self.email)
            
            logger.info(f"🔑 Ввожу пароль для {self.person_name}")
            self.driver.find_element(By.NAME,'st.password').send_keys(self.password)
            
            logger.info(f"✅ Нажимаю кнопку входа для {self.person_name}")
            self.driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
            time.sleep(2)
            
            logger.info(f"🔍 Проверяю подтверждение личности для {self.person_name}")
            self.try_confirm_identity()
            
            logger.info(f"📱 Проверяю необходимость SMS-верификации для {self.person_name}")
            if self.try_sms_verification():
                self.authenticated = True
                logger.info(f"🎉 УСПЕШНАЯ АВТОРИЗАЦИЯ для {self.person_name}!")
                logger.info(f"✅ {self.person_name} готов к работе. Ожидаю команды...")
                return True
            else:
                logger.error(f"❌ Не удалось пройти SMS-верификацию для {self.person_name}")
                return False
        except Exception as e:
            logger.error(f"💥 ОШИБКА АВТОРИЗАЦИИ для {self.person_name}: {str(e)}")
            return False

    def retrieve_groups(self, poll=5):
        api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        last = None
        try:
            init = requests.get(api, params={'timeout':0}).json()
            if init.get('ok'):
                ids = [u['update_id'] for u in init['result']]
                last = max(ids) + 1 if ids else None
        except:
            pass
        logger.info("Жду команду #группы")
        while True:
            resp = requests.get(api, params={'timeout':0,'offset': last}).json()
            if resp.get('ok'):
                for upd in resp['result']:
                    last = upd['update_id'] + 1
                    msg = upd.get('message') or {}
                    if str(msg.get('chat',{}).get('id')) != TELEGRAM_USER_ID:
                        continue
                    txt = msg.get('text','').strip()
                    m = re.match(r"#группы\s+(.+)", txt, re.IGNORECASE)
                    if m:
                        urls = re.findall(r"https?://ok\.ru/group/\d+/?", m.group(1))
                        if urls:
                            logger.info("Список групп получен")
                            return urls
            time.sleep(poll)

    def retrieve_post_info(self, poll=5):
        api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        last = None
        try:
            init = requests.get(api, params={'timeout':0}).json()
            if init.get('ok'):
                ids = [u['update_id'] for u in init['result']]
                last = max(ids) + 1 if ids else None
        except:
            pass
        logger.info("Жду команду #пост")
        while True:
            resp = requests.get(api, params={'timeout':0,'offset': last}).json()
            if resp.get('ok'):
                for upd in resp['result']:
                    last = upd['update_id'] + 1
                    msg = upd.get('message') or {}
                    if str(msg.get('chat',{}).get('id')) != TELEGRAM_USER_ID:
                        continue
                    txt = msg.get('text','').strip()
                    m = re.match(r"#пост\s+(.+)", txt, re.IGNORECASE)
                    if m:
                        rest = m.group(1).strip()
                        url_m = re.search(r"https?://\S+", rest)
                        if url_m:
                            vid = url_m.group(0)
                            body = rest.replace(vid, "").strip()
                            logger.info("Инфо для поста получено")
                            return vid, body
            time.sleep(poll)

    def post_to_group(self, group_url, video_url, text):
        self.driver.get(group_url.rstrip('/') + '/post')
        logger.info("Открываю страницу постинга")
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
        # 3) по строкам вставляем текст
        logger.info("Вставляю текст по строкам")
        for line in text.splitlines():
            field.send_keys(line)
            field.send_keys(Keys.SHIFT, Keys.ENTER)
            time.sleep(5)
        # 4) публикуем
        btn = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
            "button.js-pf-submit-btn[data-action='submit']"
        )))
        btn.click()
        logger.info("Пост опубликован")
        time.sleep(1)

    def start_posting_workflow(self):
        try:
            groups = self.retrieve_groups()
            video_url, post_text = self.retrieve_post_info()
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
            message += "📝 #пост [ссылка на видео] [текст поста]"
            
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

# Запуск бота
if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке для health check
    def run_flask():
        port = int(os.environ.get('PORT', 5000))
        app.run(host='0.0.0.0', port=port, debug=False)
    
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    logger.info("🤖 Запускаю Telegram бота...")
    logger.info("🌐 Flask health check запущен")
    
    try:
        application.run_polling()
    finally:
        # Закрываем активную сессию при завершении
        if current_session:
            logger.info("🔄 Закрываю активную сессию...")
            current_session.close()
        logger.info("👋 Бот остановлен")
