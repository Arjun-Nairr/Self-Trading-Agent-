FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py test_status_mapping.py test_historical_news.py test_account_math.py ./

RUN groupadd -g 1000 mcp && useradd -u 1000 -g mcp -s /bin/sh -m mcp && chown -R mcp:mcp /app
USER mcp

EXPOSE 8000

CMD ["python", "server.py"]
