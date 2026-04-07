from __future__ import annotations

from dataclasses import dataclass

import falcon

from kfboot.config import Config


AUTHORIZATION_HEADER = "Authorization"
CALLER_HEADER = "X-KF-Caller"


@dataclass(frozen=True)
class AuthContext:
    principal: str
    is_admin: bool


class AuthMiddleware:
    """Very small auth hook.

    v1 expects an authenticated caller id from a front-door auth layer or local
    development client. That caller id may come from:

    - ``Authorization: Bearer <caller>``
    - ``X-KF-Caller``

    Later, an ESSR edge can map the verified sender AID into the same request
    context without changing the handlers.
    """

    def __init__(self, config: Config):
        self.config = config

    def process_request(self, req: falcon.Request, _resp: falcon.Response) -> None:
        if req.path in {"/health", "/bootstrap/config"}:
            return
        
        # Don't require auth for public session endpoints
        if req.path.startswith("/session"):
            return

        principal = _caller_from_request(req)
        if not principal:
            raise falcon.HTTPUnauthorized(
                title="Missing caller",
                description=(
                    "Provide Authorization: Bearer <caller>, "
                    f"or {CALLER_HEADER}"
                ),
            )

        req.context.auth = AuthContext(
            principal=principal,
            is_admin=principal in self.config.admin_principals,
        )


def get_auth(req: falcon.Request) -> AuthContext:
    auth = getattr(req.context, "auth", None)
    if auth is None:
        raise falcon.HTTPUnauthorized(title="Missing auth context")
    return auth


def _caller_from_request(req: falcon.Request) -> str:
    authorization = req.get_header(AUTHORIZATION_HEADER) or ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()

    caller = req.get_header(CALLER_HEADER)
    if caller:
        return caller.strip()

    return ""
