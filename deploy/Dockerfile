# Build from the project root:  docker compose -f deploy/docker-compose.yml up -d --build
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RUNTIME_PROFILE=persistent

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
COPY tools ./tools

RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /app/data /app/logs \
    && chown -R botuser /app
USER botuser

# the bot handles SIGTERM: stops polling, finishes the in-flight publish, releases the lease
STOPSIGNAL SIGTERM
CMD ["python", "-m", "bot"]
