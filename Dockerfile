FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg/ffprobe for capture + freeze synthesis, curl for the container healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY app/ /app/

# docker-exec entrypoint: docker exec frigate-dejavu dejavu on|off|status ...
RUN printf '#!/bin/sh\nexec python3 /app/dejavu.py "$@"\n' > /usr/local/bin/dejavu \
    && chmod 755 /usr/local/bin/dejavu

WORKDIR /app
EXPOSE 8898

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://localhost:8898/healthz >/dev/null || exit 1

CMD ["python3", "/app/api.py"]
