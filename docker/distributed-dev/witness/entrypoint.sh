#!/usr/bin/env sh
set -eu

: "${WIT_ALIAS:?WIT_ALIAS is required}"
: "${WIT_BASE:?WIT_BASE is required}"
: "${WIT_BOOT_PORT:?WIT_BOOT_PORT is required}"
: "${WIT_HTTP_PORT:?WIT_HTTP_PORT is required}"
: "${WIT_PUBLIC_URL:?WIT_PUBLIC_URL is required}"

CONFIG_DIR="/data/config"
mkdir -p "${CONFIG_DIR}/keri/cf/main"

cat > "${CONFIG_DIR}/keri/cf/main/witopnet.json" <<EOF
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "witopnet": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["${WIT_PUBLIC_URL%/}/"]
  }
}
EOF

export KERI_BASER_MAP_SIZE="${KERI_BASER_MAP_SIZE:-1099511627776}"

exec witopnet marshal start \
  --base "${WIT_BASE}" \
  --config-dir "${CONFIG_DIR}" \
  --boothost 0.0.0.0 \
  --bootport "${WIT_BOOT_PORT}" \
  --host 0.0.0.0 \
  --http "${WIT_HTTP_PORT}" \
  --loglevel INFO
