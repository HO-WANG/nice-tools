"""Local-only IQ connection and owned SELECT-export API."""
import asyncio
import json
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from .ase_api import local_request
from .iq_jobs import manager

router = APIRouter(prefix='/api/iq')
HEADERS = {'Cache-Control': 'no-store'}

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



async def _connect_and_save(request, data):
    from . import iq_profiles
    if 'profile_id' in data:
        if set(data) != {'profile_id'}:
            raise ValueError('저장된 서버의 접속 프로필을 선택하세요.')
        selected = await run_in_threadpool(iq_profiles.decrypt_connection, _handle(data['profile_id']))
        result = await _connect_for_request(request, selected['connection'])
        result['profile_name'] = selected['profile_name']
        save_function, save_argument = iq_profiles.remember_connection, data['profile_id']
    else:
        prepared = await run_in_threadpool(iq_profiles.prepare_connection, data)
        result = await _connect_for_request(request, prepared['connection'])
        save_function, save_argument = iq_profiles.save_connection, prepared
    try:
        try:
            result.update(await run_in_threadpool(save_function, save_argument))
        except (ValueError, OSError):
            # A valid native connection remains usable when its optional local
            # configuration is malformed, read-only or externally changed.
            result.update(saved=False, save_warning=iq_profiles.SAVE_ERROR)
        if await request.is_disconnected():
            raise ValueError('접속 요청이 종료되었습니다. 다시 접속하세요.')
        return result
    except (Exception, asyncio.CancelledError):
        await asyncio.shield(_schedule_disconnect(result['session']))
        raise


@router.get('/profiles')
async def profiles(request: Request):
    try:
        local_request(request, mutation=False)
        from .iq_profiles import public_profiles
        return JSONResponse(await run_in_threadpool(public_profiles), headers=HEADERS)
    except PermissionError as error:
        return JSONResponse({'detail': str(error)}, status_code=403, headers=HEADERS)
    except ValueError as error:
        return JSONResponse({'detail': str(error)}, status_code=400, headers=HEADERS)
    except Exception:
        return JSONResponse({'detail': 'IQ 연결 설정 파일을 읽지 못했습니다.'}, status_code=400, headers=HEADERS)


@router.get('/status')
async def status(request: Request):
    try:
        local_request(request, mutation=False)
        return JSONResponse(await run_in_threadpool(manager.status), headers=HEADERS)
    except PermissionError as error:
        return JSONResponse({'detail': str(error)}, status_code=403, headers=HEADERS)
    except Exception:
        return JSONResponse({'available': False, 'module_available': False, 'drivers': [], 'driver': '',
                             'detail': 'IQ ODBC 드라이버를 확인하지 못했습니다.'}, headers=HEADERS)


def _handle(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError('다시 연결하거나 작업을 선택하세요.')
    return value


@router.post('/{operation}')
async def operation(request: Request, operation: str):
    try:
        local_request(request, mutation=False)
        if request.headers.get('x-nice-tools') != 'iq':
            raise PermissionError('도구 화면에서 요청하세요.')
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise ValueError('JSON 형식으로 입력을 보내세요.')
        try:
            data = await request.json()
        except (ValueError, RecursionError):
            raise ValueError('JSON 입력을 읽을 수 없습니다.') from None
        fields = {'connect': {'host','port','database','server_name','user','password','driver','profile_id'},
                  'pick-directory': {'session'}, 'jobs': {'session','sql','filename','options'},
                  'job': {'session','job'}, 'cancel': {'session','job'}, 'disconnect': {'session'}}
        if operation not in fields or not isinstance(data, dict) or set(data) - fields[operation]:
            raise ValueError('지원하지 않는 입력 형식입니다.')
        if operation == 'connect':
            result = await _connect_and_save(request, data)
        else:
            session = _handle(data.get('session'))
            if operation == 'jobs':
                options = data.get('options', {})
                if not isinstance(options, dict) or set(options) - {'encoding','field_terminator','row_terminator','include_header'}:
                    raise ValueError('내보내기 설정을 확인하세요.')
                result = await run_in_threadpool(manager.start_export, session, data.get('sql'), data.get('filename'), options)
            elif operation in ('job','cancel'):
                result = await run_in_threadpool(getattr(manager, operation), session, _handle(data.get('job')))
            else:
                result = await run_in_threadpool(getattr(manager, operation.replace('-', '_')), session)
        return JSONResponse(result, headers=HEADERS)
    except PermissionError as error:
        return JSONResponse({'detail': str(error)}, status_code=403, headers=HEADERS)
    except ValueError as error:
        return JSONResponse({'detail': str(error)}, status_code=400, headers=HEADERS)
    except Exception:
        return JSONResponse({'detail': 'IQ 작업을 완료하지 못했습니다. 연결과 출력 폴더를 확인하세요.'}, status_code=400, headers=HEADERS)
