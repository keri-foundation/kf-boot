import pytest
from falcon import testing
import json

from keri.core import Prefixer, Verfer, Siger, Salter, Signer, MtrDex, coring
from keri.end import ending

from kfboot.app import create_app   

@pytest.fixture
def client():
    """
    Create a Falcon test client backed by a temporary bootserver context.

    This fixture:
      - Instantiates the application with temp=True (isolated LMDB + store)
      - Wraps the Falcon app in a TestClient
      - Exposes the app context via client.ctx for test access
      - Ensures the habery and store are closed after the test
    """
    app, ctx = create_app(temp=True)          # unpack the tuple
    client = testing.TestClient(app) # Falcon only wants the app
    client.ctx = ctx                 # attach context for tests
    yield client
    ctx.habery.close(clear=True)
    ctx.store.close()

def make_signature_header_from_sigers(sigers):
    """
    Construct a CESR-compliant HTTP Signature header from one or more Siger objects.

    The function:
      - Assigns numeric markers ("0", "1", ...) to each Siger
      - Wraps them in a Signage structure
      - Serializes the signage into a `Signature:` header value

    Args:
        sigers (Iterable[Siger]): One or more indexed signatures.

    Returns:
        dict: A mapping containing the `Signature` header.
    """
    markers = {str(i): siger for i, siger in enumerate(sigers)}

    signage = ending.Signage(
        markers=markers,
        indexed=True,
        signer=None,
        ordinal=None,
        digest=None,
        kind=None,
    )

    return ending.signature([signage])


def test_session_upgrade_success(client):
    """
    End‑to‑end test of the bootserver session upgrade protocol.

    This test verifies the full cryptographic handshake:

    1. Client creates an ephemeral AID.
    2. Client SAID‑ifies and signs the session creation request.
    3. Server verifies the signature and returns a signed session record.
    4. Test recomputes the SAID of the response and verifies the server signature.
    5. Client generates a permanent AID.
    6. Client SAID‑ifies and signs the upgrade request (binding cid + session_id).
    7. Server verifies the permanent AID signature and upgrades the session.
    8. Test recomputes the SAID of the upgrade response and verifies the server signature.

    This ensures:
      - Request integrity (SAID correctness)
      - Signature correctness (ephemeral + permanent)
      - Server authenticity (hostHab signature)
      - Session binding correctness (session_id included in signed material)
    """
    
    # Create ephemeral AID
    ephSigner = Signer()                 # ephemeral keypair
    ephVerfer = ephSigner.verfer
    ephPrefix = Prefixer(raw=ephVerfer.raw, code=ephVerfer.code).qb64
    
    # Build body
    body = {
        "i": ephPrefix,
        "ts": "2026-04-08T12:00:00Z",
        "d": ""                           # placeholder for SAID
    }

    # Saidify body inside d
    saider, body  = coring.Saider.saidify(sad=body, label="d")
    said = saider.qb64
    
    # Sign SAID with ephemeral key
    sigers = ephSigner.sign(ser=said.encode("utf-8"), index=0)
    header = make_signature_header_from_sigers([sigers])

    # Send the POST request, first contact
    resp = client.simulate_post("/session", json=body, headers={"Signature": header["Signature"]})
    assert resp.status_code == 201

    # Assert server response and signature
    data = resp.json

    # Extract signature header
    sig_header = resp.headers.get("Signature")
    assert sig_header is not None

    # Parse Siger from header
    signages = ending.designature(sig_header)
    signage = signages[0]
    server_siger = list(signage.markers.values())[0]

    # Recompute SAID of the response body
    resp_saider, _ = coring.Saider.saidify(sad=dict(data), label="d")
    resp_said = resp_saider.qb64

    # Get server's public key
    server_verfer = client.ctx.hostHab.kever.verfers[0]

    # Verify signature
    assert server_verfer.verify(server_siger.raw, resp_said.encode("utf-8"))

    # Assert status
    assert data["status"] == "pending"
    
    # Get the session ID from the server response
    sessionId = data["session_id"]

    # Create a Random permanent AID 
    permSigner = Signer()
    permVerfer = permSigner.verfer

    # Build permanent AID prefix
    permPrefixer = Prefixer(raw=permVerfer.raw, code=permVerfer.code)
    cid = permPrefixer.qb64
    
    # Build + SAID-ify /upgrade body
    upgradeBody = {
        "cid": cid,
        "session_id": sessionId,
        "d": ""
    }

    # Saidify the body and put it in label d
    upgradeSaider, upgradeBody = coring.Saider.saidify(sad=upgradeBody, label="d")
    upgradeSaid = upgradeSaider.qb64

    # Sign SAID with permanent key
    permSiger = permSigner.sign(ser=upgradeSaid.encode("utf-8"), index=0)
    permHeader = make_signature_header_from_sigers([permSiger])

    # Send upgrade request with the signature header and permanent AID
    resp2 = client.simulate_post(
        f"/session/upgrade",
        json=upgradeBody,
        headers={"Signature": permHeader["Signature"]},
    )

    # Assert Validate success
    assert resp2.status_code == 200

    # Assert server response and signature
    data = resp2.json

    # Extract signature header
    sig_header = resp2.headers.get("Signature")
    assert sig_header is not None

    # Parse Siger from header
    signages = ending.designature(sig_header)
    signage = signages[0]
    server_siger = list(signage.markers.values())[0]

    # Recompute SAID of the response body
    resp_saider, _ = coring.Saider.saidify(sad=dict(data), label="d")
    resp_said = resp_saider.qb64

    # Get server's public key
    server_verfer = client.ctx.hostHab.kever.verfers[0]

    # Verify signature
    assert server_verfer.verify(server_siger.raw, resp_said.encode("utf-8"))

    assert data["status"] == "upgraded"
    assert data["principal"] == cid