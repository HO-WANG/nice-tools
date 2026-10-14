"""Local database/file operations must reject foreign browser requests."""

import asyncio
import json
import threading

from fastapi.testclient import TestClient
import pytest

from nice_tools.main import app


def test_ase_api_requires_custom_header():
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post("/api/ase/connect", json={"password": "do-not-echo"})
    assert response.status_code == 403
    assert "do-not-echo" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_ase_api_rejects_foreign_origin_and_host():
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        for headers in (
            {"Origin": "https://foreign.example"},
            {"Origin": "http://127.0.0.1:9999"},
            {"Host": "foreign.example:8000"},
        ):
            response = client.post("/api/ase/connect", json={}, headers={"X-Nice-Tools": "ase", **headers})
            assert response.status_code == 403


def test_ase_validation_errors_never_echo_secrets():
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post("/api/ase/connect", json={
            "host": "server\n[bad]", "port": 5000, "user": "u", "password": "do-not-echo"
        }, headers={"X-Nice-Tools": "ase"})
    assert response.status_code == 400
    assert "do-not-echo" not in response.text


def test_ase_json_required():
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post("/api/ase/connect", data={"password": "do-not-echo"}, headers={"X-Nice-Tools": "ase"})
    assert response.status_code == 400
    assert "do-not-echo" not in response.text


@pytest.mark.parametrize("owner", [{}, {"owner_id": None}, {"owner_id": 7}], ids=["omitted", "all", "specific"])
def test_object_search_accepts_all_owners_and_forwards_search_filters(monkeypatch, owner):
    from nice_tools import ase_api

    calls = []

    def catalog(session, operation, **parameters):
        calls.append((session, operation, parameters))
        return {"objects": []}

    monkeypatch.setattr(ase_api.manager, "catalog", catalog)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post("/api/ase/objects", json={
            "session": "test-session", "database": "testdb", "search": "order_%", "kind": "V", **owner,
        }, headers={"X-Nice-Tools": "ase"})
    assert response.status_code == 200
    assert response.json() == {"objects": []}
    assert response.headers["cache-control"] == "no-store"
    assert calls == [("test-session", "objects", {
        "database": "testdb", "owner_id": owner.get("owner_id"), "search": "order_%", "kind": "V",
    })]


def test_object_search_defaults_to_all_kinds_and_empty_search(monkeypatch):
    from nice_tools import ase_api

    calls = []
    monkeypatch.setattr(ase_api.manager, "catalog", lambda *args, **parameters: calls.append(parameters) or {"objects": []})
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post("/api/ase/objects", json={"session": "test-session", "database": "testdb"},
                               headers={"X-Nice-Tools": "ase"})
    assert response.status_code == 200
    assert calls == [{"database": "testdb", "owner_id": None, "search": "", "kind": "all"}]


@pytest.mark.parametrize("invalid", [
    {"owner_id": value} for value in [True, False, 0, -1, 2147483648, "7", 1.5]
] + [
    {"search": value} for value in [None, 1, True, "bad\x00search", "x" * 256]
] + [
    {"kind": value} for value in [None, "", "u", "X", True, 1, []]
])
def test_object_search_rejects_invalid_filters_before_worker_creation(monkeypatch, invalid):
    from nice_tools import ase_api

    calls = []
    monkeypatch.setattr(ase_api.manager, "catalog", lambda *args, **parameters: calls.append(parameters) or {"objects": []})
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post("/api/ase/objects", json={
            "session": "test-session", "database": "testdb", "owner_id": 7, **invalid,
        }, headers={"X-Nice-Tools": "ase"})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert not calls


def test_profiles_endpoint_is_local_and_does_not_expose_passwords(tmp_path, monkeypatch):
    from nice_tools.jasypt import transform
    from nice_tools import ase_profiles

    cipher = transform({"operation": "encrypt", "text": "test-db-secret", "password": "nice1234!!", "wrap_enc": True})
    path = tmp_path / "ase-connections.txt"
    path.write_text(f"[dev]\nhost=localhost\nport=5000\nuser=test\npassword={cipher}\ndatabase=testdb\n", encoding="utf-8")
    monkeypatch.setattr(ase_profiles, "configuration_path", lambda: path)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get("/api/ase/profiles")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["profiles"][0]["host"] == "localhost"
        assert cipher not in response.text
        assert "test-db-secret" not in response.text
        assert "nice1234!!" not in response.text
        foreign = client.get("/api/ase/profiles", headers={"Origin": "https://foreign.example"})
        assert foreign.status_code == 403


