"""The native comparison boundary accepts owned handles, never native paths."""
import asyncio
import importlib
import threading
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


HEADERS = {'X-Nice-Tools': 'comparison'}


@pytest.fixture
def api():
    return importlib.import_module('nice_tools.local_compare_api')


@pytest.fixture
def service(api, monkeypatch):
    calls = []
    results = {
        'status': {'native_available': False, 'file_limit': 1000, 'diff_page_size': 200},
        'create_session': {'session': 'session-1'},
        'pick_directory': {'cancelled': True},
        'start': {'job': 'job-1'},
        'job': {'state': 'running', 'progress': {}},
        'cancel': {'state': 'cancelled'},
        'diff': {'lines': [], 'page': 0, 'page_size': 200},
        'close_session': {'ok': True},
    }
    def function(name):
        def call(*args):
            calls.append((name, args))
            return results[name]
        return call
    manager = SimpleNamespace(**{name: function(name) for name in results})
    monkeypatch.setattr(api, 'manager', manager)
    return manager, calls


@pytest.fixture
def client(api, service):
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app, base_url='http://127.0.0.1:8000') as instance:
        yield instance


def test_routes_forward_only_owned_handles_and_options(client, service):
    operations = [
        ('session', {}, {'session': 'session-1'}),
        ('pick-directory', {'session': 's', 'side': 'left'}, {'cancelled': True}),
        ('start', {'session': 's', 'left': 'l', 'right': 'r',
                   'options': {'sort_lines': True, 'ignore_whitespace': False,
                               'ignore_start': 1, 'ignore_end': 2}}, {'job': 'job-1'}),
        ('job', {'session': 's', 'job': 'j'}, {'state': 'running', 'progress': {}}),
        ('cancel', {'session': 's', 'job': 'j'}, {'state': 'cancelled'}),
        ('diff', {'session': 's', 'job': 'j', 'path': '하위/a.txt'},
         {'lines': [], 'page': 0, 'page_size': 200}),
        ('close', {'session': 's'}, {'ok': True}),
    ]
    for operation, payload, result in operations:
        response = client.post('/api/comparison/' + operation, json=payload, headers=HEADERS)
        assert response.status_code == 200, response.text
        assert response.json() == result
        assert response.headers['cache-control'] == 'no-store'
    assert service[1] == [
        ('create_session', ()), ('pick_directory', ('s', 'left')),
        ('start', ('s', 'l', 'r', {'sort_lines': True, 'ignore_whitespace': False,
                                 'ignore_start': 1, 'ignore_end': 2})),
        ('job', ('s', 'j')), ('cancel', ('s', 'j')),
        ('diff', ('s', 'j', '하위/a.txt', 'compared', 0)), ('close_session', ('s',)),
    ]


def test_status_works_without_custom_header_and_sets_no_store(client, service):
    response = client.get('/api/comparison/status')
    assert response.status_code == 200
    assert response.json() == {'native_available': False, 'file_limit': 1000, 'diff_page_size': 200}
    assert response.headers['cache-control'] == 'no-store'
    assert service[1] == [('status', ())]


@pytest.mark.parametrize('headers', [{}, {'X-Nice-Tools': 'ase'},
    {'X-Nice-Tools': 'Comparison'}, {**HEADERS, 'Host': 'foreign.example'},
    {**HEADERS, 'Origin': 'https://127.0.0.1:8000'},
    {**HEADERS, 'Origin': 'http://127.0.0.1:9999'},
    {**HEADERS, 'Origin': 'http://foreign.example'}])
def test_mutation_boundary_rejects_foreign_requests(client, service, headers):
    response = client.post('/api/comparison/session', json={}, headers=headers)
    assert response.status_code == 403
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


@pytest.mark.parametrize('headers', [{'Host': 'foreign.example'}, {'Origin': 'http://foreign.example'}])
def test_status_retains_loopback_origin_boundary(client, service, headers):
    response = client.get('/api/comparison/status', headers=headers)
    assert response.status_code == 403
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


@pytest.mark.parametrize('method,path', [('get', ''), ('post', ''), ('get', '/unknown'),
    ('post', '/nested/unknown'), ('delete', '/session'), ('get', '/start')])
def test_unknown_routes_and_methods_are_no_store_400(client, service, method, path):
    response = getattr(client, method)('/api/comparison' + path, headers=HEADERS)
    assert response.status_code == 400
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


