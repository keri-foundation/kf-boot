from __future__ import annotations

import json as jsoning
from dataclasses import dataclass
from typing import Any

from hio.base import tyming
from keri import help

logger = help.ogler.getLogger(__name__)


class BootError(RuntimeError):
    """Raised when a boot API call fails."""

    def __init__(self, description: str, status_code: int | None = None):
        super().__init__(description)
        self.status_code = status_code


@dataclass(frozen=True)
class HioBootClient:
    base_url: str
    clienter: Any
    timeout: float = 10.0

    def allocateWitnessDo(
        self,
        account_aid: str,
        *,
        idempotency_key: str = "",
        tymth,
        tock: float = 0.0,
    ):
        response = yield from self._requestDo(
            "POST",
            "/witnesses",
            json={"aid": account_aid},
            idempotency_key=idempotency_key,
            tymth=tymth,
            tock=tock,
        )
        return _responseJson(response)

    def allocateWatcherDo(
        self,
        account_aid: str,
        *,
        oobi: str | None = None,
        idempotency_key: str = "",
        tymth,
        tock: float = 0.0,
    ):
        payload = {"aid": account_aid}
        if oobi:
            payload["oobi"] = oobi
        response = yield from self._requestDo(
            "POST",
            "/watchers",
            json=payload,
            idempotency_key=idempotency_key,
            tymth=tymth,
            tock=tock,
        )
        return _responseJson(response)

    def watcherStatusDo(self, eid: str, *, tymth, tock: float = 0.0):
        response = yield from self._requestDo(
            "GET",
            f"/watchers/{eid}/status",
            tymth=tymth,
            tock=tock,
        )
        return _responseJson(response)

    def deleteWitnessDo(self, eid: str, *, tymth, tock: float = 0.0):
        yield from self._emptyDo("DELETE", f"/witnesses/{eid}", tymth=tymth, tock=tock)

    def deleteWatcherDo(self, eid: str, *, tymth, tock: float = 0.0):
        yield from self._emptyDo("DELETE", f"/watchers/{eid}", tymth=tymth, tock=tock)

    def _emptyDo(self, method: str, path: str, *, tymth, tock: float = 0.0):
        yield from self._requestDo(method, path, tymth=tymth, tock=tock)

    def _requestDo(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotency_key: str = "",
        tymth,
        tock: float = 0.0,
    ):
        url = f"{self.base_url}{path}"
        headers = None
        body = None
        if json is not None:
            headers = {"Content-Type": "application/json"}
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            body = jsoning.dumps(json, separators=(",", ":")).encode("utf-8")
        try:
            if body is None and headers is None:
                client = self.clienter.request(method, url)
            else:
                client = self.clienter.request(method, url, headers=headers, body=body)
        except Exception as exc:
            logger.warning(
                f"BOOT API request failed due to HIO client exception: `{exc}`",
            )
            raise BootError(f"Boot API request failed: {exc}") from exc
        if client is None:
            raise BootError("Boot API request failed to create HIO client")

        tymer = tyming.Tymer(tymth=tymth, duration=self.timeout)
        try:
            while not client.responses and not tymer.expired:
                yield tock

            if not client.responses:
                raise BootError("Boot API request timed out")

            response = client.respond() if hasattr(client, "respond") else client.responses.popleft()
            status = _responseStatus(response)
            if status >= 400:
                description = _responseBodyText(response) or f"HTTP {status}"
                logger.warning(
                    "BOOT API request failed: "
                    f"method={method} url={url} status={status} "
                    f"body={description!r}",
                )
                raise BootError(description, status_code=status)

            logger.info(
                f"BOOT API request succeeded: `{response}`",
            )
            return response
        finally:
            self.clienter.remove(client)


def _responseStatus(response: Any) -> int:
    status = response.get("status") if isinstance(response, dict) else getattr(response, "status", None)
    if status is None:
        raise BootError("Boot API response missing status")
    try:
        status = int(status)
    except (TypeError, ValueError) as exc:
        raise BootError(f"Boot API response has invalid status: {status!r}") from exc
    if status <= 0:
        raise BootError(f"Boot API response has invalid status: {status}")
    return status


def _responseBodyText(response: Any) -> str:
    body = response.get("body", b"") if isinstance(response, dict) else getattr(response, "body", b"")
    if isinstance(body, memoryview):
        body = bytes(body)
    if isinstance(body, bytearray):
        body = bytes(body)
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace").strip()
    return str(body or "").strip()


def _responseJson(response: Any) -> dict[str, Any]:
    status = _responseStatus(response)
    body = _responseBodyText(response)
    try:
        data = jsoning.loads(body)
    except ValueError as exc:
        raise BootError(
            f"Invalid JSON from boot API: {exc}",
            status_code=status,
        ) from exc
    if not isinstance(data, dict):
        raise BootError("Invalid JSON from boot API: expected object", status_code=status)
    return data