def test_profile_connect_uses_fixed_key_without_user_key_input(tmp_path, monkeypatch):
    from nice_tools.jasypt import transform
    from nice_tools import ase_profiles, ase_api

    cipher = transform({"operation": "encrypt", "text": "test-db-secret", "password": "nice1234!!", "wrap_enc": True})
    path = tmp_path / "ase-connections.txt"
    path.write_text(f"[dev]\nhost=localhost\nport=5000\nuser=test\npassword={cipher}\ndatabase=testdb\n", encoding="utf-8")
    monkeypatch.setattr(ase_profiles, "configuration_path", lambda: path)
    received = []
    def connect(connection):
        received.append(connection)
        return {"session": "fixture-session", "databases": [{"id": 1, "name": "testdb"}]}
    monkeypatch.setattr(ase_api.manager, "connect", connect)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        profile_id = client.get("/api/ase/profiles").json()["profiles"][0]["id"]
        response = client.post("/api/ase/connect", json={"profile_id": profile_id}, headers={"X-Nice-Tools": "ase"})
    assert response.status_code == 200
    assert received[0]["password"] == "test-db-secret"
    assert response.json()["database"] == "testdb"
    assert response.json()["profile_name"] == "dev"
    assert "test-db-secret" not in response.text and "nice1234!!" not in response.text


class ConnectRequest:
    headers = {"host": "127.0.0.1:8000", "content-type": "application/json", "x-nice-tools": "ase"}

    def __init__(self, data, disconnected=False):
        self.data = data
        self.disconnected = disconnected

    async def json(self):
        return self.data

    async def is_disconnected(self):
        return self.disconnected


def connect_request(tmp_path, monkeypatch, profile, disconnected=False):
    data = {"host": "localhost", "port": 5000, "user": "test", "password": "test-db-secret"}
    from nice_tools import ase_profiles
    monkeypatch.setattr(ase_profiles, "configuration_path", lambda: tmp_path / "ase-connections.txt")
    if profile:
        from nice_tools import ase_profiles, jasypt

        cipher = jasypt.transform({"operation": "encrypt", "text": data["password"],
                                   "password": "nice1234!!", "wrap_enc": True})
        path = tmp_path / "ase-connections.txt"
        path.write_text(f"[dev]\nhost=localhost\nport=5000\nuser=test\npassword={cipher}\n", encoding="utf-8")
        monkeypatch.setattr(ase_profiles, "configuration_path", lambda: path)
        data = {"profile_id": ase_profiles.public_profiles()["profiles"][0]["id"]}
    return ConnectRequest(data, disconnected)


