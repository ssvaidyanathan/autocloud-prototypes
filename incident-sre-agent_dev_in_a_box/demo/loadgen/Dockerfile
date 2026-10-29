FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir httpx>=0.27.0

COPY loadgen.py .

ENTRYPOINT ["python", "loadgen.py"]
