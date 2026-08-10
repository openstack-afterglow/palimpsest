FROM python:3.12-slim AS builder

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.8.14
COPY src/ ./src/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl qemu-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin palimpsest \
    && chown -R palimpsest:palimpsest /app
ENV PATH="/app/.venv/bin:$PATH"
USER palimpsest

FROM runtime AS palimpsest-hub-api
CMD ["uvicorn", "palimpsest_hub.main:app", "--host", "0.0.0.0", "--port", "8020"]

FROM runtime AS palimpsest-hub-worker
CMD ["python", "-m", "palimpsest_hub.worker"]
