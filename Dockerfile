FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN python -m venv /opt/venv

COPY pyproject.toml README.md ./
COPY src ./src

RUN /opt/venv/bin/pip install .


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 ics-merger \
    && useradd --system --uid 10001 --gid ics-merger --home-dir /app ics-merger \
    && mkdir -p /app \
    && chown ics-merger:ics-merger /app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER ics-merger

EXPOSE 8000

HEALTHCHECK --interval=5m --timeout=3s --start-period=30s --start-interval=2s --retries=3 \
    CMD ["python", "-S", "-c", "import socket;s=socket.create_connection(('127.0.0.1',8000),2);s.sendall(b'GET /api/health HTTP/1.0\\r\\n\\r\\n');raise SystemExit(b'200 OK' not in s.recv(128))"]

CMD ["python", "-m", "ics_merger", "--config", "/config/config.yaml", "--host", "0.0.0.0", "--port", "8000"]