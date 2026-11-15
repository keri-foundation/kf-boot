# Four-host pool image (wit/wat + kf-boot/registrar/observer/mailbox).
# Installs match full_setup_guide.md: keri from keripy Git, then each service from GitHub main.
# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsodium-dev \
    gcc \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /opt/keri
RUN uv venv /opt/keri/venv
ENV VIRTUAL_ENV=/opt/keri/venv
ENV PATH="/opt/keri/venv/bin:$PATH"
ENV UV_LINK_MODE=copy

# Same Git sources as full_setup_guide.md §4 (default branch = main).
RUN uv pip install --no-cache "keri @ git+https://github.com/WebOfTrust/keripy.git" \
    && uv pip install --no-cache git+https://github.com/keri-foundation/witness-hk.git \
    && uv pip install --no-cache git+https://github.com/keri-foundation/watcher-hk.git \
    && uv pip install --no-cache git+https://github.com/keri-foundation/kf-mailbox.git \
    && uv pip install --no-cache git+https://github.com/keri-foundation/kf-registrar.git \
    && uv pip install --no-cache git+https://github.com/keri-foundation/kf-observer.git \
    && uv pip install --no-cache git+https://github.com/keri-foundation/kf-boot.git

COPY docker/entrypoint-syslog.sh /entrypoint-syslog.sh
COPY docker/entrypoint-common.sh /entrypoint-common.sh
COPY docker/entrypoint-host1.sh /entrypoint-host1.sh
COPY docker/entrypoint-host2.sh /entrypoint-host2.sh
COPY docker/entrypoint-host3.sh /entrypoint-host3.sh
COPY docker/entrypoint-host4.sh /entrypoint-host4.sh
RUN chmod +x \
    /entrypoint-syslog.sh \
    /entrypoint-common.sh \
    /entrypoint-host1.sh \
    /entrypoint-host2.sh \
    /entrypoint-host3.sh \
    /entrypoint-host4.sh

RUN mkdir -p \
    /opt/keri/witness/config/keri/cf/main \
    /opt/keri/watcher/config/keri/cf/main \
    /opt/keri/kfboot/var \
    /opt/keri/kfboot/keri \
    /opt/keri/mailbox \
    /opt/keri/registrar/config/keri/cf \
    /opt/keri/observer/config/keri/cf \
    /shared \
    /var/log/keri

ENV KERI_BASER_MAP_SIZE=2147483648
ENV LOGLEVEL=INFO

ENTRYPOINT ["/entrypoint-host4.sh"]
