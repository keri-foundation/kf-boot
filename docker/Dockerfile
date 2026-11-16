# Four-host pool image (wit/wat + kf-boot/registrar/observer/mailbox).
# Installs from sibling working trees via Compose additional_contexts
# (see docker-compose.yml). Layout keeps ../keripy uv.sources valid.
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

# Sibling tree so kf-registrar / kf-observer [tool.uv.sources] ../keripy resolves.
COPY --from=keripy . /opt/src/keripy
COPY --from=witness-hk . /opt/src/witness-hk
COPY --from=watcher-hk . /opt/src/watcher-hk
COPY --from=kf-mailbox . /opt/src/kf-mailbox
COPY --from=kf-registrar . /opt/src/kf-registrar
COPY --from=kf-observer . /opt/src/kf-observer
COPY . /opt/src/kf-boot

# keri first from local keripy; then service packages without re-resolving keri
# from GitHub (witness-hk / watcher-hk / etc. still declare git+keripy).
RUN uv pip install --no-cache -e /opt/src/keripy \
    && uv pip install --no-cache \
        "hio>=0.7.19" \
        "multicommand>=1.0.0" \
        "falcon>=4.2.0" \
        "pyotp>=2.9.0" \
        "requests>=2.32.3" \
    && uv pip install --no-cache --no-deps \
        -e /opt/src/witness-hk \
        -e /opt/src/watcher-hk \
        -e /opt/src/kf-mailbox \
        -e /opt/src/kf-registrar \
        -e /opt/src/kf-observer \
        -e /opt/src/kf-boot

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
