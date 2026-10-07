# Контейнер бэкенда LearnQuest. Данные (SQLite, загруженные файлы) — в томе /data.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=80

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server ./server
COPY run.py .

VOLUME ["/data"]
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:80/api/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "80", "--proxy-headers", "--forwarded-allow-ips", "*"]
