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


_SESSION_CLEANUPS = set()


async def _disconnect_session(session):
    try:
        await run_in_threadpool(manager.disconnect, session)
    except Exception:
        # Shutdown/other cleanup may have already removed the session. ODBC
        # driver errors are never logged or surfaced from background cleanup.
        pass


def _cleanup_finished(task):
    _SESSION_CLEANUPS.discard(task)
    try:
        task.result()
    except (Exception, asyncio.CancelledError):
        pass


def _schedule_disconnect(session):
    task = asyncio.create_task(_disconnect_session(session))
    _SESSION_CLEANUPS.add(task)
    task.add_done_callback(_cleanup_finished)
    return task


def _connected_session(task):
    try:
        return task.result()["session"]
    except (Exception, asyncio.CancelledError):
        # Consume failures after HTTP cancellation without retaining secrets.
        return None


def _discard_connected_session(task):
    session = _connected_session(task)
    if session is not None:
        _schedule_disconnect(session)


async def _connect_for_request(request, connection):
    task = asyncio.create_task(run_in_threadpool(manager.connect, connection))
    try:
        result = await asyncio.shield(task)
        if await request.is_disconnected():
            raise ValueError("접속 요청이 종료되었습니다. 다시 접속하세요.")
        return result
    except asyncio.CancelledError:
        # CLI work continues in its thread. Own its eventual session until
        # completion, including cancellation during the disconnect check.
        if task.done():
            session = _connected_session(task)
            if session is not None:
                await asyncio.shield(_schedule_disconnect(session))
        else:
            task.add_done_callback(_discard_connected_session)
        raise
    except Exception:
        session = _connected_session(task)
        if session is not None:
            await asyncio.shield(_schedule_disconnect(session))
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
        return JSONResponse({"available": False, "detail": "SAP 클라이언트 상태를 확인하지 못했습니다."}, headers=HEADERS)


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
            from .ase_profiles import (decrypt_connection, prepare_connection, remember_connection,
                                       save_connection, SAVE_ERROR)

            if "profile_id" in data:
                if set(data) - {"profile_id"}:
                    raise ValueError("저장된 서버의 접속 프로필을 선택하세요.")
                selected = await run_in_threadpool(decrypt_connection, data.get("profile_id"))
                result = await _connect_for_request(request, selected["connection"])
                result["profile_name"] = selected["profile_name"]
                if selected.get("database"):
                    result["database"] = selected["database"]
                save_function, save_arguments = remember_connection, (data.get("profile_id"),)
            else:
                if set(data) - {"host", "port", "user", "password"}:
                    raise ValueError("서버 주소·포트·사용자·DB 비밀번호를 입력하세요.")
                prepared = await run_in_threadpool(prepare_connection, data)
                result = await _connect_for_request(request, prepared["connection"])
                save_function, save_arguments = save_connection, (prepared,)
            try:
                try:
                    saved = await run_in_threadpool(save_function, *save_arguments)
                    result.update(saved)
                except (ValueError, OSError):
                    # A read-only/malformed/externally modified configuration
                    # must not turn a successful DB login into an API failure.
                    result.update(saved=False, save_warning=SAVE_ERROR)
                if await request.is_disconnected():
                    raise ValueError("접속 요청이 종료되었습니다. 다시 접속하세요.")
            except (asyncio.CancelledError, ValueError):
                await asyncio.shield(_schedule_disconnect(result["session"]))
                raise
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
