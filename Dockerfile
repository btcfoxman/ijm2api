FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IJM_CHROME_EXECUTABLE=/usr/bin/chromium \
    IJM_CHROME_USER_DATA_ROOT=/app/data/chrome-profiles \
    IJM_DATA_DIR=/app/data

WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium xauth xvfb x11vnc fluxbox novnc fonts-liberation fonts-noto-cjk ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir -p /app/data/chrome-profiles

EXPOSE 8808
CMD ["xvfb-run", "-a", "-s", "-screen 0 1280x960x24 -nolisten tcp", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8808"]
