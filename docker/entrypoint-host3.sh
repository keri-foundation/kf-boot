#!/usr/bin/env bash
# Host 3 (four-host layout): witopnet + watopnet + kf-observer
set -euo pipefail

# shellcheck source=/dev/null
source /entrypoint-common.sh

trap host_cleanup EXIT INT TERM
host_start_wit_wat

OBSERVER_HTTP_PORT="${OBSERVER_HTTP_PORT:-6633}"
PUBLIC_OBSERVER_URL="${PUBLIC_OBSERVER_URL:-http://127.0.0.1:6633}"
PUBLIC_OBSERVER_URL="${PUBLIC_OBSERVER_URL%/}"
INTERNAL_OBSERVER_URL="${INTERNAL_OBSERVER_URL:-http://${HOST_IP}:${OBSERVER_HTTP_PORT}}"
INTERNAL_OBSERVER_URL="${INTERNAL_OBSERVER_URL%/}"
# Registrar lives on host-2 (same co-location as full_setup_guide.md).
REGISTRAR_URL="${REGISTRAR_URL:-http://192.0.2.12:6632/}"
WITNESS_URL="${WITNESS_URL:-http://192.0.2.11:5632/}"
SHARED_DIR="${SHARED_DIR:-/shared}"
CONFIG_DIR=/opt/keri/observer/config
NAME="${OBSERVER_NAME:-observer}"
ALIAS="${OBSERVER_ALIAS:-observer}"
BASE="${OBSERVER_BASE:-observer}"
PASSCODE="${KERI_PASSCODE:-DoB2-e4Rr-gVOr-Nb1Y-7yBl-gI3n-i4cB-gf07}"
PASSCODE="${PASSCODE//-/}"

mkdir -p "${CONFIG_DIR}/keri/cf" "${SHARED_DIR}" /var/log/keri
export HOME=/opt/keri/observer
mkdir -p "${HOME}"

cat > "${CONFIG_DIR}/keri/cf/kf-observer.json" <<EOF
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "kf-observer": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["${PUBLIC_OBSERVER_URL}/"],
    "registrars": ["${REGISTRAR_URL}"],
    "witnesses": ["${WITNESS_URL}"]
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

AID_FILE="${SHARED_DIR}/observer.aid"
OOBI_FILE="${SHARED_DIR}/observer.oobi"
KEL_FILE="${SHARED_DIR}/observer.kel"
echo "Ensuring observer habitat and publishing OOBI URL + KEL to ${SHARED_DIR}"
python - <<PY
from keri.app import Configer, Habery
from keri.kering import Vrsn_2_0

cf = Configer(
    name="kf-observer",
    headDirPath="${CONFIG_DIR}",
    temp=False,
    reopen=True,
    clear=False,
)
hby = Habery(name="${NAME}", base="${BASE}", bran="${PASSCODE}", cf=cf, version=Vrsn_2_0)
hab = hby.habByName("${ALIAS}")
if hab is None:
    hab = hby.makeHab(name="${ALIAS}", transferable=True)
aid = hab.pre
# Share KEL CESR for registrar introductions (matches bare-metal AID+KEL copy).
kel = b"".join(hby.db.clonePreIter(pre=aid, gvrsn=Vrsn_2_0))
hby.close()
oobi = f"${INTERNAL_OBSERVER_URL}/oobi/{aid}"
with open("${AID_FILE}", "w", encoding="utf-8") as fh:
    fh.write(aid)
with open("${OOBI_FILE}", "w", encoding="utf-8") as fh:
    fh.write(oobi)
with open("${KEL_FILE}", "wb") as fh:
    fh.write(kel)
print(f"Observer AID {aid}")
print(f"Observer OOBI {oobi}")
print(f"Observer KEL bytes {len(kel)}")
PY

echo "Starting kf-observer on ${HOST_IP}:${OBSERVER_HTTP_PORT} (public ${PUBLIC_OBSERVER_URL})"
kf-observer start \
  --name "${NAME}" \
  --alias "${ALIAS}" \
  --base "${BASE}" \
  --config-dir "${CONFIG_DIR}" \
  --config-file kf-observer \
  --host 0.0.0.0 \
  --http "${OBSERVER_HTTP_PORT}" \
  --registrar "${REGISTRAR_URL}" \
  --witness "${WITNESS_URL}" \
  --passcode "${PASSCODE}" \
  --no-prompt \
  --loglevel "${LOGLEVEL}" &
pids+=("$!")

host_supervise
