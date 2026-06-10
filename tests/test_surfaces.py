from __future__ import annotations

import pytest
from keri.app import habbing
from keri.app.httping import CESR_ATTACHMENT_HEADER, CESR_CONTENT_TYPE
from keri.core import eventing

from kfboot.app import create_app
from kfboot.onboarding import _clientIp

from .support import (
    build_exn,
    build_signed_serder,
    make_witness_backends,
    make_config,
    post_cesr,
    register_aid,
    split_cesr_message,
    start_session,
)


def test_public_discovery_stays_plain_json_and_reply_frames_prepend_boot_kel(contract):
    health = contract.simulate_get("/health")
    assert health.status_code == 200
    assert health.json == {
        "status": "ok",
        "cleanup": {
            "configured": True,
            "expected_running": False,
            "running": False,
            "pending_tasks": 0,
            "due_tasks": 0,
            "claimed_tasks": 0,
            "blocked_tasks": 0,
            "oldest_due_at": None,
            "oldest_due_age_seconds": None,
            "oldest_claimed_at": None,
            "oldest_claimed_age_seconds": None,
            "oldest_blocked_at": None,
            "oldest_blocked_age_seconds": None,
            "last_sweep_started_at": None,
            "last_sweep_finished_at": None,
            "last_progress_at": None,
            "current_sweep_started_at": None,
            "current_sweep_age_seconds": None,
            "last_error_at": None,
            "last_error": None,
            "last_recovery_at": None,
            "recovered_claimed_tasks": 0,
        },
    }
    assert "connection" not in {key.lower() for key in health.headers}

    config = contract.simulate_get("/bootstrap/config")
    assert config.status_code == 200
    assert config.json == {
        "bootstrap": {
            "account_options": [
                {"code": "1-of-1", "witness_count": 1, "toad": 1},
                {"code": "3-of-4", "witness_count": 4, "toad": 3},
            ],
            "watcher_required": True,
        },
        "region": {"id": "test-region", "name": "Test Region"},
        "surfaces": {
            "onboarding": {"path": "/onboarding", "url": "http://127.0.0.1:9723/onboarding"},
            "account": {"path": "/account", "url": "http://127.0.0.1:9723/account"},
        },
    }

    with habbing.openHab(name="surface-ephemeral", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        response, serders, reply = start_session(contract, ephemeral)

        assert response.content.startswith(contract.ctx.host_hab.replay())
        assert "connection" not in {key.lower() for key in response.headers}
        assert serders[0].ked["t"] == "icp"
        assert reply.ked["r"] == "/onboarding/session/start"
        assert reply.ked["i"] == contract.ctx.host_hab.pre
        assert reply.ked["a"]["session_id"].startswith("sess_")


def test_client_ip_normalizes_hio_tuple_remote_addr():
    assert _clientIp(("198.51.100.20", 49152)) == "198.51.100.20"
    assert _clientIp("198.51.100.20") == "198.51.100.20"
    assert _clientIp(None) == ""


def test_health_reports_cleanup_runtime_failure_when_cleanup_should_be_running(contract_factory):
    contract = contract_factory(cleanup_interval_seconds=60)

    health = contract.simulate_get("/health")   
    
    # Assert health report
    assert health.status_code == 503
    assert health.json["status"] == "degraded"
    assert health.json["cleanup"]["configured"] is True
    assert health.json["cleanup"]["expected_running"] is True
    assert health.json["cleanup"]["running"] is False
    assert health.json["cleanup"]["reason"] == "runner_not_running"
    assert health.json["cleanup"]["reasons"] == ["runner_not_running"]


def test_create_app_does_not_construct_sync_boot_clients(tmp_path):
    config = make_config(tmp_path, boot_api_timeout_seconds=7, cleanup_interval_seconds=0)
    _app, ctx = create_app(config=config, temp=True)
    try:
        assert ctx.witness_boots == {}
        assert ctx.watcher_boot is None
    finally:
        ctx.close(clear=True)


def test_public_discovery_only_advertises_profiles_supported_by_configured_witness_backends(contract_factory):
    contract = contract_factory(witness_backends=make_witness_backends(1))

    response = contract.simulate_get("/bootstrap/config")

    assert response.status_code == 200
    assert response.json["bootstrap"]["account_options"] == [
        {"code": "1-of-1", "witness_count": 1, "toad": 1},
    ]


def test_cesr_ingress_rejects_missing_state_attachment_signature_and_malformed_body(contract):
    with habbing.openHab(name="unknown-sender", temp=True, transferable=False) as (_, unknown):
        missing_state = post_cesr(
            contract,
            "/onboarding",
            build_exn(unknown, route="/onboarding/session/start", payload={"account_alias": "x"}),
        )
        assert missing_state.status_code == 401
        assert missing_state.json["title"] == "Unknown sender key state"

    with habbing.openHab(name="known-sender", temp=True, transferable=False) as (_, known):
        register_aid(contract, "/onboarding", known)

        signed = build_exn(known, route="/onboarding/session/start", payload={"account_alias": "x"})
        body, _ = split_cesr_message(signed)

        missing_attachment = contract.simulate_post(
            "/onboarding",
            body=body,
            headers={"Content-Type": CESR_CONTENT_TYPE},
        )
        assert missing_attachment.status_code == 412
        assert missing_attachment.json["title"] == "Attachment error"

        missing_signature = contract.simulate_post(
            "/onboarding",
            body=body,
            headers={"Content-Type": CESR_CONTENT_TYPE, CESR_ATTACHMENT_HEADER: ""},
        )
        assert missing_signature.status_code == 401
        assert missing_signature.json["title"] == "Request rejected"

    malformed = contract.simulate_post(
        "/onboarding",
        body=b"not-json",
        headers={"Content-Type": CESR_CONTENT_TYPE, CESR_ATTACHMENT_HEADER: ""},
    )
    assert malformed.status_code == 400
    assert malformed.json["title"] == "Malformed JSON"


def test_internal_route_handler_attribute_errors_do_not_fall_through_as_request_rejected(contract, monkeypatch):
    with habbing.openHab(name="handler-attribute-error", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral)

        def brokenRequireSession(_session_id):
            raise AttributeError("simulated internal rename miss")

        monkeypatch.setattr(contract.ctx.exchanger, "requireSession", brokenRequireSession)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/status",
                payload={"session_id": start_reply.ked["a"]["session_id"]},
            ),
        )

    assert response.status_code == 500
    assert response.json["title"] == "Route handler failed"


