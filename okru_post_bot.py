import asyncio
import os
from telegram import Bot

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

async def clear_webhook():
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.delete_webhook()
    print("✅ Webhook удален!")
    
    # Проверяем статус
    webhook_info = await bot.get_webhook_info()
    print(f"Текущий webhook: {webhook_info.url}")
    print(f"Pending updates: {webhook_info.pending_update_count}")

if __name__ == "__main__":
    asyncio.run(clear_webhook())
