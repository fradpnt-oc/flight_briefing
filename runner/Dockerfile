FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY runner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the briefing script
COPY runner/flight_briefing.py .
COPY runner/briefing_template.html .
COPY runner/init_db.py .
COPY db/seed.sql .
COPY db/prompts.json .

RUN mkdir -p /data

# Run DB init then keep container alive for docker exec calls from bot
CMD ["sh", "-c", "python3 init_db.py && sleep infinity"]
