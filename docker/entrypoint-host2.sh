#!/usr/bin/env bash
# Host 2 (four-host layout): witopnet + watopnet + kf-registrar
set -euo pipefail

# shellcheck source=/dev/null
source /entrypoint-common.sh

trap host_cleanup EXIT INT TERM
host_start_wit_wat

PUBLIC_REGISTRAR_URL="${PUBLIC_REGISTRAR_URL:-http://127.0.0.1:6632}"
PUBLIC_REGISTRAR_URL="${PUBLIC_REGISTRAR_URL%/}"
REGISTRAR_HTTP_PORT="${REGISTRAR_HTTP_PORT:-6632}"
REGISTRAR_BOOT_PORT="${REGISTRAR_BOOT_PORT:-6631}"
SHARED_DIR="${SHARED_DIR:-/shared}"
CONFIG_DIR=/opt/keri/registrar/config
NAME="${REGISTRAR_NAME:-registrar}"
ALIAS="${REGISTRAR_ALIAS:-registrar}"
BASE="${REGISTRAR_BASE:-registrar}"
AID_FILE="${SHARED_DIR}/observer.aid"
OOBI_FILE="${SHARED_DIR}/observer.oobi"
PASSCODE="${KERI_PASSCODE:-DoB2-e4Rr-gVOr-Nb1Y-7yBl-gI3n-i4cB-gf07}"
PASSCODE="${PASSCODE//-/}"

mkdir -p "${CONFIG_DIR}/keri/cf" "${SHARED_DIR}" /var/log/keri
export HOME=/opt/keri/registrar
mkdir -p "${HOME}"

echo "Waiting for observer OOBI advertisement at ${OOBI_FILE}"
for _ in $(seq 1 120); do
  if [[ -s "${AID_FILE}" && -s "${OOBI_FILE}" ]]; then
    break
  fi
  sleep 1
done
if [[ ! -s "${AID_FILE}" || ! -s "${OOBI_FILE}" ]]; then
  echo "Timed out waiting for ${AID_FILE} and ${OOBI_FILE}" >&2
  exit 1
fi

KEL_FILE="${SHARED_DIR}/observer.kel"
echo "Waiting for observer KEL (shared file or HTTP OOBI)"
OBSERVER_AID=""
OBSERVER_OOBI=""
ready=0
for _ in $(seq 1 120); do
  OBSERVER_AID="$(tr -d '[:space:]' < "${AID_FILE}" 2>/dev/null || true)"
  OBSERVER_OOBI="$(tr -d '[:space:]' < "${OOBI_FILE}" 2>/dev/null || true)"
  if [[ -n "${OBSERVER_AID}" && -s "${KEL_FILE}" ]]; then
    # Preferred: host-3 writes CESR KEL (same flow as bare-metal guide).
    cp "${KEL_FILE}" /tmp/observer-oobi.cesr
    ready=1
    break
  fi
  if [[ -n "${OBSERVER_AID}" && -n "${OBSERVER_OOBI}" ]]; then
    # Fallback when observer serves GET /oobi/{aid}.
    code="$(curl -sS -m 10 -o /tmp/observer-oobi.cesr -w '%{http_code}' \
      -H 'Accept: application/cesr' "${OBSERVER_OOBI}" || true)"
    if [[ "${code}" == "200" && -s /tmp/observer-oobi.cesr ]]; then
      ready=1
      break
    fi
  fi
  sleep 1
done
if [[ "${ready}" -ne 1 ]]; then
  echo "Timed out waiting for observer KEL at ${KEL_FILE} or OOBI ${OBSERVER_OOBI}" >&2
  exit 1
fi
echo "Allow-listing observer ${OBSERVER_AID} (source ${KEL_FILE:-${OBSERVER_OOBI}})"

cat > "${CONFIG_DIR}/keri/cf/kf-registrar.json" <<EOF
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "kf-registrar": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["${PUBLIC_REGISTRAR_URL}/"],
    "observers": ["${OBSERVER_AID}"]
  },
  "kram": {
    "enabled": true,
    "denials": [],
    "caches": {
      "~": [1000, 5000, 60000, 300000, 5000, 60000, 300000]
    }
  }
}
EOF

echo "Starting kf-registrar on ${HOST_IP} (boot ${REGISTRAR_BOOT_PORT}, http ${REGISTRAR_HTTP_PORT}, public ${PUBLIC_REGISTRAR_URL})"
kf-registrar start \
  --name "${NAME}" \
  --alias "${ALIAS}" \
  --base "${BASE}" \
  --config-dir "${CONFIG_DIR}" \
  --config-file kf-registrar \
  --host 0.0.0.0 \
  --http "${REGISTRAR_HTTP_PORT}" \
  --boothost 0.0.0.0 \
  --bootport "${REGISTRAR_BOOT_PORT}" \
  --observer "${OBSERVER_AID}" \
  --passcode "${PASSCODE}" \
  --no-prompt \
  --loglevel "${LOGLEVEL}" &
pids+=("$!")

host_wait_for_http "http://127.0.0.1:${REGISTRAR_HTTP_PORT}/health" "kf-registrar"

echo "Introducing observer KEL via POST /introductions"
intro_ok=0
for _ in $(seq 1 30); do
  code="$(curl -sS -m 10 -o /tmp/intro.out -w '%{http_code}' \
    -H 'Content-Type: application/cesr' \
    --data-binary @/tmp/observer-oobi.cesr \
    "http://127.0.0.1:${REGISTRAR_HTTP_PORT}/introductions" || true)"
  if [[ "${code}" == "200" || "${code}" == "204" ]]; then
    intro_ok=1
    break
  fi
  sleep 1
done
if [[ "${intro_ok}" -ne 1 ]]; then
  echo "Failed to introduce observer KEL (last HTTP ${code:-none})" >&2
  cat /tmp/intro.out 2>/dev/null || true
  exit 1
fi
echo "Observer KEL introduced"

host_supervise
