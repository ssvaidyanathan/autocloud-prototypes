# Multi-stage build for FastMCP Calculator Tool Service
FROM python:3.12-slim AS builder

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
COPY __init__.py main.py server.py /app/
RUN pip install --no-cache-dir --prefix=/install .

# Final runtime stage
FROM python:3.12-slim AS runner

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

RUN adduser --disabled-password --gecos "" --uid 10001 appuser

COPY --from=builder /install /usr/local

COPY . /app/

USER appuser

EXPOSE 8080

CMD ["python", "main.py"]
