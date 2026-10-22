"""Local-only HTTP boundary for comparisons of native folder selections."""
import asyncio
import re
import unicodedata

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .ase_api import local_request
from .local_compare_jobs import SessionExpired, manager
from .local_compare_worker import ComparisonError


router = APIRouter(prefix='/api/comparison')
HEADERS = {'Cache-Control': 'no-store'}
_CLEANUPS = set()
_HANDLE = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_FIELDS = {
    'session': (set(), set()),
    'pick-directory': ({'session', 'side'}, set()),
    'start': ({'session', 'left', 'right'}, {'options'}),
    'job': ({'session', 'job'}, set()),
    'cancel': ({'session', 'job'}, set()),
    'diff': ({'session', 'job', 'path'}, {'mode', 'page'}),
    'close': ({'session'}, set()),
}
_INVALID = '파일 비교 요청을 확인하고 다시 시도하세요.'
_FAILED = '파일 비교 작업을 완료하지 못했습니다. 폴더와 작업 상태를 확인하세요.'


def _response(data, status=200):
    return JSONResponse(data, status_code=status, headers=HEADERS)


def _boundary(request, *, mutation):
    local_request(request, mutation=False)
    if mutation and request.headers.get('x-nice-tools') != 'comparison':
        raise PermissionError


def _handle(value):
    if not isinstance(value, str) or not _HANDLE.fullmatch(value):
        raise ValueError(_INVALID)
    return value


def _options(value):
    if not isinstance(value, dict) or set(value) - {
        'sort_lines', 'ignore_whitespace', 'ignore_start', 'ignore_end'
    }:
        raise ValueError(_INVALID)
    for name in ('sort_lines', 'ignore_whitespace'):
        if name in value and type(value[name]) is not bool:
            raise ValueError(_INVALID)
    for name in ('ignore_start', 'ignore_end'):
        if name in value and (type(value[name]) is not int or value[name] < 0):
            raise ValueError(_INVALID)
    return value


def _relative_path(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or any(unicodedata.category(char).startswith('C') for char in value)
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError(_INVALID)
    return value


def _cleanup_finished(task):
    _CLEANUPS.discard(task)
    try:
        task.result()
    except (Exception, asyncio.CancelledError):
        # Never record source contents or native exception strings.
        pass


def _schedule_cleanup(function, *args):
    task = asyncio.create_task(run_in_threadpool(function, *args))
    _CLEANUPS.add(task)
    task.add_done_callback(_cleanup_finished)
    return task


def _discard_result(task, cleanup):
    try:
        result = task.result()
    except (Exception, asyncio.CancelledError):
        return None
    return _schedule_cleanup(cleanup, result)


async def _owned_for_request(request, function, *args, cleanup, close_immediately=False):
    """Own eventual thread results when the requesting browser disappears.

    Folder pickers belong to a known session, which is closed immediately on
    cancellation. Creation/start threads must finish before their newly
    returned session/job handle can be released. A diff cache remains owned by
    its existing job when only the HTTP page request is abandoned.
    """
    task = asyncio.create_task(run_in_threadpool(function, *args))
    try:
        result = await asyncio.shield(task)
        if not await request.is_disconnected():
            return result
    except asyncio.CancelledError:
        if close_immediately:
            cleanup_task = _schedule_cleanup(cleanup, None)
            task.add_done_callback(_cleanup_finished)
        elif task.done():
            cleanup_task = _discard_result(task, cleanup)
        else:
            task.add_done_callback(lambda completed: _discard_result(completed, cleanup))
            cleanup_task = None
        if cleanup_task is not None:
            await asyncio.shield(cleanup_task)
        raise
    except Exception:
        # A disconnect probe can fail after native creation has succeeded.
        # Consume a failed worker result, or release its successful resource.
        cleanup_task = _discard_result(task, cleanup)
        if cleanup_task is not None:
            await asyncio.shield(cleanup_task)
        raise
    # The call completed but the response would be lost. Finish cleanup before
    # reporting failure; no untracked folder grant or job remains behind.
    await asyncio.shield(_schedule_cleanup(cleanup, None if close_immediately else result))
    raise ValueError(_INVALID)


def _error(error):
    if isinstance(error, SessionExpired):
        return _response({'detail': '파일 비교 세션이 만료되었습니다. 폴더를 다시 선택하세요.',
                          'code': 'session_expired'}, 404)
    if isinstance(error, PermissionError):
        return _response({'detail': '같은 PC의 도구 화면에서 요청하세요.'}, 403)
    if isinstance(error,ComparisonError):
        return _response({'detail':str(error),'code':error.code},400)
    # Even ValueError/OSError may come from a native library. Only fixed text
    # crosses this boundary; native exception data is never logged here.
    return _response({'detail': _FAILED}, 400)


@router.get('/status')
async def status(request: Request):
    try:
        _boundary(request, mutation=False)
        return _response(await run_in_threadpool(manager.status))
    except Exception as error:
        return _error(error)


@router.post('/{operation}')
async def operation(request: Request, operation: str):
    try:
        _boundary(request, mutation=True)
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise ValueError(_INVALID)
        data = await request.json()
        if operation not in _FIELDS or not isinstance(data, dict):
            raise ValueError(_INVALID)
        required, optional = _FIELDS[operation]
        if not required <= set(data) or set(data) - (required | optional):
            raise ValueError(_INVALID)
        if operation == 'session':
            result = await _owned_for_request(
                request, manager.create_session,
                cleanup=lambda result: manager.close_session(result['session']))
        else:
            session = _handle(data['session'])
            if operation == 'pick-directory':
                if data['side'] not in ('left', 'right'):
                    raise ValueError(_INVALID)
                result = await _owned_for_request(
                    request, manager.pick_directory, session, data['side'],
                    cleanup=lambda _: manager.close_session(session), close_immediately=True)
            elif operation == 'start':
                result = await _owned_for_request(
                    request, manager.start, session, _handle(data['left']), _handle(data['right']),
                    _options(data.get('options', {})),
                    cleanup=lambda result: manager.cancel(session, result['job']))
            elif operation in ('job', 'cancel'):
                result = await run_in_threadpool(getattr(manager, operation), session, _handle(data['job']))
            elif operation == 'diff':
                mode, page = data.get('mode', 'compared'), data.get('page', 0)
                if mode not in ('compared', 'original') or type(page) is not int or page < 0:
                    raise ValueError(_INVALID)
                result = await _owned_for_request(
                    request, manager.diff, session, _handle(data['job']), _relative_path(data['path']), mode, page,
                    cleanup=lambda _: None)
            else:
                result = await run_in_threadpool(manager.close_session, session)
        return _response(result)
    except Exception as error:
        return _error(error)


@router.api_route('', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
@router.api_route('/{unknown:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
async def unknown(request: Request, unknown: str = ''):
    try:
        _boundary(request, mutation=request.method not in ('GET', 'HEAD'))
        raise ValueError(_INVALID)
    except Exception as error:
        return _error(error)
