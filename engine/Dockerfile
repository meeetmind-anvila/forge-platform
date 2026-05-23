FROM docker:29-cli AS docker-cli

FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI for spawning job containers through the mounted Docker socket.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# Python deps
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code (engine + registry modules)
COPY engine/ ./engine/
COPY registry/auth.py ./registry/auth.py
COPY registry/metadata.py ./registry/metadata.py

WORKDIR /app/engine

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--log-level", "info"]
