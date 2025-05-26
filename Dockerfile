
# Используем официальный Python образ
FROM python:3.10-slim

# Рабочая директория
WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY . .

# Открываем порт для вебхука
EXPOSE 5000

# Запускаем приложение с asyncio (без Gunicorn)
CMD ["sh", "-c", "python okru_post_bot.py"]
