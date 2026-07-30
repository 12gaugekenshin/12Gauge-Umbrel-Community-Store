FROM python:3.12-alpine3.22

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/data
WORKDIR /app
COPY app ./app
COPY web ./web
RUN mkdir -p /data && chown -R 1000:1000 /app /data
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1
CMD ["python", "-m", "app.main"]