@pytest.mark.parametrize("profile", [False, True], ids=["manual", "profile"])
def test_cancelled_connect_cleans_session_created_after_request_ends(tmp_path, monkeypatch, profile):
    from nice_tools import ase_api

    request = connect_request(tmp_path, monkeypatch, profile)
    started, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    sessions = {"other-session": {"password": "unrelated-test-password"}}

    def connect(connection):
        started.set()
        assert release.wait(3)
        sessions["new-session"] = connection
        return {"session": "new-session", "databases": []}

    def disconnect(session):
        sessions.pop(session)
        cleaned.set()
        return {"ok": True}

    monkeypatch.setattr(ase_api.manager, "connect", connect)
    monkeypatch.setattr(ase_api.manager, "disconnect", disconnect)

    async def scenario():
        task = asyncio.create_task(ase_api.operation(request, "connect"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(cleaned.wait, 2), "late connection credentials were retained"
        assert set(sessions) == {"other-session"}

    asyncio.run(scenario())


@pytest.mark.parametrize("profile", [False, True], ids=["manual", "profile"])
def test_browser_disconnect_cleans_successful_connect_without_returning_session(tmp_path, monkeypatch, profile):
    from nice_tools import ase_api

    request = connect_request(tmp_path, monkeypatch, profile, disconnected=True)
    sessions = {}

    def connect(connection):
        sessions["new-session"] = connection
        return {"session": "new-session", "databases": []}

    monkeypatch.setattr(ase_api.manager, "connect", connect)
    monkeypatch.setattr(ase_api.manager, "disconnect", lambda session: sessions.pop(session))
    response = asyncio.run(ase_api.operation(request, "connect"))
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert not sessions
    body = response.body.decode()
    assert "new-session" not in body and "test-db-secret" not in body and "nice1234!!" not in body


@pytest.mark.parametrize("profile", [False, True], ids=["manual", "profile"])
def test_cancellation_during_disconnect_check_releases_connection(tmp_path, monkeypatch, profile):
    from nice_tools import ase_api

    request = connect_request(tmp_path, monkeypatch, profile)
    sessions = {}

    def connect(connection):
        sessions["new-session"] = connection
        return {"session": "new-session", "databases": []}

    monkeypatch.setattr(ase_api.manager, "connect", connect)
    monkeypatch.setattr(ase_api.manager, "disconnect", lambda session: sessions.pop(session))

    async def scenario():
        checking = asyncio.Event()

        async def check():
            checking.set()
            await asyncio.Event().wait()

        request.is_disconnected = check
        task = asyncio.create_task(ase_api.operation(request, "connect"))
        await asyncio.wait_for(checking.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not sessions

    asyncio.run(scenario())


@pytest.mark.parametrize("profile", [False, True], ids=["manual", "profile"])
def test_connect_errors_preserve_sanitized_validation_response(tmp_path, monkeypatch, profile):
    from nice_tools import ase_api

    request = connect_request(tmp_path, monkeypatch, profile)

    def fail(connection):
        raise ValueError("ASE 연결을 확인하세요.")

    def unexpected_disconnect(session):
        pytest.fail("a failed connection has no session to disconnect")

    monkeypatch.setattr(ase_api.manager, "connect", fail)
    monkeypatch.setattr(ase_api.manager, "disconnect", unexpected_disconnect)
    response = asyncio.run(ase_api.operation(request, "connect"))
    assert response.status_code == 400
    assert json.loads(response.body) == {"detail": "ASE 연결을 확인하세요."}
    assert response.headers["cache-control"] == "no-store"


def test_late_failed_connect_is_consumed_after_cancellation(tmp_path, monkeypatch):
    from nice_tools import ase_api

    request = connect_request(tmp_path, monkeypatch, False)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def fail(connection):
        started.set()
        assert release.wait(3)
        finished.set()
        raise ValueError("ASE 연결을 확인하세요.")

    monkeypatch.setattr(ase_api.manager, "connect", fail)
    run_in_threadpool = ase_api.run_in_threadpool

    async def scenario():
        loop_errors = []
        completed = asyncio.Event()

        async def observe_completion(*args, **kwargs):
            try:
                return await run_in_threadpool(*args, **kwargs)
            finally:
                completed.set()

        monkeypatch.setattr(ase_api, "run_in_threadpool", observe_completion)
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: loop_errors.append(context))
        task = asyncio.create_task(ase_api.operation(request, "connect"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.wait_for(completed.wait(), timeout=2)
        # The completed task's callbacks run on the next event-loop turn.
        await asyncio.sleep(0)
        assert not loop_errors

    asyncio.run(scenario())


def test_manual_connect_saves_only_successful_credentials_and_preselects_next_launch(tmp_path, monkeypatch):
    from nice_tools import ase_api, ase_profiles
    path = tmp_path / 'ase-connections.txt'
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: path)
    calls = []
    def connect(connection):
        calls.append(connection)
        assert not path.exists()
        return {'session': 'test-session', 'databases': []}
    monkeypatch.setattr(ase_api.manager, 'connect', connect)
    data = {'host': 'localhost', 'port': 5000, 'user': 'test', 'password': 'test-db-secret'}
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/ase/connect', json=data, headers={'X-Nice-Tools': 'ase'})
        assert response.status_code == 200
        result = response.json()
        assert result['saved'] is True
        public = client.get('/api/ase/profiles').json()
        assert public['last_profile_id'] == result['profile_id']
        assert ase_profiles.decrypt_connection(result['profile_id'])['connection']['password'] == data['password']
    assert set(calls[0]) == {'host', 'port', 'user', 'password'}
    assert data['password'] not in path.read_text() and 'nice1234!!' not in path.read_text()
    assert data['password'] not in response.text and 'nice1234!!' not in response.text


def test_manual_failed_login_does_not_write_connection_file(tmp_path, monkeypatch):
    from nice_tools import ase_api, ase_profiles
    path = tmp_path / 'ase-connections.txt'
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: path)
    def fail(connection):
        raise ValueError('접속 실패')
    monkeypatch.setattr(ase_api.manager, 'connect', fail)
    request = ConnectRequest({'host': 'localhost', 'port': 5000, 'user': 'test', 'password': 'db-secret'})
    response = asyncio.run(ase_api.operation(request, 'connect'))
    assert response.status_code == 400
    assert not path.exists()


@pytest.mark.parametrize("profile", [False, True])
def test_connect_rejects_supplied_master_key_and_never_attempts_connection(tmp_path, monkeypatch, profile):
    from nice_tools import ase_api
    request = connect_request(tmp_path, monkeypatch, profile)
    request.data['master_password'] = 'old-custom-key'
    calls = []
    monkeypatch.setattr(ase_api.manager, 'connect', lambda connection: calls.append(connection) or {'session': 'test'})
    response = asyncio.run(ase_api.operation(request, 'connect'))
    assert response.status_code == 400
    assert not calls
    assert 'old-custom-key' not in response.body.decode()


def test_successful_login_with_bad_config_keeps_session_and_warns_without_overwrite(tmp_path, monkeypatch):
    from nice_tools import ase_api, ase_profiles
    path = tmp_path / 'ase-connections.txt'
    original = b'[bad]\npassword=hidden-secret\n'
    path.write_bytes(original)
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: path)
    monkeypatch.setattr(ase_api.manager, 'connect', lambda connection: {'session': 'connected-session', 'databases': []})
    request = ConnectRequest({'host': 'localhost', 'port': 5000, 'user': 'test', 'password': 'db-secret'})
    response = asyncio.run(ase_api.operation(request, 'connect'))
    assert response.status_code == 200
    result = json.loads(response.body)
    assert result['session'] == 'connected-session' and result['saved'] is False
    assert result['save_warning']
    assert path.read_bytes() == original
    assert 'hidden-secret' not in response.body.decode()


def test_cancelled_connect_during_save_cleans_live_session(tmp_path, monkeypatch):
    from nice_tools import ase_api, ase_profiles
    request = connect_request(tmp_path, monkeypatch, False)
    saving, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    sessions = {}
    monkeypatch.setattr(ase_api.manager, 'connect', lambda connection: sessions.setdefault('live', connection) and {'session': 'live', 'databases': []})
    def disconnect(session):
        sessions.pop(session)
        cleaned.set()
    def slow_save(prepared):
        saving.set()
        assert release.wait(3)
        raise ValueError('save failed')
    monkeypatch.setattr(ase_api.manager, 'disconnect', disconnect)
    monkeypatch.setattr(ase_profiles, 'save_connection', slow_save)
    async def scenario():
        task = asyncio.create_task(ase_api.operation(request, 'connect'))
        try:
            assert await asyncio.to_thread(saving.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cleaned.is_set() and not sessions
        finally:
            release.set()
    asyncio.run(scenario())


def test_obsolete_key_profile_failure_does_not_connect_or_rewrite_file(tmp_path, monkeypatch):
    from nice_tools import ase_api, ase_profiles, jasypt
    path = tmp_path / 'ase-connections.txt'
    cipher = jasypt.transform({'operation': 'encrypt', 'text': 'old-db-secret', 'password': 'old-custom-key', 'wrap_enc': True})
    path.write_text(f'[old]\nhost=localhost\nport=5000\nuser=test\npassword={cipher}\n')
    original = path.read_bytes()
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: path)
    calls = []
    monkeypatch.setattr(ase_api.manager, 'connect', lambda connection: calls.append(connection) or {'session': 'unused'})
    profile_id = ase_profiles.public_profiles()['profiles'][0]['id']
    response = asyncio.run(ase_api.operation(ConnectRequest({'profile_id': profile_id}), 'connect'))
    assert response.status_code == 400
    assert not calls and path.read_bytes() == original
    assert '다시' in response.body.decode() and '저장' in response.body.decode()
    for secret in ('old-db-secret', 'old-custom-key', 'nice1234!!', cipher):
        assert secret not in response.body.decode()


@pytest.mark.parametrize('stage', ['late-connect', 'socket-gone', 'save-cancelled'])
def test_session_cleanup_runs_outside_asgi_event_loop(tmp_path, monkeypatch, stage):
    from nice_tools import ase_api, ase_profiles
    request = connect_request(tmp_path, monkeypatch, False, disconnected=stage == 'socket-gone')
    entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    disconnect_threads = []
    sessions = {}
    def connect(connection):
        if stage == 'late-connect':
            entered.set()
            assert release.wait(3)
        sessions['new-session'] = connection
        return {'session': 'new-session', 'databases': []}
    def save(prepared):
        entered.set()
        assert release.wait(3)
        raise ValueError('save aborted')
    def disconnect(session):
        disconnect_threads.append(threading.get_ident())
        sessions.pop(session)
        cleaned.set()
    monkeypatch.setattr(ase_api.manager, 'connect', connect)
    monkeypatch.setattr(ase_api.manager, 'disconnect', disconnect)
    if stage == 'save-cancelled':
        monkeypatch.setattr(ase_profiles, 'save_connection', save)
    async def scenario():
        asgi_thread = threading.get_ident()
        task = asyncio.create_task(ase_api.operation(request, 'connect'))
        try:
            if stage == 'socket-gone':
                response = await task
                assert response.status_code == 400
            else:
                assert await asyncio.to_thread(entered.wait, 2)
                task.cancel()
                if stage == 'late-connect':
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    release.set()
                else:
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            assert await asyncio.to_thread(cleaned.wait, 2)
            assert len(disconnect_threads) == 1
            assert disconnect_threads[0] != asgi_thread, 'ODBC close ran on the HTTP event loop'
            assert not sessions
        finally:
            release.set()
    asyncio.run(scenario())


def test_background_disconnect_failures_are_consumed_without_event_loop_errors(tmp_path, monkeypatch):
    from nice_tools import ase_api
    request = connect_request(tmp_path, monkeypatch, False)
    entered, release = threading.Event(), threading.Event()
    def connect(connection):
        entered.set()
        assert release.wait(3)
        return {'session': 'late-session', 'databases': []}
    def disconnect(session):
        raise RuntimeError('driver-error-containing-private-secret')
    monkeypatch.setattr(ase_api.manager, 'connect', connect)
    monkeypatch.setattr(ase_api.manager, 'disconnect', disconnect)
    run_in_threadpool = ase_api.run_in_threadpool
    async def scenario():
        loop_errors = []
        cleanup_returned = asyncio.Event()
        async def observe_worker(function, *args, **kwargs):
            try:
                return await run_in_threadpool(function, *args, **kwargs)
            finally:
                if function is disconnect:
                    cleanup_returned.set()
        monkeypatch.setattr(ase_api, 'run_in_threadpool', observe_worker)
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: loop_errors.append(context))
        task = asyncio.create_task(ase_api.operation(request, 'connect'))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        await asyncio.wait_for(cleanup_returned.wait(), timeout=2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not loop_errors
    asyncio.run(scenario())


def test_manual_dsn_connect_persists_and_saved_profile_forwards_same_dsn(tmp_path, monkeypatch):
    from nice_tools import ase_api, ase_profiles
    path = tmp_path / 'ase-connections.txt'
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: path)
    calls = []
    def connect(connection):
        calls.append(dict(connection))
        return {'session': 'test-session', 'databases': []}
    monkeypatch.setattr(ase_api.manager, 'connect', connect)
    data = {'host': 'ase-host', 'port': 5000, 'user': 'test', 'password': 'db-secret', 'dsn': ' My ASE DSN '}
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/ase/connect', json=data, headers={'X-Nice-Tools': 'ase'})
        assert response.status_code == 200
        saved = response.json()
        public = client.get('/api/ase/profiles').json()
        assert public['profiles'][0]['dsn'] == 'My ASE DSN'
        response = client.post('/api/ase/connect', json={'profile_id': saved['profile_id']}, headers={'X-Nice-Tools': 'ase'})
        assert response.status_code == 200
    assert [connection['dsn'] for connection in calls] == ['My ASE DSN', 'My ASE DSN']
    assert 'db-secret' not in path.read_text() and 'db-secret' not in response.text


