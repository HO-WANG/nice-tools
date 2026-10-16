import asyncio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nice_tools import iq_api

app = FastAPI()
app.include_router(iq_api.router)
HEAD = {'X-Nice-Tools': 'iq'}


@pytest.fixture(autouse=True)
def isolated_profile_file(tmp_path, monkeypatch):
    from nice_tools import iq_profiles
    path = tmp_path / 'iq-connections.txt'
    monkeypatch.setattr(iq_profiles, 'configuration_path', lambda: path)
    return path


def test_status_is_available_without_driver(monkeypatch):
    monkeypatch.setattr(iq_api.manager, 'status', lambda: {'available': False, 'module_available': True, 'drivers': []})
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.get('/api/iq/status')
    assert response.status_code == 200 and response.json()['available'] is False
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('headers', [{}, {'X-Nice-Tools': 'ase'}, {**HEAD, 'Origin': 'https://foreign.example'}, {**HEAD, 'Host': 'foreign.example'}, {**HEAD, 'Origin': 'http://127.0.0.1:9999'}])
def test_rejects_foreign_requests(headers):
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/iq/connect', json={'password': 'secret-not-echoed'}, headers=headers)
    assert response.status_code == 403 and 'secret-not-echoed' not in response.text
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('operation,payload', [
    ('pick-directory', {'session': 's', 'path': 'secret-not-echoed'}),
    ('jobs', {'session': 's', 'sql': 'select 1', 'filename': 'a.txt', 'options': {'direction': 'in'}}),
    ('job', {'session': 's', 'job': 'j', 'password': 'secret-not-echoed'}),
    ('connect', {'dsn': 'secret-not-echoed'}),
    ('disconnect', {'session': []}),
])
def test_strict_input(operation, payload):
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/iq/' + operation, json=payload, headers=HEAD)
    assert response.status_code == 400 and 'secret-not-echoed' not in response.text


def test_routes_forward_owned_handles(monkeypatch):
    calls = []
    for name, result in [('connect', {'session': 's'}), ('pick_directory', {'cancelled': True}), ('start_export', {'job': 'j'}), ('job', {'state': 'running'}), ('cancel', {'state': 'cancelled'}), ('disconnect', {'ok': True})]:
        monkeypatch.setattr(iq_api.manager, name, lambda *args, name=name, result=result: calls.append((name,args)) or result)
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        for operation, payload in [('connect', {'host':'localhost','user':'u','password':'p'}), ('pick-directory', {'session':'s'}), ('jobs', {'session':'s','sql':'select 1','filename':'a.txt','options':{}}), ('job', {'session':'s','job':'j'}), ('cancel', {'session':'s','job':'j'}), ('disconnect', {'session':'s'})]:
            response=client.post('/api/iq/'+operation,json=payload,headers=HEAD)
            assert response.status_code == 200 and response.headers['cache-control']=='no-store'
    assert [name for name,args in calls] == ['connect','pick_directory','start_export','job','cancel','disconnect']
    assert calls[2][1] == ('s','select 1','a.txt',{})


def test_abandoned_connection_is_disconnected(monkeypatch):
    calls=[]
    monkeypatch.setattr(iq_api.manager,'connect',lambda data: {'session':'orphan'})
    monkeypatch.setattr(iq_api.manager,'disconnect',lambda token: calls.append(token))
    class Request:
        async def is_disconnected(self): return True
    async def run():
        with pytest.raises(ValueError): await iq_api._connect_for_request(Request(),{})
    asyncio.run(run())
    assert calls==['orphan']


