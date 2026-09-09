# Production Dockerfile for Business AI
# Multi-stage, minimal, non-root — same proven pattern as Shri AI's
# Dockerfile (this session's earlier fix: root-owned mounted volumes get
# fixed at boot by the entrypoint running as root, then the actual server
# process drops to a non-root user via gosu before exec'ing).

FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip wheel \
    && pip wheel --no-cache-dir --wheel-dir /build/wheels -r requirements.txt

# ==============================================================================
# Production Runtime Stage
# ==============================================================================
FROM python:3.11-slim AS runner

RUN groupadd -g 1000 appgroup && \
    useradd -u 1000 -g appgroup -m -s /bin/bash appuser

# gosu lets the entrypoint (which must run as root to fix the ownership of
# a freshly-mounted Railway volume on boot) drop privileges before exec'ing
# the actual server process — the app itself never runs as root.
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /build/wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

COPY src/ /app/src/
COPY static/ /app/static/
COPY pyproject.toml README.md entrypoint.sh /app/

# Editable install: business_ai's PROJECT_ROOT is computed from __file__ at
# runtime (Path(__file__).resolve().parent.parent.parent), so it resolves
# correctly to /app as long as src/ and static/ stay siblings on disk here
# — this is the same reason local dev works with PYTHONPATH=src instead of
# a real install.
RUN pip install --no-cache-dir --no-deps -e .

RUN mkdir -p /app/data && \
    chmod +x /app/entrypoint.sh && \
    chown -R appuser:appgroup /app

# Container starts as root: entrypoint.sh needs root to fix ownership of
# the mounted Railway volume on boot, then drops to `appuser` via gosu
# before exec'ing uvicorn — the app process itself never runs as root.
USER root

EXPOSE 8000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["/app/entrypoint.sh"]
