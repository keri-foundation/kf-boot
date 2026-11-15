#!/usr/bin/env bash
# Restore a snapshot created by snapshot-state.sh.
#
# Usage:
#   ./kf-boot/docker/restore-state.sh                  # restores "latest"
#   ./kf-boot/docker/restore-state.sh latest
#   ./kf-boot/docker/restore-state.sh 20261106-001200-pre-ipex
#   ./kf-boot/docker/restore-state.sh --force 20261106-001200
#
# Stops the Docker stack, replaces kf-boot_* volume contents and host
# core-demo-* / locksmith vaults from the snapshot, then starts the stack.
# Does not run `docker compose down -v`.
set -euo pipefail

KF_BOOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${KF_BOOT}/docker-compose.yml"
COMPOSE=(docker compose -f "${COMPOSE_FILE}")
PROJECT_PREFIX="kf-boot_"
KERI_HOME="${KERI_HOME:-${HOME}/.keri}"
SNAPSHOTS_ROOT="${KF_BOOT}/var/snapshots"

FORCE=0
NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force|-f)
      FORCE=1
      shift
      ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      if [[ -n "${NAME}" ]]; then
        echo "error: unexpected arg: $1" >&2
        exit 2
      fi
      NAME="$1"
      shift
      ;;
  esac
done

NAME="${NAME:-latest}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: missing command: $1" >&2; exit 1; }; }
need docker
need tar

if ! docker info >/dev/null 2>&1; then
  echo "error: cannot talk to Docker daemon" >&2
  exit 1
fi

SRC="${SNAPSHOTS_ROOT}/${NAME}"
# Resolve symlink (latest -> timestamped dir)
if [[ -L "${SRC}" ]]; then
  SRC="$(cd "${SNAPSHOTS_ROOT}" && cd "$(readlink "${NAME}")" && pwd)"
elif [[ -d "${SRC}" ]]; then
  SRC="$(cd "${SRC}" && pwd)"
else
  echo "error: snapshot not found: ${SNAPSHOTS_ROOT}/${NAME}" >&2
  echo "Available:" >&2
  ls -1 "${SNAPSHOTS_ROOT}" 2>/dev/null | grep -v '^latest$' >&2 || echo "  (none)" >&2
  exit 1
fi

if [[ ! -f "${SRC}/MANIFEST.txt" ]] || [[ ! -d "${SRC}/volumes" ]]; then
  echo "error: incomplete snapshot at ${SRC}" >&2
  exit 1
fi

locksmith_running() {
  pgrep -fi 'locksmith|Locksmith' 2>/dev/null | grep -v 'snapshot-state\|restore-state' >/dev/null
}

if locksmith_running && [[ "${FORCE}" -ne 1 ]]; then
  echo "error: Locksmith appears to be running. Quit Utah / SEDI / Kerify, then re-run." >&2
  echo "  Or pass --force to restore anyway." >&2
  exit 1
fi

echo "==> Restoring from: ${SRC}"
echo "==> Stopping Docker stack (volumes kept)..."
"${COMPOSE[@]}" stop

# Ensure named volumes exist (create empty ones if a prior down -v wiped them)
echo "==> Ensuring compose volumes exist..."
"${COMPOSE[@]}" up -d --no-start >/dev/null

echo "==> Restoring Docker volumes..."
shopt -s nullglob
vol_tars=("${SRC}/volumes"/*.tar.gz)
shopt -u nullglob
if [[ ${#vol_tars[@]} -eq 0 ]]; then
  echo "error: no volume archives in ${SRC}/volumes" >&2
  exit 1
fi

for archive in "${vol_tars[@]}"; do
  base="$(basename "${archive}" .tar.gz)"
  vol="${PROJECT_PREFIX}${base}"
  if ! docker volume inspect "${vol}" >/dev/null 2>&1; then
    echo "    creating missing volume ${vol}"
    docker volume create "${vol}" >/dev/null
  fi
  echo "    ${archive##*/} -> ${vol}"
  # Wipe then extract so deleted LMDB pages do not linger.
  docker run --rm \
    -v "${vol}:/to" \
    -v "${SRC}/volumes:/from:ro" \
    alpine:3.20 \
    sh -c "find /to -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar -C /to -xzf /from/${base}.tar.gz"
done

echo "==> Restoring host KERI vaults under ${KERI_HOME}..."
mkdir -p "${KERI_HOME}"
VAULTS="${SRC}/keri/vaults.tar.gz"
PATHS_FILE="${SRC}/keri/paths.txt"
if [[ -f "${VAULTS}" ]] && [[ -s "${VAULTS}" ]] && [[ -f "${PATHS_FILE}" ]] && [[ -s "${PATHS_FILE}" ]]; then
  while IFS= read -r rel || [[ -n "${rel}" ]]; do
    [[ -z "${rel}" ]] && continue
    target="${KERI_HOME}/${rel}"
    if [[ -e "${target}" ]] || [[ -L "${target}" ]]; then
      echo "    replacing ${rel}"
      rm -rf "${target}"
    else
      echo "    placing  ${rel}"
    fi
    # Ensure parent exists before tar extract
    mkdir -p "$(dirname "${target}")"
  done < "${PATHS_FILE}"
  tar -C "${KERI_HOME}" -xzf "${VAULTS}"
else
  echo "    warning: no keri vault archive in snapshot; leaving ${KERI_HOME} unchanged"
fi

echo "==> Starting Docker stack..."
"${COMPOSE[@]}" start

echo
echo "Restored ${SRC}"
echo "Health: curl -sS http://127.0.0.1:9723/health && curl -sS http://127.0.0.1:6731/health"
