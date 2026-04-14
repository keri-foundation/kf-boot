import pytest
from falcon import testing
import json

from keri.core import Prefixer, Verfer, Siger, Salter, Signer, MtrDex, coring, eventing, Codens
from keri.core.coring import Seqner, Saider, Pather
from keri.kering import Vrsn_1_0
from keri.end import ending
from keri.app.habbing import Habery
from keri.core.eventing import SealEvent
from keri.help import helping
from keri.peer import exchanging
from keri.core.counting import Counter, CtrDex_1_0
from keri.core.serdering import SerderKERI

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

def sign_exn(hab, serder) -> bytearray:
    """Sign an EXN message with a Hab and return a mutable CESR bytearray."""
    sigs = hab.sign(ser=serder.raw, indexed=True)
    msg = bytearray(serder.raw)
    for sig in sigs:
        msg.extend(sig.qb64b)
    return msg



def build_exn_with_cigar(hab, serder):
    # Create a non-transferable signature (cigar)
    cigar = hab.sign(ser=serder.raw, indexed=False)[0]   # Cigar
    verfer = Verfer(qb64=hab.pre)                        # Public key

    msg = bytearray(serder.raw)

    # Add NonTransReceiptCouples counter
    msg.extend(
        Counter(
            CtrDex_1_0.NonTransReceiptCouples,
            count=1,
            version=Vrsn_1_0,
        ).qb64b
    )

    # Add verfer + cigar
    msg.extend(verfer.qb64b)
    msg.extend(cigar.qb64b)

    return bytes(msg)


def build_exn_with_tsgs(hab, serder, end: bytes) -> bytes:
    """
    Build a fully valid EXN with transferable signature groups (tsgs)
    for a transferable AID (permanent account AID).

    - Uses TransIdxSigGroups for tsgs
    - Includes prefixer, seqner, saider, and ControllerIdxSigs + sigers
    """
    end = end or b""
    # Sign EXN body with indexed signatures
    sigers = hab.sign(ser=serder.raw, indexed=True)

    # Signer identity (transferable AID)
    prefixer = Prefixer(qb64=hab.pre)
    seqner = Seqner(sn=hab.kever.sn)                 # current est event sequence
    saider = Saider(qb64=hab.kever.serder.said)      # current est event digest

    msg = bytearray(serder.raw)

    # Add embedded attachments from exchanging.exchange (end)
    msg.extend(end)

    # TransIdxSigGroups (one group)
    msg.extend(
        Counter(
            CtrDex_1_0.TransIdxSigGroups,
            count=1,
            version=Vrsn_1_0,
        ).qb64b
    )

    # prefixer + seqner + saider for this tsg
    msg.extend(prefixer.qb64b)
    msg.extend(seqner.qb64b)
    msg.extend(saider.qb64b)

    # ControllerIdxSigs
    msg.extend(
        Counter(
            CtrDex_1_0.ControllerIdxSigs,
            count=len(sigers),
            version=Vrsn_1_0,
        ).qb64b
    )

    # signatures
    for siger in sigers:
        msg.extend(siger.qb64b)

    return bytes(msg)

def post_cesr(client, msg: bytes):
    return client.simulate_post(
        "/onboarding",
        body=msg,
        headers={"Content-Type": "application/cesr"},
    )


def test_onboarding_end_accepts_inception(client):
    """
    Verify that OnboardingEnd:
      - accepts a valid KERI inception event
      - parses it without raising
      - returns HTTP 200
      - returns no EXN reply (correct for inception)
    """

    # 1. Create ephemeral AID
    eph_hby = Habery(name="eph", temp=True)
    eph = eph_hby.makeHab(name="eph")

    # 2. Fully framed, signed inception event
    icp_msg = eph.makeOwnInception()

    # 3. POST to /onboarding
    resp = client.simulate_post(
        "/onboarding",
        body=icp_msg,
        headers={"Content-Type": "application/cesr"},
    )

    # 4. Endpoint must accept inception events
    assert resp.status_code == 200

    # 5. Inception events do NOT produce EXN replies
    assert resp.content == b""

