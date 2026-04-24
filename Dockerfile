# KAI Agent — multi-stage build
#
# Builds the daemon + web UI in a single image.
# Runtime: python:3.13-slim with Node for the SvelteKit build step.
#
# Usage:
#   docker build -t kai-agent .
#   docker run -p 8765:8765 -v ./workspaces:/app/workspaces kai-agent

# ── Stage 1: build the web UI ──────────────────────────────
FROM node:20-slim AS web-builder

WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ .
RUN npm run build

# ── Stage 2: Python runtime ───────────────────────────────
FROM python:3.13-slim

# System deps for native wheels (numpy, pandas, etc.)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir pytest

# Copy the built web UI from stage 1
COPY --from=web-builder /build/build web/build/

# Copy application code
COPY main.py .
COPY agent-config.json .
COPY agent/ agent/
COPY daemon/ daemon/
COPY taskboard_gateway/ taskboard_gateway/
COPY tui/ tui/
COPY bin/ bin/
COPY deploy/ deploy/

# Create workspaces dir (will be mounted as volume)
RUN mkdir -p workspaces/strategies workspaces/sessions

# Default daemon port. Docker Compose may map 18789 to this same daemon app
# for OpenClaw/taskboard compatibility.
EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/health')" || exit 1

# Run the daemon by default
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py", "--daemon"]
