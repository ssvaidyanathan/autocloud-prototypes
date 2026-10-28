FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY topology/ /app/topology/
COPY app/ /app/app/

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
