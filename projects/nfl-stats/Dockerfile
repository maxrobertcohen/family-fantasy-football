FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV FFL_DB=/data/league.db
EXPOSE 8080
# --preload: load + warm the app once in the master, then fork workers that
# inherit the populated cache (copy-on-write) — so no worker downloads data
# mid-request. --timeout 180 covers the one-time startup CSV fetch.
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--timeout", "180", "--workers", "2", "--preload"]
