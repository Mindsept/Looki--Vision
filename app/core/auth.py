import base64
import hashlib
import hmac
import json
import secrets
import time
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import DomainError

bearer = HTTPBearer(auto_error=False)


def issue_session_token(key: str, session_id: UUID, role: str, ttl: int) -> str:
    claims = {
        "session_id": str(session_id),
        "role": role,
        "exp": int(time.time()) + ttl,
        "nonce": secrets.token_urlsafe(16),
    }
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(
        key.encode(), ("looki-session-v1." + payload).encode(), hashlib.sha256
    ).hexdigest()
    return f"looki1.{payload}.{signature}"


def session_role(header: str | None, key: str, session_id: str | UUID | None) -> str:
    if token_matches(header, key):
        return "operator"
    try:
        scheme, token = (header or "").split(" ", 1)
        prefix, payload, signature = token.split(".")
        if scheme.lower() != "bearer" or prefix != "looki1":
            raise ValueError
        expected = hmac.new(
            key.encode(), ("looki-session-v1." + payload).encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if claims["exp"] <= time.time() or claims["role"] not in {"mobile", "pi"}:
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise DomainError(401, "unauthenticated", "Jeton invalide ou expiré.") from None
    if session_id is None or claims["session_id"] != str(session_id):
        raise DomainError(403, "session_forbidden", "Ce jeton ne donne pas accès à cette session.")
    return claims["role"]


def token_matches(header: str | None, expected: str) -> bool:
    if not header:
        return False
    parts = header.split(" ", 1)
    return (
        len(parts) == 2
        and parts[0].lower() == "bearer"
        and secrets.compare_digest(parts[1].encode(), expected.encode())
    )


async def require_auth(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
):
    role = session_role(
        request.headers.get("authorization"),
        request.app.state.runtime.settings.api_key,
        request.path_params.get("session_id"),
    )
    request.state.role = role
    path = request.url.path
    if role == "operator":
        return
    if path.endswith("/access-tokens"):
        raise DomainError(403, "session_forbidden", "Émission réservée à l’opérateur.")
    if role == "pi":
        sid = request.path_params["session_id"]
        base = f"/api/v1/sessions/{sid}"
        allowed = (request.method == "POST" and path == base + "/frames") or (
            request.method == "GET" and (path == base or path.startswith(base + "/frames/"))
        )
        if not allowed:
            raise DomainError(
                403, "session_forbidden", "Jeton Pi limité aux captures de sa session."
            )
    elif request.method == "POST" and path.endswith("/frames"):
        raise DomainError(403, "session_forbidden", "Jeton Pi requis pour la capture.")