def test_onboarding_end_invalid_cesr(client):
    """
    OnboardingEnd does not reject malformed CESR messages with HTTP 400.
    """

    # Not CESR at all
    bad = b"A"

    resp = client.simulate_post(
        "/onboarding",
        body=bad,
        headers=[("Content-Type", "application/cesr")],
    )

    assert resp.status_code == 400
    assert b"Invalid CESR message" in resp.content


def test_onboarding_end_rejects_empty_body(client):
    """
    OnboardingEnd must reject empty POST bodies.
    """

    resp = client.simulate_post(
        "/onboarding",
        body=b"",
        headers={"Content-Type": "application/cesr"},
    )

    assert resp.status_code == 400
    assert b"Missing CESR message" in resp.content


def test_onboarding_end_inception_produces_no_cues(client):
    """
    After posting an inception event, the exchanger must have no cues.
    """

    eph_hby = Habery(name="eph", temp=True)
    eph = eph_hby.makeHab(name="eph")

    icp_msg = eph.makeOwnInception()

    resp = client.simulate_post(
        "/onboarding",
        body=icp_msg,
        headers={"Content-Type": "application/cesr"},
    )

    assert resp.status_code == 200
    assert resp.content == b""

    # The BootExchanger must not have produced any cues
    assert not client.ctx.exchanger.cues


