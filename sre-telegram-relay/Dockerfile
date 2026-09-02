FROM python:3.11-slim

WORKDIR /app

RUN addgroup --system --gid 10001 relay && adduser --system --uid 10001 --ingroup relay relay
COPY --chown=10001:10001 app ./app

USER 10001:10001
EXPOSE 8080

CMD ["python", "-m", "app.main"]
