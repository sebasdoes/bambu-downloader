FROM python:3.13-slim

# No __pycache__ bloat; log to stdout (12-factor); uvicorn handles SIGTERM.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Unprivileged runtime user. BND_UID is configurable at build time so the
# container user matches whoever owns the bind-mounted downloads/ and data/
# (rootless podman maps container UIDs into a subuid range; container 1000
# is NOT host 1000 there — build with --build-arg BND_UID=$(id -u) if you
# hit "attempt to write a readonly database").
ARG BND_UID=1000
RUN useradd --uid ${BND_UID} --no-user-group --create-home downloader \
    && mkdir -p /app/downloads /app/data \
    && chown -R ${BND_UID}:0 /app
USER ${BND_UID}

ENV BND_DOWNLOAD_DIR=/app/downloads \
    BND_DATA_DIR=/app/data \
    BND_PORT=8080

VOLUME ["/app/downloads", "/app/data"]

EXPOSE 8080

# Uvicorn runs as PID 1: it traps SIGTERM/SIGINT itself, drains connections,
# and runs the lifespan shutdown (scheduler cancel) — clean podman stop.
# HEALTHCHECK pings the status endpoint; podman/Kube share it via healthfile.
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os,sys;sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"BND_PORT\",\"8080\")}/api/status',timeout=4).status==200 else 1)"

# --timeout-graceful-shutdown bounds the drain window so podman stop's 10s
# default never escalates to SIGKILL mid-checkpoint.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--no-access-log", "--timeout-graceful-shutdown", "15"]