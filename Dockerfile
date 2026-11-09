FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

RUN apt-get update
RUN apt-get install -y --no-install-recommends chromium chromium-driver fonts-liberation fonts-dejavu-core libnss3 libxss1 libgbm1 libasound2 libdrm2 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libgtk-3-0 libcups2 libpango-1.0-0 libcairo2 libxcomposite1 libxdamage1 libxfixes3 libxrandr2
RUN rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN mkdir -p /data
CMD ["python", "bot.py"]
