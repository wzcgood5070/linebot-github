FROM cloudflare/cloudflared:latest AS cloudflared

FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BROWSER_HEADLESS=true \
    BROWSER_CHANNEL=

WORKDIR /app

COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared

COPY requirements.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-impress fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY setup-dsm-login.py test-nas.py pytest.ini ./
COPY tests ./tests
COPY n8n ./n8n
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN mkdir -p /app/data /app/work/logs && \
    useradd --create-home --uid 10001 linebot && \
    sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && \
    chmod +x /usr/local/bin/docker-entrypoint.sh && \
    chown -R linebot:linebot /app

USER linebot
EXPOSE 8000
VOLUME ["/app/data", "/app/work"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
