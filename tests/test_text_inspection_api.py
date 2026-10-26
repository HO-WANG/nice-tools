"""Hidden-character API validates local ownership without reading file content."""
import asyncio
import importlib
import io
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import FormData, UploadFile


HEADERS = {'X-Nice-Tools': 'text-inspection'}
PREFIX = '/api/text-inspection'


@pytest.fixture
def api():
    return importlib.import_module('nice_tools.text_inspection_api')


@pytest.fixture
def service(api, monkeypatch):
    calls = []
    results = {
        'status': {'native_available': False, 'page_size': 200},
        'create_session': {'session': 'session-1'},
        'pick_file': {'cancelled': True},
        'select_upload': {'cancelled': False, 'selection': {'token': 'file-1',
                          'path': 'uploaded.txt', 'name': 'uploaded.txt', 'size': 1}},
        'start': {'job': 'job-1'},
        'job': {'job': 'j', 'state': 'running', 'phase': 'scan', 'completed': 0,
                'total': 0, 'elapsed_seconds': 0},
        'cancel': {'job': 'j', 'state': 'cancelled', 'phase': 'scan', 'completed': 0,
                   'total': 0, 'elapsed_seconds': 0},
        'page': {'summary': {}, 'results': [], 'page': 0, 'page_size': 200,
                 'pages': 1, 'total': 0},
        'close_session': {'ok': True},
    }
    def method(name):
        def call(*args):
            calls.append((name, args))
            return results[name]
        return call
    manager = SimpleNamespace(**{name: method(name) for name in results})
    monkeypatch.setattr(api, 'manager', manager)
    return manager, calls, results


@pytest.fixture
def client(api, service):
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app, base_url='http://127.0.0.1:8000') as instance:
        yield instance


def post(client, operation, payload):
    return client.post(PREFIX + '/' + operation, json=payload, headers=HEADERS)


def test_status_is_local_no_store_without_custom_header(client, service):
    response = client.get(PREFIX + '/status')
    assert response.status_code == 200
    assert response.json() == service[2]['status']
    assert response.headers['cache-control'] == 'no-store'
    assert service[1] == [('status', ())]


def test_routes_forward_only_handles_options_and_unlimited_page_index(client, service):
    requests = [
        ('session', {}, 'create_session', ()),
        ('pick-file', {'session': 's'}, 'pick_file', ('s',)),
        ('start', {'session': 's', 'file': 'f', 'options': {
            'encoding': 'cp949', 'include_common_whitespace': True}}, 'start',
         ('s', 'f', {'encoding': 'cp949', 'include_common_whitespace': True})),
        ('start', {'session': 's', 'file': 'f'}, 'start', ('s', 'f', {})),
        ('job', {'session': 's', 'job': 'j'}, 'job', ('s', 'j')),
        ('cancel', {'session': 's', 'job': 'j'}, 'cancel', ('s', 'j')),
        ('page', {'session': 's', 'job': 'j'}, 'page', ('s', 'j', 0)),
        ('page', {'session': 's', 'job': 'j', 'page': 10**30}, 'page', ('s', 'j', 10**30)),
        ('close', {'session': 's'}, 'close_session', ('s',)),
    ]
    for route, payload, name, args in requests:
        response = post(client, route, payload)
        assert response.status_code == 200, response.text
        assert response.json() == service[2][name]
        assert response.headers['cache-control'] == 'no-store'
    assert service[1] == [(name, args) for _, _, name, args in requests]


@pytest.mark.parametrize('headers', [{}, {'X-Nice-Tools': 'ase'},
    {'X-Nice-Tools': 'Text-Inspection'}, {**HEADERS, 'Host': 'foreign.example'},
    {**HEADERS, 'Origin': 'https://127.0.0.1:8000'},
    {**HEADERS, 'Origin': 'http://127.0.0.1:9999'},
    {**HEADERS, 'Origin': 'http://foreign.example'},
    {**HEADERS, 'Origin': 'http://127.0.0.1:invalid'},
    {**HEADERS, 'Origin': 'http://[invalid'}])