@pytest.mark.parametrize('operation,payload', [
    ('unknown', {}), ('session', {'path': '/private'}), ('session', []),
    ('pick-directory', {'session': 's', 'side': 'both'}),
    ('pick-directory', {'session': 's', 'side': 'left', 'path': '/private'}),
    ('start', {'session': 's', 'left': 'l'}),
    ('start', {'session': 's', 'left': 'l', 'right': 'r', 'left_path': '/private'}),
    ('start', {'session': 's', 'left': 'l', 'right': 'r', 'options': {'path': '/private'}}),
    ('start', {'session': 's', 'left': 'l', 'right': 'r', 'options': None}),
    ('job', {'session': 's', 'job': 'j', 'password': '/private'}),
    ('cancel', {'session': [], 'job': 'j'}), ('close', {}),
    ('close', {'session': '/private'}), ('close', {'session': 'bad\nhandle'}),
    ('close', {'session': 'a' * 129}), ('close', {'session': ''}),
    ('diff', {'session': 's', 'job': 'j', 'path': 'a', 'mode': 'raw'}),
    ('diff', {'session': 's', 'job': 'j', 'path': 'a', 'page': True}),
    ('diff', {'session': 's', 'job': 'j', 'path': 'a', 'page': -1}),
    ('diff', {'session': 's', 'job': 'j', 'path': 'a', 'page': 0.5}),
    ('diff', {'session': 's', 'job': 'j', 'path': 'a', 'analyze': 1}),
    ('diff', {'session': 's', 'job': 'j', 'path': 'a', 'analyze': 'true'}),
    ('diff', {'session': 's', 'job': 'j', 'path': 'a', 'analyze': None}),
])
def test_rejects_missing_unknown_or_invalid_fields(client, service, operation, payload):
    response = client.post('/api/comparison/' + operation, json=payload, headers=HEADERS)
    assert response.status_code == 400
    assert '/private' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


@pytest.mark.parametrize('path', ['', '/a', '../a', 'a/../b', './a', 'a//b', 'a/',
    'C:/a', 'C:a', 'a\\b', '\\server\\share', 'a\x00b', 'a\nb', 'a\x7fb'])
def test_diff_rejects_paths_before_calling_manager(client, service, path):
    response = client.post('/api/comparison/diff',
                           json={'session': 's', 'job': 'j', 'path': path}, headers=HEADERS)
    assert response.status_code == 400
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


@pytest.mark.skipif(sys.platform == 'win32', reason='Colon is not a Windows filename character')
def test_uploaded_posix_filename_can_be_used_for_report_lookup(client, service):
    response = client.post('/api/comparison/diff', json={
        'session': 's', 'job': 'j', 'path': 'sub/foo:bar.txt'}, headers=HEADERS)
    assert response.status_code == 200
    assert service[1] == [('diff', ('s', 'j', 'sub/foo:bar.txt', 'compared', 0))]


def test_explicit_analysis_is_forwarded_only_for_true(client, service):
    response = client.post('/api/comparison/diff', json={
        'session': 's', 'job': 'j', 'path': 'data.txt', 'analyze': True}, headers=HEADERS)
    assert response.status_code == 200
    assert service[1] == [('diff', ('s', 'j', 'data.txt', 'compared', 0, True))]


@pytest.mark.parametrize('options', [{'sort_lines': 1}, {'sort_lines': 'true'},
    {'ignore_whitespace': None}, {'ignore_start': True}, {'ignore_start': -1},
    {'ignore_end': 1.5}, {'ignore_end': '2'}])
def test_option_types_and_ranges_are_strict(client, service, options):
    response = client.post('/api/comparison/start',
                           json={'session': 's', 'left': 'l', 'right': 'r', 'options': options},
                           headers=HEADERS)
    assert response.status_code == 400
    assert not service[1]


def test_optional_defaults_and_original_diff_page(client, service):
    assert client.post('/api/comparison/start', json={'session': 's', 'left': 'l', 'right': 'r'},
                       headers=HEADERS).status_code == 200
    response = client.post('/api/comparison/diff', headers=HEADERS,
                           json={'session': 's', 'job': 'j', 'path': 'a', 'mode': 'original', 'page': 7})
    assert response.status_code == 200
    assert service[1] == [('start', ('s', 'l', 'r', {})),
                          ('diff', ('s', 'j', 'a', 'original', 7))]


@pytest.mark.parametrize('content,content_type', [('{}', 'text/plain'),
    ('private-invalid-json', 'application/json'), ('[]', 'application/json')])
def test_malformed_or_non_json_requests_are_sanitized(client, service, content, content_type):
    response = client.post('/api/comparison/session', content=content,
                           headers={**HEADERS, 'Content-Type': content_type})
    assert response.status_code == 400
    assert 'private-invalid-json' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert not service[1]


@pytest.mark.parametrize('error', [RuntimeError('/private/native secret-data'),
    OSError('/private/native secret-data'), ValueError('/private/native secret-data')])
def test_manager_failures_do_not_echo_native_details(client, service, monkeypatch, error, caplog):
    def fail(*args):
        raise error
    monkeypatch.setattr(service[0], 'job', fail)
    response = client.post('/api/comparison/job', json={'session': 's', 'job': 'j'}, headers=HEADERS)
    assert response.status_code == 400
    assert 'secret-data' not in response.text + caplog.text
    assert '/private/native' not in response.text + caplog.text
    assert response.headers['cache-control'] == 'no-store'


