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


def test_profiles_endpoint_is_local_and_does_not_expose_passwords(tmp_path, monkeypatch):
    from nice_tools.jasypt import transform
    from nice_tools import ase_profiles

    cipher = transform({"operation": "encrypt", "text": "test-db-secret", "password": "test-master-secret", "wrap_enc": True})
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
        assert "test-master-secret" not in response.text
        foreign = client.get("/api/ase/profiles", headers={"Origin": "https://foreign.example"})
        assert foreign.status_code == 403


def test_profile_connect_decrypts_only_after_entering_key(tmp_path, monkeypatch):
    from nice_tools.jasypt import transform
    from nice_tools import ase_profiles, ase_api

    cipher = transform({"operation": "encrypt", "text": "test-db-secret", "password": "test-master-secret", "wrap_enc": True})
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
        for key in ("wrong-test-key", ""):
            response = client.post("/api/ase/connect", json={"profile_id": profile_id, "master_password": key}, headers={"X-Nice-Tools": "ase"})
            assert response.status_code == 400
            assert not received
            assert "wrong-test-key" not in response.text
        response = client.post("/api/ase/connect", json={"profile_id": profile_id, "master_password": "test-master-secret"}, headers={"X-Nice-Tools": "ase"})
        assert response.status_code == 200
        assert received[0]["password"] == "test-db-secret"
        assert response.json()["database"] == "testdb"
        assert response.json()["profile_name"] == "dev"
        assert "test-db-secret" not in response.text
        assert "test-master-secret" not in response.text


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
    if profile:
        from nice_tools import ase_profiles, jasypt

        cipher = jasypt.transform({"operation": "encrypt", "text": data["password"],
                                   "password": "test-master-secret", "wrap_enc": True})
        path = tmp_path / "ase-connections.txt"
        path.write_text(f"[dev]\nhost=localhost\nport=5000\nuser=test\npassword={cipher}\n", encoding="utf-8")
        monkeypatch.setattr(ase_profiles, "configuration_path", lambda: path)
        data = {"profile_id": ase_profiles.public_profiles()["profiles"][0]["id"],
                "master_password": "test-master-secret"}
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
    assert "new-session" not in body and "test-db-secret" not in body and "test-master-secret" not in body


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
