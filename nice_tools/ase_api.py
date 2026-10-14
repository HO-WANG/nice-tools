"""Loopback-only JSON boundary for ASE database and local-file operations."""

from urllib.parse import urlsplit
import asyncio
import json

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .ase_jobs import manager, object_data

router = APIRouter(prefix="/api/ase")
HEADERS = {"Cache-Control": "no-store"}


def _discard_connected_session(task):
    try:
        result = task.result()
    except (Exception, asyncio.CancelledError):
        # Retrieve late failures so cancelled requests cannot leave unhandled
        # task errors, including errors whose details contain credentials.
        return
    try:
        manager.disconnect(result["session"])
    except Exception:
        # The session may already have been removed by shutdown/another cleanup.
        # Preserve the original request error or cancellation without logging.
        pass


async def _connect_for_request(request, connection):
    task = asyncio.create_task(run_in_threadpool(manager.connect, connection))
    try:
        result = await asyncio.shield(task)
        if await request.is_disconnected():
            raise ValueError("접속 요청이 종료되었습니다. 다시 접속하세요.")
        return result
    except asyncio.CancelledError:
        # Native work continues in its thread. Own its eventual session until
        # completion, including cancellation during the disconnect check.
        task.add_done_callback(_discard_connected_session)
        raise
    except Exception:
        _discard_connected_session(task)
        raise


def local_request(request, mutation=True):
    host = urlsplit("http://" + request.headers.get("host", ""))
    if host.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise PermissionError("이 도구는 같은 PC에서만 사용할 수 있습니다.")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != host.hostname or (parsed.port or 80) != (host.port or 80):
            raise PermissionError("같은 PC의 도구 화면에서 요청하세요.")
    if mutation and request.headers.get("x-nice-tools") != "ase":
        raise PermissionError("도구 화면에서 요청하세요.")


@router.get("/status")
async def status(request: Request):
    try:
        local_request(request, mutation=False)
        result = await run_in_threadpool(manager.status)
        return JSONResponse(result, headers=HEADERS)
    except PermissionError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403, headers=HEADERS)
    except (ValueError, OSError):
        return JSONResponse({"available": False, "detail": "ASE 드라이버 상태를 확인하지 못했습니다."}, headers=HEADERS)


@router.get("/profiles")
async def profiles(request: Request):
    try:
        local_request(request, mutation=False)
        from .ase_profiles import public_profiles

        result = await run_in_threadpool(public_profiles)
        return JSONResponse(result, headers=HEADERS)
    except PermissionError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403, headers=HEADERS)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400, headers=HEADERS)
    except OSError:
        return JSONResponse({"detail": "연결 설정 파일을 읽지 못했습니다."}, status_code=400, headers=HEADERS)


@router.post("/{operation}")
async def operation(request: Request, operation: str):
    try:
        local_request(request)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("JSON 형식으로 입력을 보내세요.")
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("입력 형식이 올바르지 않습니다.")
        session = data.get("session")
        if operation == "connect":
            if "profile_id" in data:
                if set(data) - {"profile_id", "master_password"}:
                    raise ValueError("저장된 서버는 선택한 설정과 Jasypt 키로 연결하세요.")
                from .ase_profiles import decrypt_connection

                selected = await run_in_threadpool(decrypt_connection, data.get("profile_id"), data.get("master_password"))
                result = await _connect_for_request(request, selected["connection"])
                result["profile_name"] = selected["profile_name"]
                if selected.get("database"):
                    result["database"] = selected["database"]
            else:
                result = await _connect_for_request(request, data)
        elif operation in ("owners", "objects"):
            database = data.get("database")
            if not isinstance(database, str) or not database or "\x00" in database or len(database) > 255:
                raise ValueError("데이터베이스를 선택하세요.")
            parameters = {"database": database}
            if operation == "objects":
                owner_id = data.get("owner_id")
                if owner_id is not None and (isinstance(owner_id, bool) or not isinstance(owner_id, int) or not 0 < owner_id <= 2147483647):
                    raise ValueError("소유자 선택이 올바르지 않습니다.")
                search = data.get("search", "")
                if not isinstance(search, str) or "\x00" in search or len(search) > 255:
                    raise ValueError("객체 검색어는 255자 이내로 입력하세요.")
                kind = data.get("kind", "all")
                if kind not in ("all", "U", "V", "P"):
                    raise ValueError("객체 종류를 선택하세요.")
                parameters.update(owner_id=owner_id, search=search, kind=kind)
            result = await run_in_threadpool(manager.catalog, session, operation, **parameters)
        elif operation == "columns":
            result = await run_in_threadpool(manager.catalog, session, operation, object=object_data(data.get("object")))
        elif operation == "pick-file":
            result = await run_in_threadpool(manager.pick_file, session, data.get("direction"), data.get("suggested_name"))
        elif operation == "preview":
            result = await run_in_threadpool(manager.preview, session, data.get("object"), data.get("file"), data.get("options"))
        elif operation == "jobs":
            result = await run_in_threadpool(manager.start, session, data.get("review"))
        elif operation == "job":
            result = await run_in_threadpool(manager.job, session, data.get("job"))
        elif operation == "cancel":
            result = await run_in_threadpool(manager.cancel, session, data.get("job"))
        elif operation == "disconnect":
            result = await run_in_threadpool(manager.disconnect, session)
        else:
            return JSONResponse({"detail": "요청을 찾을 수 없습니다."}, status_code=404, headers=HEADERS)
        return JSONResponse(result, headers=HEADERS)
    except PermissionError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403, headers=HEADERS)
    except (ValueError, RecursionError) as exc:
        detail = str(exc) if not isinstance(exc, RecursionError) else "입력 형식이 올바르지 않습니다."
        # JSON decoder errors should never reflect submitted text.
        if isinstance(exc, json.JSONDecodeError):
            detail = "JSON 입력을 읽지 못했습니다."
        return JSONResponse({"detail": detail}, status_code=400, headers=HEADERS)
    except (OSError, TypeError, KeyError):
        return JSONResponse({"detail": "ASE 작업을 완료하지 못했습니다. 입력과 접속 상태를 확인하세요."}, status_code=400, headers=HEADERS)
