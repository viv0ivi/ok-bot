
# Используем официальный Python образ
FROM python:3.10-slim

# Рабочая директория
WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY okru_post_bot.py .

# Открываем порт для вебхука
EXPOSE 5000

# Запускаем Gunicorn
CMD ["sh", "-c", "gunicorn --workers 2 --worker-class gthread --bind 0.0.0.0:$PORT bot:application"]
