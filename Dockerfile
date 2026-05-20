FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# System deps for Playwright + Tesseract
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    wget curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# Install Python deps + Playwright browsers in one layer
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium \
    && playwright install-deps chromium

COPY . .

EXPOSE 5000
CMD gunicorn --worker-class eventlet -w 1 --timeout 300 --bind 0.0.0.0:$PORT app:app
