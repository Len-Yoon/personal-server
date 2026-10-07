FROM python:3.11-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir setuptools==84.0.0 wheel==0.48.0 \
    && pip install --no-cache-dir -r requirements.txt

RUN addgroup -S -g 10001 app \
    && adduser -S -u 10001 -G app app \
    && mkdir -p /data/files /var/lib/portal \
    && chown -R 10001:10001 /data /var/lib/portal

COPY --chown=10001:10001 . .

USER 10001:10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