def test_cesr_ingress_rejects_unsupported_content_type(contract):
    with habbing.openHab(name="unsupported-content-type", temp=True, transferable=False) as (_, known):
        register_aid(contract, "/onboarding", known)
        signed = build_exn(known, route="/onboarding/session/start", payload={"account_aid": "AID1"})
        body, attachment = split_cesr_message(signed)

        response = contract.simulate_post(
            "/onboarding",
            body=body,
            headers={
                "Content-Type": "application/json",
                CESR_ATTACHMENT_HEADER: attachment.decode("utf-8"),
            },
        )

    assert response.status_code == 406
    assert response.json["title"] == "Content type error"


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/onboarding", CESR_CONTENT_TYPE),
        ("/account", CESR_CONTENT_TYPE),
        ("/onboarding", f"{CESR_CONTENT_TYPE}; charset=utf-8"),
        ("/account", f"{CESR_CONTENT_TYPE}; charset=utf-8"),
        ("/onboarding", "application/cesr+json"),
        ("/account", "application/cesr+json"),
        ("/onboarding", "application/cesr+json; charset=utf-8"),
        ("/account", "application/cesr+json; charset=utf-8"),
    ],
)
def test_cesr_ingress_accepts_supported_content_types_for_event_messages(contract, path, content_type):
    with habbing.openHab(name=f"content-type-{path.strip('/')}", temp=True, transferable=False) as (_, hab):
        body, attachment = split_cesr_message(hab.msgOwnInception())
        response = contract.simulate_post(
            path,
            body=body,
            headers={
                "Content-Type": content_type,
                CESR_ATTACHMENT_HEADER: attachment.decode("utf-8"),
            },
        )

    assert response.status_code == 204


@pytest.mark.parametrize(
    ("path", "route", "payload"),
    [
        ("/account", "/onboarding/session/start", {"account_alias": "wrong-surface"}),
        ("/onboarding", "/account/witnesses", {"account_aid": "AID1"}),
    ],
)
def test_surface_separation_rejects_routes_from_the_other_surface(contract, path, route, payload):
    with habbing.openHab(name=f"misroute-{route}", temp=True, transferable=False) as (_, hab):
        response = post_cesr(contract, path, build_exn(hab, route=route, payload=payload))

    assert response.status_code == 404
    assert response.json["title"] == "Unknown route"


@pytest.mark.parametrize(("path", "surface"), [("/onboarding", "onboarding"), ("/account", "account")])
def test_surfaces_reject_non_exn_business_messages(contract, path, surface):
    with habbing.openHab(name=f"qry-{surface}", temp=True, transferable=False) as (_, hab):
        qry = eventing.query(pre=hab.pre, route="logs", query={"i": hab.pre})
        response = post_cesr(contract, path, build_signed_serder(hab, qry))

    assert response.status_code == 400
    assert response.json == {
        "title": "Unsupported message type",
        "description": f"qry is not supported on the {surface} surface.",
    }


def test_event_messages_are_accepted_on_both_cesr_surfaces(contract):
    with (
        habbing.openHab(name="onboarding-event", temp=True, transferable=False) as (_, onboarding_hab),
        habbing.openHab(name="account-event", temp=True, transferable=False) as (_, account_hab),
    ):
        assert post_cesr(contract, "/onboarding", onboarding_hab.msgOwnInception()).status_code == 204
        assert post_cesr(contract, "/account", account_hab.msgOwnInception()).status_code == 204


def test_surface_rejects_key_events_that_do_not_advance_accepted_state(contract, monkeypatch):
    with habbing.openHab(name="account-rot", temp=True) as (_, hab):
        assert post_cesr(contract, "/account", hab.msgOwnInception()).status_code == 204

        hab.rotate()
        rot = hab.msgOwnEvent(sn=hab.kever.sn)

        monkeypatch.setattr(contract.ctx.parser, "parseOne", lambda **kwa: None)

        response = post_cesr(contract, "/account", rot)

    assert response.status_code == 409
    assert response.json["title"] == "Key state pending"
