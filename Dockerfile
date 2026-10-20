# syntax=docker/dockerfile:1.7
# Reproducible image: base pinned by digest, every Python dependency pinned by version and hash.
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.lock .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps --only-binary=:all: -r requirements.lock

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="calendar-bridge" \
      org.opencontainers.image.description="Bidirectional iCloud CalDAV <-> Google Calendar synchronization" \
      org.opencontainers.image.version="0.2.0" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/Nicolas1bhr/calendar-bridge"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CB_DATA_DIR=/data \
    TZ=UTC
RUN groupadd --system --gid 10001 bridge \
 && useradd --system --uid 10001 --gid bridge --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin bridge \
 && mkdir -p /data /config \
 && chown bridge:bridge /data \
 && chmod 0700 /data
COPY --from=build /opt/venv /opt/venv
COPY src/calendar_bridge /app/calendar_bridge
RUN printf '#!/bin/sh\nexec python -m calendar_bridge "$@"\n' > /usr/local/bin/calendar-bridge \
 && chmod 0755 /usr/local/bin/calendar-bridge \
 && python -m compileall -q /app
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
# No EXPOSE: the service only makes outbound HTTPS requests.
HEALTHCHECK --interval=60s --timeout=30s --start-period=180s --retries=3 CMD ["calendar-bridge", "health"]
ENTRYPOINT ["calendar-bridge"]
CMD ["daemon"]
