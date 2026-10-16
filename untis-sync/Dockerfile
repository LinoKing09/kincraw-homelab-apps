FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 TZ=Europe/Berlin DATA_DIR=/data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static ./static

VOLUME /data
EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4)"

# 1 Worker: der Auto-Sync-Thread darf nur einmal laufen
CMD ["gunicorn", "-w", "1", "--threads", "4", "--timeout", "180", "-b", "0.0.0.0:5000", "app:app"]
