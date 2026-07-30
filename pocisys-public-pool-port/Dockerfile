FROM python:3.12-alpine3.22

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/data
WORKDIR /app
COPY app ./app
COPY web ./web
COPY entrypoint.sh /entrypoint.sh
RUN apk add --no-cache su-exec \
    && mkdir -p /data \
    && chmod 0755 /entrypoint.sh \
    && chown -R 1000:1000 /app /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
