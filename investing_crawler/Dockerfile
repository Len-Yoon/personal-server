FROM python:3.11-slim

WORKDIR /app

COPY investing_crawler/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY investing_crawler /app

CMD ["python", "-m", "app.main"]
