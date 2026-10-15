from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """Bound raw HTTP bodies including chunked multipart, before disk spooling grows."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > self.max_bytes:
            response = JSONResponse(
                {"error": {"code": "request_too_large", "message": "Requête trop volumineuse."}},
                status_code=413,
            )
            return await response(scope, receive, send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(
                        413,
                        detail={
                            "code": "request_too_large",
                            "message": "Requête trop volumineuse.",
                        },
                    )
            return message

        await self.app(scope, limited_receive, send)
