# Polygon Indexer — multi-target Dockerfile
# Each service is a separate target sharing the same base

# ── Base ────────────────────────────────────────────────────
FROM python:3.12-slim AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

# ── RPC Gateway ─────────────────────────────────────────────
FROM base AS rpc-gateway
ENV SERVICE=rpc_gateway
CMD ["python", "-m", "src.rpc_gateway"]

# ── Ingest Service ──────────────────────────────────────────
FROM base AS ingest
ENV SERVICE=ingest
CMD ["python", "-m", "src.ingest"]

# ── Decoder ─────────────────────────────────────────────────
FROM base AS decoder
ENV SERVICE=decoder
CMD ["python", "-m", "src.decoder"]

# ── Analytics ───────────────────────────────────────────────
FROM base AS analytics
ENV SERVICE=analytics
CMD ["python", "-m", "src.analytics"]
