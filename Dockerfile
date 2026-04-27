FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY server-poller-json-linux-amd64 .
RUN chmod +x /app/server-poller-json-linux-amd64

VOLUME ["/app/data"]

CMD ["python", "bot.py"]
