FROM python:3.12-alpine3.24@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup -S -g 10001 app \
    && adduser -S -u 10001 -G app app \
    && mkdir -p /data/car-care /data/oauth \
    && chown -R 10001:10001 /data

COPY --chown=10001:10001 app ./app

USER 10001:10001
CMD ["python", "-m", "app.main"]