@pytest.mark.parametrize('upload', [False, True])
def test_mutations_require_exact_header_and_local_origin(client, service, headers, upload):
    kwargs = {'files': {'file': ('a.txt', b'x')}, 'data': {'session': 's'}} if upload else {'json': {}}
    response = client.post(PREFIX + ('/upload' if upload else '/session'), headers=headers, **kwargs)
    assert response.status_code == 403
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


@pytest.mark.parametrize('headers', [{'Host': 'foreign.example'}, {'Origin': 'http://foreign.example'}])
def test_status_rejects_nonlocal_boundary(client, service, headers):
    response = client.get(PREFIX + '/status', headers=headers)
    assert response.status_code == 403
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


@pytest.mark.parametrize('method,path', [('get', ''), ('post', ''), ('get', '/unknown'),
    ('post', '/nested/unknown'), ('delete', '/session'), ('get', '/start'),
    ('put', '/upload'), ('head', '/status'), ('options', '/session'), ('trace', '/status'), ('propfind', '/status'), ('custom', '/unknown')])
def test_unknown_routes_methods_are_no_store_400(client, service, method, path):
    response = client.request(method, PREFIX + path, headers=HEADERS)
    assert response.status_code == 400
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


@pytest.mark.parametrize('operation,payload', [
    ('unknown', {}), ('session', {'path': '/private'}), ('session', []),
    ('pick-file', {'session': 's', 'path': '/private'}), ('pick-file', {}),
    ('start', {'session': 's'}), ('start', {'session': 's', 'file': '/private'}),
    ('start', {'session': 's', 'file': 'f', 'path': '/private'}),
    ('start', {'session': 's', 'file': 'f', 'options': None}),
    ('start', {'session': 's', 'file': 'f', 'options': {'path': '/private'}}),
    ('job', {'session': 's', 'job': 'j', 'password': '/private'}),
    ('cancel', {'session': [], 'job': 'j'}), ('close', {}),
    ('close', {'session': '/private'}), ('close', {'session': 'bad\nhandle'}),
    ('close', {'session': 'a' * 129}), ('close', {'session': ''}),
    ('page', {'session': 's', 'job': 'j', 'page': True}),
    ('page', {'session': 's', 'job': 'j', 'page': -1}),
    ('page', {'session': 's', 'job': 'j', 'page': 0.5}),
])
def test_strict_fields_reject_path_injection_before_manager(client, service, operation, payload):
    response = post(client, operation, payload)
    assert response.status_code == 400
    assert '/private' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


@pytest.mark.parametrize('options', [{'encoding': ''}, {'encoding': 'x' * 129},
    {'encoding': 'utf-8\n'}, {'encoding': 'utf-8\x00'}, {'encoding': 'utf-8\x85'},
    {'encoding': 'utf-8\u202e'}, {'encoding': None}, {'encoding': []},
    {'include_common_whitespace': 1}, {'include_common_whitespace': 'true'},
    {'include_common_whitespace': None}, {'strict_mode': 0}, {'strict_mode': 1},
    {'strict_mode': 'false'}, {'strict_mode': None}, {'strict_mode': []}])
def test_option_types_and_encoding_name_bounds_are_strict(client, service, options):
    assert post(client, 'start', {'session': 's', 'file': 'f', 'options': options}).status_code == 400
    assert not service[1]


@pytest.mark.parametrize('encoding', ['auto', 'utf-8', 'cp949', 'euc-kr', 'cp932',
    'cp1252', 'utf-16', 'utf-16-le', 'utf-16-be', 'utf-32', 'utf-32-le', 'utf-32-be',
    'UTF-8', 'MS949', 'euckr', 'ascii', 'latin-1', 'shift_jis', 'private-unknown-codec'])
def test_free_text_encoding_names_forward_for_core_validation(client, service, encoding):
    assert post(client, 'start', {'session': 's', 'file': 'f',
        'options': {'encoding': encoding}}).status_code == 200
    assert service[1] == [('start', ('s', 'f', {'encoding': encoding}))]


@pytest.mark.parametrize('content,content_type', [('{}', 'text/plain'),
    ('private-invalid-json', 'application/json'), ('[]', 'application/json')])
def test_only_json_except_upload(client, service, content, content_type):
    response = client.post(PREFIX + '/session', content=content,
        headers={**HEADERS, 'Content-Type': content_type})
    assert response.status_code == 400
    assert 'private-invalid-json' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


