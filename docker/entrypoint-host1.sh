#!/usr/bin/env bash
# Host 1 (four-host layout): kf-boot + witopnet + watopnet
set -euo pipefail

# shellcheck source=/dev/null
source /entrypoint-common.sh

trap host_cleanup EXIT INT TERM
host_start_wit_wat
host_wait_for_http "http://${HOST_IP}:${WAT_BOOT_PORT}/watchers" "watopnet boot"

export KF_BOOT_HOST="${KF_BOOT_HOST:-0.0.0.0}"
export KF_BOOT_PORT="${KF_BOOT_PORT:-9723}"
export KF_BOOT_DB_PATH="${KF_BOOT_DB_PATH:-/opt/keri/kfboot/var}"
export KF_BOOT_KERI_DIR="${KF_BOOT_KERI_DIR:-/opt/keri/kfboot/keri}"
# Mailbox lives on host-4; compose sets KF_BOOT_MBX_BOOT_URL to that boot plane.
export KF_BOOT_MBX_BOOT_URL="${KF_BOOT_MBX_BOOT_URL:-http://192.0.2.14:6731}"

: "${KF_BOOT_WITNESS_BACKENDS:?KF_BOOT_WITNESS_BACKENDS is required}"
: "${KF_BOOT_WAT_BOOT_URL:?KF_BOOT_WAT_BOOT_URL is required}"
: "${KF_BOOT_WAT_PUBLIC_URL:?KF_BOOT_WAT_PUBLIC_URL is required}"

mkdir -p "${KF_BOOT_DB_PATH}" "${KF_BOOT_KERI_DIR}"

echo "Starting kf-boot on ${KF_BOOT_HOST}:${KF_BOOT_PORT} (mailbox ${KF_BOOT_MBX_BOOT_URL})"
kf-boot &
pids+=("$!")

host_supervise
