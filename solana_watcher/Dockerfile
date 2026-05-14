FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tini && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY solana_watcher/requirements.txt ./
RUN pip install -r requirements.txt

# Copy the watcher package as a top-level module (so we can `python -m solana_watcher.main`)
COPY solana_watcher /app/solana_watcher

RUN useradd --create-home --uid 1000 bot && chown -R bot:bot /app
USER bot

ENV DATA_DIR=/app/data
VOLUME ["/app/data"]

ENTRYPOINT ["tini", "--", "python", "-m", "solana_watcher.main"]