@pytest.mark.parametrize('error', [RuntimeError('/private/native secret-data'),
    OSError('/private/native secret-data'), ValueError('/private/native secret-data')])
def test_errors_never_echo_native_data(client, service, monkeypatch, error, caplog):
    def fail(*args):
        raise error
    monkeypatch.setattr(service[0], 'job', fail)
    response = post(client, 'job', {'session': 's', 'job': 'j'})
    assert response.status_code == 400
    assert 'secret-data' not in response.text + caplog.text
    assert '/private/native' not in response.text + caplog.text
    assert response.headers['cache-control'] == 'no-store'


def test_expired_session_has_safe_code(client, service, api, monkeypatch):
    def fail(*args):
        raise api.SessionExpired('private-session-detail')
    monkeypatch.setattr(service[0], 'job', fail)
    response = post(client, 'job', {'session': 's', 'job': 'j'})
    assert response.status_code == 404
    assert response.json()['code'] == 'session_expired'
    assert 'private-session-detail' not in response.text
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('code', ['changed', 'busy', 'encoding', 'inspection_cache'])
def test_known_error_codes_use_fixed_messages(client, service, api, monkeypatch, code):
    def fail(*args):
        error = api.ComparisonError('changed')
        error.code = code
        error.args = ('private-source-secret',)
        raise error
    monkeypatch.setattr(service[0], 'job', fail)
    response = post(client, 'job', {'session': 's', 'job': 'j'})
    assert response.status_code == 400
    assert response.json()['code'] == code
    assert 'private-source-secret' not in response.text
    assert response.headers['cache-control'] == 'no-store'


def test_unknown_error_code_is_not_reflected(client, service, api, monkeypatch):
    def fail(*args):
        error = api.ComparisonError('changed')
        error.code = 'private-error-code'
        raise error
    monkeypatch.setattr(service[0], 'job', fail)
    response = post(client, 'job', {'session': 's', 'job': 'j'})
    assert response.status_code == 400
    assert 'private-error-code' not in response.text
    assert 'code' not in response.json()


def test_invalid_codec_has_fixed_actionable_option_guidance(client,service,api,monkeypatch):
    from nice_tools.text_inspection_engine import InspectionError
    def fail(*args):
        error=InspectionError('options');error.args=('private-codec-value',);raise error
    monkeypatch.setattr(service[0],'start',fail)
    response=post(client,'start',{'session':'s','file':'f','options':{'encoding':'private-codec-value'}})
    assert response.status_code==400
    assert response.json().get('code')=='options' and '인코딩' in response.json()['detail']
    assert 'private-codec-value' not in response.text