def test_cancelled_request_cleans_eventual_connection(monkeypatch):
    import threading
    started,release,cleaned=threading.Event(),threading.Event(),threading.Event()
    sessions={'unrelated':{}}
    def connect(data):
        started.set()
        assert release.wait(3)
        sessions['late']=data
        return {'session':'late'}
    def disconnect(token):
        sessions.pop(token)
        cleaned.set()
    monkeypatch.setattr(iq_api.manager,'connect',connect)
    monkeypatch.setattr(iq_api.manager,'disconnect',disconnect)
    class Request:
        async def is_disconnected(self): return False
    async def run():
        task=asyncio.create_task(iq_api._connect_for_request(Request(),{'password':'private'}))
        try:
            assert await asyncio.to_thread(started.wait,2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        finally:
            release.set()
        assert await asyncio.to_thread(cleaned.wait,2)
    asyncio.run(run())
    assert sessions=={'unrelated':{}}


def test_profiles_route_lists_choices_without_secrets():
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.get('/api/iq/profiles')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['profiles'] == []


def test_successful_manual_connect_saves_cipher_and_reuses_saved_credentials(monkeypatch, isolated_profile_file):
    from nice_tools import iq_profiles
    calls = []
    monkeypatch.setattr(iq_api.manager, 'connect', lambda data: calls.append(data) or {'session': 'connected', 'driver': 'SAP IQ', 'version': 'SAP IQ 16'})
    payload = {'host': 'iq.internal', 'port': 2638, 'user': 'reader', 'password': 'profile-db-secret',
               'database': 'db1', 'server_name': 'engine1', 'driver': 'SAP IQ'}
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        created = client.post('/api/iq/connect', json=payload, headers=HEAD)
        assert created.status_code == 200 and created.json()['saved'] is True
        assert created.json()['profile_name'] == 'iq.internal-reader'
        raw = isolated_profile_file.read_text(encoding='utf-8')
        assert 'ENC(' in raw and payload['password'] not in raw
        profiles = client.get('/api/iq/profiles').json()
        assert len(profiles['profiles']) == 1 and profiles['last_profile_id'] == profiles['profiles'][0]['id']
        restored = client.post('/api/iq/connect', json={'profile_id': profiles['last_profile_id']}, headers=HEAD)
        assert restored.status_code == 200 and restored.json()['saved'] is True
        assert calls == [payload, payload]
        assert payload['password'] not in restored.text and 'ENC(' not in restored.text


def test_failed_login_does_not_save_connection(monkeypatch, isolated_profile_file):
    def fail(data): raise ValueError('IQ connect failed')
    monkeypatch.setattr(iq_api.manager, 'connect', fail)
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        result = client.post('/api/iq/connect', json={'host':'localhost','user':'reader','password':'private'},headers=HEAD)
    assert result.status_code == 400 and not isolated_profile_file.exists()


def test_save_failure_keeps_successful_session(monkeypatch):
    from nice_tools import iq_profiles
    disconnected=[]
    monkeypatch.setattr(iq_api.manager,'connect',lambda data: {'session':'valid'})
    monkeypatch.setattr(iq_api.manager,'disconnect',lambda token: disconnected.append(token))
    def fail(*args): raise ValueError(iq_profiles.SAVE_ERROR)
    monkeypatch.setattr(iq_profiles,'save_connection',fail)
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        result=client.post('/api/iq/connect',json={'host':'localhost','user':'reader','password':'private'},headers=HEAD)
    assert result.status_code==200 and result.json()['session']=='valid'
    assert result.json()['saved'] is False and result.json()['save_warning']==iq_profiles.SAVE_ERROR
    assert not disconnected and 'private' not in result.text


@pytest.mark.parametrize('payload',[{'profile_id':'stale','host':'secret'},{'profile_id':'stale','master_password':'secret'},{'profile_id':None}])
def test_saved_connect_rejects_mixed_or_invalid_fields(payload):
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        result=client.post('/api/iq/connect',json=payload,headers=HEAD)
    assert result.status_code==400 and 'secret' not in result.text


def test_profiles_reject_foreign_origin():
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        result=client.get('/api/iq/profiles',headers={'Origin':'https://foreign.example'})
    assert result.status_code==403 and result.headers['cache-control']=='no-store'


def test_abandoned_connect_does_not_save(monkeypatch):
    from nice_tools import iq_profiles
    saves=[]
    disconnected=[]
    monkeypatch.setattr(iq_api.manager,'connect',lambda data: {'session':'orphan'})
    monkeypatch.setattr(iq_api.manager,'disconnect',lambda token: disconnected.append(token))
    monkeypatch.setattr(iq_profiles,'save_connection',lambda prepared: saves.append(prepared))
    class Request:
        async def is_disconnected(self): return True
    async def run():
        with pytest.raises(ValueError):
            await iq_api._connect_and_save(Request(),{'host':'localhost','user':'reader','password':'private'})
    asyncio.run(run())
    assert disconnected==['orphan'] and not saves


def test_cancelled_connect_during_save_releases_session(monkeypatch):
    import threading
    from nice_tools import iq_profiles
    entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(iq_api.manager,'connect',lambda data: {'session':'valid'})
    def save(prepared):
        entered.set()
        assert release.wait(3)
        return {'saved':True}
    monkeypatch.setattr(iq_profiles,'save_connection',save)
    monkeypatch.setattr(iq_api.manager,'disconnect',lambda token: cleaned.set())
    class Request:
        async def is_disconnected(self): return False
    async def run():
        task=asyncio.create_task(iq_api._connect_and_save(Request(),{'host':'localhost','user':'reader','password':'private'}))
        try:
            assert await asyncio.to_thread(entered.wait,2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert await asyncio.to_thread(cleaned.wait,2)
        finally:
            release.set()
    asyncio.run(run())
