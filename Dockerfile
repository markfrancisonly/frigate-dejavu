FROM python:3.12-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg/ffprobe for capture + freeze synthesis
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
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
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8898/healthz', timeout=3).read()"]

CMD ["python3", "/app/api.py"]
