FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY scripts/ scripts/
COPY gunicorn.conf.py .

RUN mkdir -p /app/cache

EXPOSE 8000

# Default: 4 workers (2×CPU for I/O bound), 120s timeout for long HLS segment serves.
ENV WORKERS=4

CMD ["sh", "-c", "exec gunicorn app.main:app \
    -c gunicorn.conf.py \
    -k uvicorn.workers.UvicornWorker \
    --workers ${WORKERS} \
    --bind 0.0.0.0:8000 \
    --timeout 300 \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --max-requests 10000 \
    --max-requests-jitter 500 \
    --forwarded-allow-ips '*' \
    --access-logfile - \
    --error-logfile -"]
