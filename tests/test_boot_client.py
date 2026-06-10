from __future__ import annotations

from collections import deque
import json

import pytest

from kfboot.boot_client import BootError, HioBootClient
from kfboot.utils import bootErrorToHTTP


class FakeHioClient:
    def __init__(self, responses=None):
        self.responses = deque(responses or [])

    def respond(self):
        if self.responses:
            return self.responses.popleft()
        return None


class RecordingHioClienter:
    def __init__(self, clients=None, *, error: Exception | None = None):
        self.clients = list(clients or [])
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.removed: list[FakeHioClient] = []

    def request(self, method, url, headers=None, body=None):
        call = {"method": method, "url": url}
        if headers is not None:
            call["headers"] = headers
        if body is not None:
            call["body"] = body
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        return self.clients.pop(0)

    def remove(self, client):
        self.removed.append(client)


def drain(gen, *, tick=None, max_steps: int = 20):
    for _ in range(max_steps):
        try:
            next(gen)
        except StopIteration as ex:
            return ex.value
        if tick is not None:
            tick()
    raise AssertionError("generator did not finish")


def test_boot_error_to_http_preserves_downstream_service_unavailable():
    error = bootErrorToHTTP(BootError("capacity exhausted", status_code=503))

    assert error.status == "503 Service Unavailable"
    assert error.title == "Downstream service unavailable"
    assert error.description == "capacity exhausted"


def test_hio_delete_waits_for_response_and_removes_client():
    hio_client = FakeHioClient()
    clienter = RecordingHioClienter([hio_client])
    clock = {"tyme": 0.0}
    client = HioBootClient("http://boot.local", clienter=clienter, timeout=1.0)

    gen = client.deleteWitnessDo("W1", tymth=lambda: clock["tyme"], tock=0.0)

    assert next(gen) == 0.0
    assert clienter.calls == [{"method": "DELETE", "url": "http://boot.local/witnesses/W1"}]
    assert clienter.removed == []

    hio_client.responses.append({"status": 204, "body": b""})

    with pytest.raises(StopIteration) as done:
        next(gen)

    assert done.value.value is None
    assert clienter.removed == [hio_client]


def test_hio_allocate_witness_sends_body_parses_json_and_removes_client():
    hio_client = FakeHioClient()
    clienter = RecordingHioClienter([hio_client])
    client = HioBootClient("http://boot.local", clienter=clienter, timeout=1.0)

    gen = client.allocateWitnessDo(
        "AID1",
        idempotency_key="op-1",
        tymth=lambda: 0.0,
        tock=0.0,
    )

    assert next(gen) == 0.0
    assert clienter.calls == [
        {
            "method": "POST",
            "url": "http://boot.local/witnesses",
            "headers": {
                "Content-Type": "application/json",
                "Idempotency-Key": "op-1",
            },
            "body": json.dumps({"aid": "AID1"}, separators=(",", ":")).encode("utf-8"),
        }
    ]

    hio_client.responses.append({"status": 201, "body": b'{"eid":"W1"}'})

    with pytest.raises(StopIteration) as done:
        next(gen)

    assert done.value.value == {"eid": "W1"}
    assert clienter.removed == [hio_client]


def test_hio_watcher_status_parses_json_and_removes_client():
    hio_client = FakeHioClient(
        [
            {
                "status": 200,
                "body": b'{"watcher_id":"WA1","summary":{"responsive_witnesses":1}}',
            }
        ]
    )
    clienter = RecordingHioClienter([hio_client])
    client = HioBootClient("http://boot.local", clienter=clienter)

    status = drain(client.watcherStatusDo("WA1", tymth=lambda: 0.0, tock=0.0))

    assert status == {"watcher_id": "WA1", "summary": {"responsive_witnesses": 1}}
    assert clienter.calls == [{"method": "GET", "url": "http://boot.local/watchers/WA1/status"}]
    assert clienter.removed == [hio_client]


def test_hio_json_invalid_response_raises_boot_error_and_removes_client():
    hio_client = FakeHioClient([{"status": 200, "body": b"not-json"}])
    clienter = RecordingHioClienter([hio_client])
    client = HioBootClient("http://boot.local", clienter=clienter)

    with pytest.raises(BootError) as excinfo:
        drain(client.watcherStatusDo("WA1", tymth=lambda: 0.0, tock=0.0))

    assert "Invalid JSON from boot API" in str(excinfo.value)
    assert excinfo.value.status_code == 200
    assert clienter.removed == [hio_client]


def test_hio_delete_error_preserves_status_and_removes_client():
    hio_client = FakeHioClient([{"status": 503, "body": b"downstream unavailable"}])
    clienter = RecordingHioClienter([hio_client])
    client = HioBootClient("http://boot.local", clienter=clienter)

    with pytest.raises(BootError) as excinfo:
        drain(client.deleteWatcherDo("WA1", tymth=lambda: 0.0, tock=0.0))

    assert str(excinfo.value) == "downstream unavailable"
    assert excinfo.value.status_code == 503
    assert clienter.removed == [hio_client]


def test_hio_request_exception_raises_boot_error():
    clienter = RecordingHioClienter(error=RuntimeError("boom"))
    client = HioBootClient("http://boot.local", clienter=clienter)

    with pytest.raises(BootError) as excinfo:
        drain(client.deleteWatcherDo("WA1", tymth=lambda: 0.0, tock=0.0))

    assert str(excinfo.value) == "Boot API request failed: boom"
    assert excinfo.value.status_code is None
    assert clienter.calls == [{"method": "DELETE", "url": "http://boot.local/watchers/WA1"}]
    assert clienter.removed == []


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"body": b"missing status"}, "Boot API response missing status"),
        ({"status": "wat", "body": b"invalid status"}, "Boot API response has invalid status: 'wat'"),
        ({"status": 0, "body": b"zero status"}, "Boot API response has invalid status: 0"),
    ],
)
def test_hio_delete_malformed_status_raises_boot_error_and_removes_client(response, message):
    hio_client = FakeHioClient([response])
    clienter = RecordingHioClienter([hio_client])
    client = HioBootClient("http://boot.local", clienter=clienter)

    with pytest.raises(BootError) as excinfo:
        drain(client.deleteWatcherDo("WA1", tymth=lambda: 0.0, tock=0.0))

    assert str(excinfo.value) == message
    assert excinfo.value.status_code is None
    assert clienter.removed == [hio_client]


def test_hio_delete_timeout_removes_client():
    hio_client = FakeHioClient()
    clienter = RecordingHioClienter([hio_client])
    clock = {"tyme": 0.0}
    client = HioBootClient("http://boot.local", clienter=clienter, timeout=0.2)

    with pytest.raises(BootError) as excinfo:
        drain(
            client.deleteWitnessDo("W1", tymth=lambda: clock["tyme"], tock=0.0),
            tick=lambda: clock.__setitem__("tyme", clock["tyme"] + 0.1),
        )

    assert str(excinfo.value) == "Boot API request timed out"
    assert excinfo.value.status_code is None
    assert clienter.removed == [hio_client]
