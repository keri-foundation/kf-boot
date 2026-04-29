#!/usr/bin/env sh
set -eu

mkdir -p "${KF_BOOT_DB_PATH:-/data/var}" "${KF_BOOT_KERI_DIR:-/data/keri}"
export KERI_BASER_MAP_SIZE="${KERI_BASER_MAP_SIZE:-1099511627776}"

exec kf-boot
