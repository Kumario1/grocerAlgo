FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DISPLAY=:99 \
    CHROME_PATH=/usr/bin/google-chrome \
    HEB_RUNTIME_DIR=/app/runtime

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends wget xvfb tesseract-ocr \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p /app/runtime

EXPOSE 8000
VOLUME ["/app/runtime"]

CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x720x24 -nolisten tcp & exec python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
