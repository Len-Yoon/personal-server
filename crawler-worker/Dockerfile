FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

LABEL org.opencontainers.image.source="https://github.com/Len-Yoon/personal-server"
LABEL org.opencontainers.image.title="crawler-worker"
LABEL org.opencontainers.image.description="Personal Server crawler worker"
LABEL org.opencontainers.image.vendor="Len-Yoon"
LABEL org.opencontainers.image.url="https://github.com/Len-Yoon/personal-server"
LABEL org.opencontainers.image.documentation="https://github.com/Len-Yoon/personal-server#readme"

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup --system --gid 10001 app && adduser --system --uid 10001 --ingroup app app
COPY --chown=10001:10001 . .
RUN mkdir -p /app/data/logs && chown -R 10001:10001 /app/data
USER 10001:10001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
