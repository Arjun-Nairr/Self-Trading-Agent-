# Dedicated, private Laya worker image (EXPERIMENTAL fixed-30 selector).
# NOT part of docker-compose.yml and not a service: the controller runs it with
#   docker run --network none --read-only --cap-drop ALL ... (see
#   laya_experiment.docker_run_command) and talks to it over stdin/stdout only.
#
# Build (context = backtest-harness/, so only the files named below can enter):
#   docker build -f backtest-harness/laya_worker_image/Dockerfile \
#       -t hermes-laya-worker:2 backtest-harness
#
# Contents: CPU-only torch + the pinned Laya packages + exactly three project
# modules (the worker, the adapter, the pure card/selector types). No trading,
# risk, Hermes, masking, Neon, or Alpaca code and no credentials of any kind.
# The checkpoint is NOT baked in: it is mounted read-only at run time.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 HOME=/tmp

COPY requirements-laya.txt /tmp/requirements-laya.txt
# CPU wheel first (the default PyPI Linux wheel drags in GBs of CUDA libraries),
# then every other pin from the same requirements file the host venv uses.
RUN pip install "torch==2.14.1" --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r /tmp/requirements-laya.txt \
 && rm /tmp/requirements-laya.txt

RUN useradd --no-create-home --uid 10001 --shell /usr/sbin/nologin laya
COPY laya_worker.py laya_adapter.py laya_selector.py /app/
WORKDIR /app
USER 10001:10001
ENTRYPOINT ["python", "-u", "/app/laya_worker.py"]
