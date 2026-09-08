FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup --system --gid 10001 app && adduser --system --uid 10001 --ingroup app app
COPY --chown=10001:10001 . .
USER 10001:10001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