def test_failed_dsn_login_never_persists_configuration(tmp_path, monkeypatch):
    from nice_tools import ase_api, ase_profiles
    path = tmp_path / 'ase-connections.txt'
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: path)
    calls = []
    def fail(connection):
        calls.append(connection)
        raise ValueError('ASE 접속 실패')
    monkeypatch.setattr(ase_api.manager, 'connect', fail)
    data = {'host': 'ase-host', 'port': 5000, 'user': 'test', 'password': 'db-secret', 'dsn': 'ASE DSN'}
    response = asyncio.run(ase_api.operation(ConnectRequest(data), 'connect'))
    assert response.status_code == 400 and calls[0]['dsn'] == 'ASE DSN'
    assert not path.exists()


@pytest.mark.parametrize('dsn', [None, 123, 'bad\tprivate-dsn', 'bad\x7fprivate-dsn', 'x' * 256])
def test_bad_api_dsn_cannot_start_driver_or_echo_secret(tmp_path, monkeypatch, dsn):
    from nice_tools import ase_api, ase_profiles
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: tmp_path / 'ase-connections.txt')
    calls = []
    monkeypatch.setattr(ase_api.manager, 'connect', lambda connection: calls.append(connection) or {'session': 'unused'})
    data = {'host': 'ase-host', 'port': 5000, 'user': 'test', 'password': 'db-secret', 'dsn': dsn}
    response = asyncio.run(ase_api.operation(ConnectRequest(data), 'connect'))
    assert response.status_code == 400 and not calls
    assert 'private-dsn' not in response.body.decode() and 'db-secret' not in response.body.decode()