def test_upload_streams_complete_large_file_and_closes_handle(client, service, monkeypatch):
    payload = b'a' * (10 * 1024 * 1024 + 1) + b'last-byte'
    observed = []
    def select(session, source, filename):
        assert not source.closed
        observed.append((session, source, filename, source.read()))
        return service[2]['select_upload']
    monkeypatch.setattr(service[0], 'select_upload', select)
    response = client.post(PREFIX + '/upload', data={'session': 's'},
        files={'file': ('uploaded.txt', payload)}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() == service[2]['select_upload']
    assert response.headers['cache-control'] == 'no-store'
    assert observed[0][0] == 's' and observed[0][2:] == ('uploaded.txt', payload)
    assert observed[0][1].closed


@pytest.mark.parametrize('data,files', [
    ({'session': 's'}, [('file', ('a', b'x')), ('file', ('b', b'y'))]),
    ({'session': 's'}, [('other', ('a', b'x'))]),
    ({'session': 's', 'path': '/private'}, [('file', ('a', b'x'))]),
    ({}, [('file', ('a', b'x'))]),
    ({'session': '/private'}, [('file', ('a', b'x'))]),
    ({'session': 's', 'file': 'text'}, [('other', ('a', b'x'))]),
    ({}, [('session', ('s', b's')), ('file', ('a', b'x'))]),
    ({}, [('session', (None, 's')), ('session', (None, 't')), ('file', ('a', b'x'))]),
])
def test_multipart_accepts_exactly_one_session_and_one_file(client, service, data, files):
    response = client.post(PREFIX + '/upload', data=data, files=files, headers=HEADERS)
    assert response.status_code == 400
    assert response.headers['cache-control'] == 'no-store'
    assert '/private' not in response.text
    assert all(name != 'select_upload' for name, _ in service[1])


def test_upload_requires_multipart(client, service):
    response = post(client, 'upload', {'session': 's', 'file': '/private'})
    assert response.status_code == 400
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


def test_native_windows_rejects_manual_upload(client, service):
    service[2]['status']['native_available'] = True
    response = client.post(PREFIX + '/upload', data={'session': 's'},
        files={'file': ('a.txt', b'x')}, headers=HEADERS)
    assert response.status_code == 400
    assert response.headers['cache-control'] == 'no-store'
    assert service[1] == [('status', ())]


class JsonRequest:
    headers = {'host': '127.0.0.1:8000', 'x-nice-tools': 'text-inspection',
               'content-type': 'application/json'}
    def __init__(self, data, disconnected=False):
        self.data, self.disconnected = data, disconnected
    async def json(self):
        return self.data
    async def is_disconnected(self):
        return self.disconnected


@pytest.mark.parametrize('operation,payload,method,result,cleanup,cleanup_args', [
    ('session', {}, 'create_session', {'session': 'late'}, 'close_session', ('late',)),
    ('pick-file', {'session': 's'}, 'pick_file',
     {'cancelled': False, 'selection': {'token': 'late'}}, 'close_session', ('s',)),
    ('start', {'session': 's', 'file': 'f'}, 'start',
     {'job': 'late'}, 'cancel', ('s', 'late')),
])
@pytest.mark.parametrize('cancel_request', [False, True])
def test_abandoning_new_resources_cleans_eventual_results(api, service, monkeypatch,
        operation, payload, method, result, cleanup, cleanup_args, cancel_request):
    started, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    cleanups = []
    def work(*args):
        started.set()
        assert release.wait(3)
        return result
    def dispose(*args):
        cleanups.append(args)
        cleaned.set()
        return {'ok': True}
    monkeypatch.setattr(service[0], method, work)
    monkeypatch.setattr(service[0], cleanup, dispose)
    async def run():
        task = asyncio.create_task(api.operation(JsonRequest(payload, not cancel_request), operation))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            if cancel_request:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                if operation == 'pick-file':
                    assert cleaned.is_set()
            else:
                release.set()
                assert (await task).status_code == 400
        finally:
            release.set()
        assert await asyncio.to_thread(cleaned.wait, 2)
    asyncio.run(run())
    assert cleanups == [cleanup_args]


@pytest.mark.parametrize('cancel_request', [False, True])
def test_abandoned_page_preserves_session_job_and_cache(api, service, monkeypatch, cancel_request):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    cleanups = []
    def page(*args):
        started.set()
        assert release.wait(3)
        finished.set()
        return service[2]['page']
    monkeypatch.setattr(service[0], 'page', page)
    monkeypatch.setattr(service[0], 'close_session', lambda *args: cleanups.append(args))
    monkeypatch.setattr(service[0], 'cancel', lambda *args: cleanups.append(args))
    async def run():
        task = asyncio.create_task(api.operation(JsonRequest({'session': 's', 'job': 'j'},
                                                 not cancel_request), 'page'))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            if cancel_request:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                assert (await task).status_code == 400
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert (await api.operation(JsonRequest({'session': 's', 'job': 'j'}), 'job')).status_code == 200
        assert (await api.operation(JsonRequest({'session': 's', 'job': 'j', 'page': 1}), 'page')).status_code == 200
    asyncio.run(run())
    assert cleanups == []


@pytest.mark.parametrize('cancel_request', [False, True])
def test_upload_abandonment_closes_session_but_waits_for_reader_before_form_close(
        api, service, monkeypatch, cancel_request):
    started, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    source = io.BytesIO(b'complete-file')
    upload = UploadFile(source, filename='private-file.txt')
    observed = []
    def select(*args):
        started.set()
        assert release.wait(3)
        observed.append(source.read())
        return service[2]['select_upload']
    def close(*args):
        cleaned.set()
        return {'ok': True}
    monkeypatch.setattr(service[0], 'select_upload', select)
    monkeypatch.setattr(service[0], 'close_session', close)
    class Form:
        async def __aenter__(self):
            return FormData([('session', 's'), ('file', upload)])
        async def __aexit__(self, *args):
            await upload.close()
    class Request(JsonRequest):
        headers = {**JsonRequest.headers, 'content-type': 'multipart/form-data; boundary=test'}
        def form(self, **kwargs):
            assert kwargs['max_files'] == 1 and kwargs['max_fields'] == 1
            return Form()
    async def run():
        task = asyncio.create_task(api.upload(Request({}, not cancel_request)))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            if cancel_request:
                task.cancel()
                assert await asyncio.to_thread(cleaned.wait, 2)
                assert not source.closed and not task.done()
                # A second cancellation must not close the source underneath its reader.
                task.cancel()
                await asyncio.sleep(0)
                assert not source.closed and not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                assert (await task).status_code == 400
                assert cleaned.is_set()
        finally:
            release.set()
        assert source.closed
    asyncio.run(run())
    assert observed == [b'complete-file']


def test_disconnect_probe_failure_releases_session(api, service, monkeypatch):
    cleaned = []
    monkeypatch.setattr(service[0], 'close_session', lambda session: cleaned.append(session))
    class Request(JsonRequest):
        async def is_disconnected(self):
            raise RuntimeError('private-disconnect-error')
    response = asyncio.run(api.operation(Request({}), 'session'))
    assert response.status_code == 400
    assert 'private-disconnect-error' not in response.body.decode()
    assert cleaned == ['session-1']


@pytest.mark.parametrize('strict', [False, True])
def test_strict_mode_bool_is_forwarded(client, service, strict):
    response = post(client, 'start', {'session': 's', 'file': 'f',
        'options': {'strict_mode': strict, 'encoding': 'MS949'}})
    assert response.status_code == 200
    assert service[1] == [('start', ('s', 'f', {'strict_mode': strict, 'encoding': 'MS949'}))]


def test_unknown_codec_failure_remains_static_and_secret_free(client, service, monkeypatch):
    def fail(*args):
        raise ValueError('private-unknown-codec')
    monkeypatch.setattr(service[0], 'start', fail)
    response = post(client, 'start', {'session': 's', 'file': 'f',
        'options': {'encoding': 'private-unknown-codec'}})
    assert response.status_code == 400
    assert 'private-unknown-codec' not in response.text
    assert response.headers['cache-control'] == 'no-store'


def test_main_router_owned_token_runs_real_worker_and_preserves_source(api, monkeypatch, tmp_path):
    from nice_tools import main
    from nice_tools.text_inspection_jobs import InspectionManager
    from nice_tools.text_inspection_worker import file_descriptor
    source = tmp_path / 'source.txt'
    original = 'prefix\u200bsuffix\r\n'.encode('utf-8')
    source.write_bytes(original)
    manager = InspectionManager()
    monkeypatch.setattr(api, 'manager', manager)
    monkeypatch.setattr(main, 'inspection_manager', manager)
    monkeypatch.setattr(manager, '_choose_file', lambda session: {
        'cancelled': False, 'source': file_descriptor(source)})
    try:
        with TestClient(main.app, base_url='http://127.0.0.1:8000') as client:
            session = post(client, 'session', {}).json()['session']
            selection = post(client, 'pick-file', {'session': session}).json()['selection']
            response = post(client, 'start', {'session': session, 'file': selection['token'],
                'options': {'encoding': 'UTF-8', 'strict_mode': False}})
            assert response.status_code == 200, response.text
            job = response.json()['job']
            deadline = time.monotonic() + 8
            while True:
                response = post(client, 'job', {'session': session, 'job': job})
                assert response.status_code == 200
                status = response.json()
                if status['state'] in ('succeeded', 'failed', 'cancelled'):
                    break
                assert time.monotonic() < deadline, status
                time.sleep(.01)
            assert status['state'] == 'succeeded', status
            response = post(client, 'page', {'session': session, 'job': job})
            assert response.status_code == 200, response.text
            assert response.headers['cache-control'] == 'no-store'
            assert [row['codepoint'] for row in response.json()['results']] == ['U+200B']
            assert source.read_bytes() == original
            assert post(client, 'close', {'session': session}).json() == {'ok': True}
            assert not manager.processes
    finally:
        manager.close()
