#!/usr/bin/env bash
# Shared helpers for four-host layout entrypoints (witopnet + watopnet).
# shellcheck shell=bash

: "${HOST_IP:?HOST_IP is required}"
LOGLEVEL="${LOGLEVEL:-INFO}"
WIT_HTTP_PORT="${WIT_HTTP_PORT:-5632}"
WIT_BOOT_PORT="${WIT_BOOT_PORT:-5631}"
WAT_HTTP_PORT="${WAT_HTTP_PORT:-7632}"
WAT_BOOT_PORT="${WAT_BOOT_PORT:-7631}"

PUBLIC_WITNESS_URL="${PUBLIC_WITNESS_URL:-http://${HOST_IP}:${WIT_HTTP_PORT}}"
PUBLIC_WATCHER_URL="${PUBLIC_WATCHER_URL:-http://${HOST_IP}:${WAT_HTTP_PORT}}"
PUBLIC_WITNESS_URL="${PUBLIC_WITNESS_URL%/}"
PUBLIC_WATCHER_URL="${PUBLIC_WATCHER_URL%/}"

WIT_CONFIG_DIR=/opt/keri/witness/config
WAT_CONFIG_DIR=/opt/keri/watcher/config

pids=()

host_cleanup() {
  local pid
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}

host_wait_for_http() {
  local url="$1"
  local label="$2"
  local i
  for i in $(seq 1 60); do
    if curl -sf -o /dev/null "$url"; then
      echo "${label} ready: ${url}"
      return 0
    fi
    if curl -s -o /dev/null -w "%{http_code}" "$url" | grep -Eq '^[1-5][0-9]{2}$'; then
      echo "${label} ready: ${url}"
      return 0
    fi
    sleep 1
  done
  echo "Timed out waiting for ${label}: ${url}" >&2
  return 1
}

host_start_wit_wat() {
  mkdir -p \
    "${WIT_CONFIG_DIR}/keri/cf/main" \
    "${WAT_CONFIG_DIR}/keri/cf/main" \
    /var/log/keri

  # shellcheck source=/dev/null
  source /entrypoint-syslog.sh

  cat > "${WIT_CONFIG_DIR}/keri/cf/main/witopnet.json" <<EOF
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "witopnet": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["${PUBLIC_WITNESS_URL}/"]
  }
}
EOF

  cat > "${WAT_CONFIG_DIR}/keri/cf/main/watopnet.json" <<EOF
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "watopnet": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["${PUBLIC_WATCHER_URL}/"]
  }
}
EOF

  echo "Starting witopnet on ${HOST_IP} (boot ${WIT_BOOT_PORT}, http ${WIT_HTTP_PORT}, public ${PUBLIC_WITNESS_URL})"
  witopnet marshal start \
    --config-dir "${WIT_CONFIG_DIR}" \
    --base witopnet \
    --host 0.0.0.0 \
    --http "${WIT_HTTP_PORT}" \
    --boothost "${HOST_IP}" \
    --bootport "${WIT_BOOT_PORT}" \
    --loglevel "${LOGLEVEL}" \
    --logfile /var/log/keri/witopnet.log &
  pids+=("$!")

  echo "Starting watopnet on ${HOST_IP} (boot ${WAT_BOOT_PORT}, http ${WAT_HTTP_PORT}, public ${PUBLIC_WATCHER_URL})"
  watopnet start \
    --config-dir "${WAT_CONFIG_DIR}" \
    --host 0.0.0.0 \
    --http "${WAT_HTTP_PORT}" \
    --boothost "${HOST_IP}" \
    --bootport "${WAT_BOOT_PORT}" \
    --loglevel "${LOGLEVEL}" \
    --logfile /var/log/keri/watopnet.log &
  pids+=("$!")

  host_wait_for_http "http://${HOST_IP}:${WIT_BOOT_PORT}/health" "witopnet boot"
}

host_supervise() {
  wait -n "${pids[@]}"
  exit $?
}
