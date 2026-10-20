FROM python:3.12-slim

WORKDIR /app

# Install build dependencies (for aiosqlite, etc.) + tzdata для TZ=Europe/Bratislava
RUN apt-get update && apt-get install -y --no-install-recommends gcc tzdata && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
