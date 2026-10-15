import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)


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
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), request.app.state.runtime.settings.api_key.encode()
    ):
        raise HTTPException(
            status_code=401,
            detail="Clé API invalide ou absente.",
            headers={"WWW-Authenticate": "Bearer"},
        )