@pytest.fixture
def sql_ini_boundary(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from nice_tools import ase_api, ase_profiles
    path = tmp_path / 'ase-connections.txt'
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: path)
    calls = []
    state = {'catalog': {'path': None, 'found': False, 'entries': [], 'detail': 'sql.ini를 찾지 못했습니다.'},
             'pick': {'cancelled': True}}
    def status(selected=None):
        calls.append(('status', selected))
        return dict(state['catalog'])
    def pick():
        calls.append(('pick',))
        return dict(state['pick'])
    monkeypatch.setitem(sys.modules, 'nice_tools.ase_sqlini', SimpleNamespace(sql_ini_status=status))
    monkeypatch.setattr(ase_api.manager, 'pick_sql_ini', pick, raising=False)
    return state, calls, path


def test_sql_ini_get_missing_file_is_read_only_local_and_no_store(sql_ini_boundary):
    state, calls, path = sql_ini_boundary
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.get('/api/ase/sql-ini')
        assert response.status_code == 200 and response.json() == state['catalog']
        assert response.headers['cache-control'] == 'no-store'
        for headers in ({'Origin': 'https://foreign.example'}, {'Host': 'foreign.example:8000'}):
            forbidden = client.get('/api/ase/sql-ini', headers=headers)
            assert forbidden.status_code == 403 and forbidden.headers['cache-control'] == 'no-store'
    assert calls == [('status', None)] and not path.exists()


