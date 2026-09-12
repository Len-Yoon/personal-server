FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --ingroup app app \
    && mkdir -p /data/car-care /data/oauth \
    && chown -R 10001:10001 /data

COPY --chown=10001:10001 app ./app

USER 10001:10001
CMD ["python", "-m", "app.main"]
