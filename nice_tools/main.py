"""웹 화면과 도구 API의 실행 진입점."""

from pathlib import Path

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from .comparison import compare_directories
from .limits import BodyLimitMiddleware, MAX_FILES
from .uploads import read_uploads

app = FastAPI(title="nice-tools", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(BodyLimitMiddleware)
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/compare")
async def compare(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(400, "폴더의 파일을 multipart 형식으로 보내 주세요.")
    async with request.form(max_files=MAX_FILES, max_fields=6) as form:
        left, right, options = await read_uploads(form)
        return await run_in_threadpool(compare_directories, left, right, options)
