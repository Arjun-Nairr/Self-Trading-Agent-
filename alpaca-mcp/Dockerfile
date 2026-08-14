FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
# ponytail: upstream's Dockerfile also COPYs .github/core/, which doesn't
# exist in a plain clone (build-server-only artifact, not vendored here)
# and nothing in src/ imports it at runtime — dropped, build was broken
# without this fix.

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH"

# Non-root — upstream's Dockerfile doesn't set this up, added to match
# every other container in this stack (§12B).
RUN groupadd -g 1000 mcp && useradd -u 1000 -g mcp -s /bin/sh -m mcp && chown -R mcp:mcp /app
USER mcp

# HTTP transport for remote MCP clients (e.g. ChatGPT). Bind all interfaces; use Render's $PORT via cli default.
CMD ["alpaca-mcp-server", "--transport", "streamable-http", "--host", "0.0.0.0"]
