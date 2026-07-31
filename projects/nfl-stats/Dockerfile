FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV FFL_DB=/data/league.db
EXPOSE 8080
# One threaded worker: a single in-memory copy of the season data (2 workers
# OOM-killed the 512MB machine). Threads handle the family's low concurrency.
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--timeout", "180", "--workers", "1", "--threads", "8"]
