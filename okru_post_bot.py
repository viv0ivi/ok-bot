import os
import logging
import asyncio
from telegram import Bot
from telegram.ext import Application, CommandHandler

# Настройки из ENV
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Проверка переменной окружения
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана обязательная переменная окружения: TELEGRAM_BOT_TOKEN")

# Логирование
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)

# Асинхронная функция для команды /start
async def cmd_start(update, context):
    await update.message.reply_text("Тест")

# Создание Telegram приложения
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Регистрация обработчика для команды /start
application.add_handler(CommandHandler("start", cmd_start))

# Запуск бота через asyncio
async def main():
    await application.run_polling()

# Для Gunicorn, запускаем через asyncio
if __name__ == "__main__":
    asyncio.run(main())
