#!/usr/bin/env bash
# Host 4 (four-host layout): witopnet + watopnet + kf-mailbox
set -euo pipefail

# shellcheck source=/dev/null
source /entrypoint-common.sh

trap host_cleanup EXIT INT TERM
host_start_wit_wat

MBX_BOOT_PORT="${MBX_BOOT_PORT:-6731}"
MBX_HTTP_PORT="${MBX_HTTP_PORT:-6732}"
PUBLIC_MAILBOX_URL="${PUBLIC_MAILBOX_URL:-http://127.0.0.1:${MBX_HTTP_PORT}}"
PUBLIC_MAILBOX_URL="${PUBLIC_MAILBOX_URL%/}"
MBX_BASE="${MBX_BASE:-mailbox}"

mkdir -p /opt/keri/mailbox /var/log/keri
export HOME=/opt/keri/mailbox

echo "Starting kf-mailbox on ${HOST_IP} (boot ${MBX_BOOT_PORT}, public ${PUBLIC_MAILBOX_URL})"
kf-mailbox serve \
  --boot-host 0.0.0.0 \
  --boot-port "${MBX_BOOT_PORT}" \
  --host 0.0.0.0 \
  --port "${MBX_HTTP_PORT}" \
  --base "${MBX_BASE}" \
  --public-url "${PUBLIC_MAILBOX_URL}" &
pids+=("$!")

host_wait_for_http "http://127.0.0.1:${MBX_BOOT_PORT}/health" "kf-mailbox boot"
host_supervise
