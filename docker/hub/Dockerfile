FROM moby/buildkit:v0.34.1@sha256:99368f3e46177c6405b9740e4461d91cee004128daeecca78d4ceac50ec1da5c AS buildkit-tools

FROM python:3.12-slim AS builder

WORKDIR /app/hub
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.8.14
COPY hub/src/ ./src/
COPY hub/pyproject.toml hub/uv.lock ./
RUN uv sync --frozen --no-dev

FROM builder AS build-worker-builder

COPY pyproject.toml README.md /app/client/
COPY src/ /app/client/src/
COPY deploy/kolla/ansible/roles/palimpsest/ /app/client/deploy/kolla/ansible/roles/palimpsest/
COPY deploy/kolla/ansible/palimpsest-build-worker.yml /app/client/deploy/kolla/ansible/palimpsest-build-worker.yml
RUN uv build --wheel --out-dir /app/wheels /app/client \
    && uv pip install --no-cache --python /app/hub/.venv/bin/python /app/wheels/*.whl

FROM python:3.12-slim AS runtime

WORKDIR /app/hub
COPY --from=builder /app/hub/.venv /app/hub/.venv
COPY hub/src/ ./src/
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl qemu-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin palimpsest \
    && chown -R palimpsest:palimpsest /app
ENV PATH="/app/hub/.venv/bin:$PATH"
USER palimpsest

FROM runtime AS palimpsest-hub-build-worker

USER root
COPY --from=build-worker-builder /app/hub/.venv /app/hub/.venv
COPY --from=buildkit-tools /usr/bin/buildctl /usr/bin/buildkitd /usr/bin/buildkit-runc /usr/bin/
COPY --from=buildkit-tools /usr/bin/buildkit-cni-bridge /usr/bin/buildkit-cni-loopback /usr/bin/buildkit-cni-host-local /usr/bin/buildkit-cni-firewall /usr/bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap squashfs-tools util-linux mount e2fsprogs skopeo iptables iproute2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
CMD ["python", "-m", "palimpsest_hub.build_worker"]

FROM runtime AS palimpsest-hub-api
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "from urllib.request import urlopen; response = urlopen('http://127.0.0.1:8020/v1/ready', timeout=5); assert response.status == 200"
CMD ["uvicorn", "palimpsest_hub.main:app", "--host", "0.0.0.0", "--port", "8020"]

FROM runtime AS palimpsest-hub-worker
CMD ["python", "-m", "palimpsest_hub.worker"]
