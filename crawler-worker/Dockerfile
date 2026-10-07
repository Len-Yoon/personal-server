FROM python:3.11-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929

LABEL org.opencontainers.image.source="https://github.com/Len-Yoon/personal-server"
LABEL org.opencontainers.image.title="crawler-worker"
LABEL org.opencontainers.image.description="Personal Server crawler worker"
LABEL org.opencontainers.image.vendor="Len-Yoon"
LABEL org.opencontainers.image.url="https://github.com/Len-Yoon/personal-server"
LABEL org.opencontainers.image.documentation="https://github.com/Len-Yoon/personal-server#readme"

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir setuptools==84.0.0 wheel==0.48.0 \
    && pip install --no-cache-dir -r requirements.txt

RUN addgroup -S -g 10001 app && adduser -S -u 10001 -G app app
COPY --chown=10001:10001 . .
RUN mkdir -p /app/data/logs && chown -R 10001:10001 /app/data
USER 10001:10001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
