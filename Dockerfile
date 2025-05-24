# Используем официальный Python образ
FROM python:3.10-slim

# Устанавливаем зависимости для Chrome
RUN apt-get update && \
    apt-get install -y wget gnupg2 unzip fonts-liberation libnss3 libxss1 libasound2 libatk-bridge2.0-0 libgtk-3-0 libx11-xcb1 libxcomposite1 libxcursor1 libxdamage1 libxi6 libxtst6 ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Устанавливаем Google Chrome
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - && \
    echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

# Рабочая директория
WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY okru_post_bot.py .

# Открываем порт для вебхука
EXPOSE 5000

# Команда запуска
CMD ["python", "okru_post_bot.py"]
