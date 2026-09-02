FROM python:3.11-slim

WORKDIR /app

RUN addgroup --system relay && adduser --system --ingroup relay relay
COPY --chown=relay:relay app ./app

USER relay
EXPOSE 8080

CMD ["python", "-m", "app.main"]
