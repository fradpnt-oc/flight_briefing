FROM python:3.12-slim

WORKDIR /app

# Install curl + Docker CLI (static binary — works on arm64/amd64)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI static binary for arm64 (Mac mini M4)
RUN curl -fsSL https://download.docker.com/linux/static/stable/aarch64/docker-27.5.1.tgz \
    | tar xz --strip-components=1 -C /usr/local/bin docker/docker

COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/bot.py .

CMD ["python", "-u", "bot.py"]
