import os
import logging
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

# Настройки из ENV
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Проверка переменной окружения
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана обязательная переменная окружения: TELEGRAM_BOT_TOKEN")

# Логирование
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)

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

# Асинхронная функция для команды /start
async def cmd_start(update, context):
    inline_keyboard = [
        [InlineKeyboardButton("🌿 Розгалуджувати", callback_data='branch')]
    ]
    reply_markup = InlineKeyboardMarkup(inline_keyboard)
    
    await update.message.reply_text(
        "Вітаю! Оберіть дію:",
        reply_markup=reply_markup
    )

# Функция для показа профилей
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

# Обработчик нажатий на inline кнопки
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
            
            # Здесь будет логика авторизации
            message = f"✅ Обрано профіль: {selected_profile['person']}\n"
            message += f"📧 Email: {selected_profile['email']}\n"
            message += "🔄 Виконується авторизація..."
            
            # Добавляем кнопку для возврата к выбору профилей
            inline_keyboard = [
                [InlineKeyboardButton("🔙 Обрати інший профіль", callback_data='branch')],
                [InlineKeyboardButton("🏠 На початок", callback_data='back_to_start')]
            ]
            reply_markup = InlineKeyboardMarkup(inline_keyboard)
            
            await query.edit_message_text(message, reply_markup=reply_markup)
            
            # TODO: Здесь добавить логику авторизации с selected_profile['email'] и selected_profile['password']
            logging.info(f"Выбран профиль: {selected_profile['person']} ({selected_profile['email']})")
        else:
            await query.edit_message_text("❌ Профіль не знайдено!")
    
    elif query.data == 'back_to_start':
        await cmd_start_callback(update, context)

# Функция для возврата к стартовому меню (для callback)
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
    application.run_polling()
