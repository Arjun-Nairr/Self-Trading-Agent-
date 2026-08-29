# Pinned by digest (2026-08-28) -- python:3.12-slim, the currently
# installed/verified base -- see docker-compose.yml gateway comment for why.
FROM python@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py test_idempotency.py test_decision_fields.py test_mode_isolation.py \
     test_run_isolation.py test_dashboard_fields.py ./

RUN groupadd -g 1000 mcp && useradd -u 1000 -g mcp -s /bin/sh -m mcp && chown -R mcp:mcp /app
USER mcp

EXPOSE 8000

CMD ["python", "server.py"]