def test_sql_ini_picker_cancel_does_not_read_or_save(sql_ini_boundary):
    state, calls, path = sql_ini_boundary
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/ase/pick-sql-ini', json={}, headers={'X-Nice-Tools': 'ase'})
    assert response.status_code == 200 and response.json() == {'cancelled': True}
    assert response.headers['cache-control'] == 'no-store'
    assert calls == [('pick',)] and not path.exists()


def test_picked_sql_ini_persists_path_only_even_if_entries_are_unsupported(sql_ini_boundary, tmp_path):
    from nice_tools import ase_profiles
    state, calls, path = sql_ini_boundary
    selected = str(tmp_path / 'sql.ini')
    state['pick'] = {'cancelled': False, 'path': selected}
    state['catalog'] = {'path': selected, 'found': True, 'entries': [{'id': 'unsupported-id', 'name': 'ASE_ALIAS', 'available': False, 'detail': '지원하지 않는 프로토콜입니다.'}], 'detail': ''}
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/ase/pick-sql-ini', json={}, headers={'X-Nice-Tools': 'ase'})
    assert response.status_code == 200
    result = response.json()
    assert result['saved'] is True and result['cancelled'] is False
    assert result['entries'] == state['catalog']['entries']
    assert ase_profiles.public_profiles()['clients']['sql_ini_path'] == selected
    assert 'ASE_ALIAS' not in path.read_text(encoding='utf-8')
    assert calls == [('pick',), ('status', selected)]


def test_invalid_picked_sql_ini_does_not_persist_path(sql_ini_boundary, tmp_path):
    state, calls, path = sql_ini_boundary
    selected = str(tmp_path / 'bad.ini')
    state['pick'] = {'cancelled': False, 'path': selected}
    state['catalog'] = {'path': selected, 'found': False, 'entries': [], 'detail': 'sql.ini를 읽지 못했습니다.'}
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/ase/pick-sql-ini', json={}, headers={'X-Nice-Tools': 'ase'})
    assert response.status_code == 200
    assert response.json()['saved'] is False and response.json()['found'] is False
    assert not path.exists()


