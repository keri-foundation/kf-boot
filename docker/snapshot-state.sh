#!/usr/bin/env bash
# Snapshot Docker infra volumes + host Locksmith core-demo vaults.
#
# Usage (from anywhere):
#   ./kf-boot/docker/snapshot-state.sh
#   ./kf-boot/docker/snapshot-state.sh --label pre-ipex
#   ./kf-boot/docker/snapshot-state.sh --force   # skip Locksmith-running check
#
# Writes: kf-boot/var/snapshots/<timestamp>[-label]/
# Does not use `docker compose down -v` (volumes are preserved).
set -euo pipefail

KF_BOOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${KF_BOOT}/docker-compose.yml"
COMPOSE=(docker compose -f "${COMPOSE_FILE}")
PROJECT_PREFIX="kf-boot_"
KERI_HOME="${KERI_HOME:-${HOME}/.keri}"
SNAPSHOTS_ROOT="${KF_BOOT}/var/snapshots"

LABEL=""
FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)
      LABEL="${2:-}"
      [[ -n "${LABEL}" ]] || { echo "error: --label needs a value" >&2; exit 2; }
      shift 2
      ;;
    --force|-f)
      FORCE=1
      shift
      ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "error: unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: missing command: $1" >&2; exit 1; }; }
need docker
need tar
need python3

if ! docker info >/dev/null 2>&1; then
  echo "error: cannot talk to Docker daemon" >&2
  exit 1
fi

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "error: compose file not found: ${COMPOSE_FILE}" >&2
  exit 1
fi

locksmith_running() {
  # Match common macOS / python Locksmith process names without false positives on this script.
  pgrep -fi 'locksmith|Locksmith' 2>/dev/null | grep -v 'snapshot-state\|restore-state' >/dev/null
}

if locksmith_running && [[ "${FORCE}" -ne 1 ]]; then
  echo "error: Locksmith appears to be running (LMDB may be mid-write)." >&2
  echo "  Quit Utah / SEDI / Kerify Locksmith apps, then re-run." >&2
  echo "  Or pass --force to snapshot anyway." >&2
  exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="${STAMP}"
if [[ -n "${LABEL}" ]]; then
  SAFE_LABEL="$(printf '%s' "${LABEL}" | tr -cs 'A-Za-z0-9._-' '-')"
  NAME="${STAMP}-${SAFE_LABEL}"
fi
DEST="${SNAPSHOTS_ROOT}/${NAME}"
mkdir -p "${DEST}/volumes" "${DEST}/keri"

WAS_RUNNING=0
if "${COMPOSE[@]}" ps -q 2>/dev/null | grep -q .; then
  WAS_RUNNING=1
fi

echo "==> Snapshot destination: ${DEST}"
echo "==> Stopping Docker stack (volumes kept)..."
"${COMPOSE[@]}" stop

VOLUMES=()
while IFS= read -r vol; do
  [[ -n "${vol}" ]] && VOLUMES+=("${vol}")
done < <(docker volume ls --format '{{.Name}}' | grep "^${PROJECT_PREFIX}" | sort || true)

if [[ ${#VOLUMES[@]} -eq 0 ]]; then
  echo "error: no Docker volumes matching ${PROJECT_PREFIX}*" >&2
  if [[ "${WAS_RUNNING}" -eq 1 ]]; then
    echo "==> Restarting Docker stack after failure..."
    "${COMPOSE[@]}" start || true
  fi
  exit 1
fi

echo "==> Archiving ${#VOLUMES[@]} Docker volumes..."
for vol in "${VOLUMES[@]}"; do
  short="${vol#${PROJECT_PREFIX}}"
  echo "    ${vol} -> volumes/${short}.tar.gz"
  docker run --rm \
    -v "${vol}:/from:ro" \
    -v "${DEST}/volumes:/to" \
    alpine:3.20 \
    tar -C /from -czf "/to/${short}.tar.gz" .
done

echo "==> Archiving host KERI vaults under ${KERI_HOME} (core-demo-* + locksmith)..."
# Preserve relative paths under ~/.keri so restore can drop them back in place.
python3 - "${KERI_HOME}" "${DEST}/keri/paths.txt" <<'PY'
import os, sys
from pathlib import Path
home = Path(sys.argv[1])
out = Path(sys.argv[2])
paths = []
if not home.is_dir():
    out.write_text("")
    raise SystemExit(0)
for p in home.rglob("*"):
    if not p.is_dir():
        continue
    name = p.name
    # Habery / vault dirs: .../core-demo-issuer, .../core-demo-holder, ...
    if name.startswith("core-demo-"):
        paths.append(p.relative_to(home).as_posix())
# Locksmith OTP / app secrets (shared across wallets)
lock = home / "locksmith"
if lock.is_dir():
    paths.append("locksmith")
# Stable, unique, depth-first-ish for tar
paths = sorted(set(paths), key=lambda s: (s.count("/"), s))
# Drop children already covered by a parent we archive (e.g. locksmith/*)
pruned = []
for p in paths:
    if any(p == parent or p.startswith(parent + "/") for parent in pruned):
        continue
    pruned.append(p)
out.write_text("\n".join(pruned) + ("\n" if pruned else ""))
for p in pruned:
    print(p)
PY

PATHS_FILE="${DEST}/keri/paths.txt"
if [[ -s "${PATHS_FILE}" ]]; then
  # shellcheck disable=SC2046
  tar -C "${KERI_HOME}" -czf "${DEST}/keri/vaults.tar.gz" $(tr '\n' ' ' < "${PATHS_FILE}")
  echo "    wrote keri/vaults.tar.gz ($(wc -l < "${PATHS_FILE}" | tr -d ' ') top-level paths)"
else
  echo "    warning: no core-demo-* or locksmith dirs found under ${KERI_HOME}"
  : > "${DEST}/keri/vaults.tar.gz"
fi

{
  echo "name=${NAME}"
  echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "compose_file=${COMPOSE_FILE}"
  echo "project_prefix=${PROJECT_PREFIX}"
  echo "keri_home=${KERI_HOME}"
  echo "was_running=${WAS_RUNNING}"
  echo "volume_count=${#VOLUMES[@]}"
  echo "volumes:"
  for vol in "${VOLUMES[@]}"; do
    echo "  - ${vol}"
  done
  echo "keri_paths:"
  if [[ -s "${PATHS_FILE}" ]]; then
    while IFS= read -r p; do
      echo "  - ${p}"
    done < "${PATHS_FILE}"
  fi
} > "${DEST}/MANIFEST.txt"

# Convenience pointer to latest successful snapshot
ln -sfn "${NAME}" "${SNAPSHOTS_ROOT}/latest"

if [[ "${WAS_RUNNING}" -eq 1 ]]; then
  echo "==> Restarting Docker stack..."
  "${COMPOSE[@]}" start
else
  echo "==> Stack was stopped before snapshot; leaving it stopped."
fi

echo
echo "Snapshot ready: ${DEST}"
echo "Restore with:   ${KF_BOOT}/docker/restore-state.sh ${NAME}"
echo "           or:  ${KF_BOOT}/docker/restore-state.sh latest"
