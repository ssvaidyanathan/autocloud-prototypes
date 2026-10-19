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

# Copy application code
COPY app/ /app/app/
COPY pyproject.toml /app/

# Cloud Run's front end terminates TLS and connects to the container from the link-local
# range 169.254.0.0/16. Trusting its X-Forwarded-Proto header there makes request.base_url
# report https, so the default callback URL handed to the agent is https (the agent rejects http).
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080 \
    FORWARDED_ALLOW_IPS=169.254.0.0/16

EXPOSE 8080

USER appuser

ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
