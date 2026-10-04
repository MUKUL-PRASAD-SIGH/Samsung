# syntax=docker/dockerfile:1
# Multi-stage: build the React UI, install pinned Python deps (CPU-only torch), then a slim non-root runtime.
#   docker build -t interruptible-agent .
#   docker build --build-arg BAKE_MODELS=0 -t interruptible-agent:slim .   # download models at first run instead

# ---- 1. frontend --------------------------------------------------------------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 2. python dependencies ---------------------------------------------------------------------------------------
FROM python:3.11-slim AS python-deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /tmp
COPY requirements.lock .
# requirements.lock was compiled on a machine whose torch pulls the CUDA stack; this image is CPU-only, so drop those
# pins and take torch from the CPU index (22M-parameter MiniLM does not need a GPU).
RUN grep -viE '^(nvidia|triton|cuda)' requirements.lock > req.txt \
 && python -m venv /opt/venv \
 && /opt/venv/bin/pip install --extra-index-url https://download.pytorch.org/whl/cpu -r req.txt

# ---- 3. runtime ---------------------------------------------------------------------------------------------------
FROM python:3.11-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /models /data && chown app:app /models /data
COPY --from=python-deps /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/hf \
    TTS_VOICE_PATH=/models/piper/en_US-lessac-medium.onnx \
    LOG_FORMAT=json \
    TRACE_LOG_PATH=/data/trace.jsonl
WORKDIR /app
COPY agent ./agent
COPY scripts ./scripts
COPY --from=frontend /app/frontend/dist ./frontend/dist
USER app
ARG BAKE_MODELS=1
RUN if [ "$BAKE_MODELS" = "1" ]; then python scripts/fetch_models.py; fi
# With models baked in, never ask the Hub for anything at runtime (works air-gapped, no startup HEAD requests).
ENV HF_HUB_OFFLINE=${BAKE_MODELS}
VOLUME ["/models", "/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "agent.server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
