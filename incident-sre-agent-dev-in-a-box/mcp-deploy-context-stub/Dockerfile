FROM python:3.12-slim

WORKDIR /app

# Set non-interactive and unbuffered environment
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Create non-root application user
RUN groupadd -r appuser && useradd -r -g appuser -m -d /home/appuser appuser

# Install dependencies
COPY pyproject.toml README.md /app/
RUN pip install --no-cache-dir -e .

# Copy application source, schemas, and bundled fixtures
COPY app/ /app/app/
COPY schemas/ /app/schemas/
COPY fixtures/ /app/fixtures/

# Ensure non-root ownership
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
