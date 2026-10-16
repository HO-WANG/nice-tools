import asyncio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nice_tools import iq_api

app = FastAPI()
app.include_router(iq_api.router)
HEAD = {'X-Nice-Tools': 'iq'}


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
