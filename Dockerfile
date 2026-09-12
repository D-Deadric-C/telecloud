FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    WEB_CONCURRENCY=2

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telecloud ./telecloud

EXPOSE 8000

CMD ["sh", "-c", "uvicorn telecloud.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY} --no-access-log"]
