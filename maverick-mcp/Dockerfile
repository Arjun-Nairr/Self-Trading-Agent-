# Dockerfile for Maverick-MCP
# Python-only MCP server

# Pinned by digest (2026-08-28, hermes-trading-agent project) --
# python:3.12-slim, the currently installed/verified base -- see
# ../docker-compose.yml gateway comment for why. A local, tracked change
# to this vendored file, not an upstream one.
FROM python@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65

# MCP registry identity label (Docker MCP Catalog / GHCR discovery)
LABEL io.modelcontextprotocol.server.name="io.github.wshobson/maverick-mcp"

WORKDIR /app

# Install system dependencies (build tools + Postgres client headers for
# psycopg2-binary). ta-lib and its compile step are gone: the backtesting
# extra now uses pandas-ta, a pure-Python dependency.
RUN apt-get update && apt-get install -yqq --no-install-recommends \
  build-essential \
  ca-certificates \
  curl \
  libpq-dev \
  python3-dev \
  && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python package management
RUN pip install --no-cache-dir uv

# Copy dependency files first for better caching
COPY pyproject.toml uv.lock README.md ./

# Install Python dependencies. Ships the backtesting and research extras so
# the container image has the full tool surface out of the box; drop
# --extra backtesting --extra research for a smaller, core-only image.
RUN uv sync --frozen --extra backtesting --extra research

# Copy application code
COPY maverick ./maverick

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Create non-root user. /app/data pre-exists here (not just at runtime) so
# a named volume mounted there inherits maverick's ownership instead of
# defaulting to root — SQLite DB + cache live here (see docker-compose.yml,
# DATABASE_URL/CACHE_SQLITE_PATH), no Postgres/Redis needed at our scale.
RUN groupadd -g 1000 maverick && \
    useradd -u 1000 -g maverick -s /bin/sh -m maverick && \
    mkdir -p /app/data && \
    chown -R maverick:maverick /app

USER maverick

EXPOSE 8000

# No HEALTHCHECK: the new server exposes no HTTP /health endpoint (it is an
# MCP server, not a REST API). Container orchestrators should instead use
# process liveness or an MCP-aware probe.

# Start MCP server (streamable HTTP transport for container deployment)
CMD ["uv", "run", "python", "-m", "maverick.server", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
