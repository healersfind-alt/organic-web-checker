FROM python:3.12-slim

# System deps for Playwright/Chromium
RUN apt-get update && apt-get install -y \
    libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

# Railway injects PORT at runtime — do not hardcode it
CMD ["sh", "-c", "gunicorn -w 1 -b 0.0.0.0:${PORT:-8000} --timeout 180 app:app"]