def test_read_success_with_bad_profile_config_keeps_catalog_and_warns_without_overwrite(sql_ini_boundary, tmp_path):
    state, calls, path = sql_ini_boundary
    original = b'[bad]\npassword=private-existing-secret\n'
    path.write_bytes(original)
    selected = str(tmp_path / 'sql.ini')
    state['pick'] = {'cancelled': False, 'path': selected}
    state['catalog'] = {'path': selected, 'found': True, 'entries': [{'id': 'ok', 'name': 'ASE_ALIAS', 'host': 'ase-host', 'port': 5000, 'available': True, 'detail': ''}], 'detail': ''}
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/ase/pick-sql-ini', json={}, headers={'X-Nice-Tools': 'ase'})
    assert response.status_code == 200
    result = response.json()
    assert result['found'] and result['entries'] == state['catalog']['entries']
    assert result['saved'] is False and result['save_warning']
    assert path.read_bytes() == original and 'private-existing-secret' not in response.text


@pytest.mark.parametrize('payload', [{'path': 'private-local-path'}, {'file_text': 'private-file-content'}, {'password': 'private-password'}, []])
def test_sql_ini_picker_rejects_browser_supplied_paths_and_contents(sql_ini_boundary, payload):
    state, calls, path = sql_ini_boundary
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/ase/pick-sql-ini', json=payload, headers={'X-Nice-Tools': 'ase'})
    assert response.status_code == 400 and response.headers['cache-control'] == 'no-store'
    assert not calls and not path.exists()
    assert 'private-' not in response.text


def test_sql_ini_picker_requires_local_origin_header_and_json(sql_ini_boundary):
    state, calls, path = sql_ini_boundary
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        no_header = client.post('/api/ase/pick-sql-ini', json={})
        assert no_header.status_code == 403
        foreign = client.post('/api/ase/pick-sql-ini', json={}, headers={'X-Nice-Tools': 'ase', 'Origin': 'https://foreign.example'})
        assert foreign.status_code == 403
        form = client.post('/api/ase/pick-sql-ini', data={'path': 'private-local-path'}, headers={'X-Nice-Tools': 'ase'})
        assert form.status_code == 400
    assert not calls and not path.exists()


@pytest.mark.parametrize('disconnect_stage', ['after-picker', 'after-read'])
def test_disconnected_picker_request_does_not_persist_late_selection(sql_ini_boundary, tmp_path, disconnect_stage):
    from nice_tools import ase_api
    state, calls, path = sql_ini_boundary
    selected = str(tmp_path / 'sql.ini')
    state['pick'] = {'cancelled': False, 'path': selected}
    state['catalog'] = {'path': selected, 'found': True, 'entries': [{'id': 'ok', 'name': 'ASE_ALIAS', 'host': 'ase-host', 'port': 5000, 'available': True, 'detail': ''}], 'detail': ''}
    request = ConnectRequest({})
    checked = []
    async def disconnected():
        checked.append(True)
        return disconnect_stage == 'after-picker' or len(checked) == 2
    request.is_disconnected = disconnected
    response = asyncio.run(ase_api.operation(request, 'pick-sql-ini'))
    assert response.status_code == 400
    assert not path.exists()
    assert calls == ([('pick',)] if disconnect_stage == 'after-picker' else [('pick',), ('status', selected)])


def test_sql_ini_read_exceptions_never_echo_contents_or_private_errors(sql_ini_boundary, monkeypatch, tmp_path):
    import sys
    state, calls, path = sql_ini_boundary
    state['pick'] = {'cancelled': False, 'path': str(tmp_path / 'sql.ini')}
    def fail(selected=None):
        raise ValueError('private-file-line-password=private-secret')
    monkeypatch.setattr(sys.modules['nice_tools.ase_sqlini'], 'sql_ini_status', fail)
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        for response in (client.get('/api/ase/sql-ini'), client.post('/api/ase/pick-sql-ini', json={}, headers={'X-Nice-Tools': 'ase'})):
            assert response.status_code == 400
            assert 'private-' not in response.text and response.headers['cache-control'] == 'no-store'
    assert not path.exists()
