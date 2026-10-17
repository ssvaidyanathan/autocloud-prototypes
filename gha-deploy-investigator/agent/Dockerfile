FROM python:3.12-slim AS builder

# Install uv binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies using uv
COPY pyproject.toml /app/
COPY app/ /app/app/
RUN uv pip install --system --no-cache .

# Final runtime image
FROM python:3.12-slim

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -u 10001 appuser

WORKDIR /app

# Copy dependencies from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code and skills
COPY app/ /app/agent/app/
COPY app/ /app/app/
COPY skills/ /app/skills/
COPY pyproject.toml /app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080

EXPOSE 8080

USER appuser

ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
