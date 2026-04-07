import pytest
from falcon import testing
import json

from keri.core import Prefixer, Verfer, Siger, Salter, Signer, MtrDex

from kfboot.app import create_app   

@pytest.fixture
def client():
    app, ctx = create_app()          # unpack the tuple
    client = testing.TestClient(app) # Falcon only wants the app
    client.ctx = ctx                 # attach context for tests
    return client


def test_session_upgrade_success_deterministic(client):
    # Step 1: Create a session
    resp = client.simulate_post("/session")
    assert resp.status_code == 201

    data = resp.json
    session_id = data["session_id"]

    # Step 2: Override challenge with deterministic test values
    challenge = "CHALLENGE-1234567890"

    record = client.ctx.store.get_session(session_id)
    record.challenge = challenge
    client.ctx.store.update_session(record)

    # Step 3: Deterministic permanent AID + valid KERI signature
    seed = bytes.fromhex(
    "000102030405060708090a0b0c0d0e0f"
    "101112131415161718191a1b1c1d1e1f"
    )

    signer = Signer(raw=seed, code=MtrDex.Ed25519_Seed)
    verfer = signer.verfer

    prefixer = Prefixer(raw=verfer.raw, code=verfer.code)
    cid = prefixer.qb64

    cigar = signer.sign(record.challenge.encode())
    sig_raw = cigar.raw 
    siger = Siger(raw=sig_raw, code=verfer.code, index=0)

    # Step 4: Send upgrade request
    resp2 = client.simulate_post(
        f"/session/{session_id}/upgrade",
        json={"cid": cid, "sig": siger.qb64},
    )

    # Step 5: Validate success
    assert resp2.status_code == 200
    body = resp2.json

    assert body["status"] == "upgraded"
    assert body["principal"] == cid

    # Clean up for the next test
    client.ctx.store.close()


def test_session_upgrade_success(client):
    # Step 1: Create a session
    resp = client.simulate_post("/session")
    assert resp.status_code == 201

    # Server responds with the session ID and the challenge to be signed
    data = resp.json

    assert data["status"] == "pending"
    
    session_id = data["session_id"]
    challenge = data["challenge"]   # <-- use the server-generated challenge

    # Step 2: Random permanent AID 
    signer = Signer()
    verfer = signer.verfer

    # Build permanent AID prefix
    prefixer = Prefixer(raw=verfer.raw, code=verfer.code)
    cid = prefixer.qb64

    # Step 3: Sign the REAL challenge returned by the server
    cigar = signer.sign(challenge.encode())
    sig_raw = cigar.raw

    # Wrap in proper KERI Siger
    siger = Siger(raw=sig_raw, code=verfer.code, index=0)
    sig = siger.qb64

    # Step 4: Send upgrade request
    resp2 = client.simulate_post(
        f"/session/{session_id}/upgrade",
        json={"cid": cid, "sig": sig},
    )

    # Step 5: Validate success
    assert resp2.status_code == 200
    body = resp2.json

    assert body["status"] == "upgraded"
    assert body["principal"] == cid

    # Step 6: Assert session was deleted after upgrade
    resp = client.simulate_get(f"/session/{session_id}")
    assert resp.status_code == 404 # Session not found after upgrade

    # Clean up for the next test
    client.ctx.store.close()
