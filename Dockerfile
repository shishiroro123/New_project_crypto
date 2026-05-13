FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install -e .

COPY config ./config

RUN useradd --create-home --uid 1000 bot && chown -R bot:bot /app
USER bot

ENV DATA_DIR=/app/data
VOLUME ["/app/data", "/app/logs"]

ENTRYPOINT ["tini", "--", "crypto-bot"]
CMD ["--help"]
