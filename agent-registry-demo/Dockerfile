# Multi-stage build for Agent Registry Demo Agent Service
FROM python:3.12-slim AS builder

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
COPY app/ /app/app/
RUN pip install --no-cache-dir --prefix=/install .

# Final runtime stage
FROM python:3.12-slim AS runner

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    PYTHONPATH=/app

RUN adduser --disabled-password --gecos "" --uid 10001 appuser

COPY --from=builder /install /usr/local

COPY app/ /app/app/
COPY pyproject.toml /app/

USER appuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
