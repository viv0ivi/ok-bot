import os
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardButton, ReplyKeyboardMarkup
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
    # Inline клавиатура
    inline_keyboard = [
        [InlineKeyboardButton("Кнопка 1", callback_data="button_1")],
        [InlineKeyboardButton("Кнопка 2", callback_data="button_2")]
    ]
    inline_reply_markup = InlineKeyboardMarkup(inline_keyboard)

    # Reply клавиатура
    reply_keyboard = [
        [ReplyKeyboardButton("Кнопка A"), ReplyKeyboardButton("Кнопка B")],
        [ReplyKeyboardButton("Кнопка C")]
    ]
    reply_markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

    # Отправляем сообщение с обеими клавишами
    await update.message.reply_text("Выберите одну из кнопок:", reply_markup=inline_reply_markup)

# Обработчик кнопок
async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "button_1":
        await query.edit_message_text("Вы нажали кнопку 1")
    elif query.data == "button_2":
        await query.edit_message_text("Вы нажали кнопку 2")

# Обработчик для кнопок Reply
async def reply_button_handler(update, context):
    text = update.message.text
    if text == "Кнопка A":
        await update.message.reply_text("Вы выбрали кнопку A")
    elif text == "Кнопка B":
        await update.message.reply_text("Вы выбрали кнопку B")
    elif text == "Кнопка C":
        await update.message.reply_text("Вы выбрали кнопку C")

# Создание Telegram приложения
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Регистрация обработчиков
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_button_handler))

# Запуск бота
if __name__ == "__main__":
    application.run_polling()
