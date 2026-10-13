"""웹 화면과 도구 API의 실행 진입점."""

from pathlib import Path

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from .comparison import compare_directories
from .diff import file_diff
from .limits import MAX_FILES
from .uploads import read_uploads

app = FastAPI(title="nice-tools", docs_url=None, redoc_url=None, openapi_url=None)
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


def require_multipart(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(400, "폴더의 파일을 multipart 형식으로 보내 주세요.")


@app.post("/api/compare")
async def compare(request: Request):
    require_multipart(request)
    async with request.form(max_files=MAX_FILES, max_fields=6) as form:
        left, right, options = await read_uploads(form)
        return await run_in_threadpool(compare_directories, left, right, options)


@app.post("/api/diff")
async def diff(request: Request):
    require_multipart(request)
    async with request.form(max_files=2, max_fields=6) as form:
        left, right, options = await read_uploads(form, allow_one_side=True)
        paths = left.keys() | right.keys()
        if len(paths) != 1:
            raise HTTPException(400, "같은 상대 경로의 파일 한 쌍만 선택해 주세요.")
        path = next(iter(paths))
        try:
            return await run_in_threadpool(file_diff, path, left.get(path), right.get(path), options)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
