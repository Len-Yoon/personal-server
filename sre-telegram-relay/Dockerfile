FROM python:3.11-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929

WORKDIR /app

RUN apk upgrade --no-cache \
    && pip install --no-cache-dir setuptools==84.0.0 wheel==0.48.0 \
    && addgroup -S -g 10001 relay \
    && adduser -S -u 10001 -G relay -H -h /nonexistent relay
COPY --chown=10001:10001 app ./app

USER 10001:10001
EXPOSE 8080

CMD ["python", "-m", "app.main"]
