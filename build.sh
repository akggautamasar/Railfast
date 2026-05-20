#!/usr/bin/env bash
# Render.com build script
# Installs Chrome, Chromedriver, Tesseract OCR

set -e

echo "==> Installing system deps..."
apt-get update -qq
apt-get install -y -qq \
  wget curl unzip gnupg \
  tesseract-ocr \
  libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
  libcairo2 fonts-liberation

echo "==> Installing Google Chrome..."
wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt-get install -y ./google-chrome-stable_current_amd64.deb
rm google-chrome-stable_current_amd64.deb

echo "==> Installing ChromeDriver (matching Chrome version)..."
CHROME_VERSION=$(google-chrome --version | awk '{print $3}' | cut -d. -f1)
DRIVER_URL="https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}.0.0/linux64/chromedriver-linux64.zip"
wget -q "$DRIVER_URL" -O chromedriver.zip || \
  wget -q "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_${CHROME_VERSION}" -O latest && \
  wget -q "https://chromedriver.storage.googleapis.com/$(cat latest)/chromedriver_linux64.zip" -O chromedriver.zip
unzip -q chromedriver.zip -d /tmp/
find /tmp -name "chromedriver" -exec mv {} /usr/local/bin/chromedriver \;
chmod +x /usr/local/bin/chromedriver

echo "==> Versions:"
google-chrome --version
chromedriver --version
tesseract --version | head -1

echo "==> Installing Python deps..."
pip install -r requirements.txt

echo "==> Build complete"
