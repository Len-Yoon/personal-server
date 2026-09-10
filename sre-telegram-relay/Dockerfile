FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

WORKDIR /app

RUN addgroup --system --gid 10001 relay && adduser --system --uid 10001 --ingroup relay relay
COPY --chown=10001:10001 app ./app

USER 10001:10001
EXPOSE 8080

CMD ["python", "-m", "app.main"]
