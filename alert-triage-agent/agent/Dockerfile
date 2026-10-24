FROM python:3.12-slim

WORKDIR /app

# Create knowledge directory for incident files
RUN mkdir -p /knowledge/incident-manager /tmp/knowledge/incident-manager && \
    chmod -R 777 /knowledge /tmp/knowledge

COPY pyproject.toml README.md /app/
COPY topology/ /app/topology/
COPY app/ /app/app/

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080 \
    INCIDENT_STORE_PATH=/knowledge/incident-manager \
    FALLBACK_STORE_PATH=/tmp/knowledge/incident-manager

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
