"""본문을 모두 읽기 전에도 적용하는 업로드 크기 제한."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

MAX_FILES = 1000
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 50 * 1024 * 1024


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_REQUEST_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        message = "업로드 요청이 너무 큽니다. 두 폴더 합계는 50 MiB 이내여야 합니다."
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    return await JSONResponse({"detail": "요청 크기 정보가 잘못되었습니다."}, status_code=400)(scope, receive, send)
                if declared > self.max_bytes:
                    return await JSONResponse({"detail": message}, status_code=413)(scope, receive, send)
        received = 0

        async def limited_receive():
            nonlocal received
            event = await receive()
            if event["type"] == "http.request":
                received += len(event.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413, detail=message)
            return event

        await self.app(scope, limited_receive, send)
