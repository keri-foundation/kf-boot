#!/usr/bin/env sh
set -eu

: "${WAT_BOOT_PORT:?WAT_BOOT_PORT is required}"
: "${WAT_HTTP_PORT:?WAT_HTTP_PORT is required}"
: "${WAT_PUBLIC_URL:?WAT_PUBLIC_URL is required}"

CONFIG_DIR="/data/config"
mkdir -p "${CONFIG_DIR}/keri/cf/main"

cat > "${CONFIG_DIR}/keri/cf/main/watopnet.json" <<EOF
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "watopnet": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["${WAT_PUBLIC_URL%/}/"]
  }
}
EOF

export KERI_BASER_MAP_SIZE="${KERI_BASER_MAP_SIZE:-1099511627776}"

exec watopnet start \
  --config-dir "${CONFIG_DIR}" \
  --boothost 0.0.0.0 \
  --bootport "${WAT_BOOT_PORT}" \
  --host 0.0.0.0 \
  --http "${WAT_HTTP_PORT}" \
  --loglevel INFO
