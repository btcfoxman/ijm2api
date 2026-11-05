FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IJM_CHROME_EXECUTABLE=/usr/bin/chromium \
    IJM_CHROME_USER_DATA_ROOT=/app/data/chrome-profiles \
    IJM_DATA_DIR=/app/data

WORKDIR /app
COPY requirements.txt .
COPY tools/install_canvas_cli.py /tmp/install_canvas_cli.py
COPY tools/build_canvas_proxy.py /tmp/build_canvas_proxy.py
COPY .canvas-proxy-build /tmp/canvas-proxy-runtime
RUN set -eu; \
    set -- ca-certificates chromium xauth xvfb x11vnc fluxbox novnc fonts-liberation fonts-noto-cjk ffmpeg; \
    (apt-get -o Acquire::Retries=2 -o APT::Update::Error-Mode=any update \
        || apt-get -o Acquire::Retries=2 -o APT::Update::Error-Mode=any \
            -o Acquire::http::Proxy=DIRECT -o Acquire::https::Proxy=DIRECT update) \
    && (apt-get -o Acquire::Retries=2 --download-only install -y --no-install-recommends "$@" \
        || apt-get -o Acquire::Retries=2 -o Acquire::http::Proxy=DIRECT \
            -o Acquire::https::Proxy=DIRECT --download-only install -y --no-install-recommends "$@") \
    && apt-get -o APT::Get::Fix-Missing=false --no-download install -y --no-install-recommends "$@" \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && python /tmp/install_canvas_cli.py \
    && python /tmp/build_canvas_proxy.py --verify-only /tmp/canvas-proxy-runtime \
    && cp /tmp/canvas-proxy-runtime/graftcp /usr/local/bin/graftcp \
    && chmod 755 /usr/local/bin/graftcp \
    && mkdir -p /usr/local/share/doc/graftcp \
    && cp -a /tmp/canvas-proxy-runtime/docs/. /usr/local/share/doc/graftcp/ \
    && /usr/local/bin/dreamina-canvas --format json version \
    && /usr/local/bin/graftcp --version \
    && rm /tmp/install_canvas_cli.py /tmp/build_canvas_proxy.py \
    && rm -rf /tmp/canvas-proxy-runtime
COPY app ./app
RUN mkdir -p /app/data/chrome-profiles

EXPOSE 8808
CMD ["xvfb-run", "-a", "-s", "-screen 0 1280x960x24 -nolisten tcp", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8808"]
