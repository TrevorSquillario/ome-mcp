FROM python:3.11-slim

LABEL maintainer="ome_mcp_v5" \
      description="Dell OME MCP v5 – Streaming HTTP MCP server for OpenManage Enterprise" \
      version="5.2.0"

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Build/runtime environment: 'dev' or 'prod'
ARG OME_APP_ENV=dev
ENV OME_APP_ENV=${OME_APP_ENV}

# Install OS deps (ca-certificates for optional SSL)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Non-root user
RUN useradd -m -u 1000 mcpuser && chown -R mcpuser:mcpuser /app
USER mcpuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:${OME_MCP_PORT:-8000}/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]