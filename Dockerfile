FROM python:3.12-slim

WORKDIR /app

# asyncpg and other deps generally ship prebuilt wheels, but keep build tools available
# in case a wheel isn't available for the target architecture (e.g. ARM EC2 instances).
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "app.main"]
