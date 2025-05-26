import os
import logging
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

# Настройки из ENV
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Проверка переменной окружения
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана обязательная переменная окружения: TELEGRAM_BOT_TOKEN")

# Логирование
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)

# Асинхронная функция для команды /start
async def cmd_start(update, context):
    # Inline кнопки (появляются под сообщением)
    inline_keyboard = [
        [InlineKeyboardButton("🔥 Кнопка 1", callback_data='button1')],
        [InlineKeyboardButton("⭐ Кнопка 2", callback_data='button2')],
        [InlineKeyboardButton("🚀 Показать обычные кнопки", callback_data='show_reply_keyboard')]
    ]
    reply_markup = InlineKeyboardMarkup(inline_keyboard)
    
    await update.message.reply_text(
        "Тест бота с кнопками!\n\nВыберите действие:", 
        reply_markup=reply_markup
    )

# Обработчик нажатий на inline кнопки
async def button_callback(update, context):
    query = update.callback_query
    await query.answer()  # Подтверждаем получение нажатия
    
    if query.data == 'button1':
        await query.edit_message_text("Вы нажали кнопку 1! 🔥")
    elif query.data == 'button2':
        await query.edit_message_text("Вы нажали кнопку 2! ⭐")
    elif query.data == 'show_reply_keyboard':
        # Показываем обычную клавиатуру
        keyboard = [
            [KeyboardButton("📊 Статистика"), KeyboardButton("⚙️ Настройки")],
            [KeyboardButton("❓ Помощь"), KeyboardButton("🔙 Назад")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await query.edit_message_text("Теперь используйте кнопки снизу:")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Кнопки активированы! Выберите действие:",
            reply_markup=reply_markup
        )

# Обработчик обычных кнопок (reply keyboard)
async def handle_reply_buttons(update, context):
    text = update.message.text
    
    if text == "📊 Статистика":
        await update.message.reply_text("Здесь будет статистика 📊")
    elif text == "⚙️ Настройки":
        await update.message.reply_text("Здесь будут настройки ⚙️")
    elif text == "❓ Помощь":
        await update.message.reply_text("Помощь: используйте /start для начала")
    elif text == "🔙 Назад":
        await cmd_start(update, context)

# Создание Telegram приложения
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Регистрация обработчиков
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CallbackQueryHandler(button_callback))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply_buttons))

# Запуск бота
if __name__ == "__main__":
    application.run_polling()
