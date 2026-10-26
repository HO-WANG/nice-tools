"""Local HTTP boundary for read-only inspection of a session-owned file."""
import asyncio
import re
import unicodedata

from fastapi import APIRouter, Request
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse
from starlette.routing import Match

from .ase_api import local_request
from .local_compare_api import _owned_for_request
from .local_compare_worker import ComparisonError
from .text_inspection_jobs import SessionExpired, manager
from .text_inspection_engine import InspectionError


router = APIRouter(prefix='/api/text-inspection')
HEADERS = {'Cache-Control': 'no-store'}
_HANDLE = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_FIELDS = {
    'session': (set(), set()),
    'pick-file': ({'session'}, set()),
    'start': ({'session', 'file'}, {'options'}),
    'job': ({'session', 'job'}, set()),
    'cancel': ({'session', 'job'}, set()),
    'page': ({'session', 'job'}, {'page'}),
    'close': ({'session'}, set()),
}
_INVALID = '숨은 문자 검사 요청을 확인하고 다시 시도하세요.'
_FAILED = '숨은 문자 검사를 완료하지 못했습니다. 파일과 작업 상태를 확인하세요.'
_SAFE_ERRORS = {
    'changed': '선택한 파일이 변경되었습니다. 파일을 다시 선택하고 검사하세요.',
    'busy': '검사 결과를 준비하고 있습니다. 잠시 후 다시 시도하세요.',
    'encoding': '선택한 인코딩으로 파일 전체를 읽을 수 없습니다. 인코딩을 확인하세요.',
    'inspection_cache': '검사 결과를 저장하거나 읽을 수 없습니다. 임시 저장 공간과 파일 상태를 확인하세요.',
}


def _response(data, status=200):
    return JSONResponse(data, status_code=status, headers=HEADERS)


def _boundary(request, *, mutation):
    try:
        local_request(request, mutation=False)
    except (ValueError, TypeError):
        # Malformed URL headers fail at the same origin boundary.
        raise PermissionError from None
    if mutation and request.headers.get('x-nice-tools') != 'text-inspection':
        raise PermissionError


def _handle(value):
    if not isinstance(value, str) or not _HANDLE.fullmatch(value):
        raise ValueError(_INVALID)
    return value


def _options(value):
    if not isinstance(value, dict) or set(value) - {'encoding', 'include_common_whitespace', 'strict_mode'}:
        raise ValueError(_INVALID)
    if 'encoding' in value:
        encoding = value['encoding']
        if (not isinstance(encoding, str) or not 1 <= len(encoding) <= 128
                or any(unicodedata.category(char).startswith('C') for char in encoding)):
            raise ValueError(_INVALID)
    for name in ('include_common_whitespace', 'strict_mode'):
        if name in value and type(value[name]) is not bool:
            raise ValueError(_INVALID)
    return value


def _error(error):
    if isinstance(error,InspectionError) and error.code=='options':
        return _response({'detail':'인코딩 이름과 검사 옵션을 확인하세요.','code':'options'},400)
    if isinstance(error, SessionExpired):
        return _response({'detail': '숨은 문자 검사 세션이 만료되었습니다. 파일을 다시 선택하세요.',
                          'code': 'session_expired'}, 404)
    if isinstance(error, PermissionError):
        return _response({'detail': '같은 PC의 도구 화면에서 요청하세요.'}, 403)
    if isinstance(error, ComparisonError):
        code = error.code
        if isinstance(code, str) and code in _SAFE_ERRORS:
            return _response({'detail': _SAFE_ERRORS[code], 'code': code}, 400)
    # Native exceptions and filenames never become error messages or logs.
    return _response({'detail': _FAILED}, 400)


async def _settle(task):
    """Keep a thread's input alive even when the request is cancelled again."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
        except Exception:
            break
    try:
        task.result()
    except (Exception, asyncio.CancelledError):
        pass


async def _upload_for_request(request, session, upload):
    task = asyncio.create_task(run_in_threadpool(
        manager.select_upload, session, upload.file, upload.filename))
    try:
        result = await asyncio.shield(task)
        if not await request.is_disconnected():
            return result
    except asyncio.CancelledError:
        # Close promptly to revoke selection ownership, but do not let the
        # form close its input while select_upload still has a reader thread.
        cleanup = asyncio.create_task(run_in_threadpool(manager.close_session, session))
        await _settle(task)
        await _settle(cleanup)
        raise
    except Exception:
        cleanup = asyncio.create_task(run_in_threadpool(manager.close_session, session))
        await _settle(task)
        await _settle(cleanup)
        raise
    cleanup = asyncio.create_task(run_in_threadpool(manager.close_session, session))
    await _settle(cleanup)
    raise ValueError(_INVALID)


@router.get('/status')
async def status(request: Request):
    try:
        _boundary(request, mutation=False)
        return _response(await run_in_threadpool(manager.status))
    except Exception as error:
        return _error(error)


@router.post('/upload')
async def upload(request: Request):
    try:
        _boundary(request, mutation=True)
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'multipart/form-data':
            raise ValueError(_INVALID)
        availability = await run_in_threadpool(manager.status)
        if availability.get('native_available') is not False:
            raise ValueError(_INVALID)
        async with request.form(max_files=1, max_fields=1) as form:
            if (set(form) != {'session', 'file'} or len(form.getlist('session')) != 1
                    or len(form.getlist('file')) != 1 or not isinstance(form['file'], UploadFile)):
                raise ValueError(_INVALID)
            session = _handle(form['session'])
            result = await _upload_for_request(request, session, form['file'])
        return _response(result)
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
            if operation == 'pick-file':
                result = await _owned_for_request(
                    request, manager.pick_file, session,
                    cleanup=lambda _: manager.close_session(session), close_immediately=True)
            elif operation == 'start':
                result = await _owned_for_request(
                    request, manager.start, session, _handle(data['file']),
                    _options(data.get('options', {})),
                    cleanup=lambda result: manager.cancel(session, result['job']))
            elif operation in ('job', 'cancel'):
                result = await run_in_threadpool(getattr(manager, operation), session, _handle(data['job']))
            elif operation == 'page':
                page = data.get('page', 0)
                if type(page) is not int or page < 0:
                    raise ValueError(_INVALID)
                result = await _owned_for_request(
                    request, manager.page, session, _handle(data['job']), page,
                    cleanup=lambda _: None)
            else:
                result = await run_in_threadpool(manager.close_session, session)
        return _response(result)
    except Exception as error:
        return _error(error)


async def unknown(request: Request, unknown: str = ''):
    try:
        _boundary(request, mutation=request.method not in ('GET', 'HEAD'))
        raise ValueError(_INVALID)
    except Exception as error:
        return _error(error)


class _UnknownRoute(APIRoute):
    """Keep even unrecognized HTTP methods inside the local error boundary."""
    def matches(self, scope):
        match, child = super().matches(scope)
        return (Match.FULL if match is Match.PARTIAL else match), child

    async def handle(self, scope, receive, send):
        await self.app(scope, receive, send)


for _path in ('', '/{unknown:path}'):
    router.add_api_route(_path, unknown, methods=['GET'],
                         route_class_override=_UnknownRoute, include_in_schema=False)
