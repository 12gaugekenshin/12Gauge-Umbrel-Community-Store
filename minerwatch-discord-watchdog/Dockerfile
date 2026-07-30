FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser -D -s /sbin/nologin watchdog

COPY config.example.json ./config.example.json
COPY miner_watchdog ./miner_watchdog

USER watchdog

EXPOSE 8787

CMD ["python", "-m", "miner_watchdog", "--config", "/data/config.json", "--web"]