def test_expired_session_has_machine_readable_code(client, service, api, monkeypatch):
    def expired(*args):
        raise api.SessionExpired('private-session-detail')
    monkeypatch.setattr(service[0], 'job', expired)
    response = client.post('/api/comparison/job', json={'session': 's', 'job': 'j'}, headers=HEADERS)
    assert response.status_code == 404
    assert response.json()['code'] == 'session_expired'
    assert 'private-session-detail' not in response.text
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('operation,payload,method,result,cleanup,cleanup_args', [
    ('session', {}, 'create_session', {'session': 'late'}, 'close_session', ('late',)),
    ('pick-directory', {'session': 's', 'side': 'left'}, 'pick_directory',
     {'cancelled': False, 'selection': {'token': 'late'}}, 'close_session', ('s',)),
    ('start', {'session': 's', 'left': 'l', 'right': 'r'}, 'start',
     {'job': 'late'}, 'cancel', ('s', 'late')),
])
@pytest.mark.parametrize('cancel_request', [False, True])
def test_abandoned_request_cleans_owned_resource(api, service, monkeypatch, operation, payload,
        method, result, cleanup, cleanup_args, cancel_request):
    started, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    cleanup_calls = []
    def work(*args):
        started.set()
        assert release.wait(3)
        return result
    def dispose(*args):
        cleanup_calls.append(args)
        cleaned.set()
        return {'ok': True}
    monkeypatch.setattr(service[0], method, work)
    monkeypatch.setattr(service[0], cleanup, dispose)
    class Request:
        headers = {'host': '127.0.0.1:8000', 'x-nice-tools': 'comparison',
                   'content-type': 'application/json'}
        async def json(self):
            return payload
        async def is_disconnected(self):
            return not cancel_request
    async def run():
        task = asyncio.create_task(api.operation(Request(), operation))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            if cancel_request:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                if operation == 'pick-directory':
                    # Closing a session must interrupt its owned native work;
                    # waiting for that work first would deadlock cleanup.
                    assert cleaned.is_set()
            else:
                release.set()
                response = await task
                assert response.status_code == 400
        finally:
            release.set()
        assert await asyncio.to_thread(cleaned.wait, 2)
    asyncio.run(run())
    assert cleanup_calls == [cleanup_args]


@pytest.mark.parametrize('cancel_request', [False, True])
def test_abandoned_diff_keeps_session_and_job_usable(api, service, monkeypatch, cancel_request):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    sessions = {'s'}
    def diff(*args):
        started.set()
        assert release.wait(3)
        assert 's' in sessions
        finished.set()
        return {'lines': [], 'page_size': 200}
    def close(session):
        sessions.discard(session)
    def job(session, handle):
        if session not in sessions:
            raise api.SessionExpired('closed')
        return {'state': 'completed'}
    monkeypatch.setattr(service[0], 'diff', diff)
    monkeypatch.setattr(service[0], 'close_session', close)
    monkeypatch.setattr(service[0], 'job', job)
    class Request:
        headers = {'host': '127.0.0.1:8000', 'x-nice-tools': 'comparison',
                   'content-type': 'application/json'}
        def __init__(self, payload, disconnected=False):
            self.payload, self.disconnected = payload, disconnected
        async def json(self):
            return self.payload
        async def is_disconnected(self):
            return self.disconnected
    async def run():
        task = asyncio.create_task(api.operation(
            Request({'session': 's', 'job': 'j', 'path': 'a'}, not cancel_request), 'diff'))
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
        response = await api.operation(Request({'session': 's', 'job': 'j'}), 'job')
        assert response.status_code == 200
        # A new mode/page request can reuse this same job's owned cache.
        response = await api.operation(Request({'session': 's', 'job': 'j', 'path': 'a',
                                                'mode': 'original', 'page': 1}), 'diff')
        assert response.status_code == 200
    asyncio.run(run())
    assert sessions == {'s'}


def test_disconnect_probe_failure_releases_new_session(api, service, monkeypatch):
    cleaned = []
    monkeypatch.setattr(service[0], 'create_session', lambda: {'session': 'new'})
    monkeypatch.setattr(service[0], 'close_session', lambda session: cleaned.append(session))
    class Request:
        headers = {'host': '127.0.0.1:8000', 'x-nice-tools': 'comparison',
                   'content-type': 'application/json'}
        async def json(self):
            return {}
        async def is_disconnected(self):
            raise RuntimeError('native-private-data')
    response = asyncio.run(api.operation(Request(), 'session'))
    assert response.status_code == 400
    assert 'native-private-data' not in response.body.decode()
    assert cleaned == ['new']
