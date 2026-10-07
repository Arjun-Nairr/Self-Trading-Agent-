# Dedicated, private image for the SHADOW-ONLY Laya prompt-injection monitor
# (backtest-harness/laya_injection.py). Separate from the selector worker image
# (hermes-laya-worker:2), which is unchanged. Not a compose service: the
# controller runs it with the same docker run isolation as the selector
# worker (--network none, --read-only, --cap-drop ALL, non-root, read-only
# checkpoint mount; laya_experiment.docker_run_command) over stdin/stdout only.
#
# Build (context = backtest-harness/, so only the files named below can enter):
#   docker build -f backtest-harness/laya_injection_image/Dockerfile \
#       -t hermes-laya-injection-monitor:1 backtest-harness
#
# Same base digest, torch wheel and pinned requirements as the selector image.
# Contents: the monitor worker plus the three modules it imports for the
# pinned load, the network deny and the identity type. No trading, risk,
# Hermes, masking, Neon or Alpaca code, and no credentials.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 HOME=/tmp

COPY requirements-laya.txt /tmp/requirements-laya.txt
RUN pip install "torch==2.14.1" --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r /tmp/requirements-laya.txt \
 && rm /tmp/requirements-laya.txt

RUN useradd --no-create-home --uid 10001 --shell /usr/sbin/nologin laya
COPY laya_injection_worker.py laya_worker.py laya_adapter.py laya_selector.py /app/
WORKDIR /app
USER 10001:10001
ENTRYPOINT ["python", "-u", "/app/laya_injection_worker.py"]
