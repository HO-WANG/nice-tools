"""Local database/file operations must reject foreign browser requests."""

from fastapi.testclient import TestClient

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