def test_full_onboarding_flow(client):
    """
    End‑to‑end test of the complete onboarding protocol.

    This test exercises the full EXN‑driven onboarding flow between a client
    (ephemeral and permanent AIDs) and the boot server. It verifies that:

    1. An ephemeral non‑transferable AID can incept and be accepted by the
       /onboarding endpoint without producing any EXN reply.

    2. The client can initiate a new onboarding session via
       /onboarding/session/start, and the boot server returns a valid EXN
       reply containing:
           - a newly allocated session_id
           - witness pool allocation
           - watcher allocation
           - updated session state

    3. The client can query the session state via
       /onboarding/session/status and receive a valid EXN reply reflecting
       the server’s stored session state.

    4. A permanent (transferable) AID can incept on the boot server and then
       authenticate an EXN request to /onboarding/account/create using a
       fully‑formed transferable signature group (tsgs). The server must:
           - authenticate the EXN
           - update the session with the permanent account AID
           - return a non‑empty EXN reply

    5. The permanent AID can complete onboarding via
       /onboarding/complete, again authenticated with tsgs, and the server
       must:
           - validate that the permanent AID matches the session principal
           - validate that witness and watcher resources exist
           - transition the session to "completed"
           - return a non‑empty EXN reply

    The test asserts that each EXN‑based step returns HTTP 200 and that all
    EXN‑producing endpoints return a non‑empty CESR payload, confirming that
    the BootExchanger dispatched the EXN to the correct handler and produced
    a reply cue.
    """
    eph_hby = Habery(name="eph", temp=True)
    eph = eph_hby.makeHab(name="eph", transferable=False)

    # 1. Inception
    icp = eph.makeOwnInception()
    resp = post_cesr(client, icp)
    assert resp.status_code == 200
    assert resp.content == b""

    # 2. Session start
    serder_start, atc_start = exchanging.exchange(
        route="/onboarding/session/start",
        payload={},
        sender=eph.pre,
    )

    msg_start = build_exn_with_cigar(eph, serder_start)
    resp = post_cesr(client, msg_start)
    assert resp.status_code == 200
    assert resp.content != b""

    # Assert reply
    reply = SerderKERI(raw=resp.content)
    assert reply.ked["t"] == "exn"
    assert reply.ked["rp"] == eph.pre
    assert reply.ked["r"] == "/onboarding/session/start"
    assert reply.ked["t"] == "exn"
    assert reply.ked["i"] == client.ctx.hostHab.pre
    
    payload = reply.ked["a"]
    assert payload["i"] == eph.pre                  # Echoes ephemeral AID
    assert payload["session_id"].startswith("sess_")
    assert payload["state"] == "witness_pool_allocated"
    
    # Get session ID
    session_id = payload["session_id"]

    # 3. Session status
    serder_status, atc_status = exchanging.exchange(
        route="/onboarding/session/status",
        payload={"session_id": session_id},
        sender=eph.pre,
    )
    msg_status = build_exn_with_cigar(eph, serder_status)
    resp = post_cesr(client, msg_status)
    assert resp.status_code == 200
    assert resp.content != b""

    reply = SerderKERI(raw=resp.content)
    ked = reply.ked
    payload = ked["a"]

    # Top-level EXN correctness
    assert ked["t"] == "exn"
    assert ked["r"] == "/onboarding/session/status" # Assert route
    assert ked["rp"] == eph.pre                     # reply addressed to ephemeral AID
    assert ked["i"] == client.ctx.hostHab.pre       # signed by boot server
    assert ked["q"] == {}
    assert ked["e"] == {}

    #  Payload correctness 
    assert payload["session_id"] == session_id
    assert payload["state"] == "witness_pool_allocated"

    # 4. Account create (permanent AID)
    perm_hby = Habery(name="perm", temp=True)
    perm = perm_hby.makeHab(name="perm")

    # Incept perm on the boot server
    perm_icp = perm.makeOwnInception()
    resp = post_cesr(client, perm_icp)
    assert resp.status_code == 200
    serder_create, atc_create = exchanging.exchange(
        route="/onboarding/account/create",
        payload={"session_id": session_id},
        sender=perm.pre,
    )
    msg_create = build_exn_with_tsgs(perm, serder_create, atc_create)
    resp = post_cesr(client, msg_create)
    assert resp.status_code == 200
    assert resp.content != b""

    reply = SerderKERI(raw=resp.content)
    ked = reply.ked
    payload = ked["a"]

    # Top-level EXN correctness 
    assert ked["t"] == "exn"
    assert ked["r"] == "/onboarding/account/create"
    assert ked["rp"] == perm.pre                    # reply addressed to permanent AID
    assert ked["i"] == client.ctx.hostHab.pre       # signed by boot server
    assert isinstance(ked["d"], str) and len(ked["d"]) > 20

    # Payload correctness 
    assert payload["session_id"] == session_id
    assert payload["principal"] == perm.pre         # permanent AID is now principal
    assert payload["state"] == "account_created"

    # Server-side session state 
    session = client.ctx.store.get_session(session_id)
    assert session.account_aid == perm.pre
    assert session.state == "account_created"

    # 5. Complete
    serder_complete, atc_complete = exchanging.exchange(
        route="/onboarding/complete",
        payload={"session_id": session_id},
        sender=perm.pre,
    )
    msg_complete = build_exn_with_tsgs(perm, serder_complete, atc_complete)
    resp = post_cesr(client, msg_complete)
    assert resp.status_code == 200
    assert resp.content != b""
    reply = SerderKERI(raw=resp.content)
    ked = reply.ked
    payload = ked["a"]

    # Top-level EXN correctness 
    assert ked["t"] == "exn"
    assert ked["r"] == "/onboarding/complete"
    assert ked["rp"] == perm.pre                    # reply addressed to permanent AID
    assert ked["i"] == client.ctx.hostHab.pre       # signed by boot server
    assert isinstance(ked["d"], str) and len(ked["d"]) > 20

    # Payload correctness
    assert payload["session_id"] == session_id
    assert payload["principal"] == perm.pre
    assert payload["state"] == "completed"

    # Server-side session state
    session = client.ctx.store.get_session(session_id)
    assert session.state == "completed"
