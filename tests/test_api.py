import importlib
import json

import pytest
from starlette.datastructures import UploadFile
from starlette.testclient import TestClient


@pytest.fixture
def api():
    try:
        return importlib.import_module("nice_tools.main")
    except ModuleNotFoundError:
        pytest.fail("비교 HTTP API가 아직 구현되지 않았습니다.")


@pytest.fixture
def client(api):
    with TestClient(api.app) as client:
        yield client


def form_data(left_paths=None, right_paths=None, **options):
    return {
        "left_paths": json.dumps(left_paths if left_paths is not None else ["sub/a.txt"]),
        "right_paths": json.dumps(right_paths if right_paths is not None else ["sub/a.txt"]),
        "sort_lines": "false", "ignore_whitespace": "false", "ignore_start": "0", "ignore_end": "0",
        **options,
    }


def pair(left=b"a\nb", right=b"a\nb"):
    return [("left_files", ("a.txt", left, "text/plain")), ("right_files", ("a.txt", right, "text/plain"))]


def test_health_and_comparison(client):
    assert client.get("/health").json() == {"status": "ok"}
    response = client.post("/api/compare", data=form_data(), files=pair())
    assert response.status_code == 200
    assert response.json()["results"] == [{"path": "sub/a.txt", "status": "same"}]
    assert response.json()["summary"]["total"] == 1


def test_all_options_reach_comparison(client):
    response = client.post("/api/compare", data=form_data(sort_lines="true", ignore_whitespace="true", ignore_start="1", ignore_end="1"), files=pair(b"Xa bY\nXcdY", b"QcdR\nQa bR"))
    assert response.status_code == 200
    assert response.json()["summary"]["same"] == 1


@pytest.mark.parametrize("path", ["../a", "/a", "sub/../a", "sub//a", "C:/a", "sub\\a", "", "a\u0000b", "./a"])
def test_unsafe_paths_rejected(client, path):
    response = client.post("/api/compare", data=form_data(left_paths=[path]), files=pair())
    assert response.status_code == 400


def test_duplicate_paths_rejected(client):
    response = client.post("/api/compare", data=form_data(left_paths=["a", "a"]), files=[*pair(), ("left_files", ("again.txt", b"x"))])
    assert response.status_code == 400


@pytest.mark.parametrize("options", [{"ignore_start": "-1"}, {"ignore_end": "1.5"}, {"ignore_start": "x"}, {"sort_lines": "yes"}, {"ignore_whitespace": "no"}])
def test_invalid_options_rejected(client, options):
    assert client.post("/api/compare", data=form_data(**options), files=pair()).status_code == 400


@pytest.mark.parametrize("value", ["broken", "{}", '["a", "b"]', '[4]'])
def test_bad_path_lists_rejected(client, value):
    assert client.post("/api/compare", data=form_data(left_paths=[] ) | {"left_paths": value}, files=pair()).status_code == 400


def test_empty_sides_rejected(client):
    assert client.post("/api/compare", data=form_data(left_paths=[], right_paths=[]), files=[("left_files", ("a", b"a"))]).status_code == 400


def test_wrong_content_type_rejected(client):
    assert client.post("/api/compare", json={"left": {}}).status_code == 400


def test_too_many_files(client):
    files = [("left_files", (f"f{i}", b"a")) for i in range(1000)] + [("right_files", ("f0", b"a"))]
    response = client.post("/api/compare", data=form_data(left_paths=[f"f{i}" for i in range(1000)], right_paths=["f0"]), files=files)
    assert response.status_code == 400


def test_large_file_rejected(client):
    assert client.post("/api/compare", data=form_data(), files=pair(b"a" * (10 * 1024 * 1024 + 1))).status_code == 413
    assert client.get("/health").status_code == 200


def test_declared_oversized_body_rejected(client):
    response = client.post("/api/compare", content=b"", headers={"Content-Length": str(50 * 1024 * 1024 + 1)})
    assert response.status_code == 413


def test_streaming_oversized_body_rejected(api):
    from nice_tools.limits import BodyLimitMiddleware
    prefix = b'--boundary\r\nContent-Disposition: form-data; name="left_files"; filename="a"\r\n\r\n'
    with TestClient(BodyLimitMiddleware(api.app, max_bytes=1024)) as client:
        response = client.post("/api/compare", content=iter([prefix, b"a" * 900, b"a" * 900]), headers={"Content-Type": "multipart/form-data; boundary=boundary"})
        assert response.status_code == 413
        assert client.get("/health").json() == {"status": "ok"}


def test_binary_is_per_file_error(client):
    response = client.post("/api/compare", data=form_data(), files=pair(b"\xff"))
    assert response.status_code == 200
    assert response.json()["summary"]["error"] == 1


@pytest.mark.parametrize("bad_option", [False, True])
def test_uploads_close_on_success_and_validation_error(client, monkeypatch, bad_option):
    closed = []
    original = UploadFile.close

    async def record_close(upload):
        await original(upload)
        closed.append(upload.file.closed)

    monkeypatch.setattr(UploadFile, "close", record_close)
    response = client.post("/api/compare", data=form_data(ignore_start="-1" if bad_option else "0"), files=pair())
    assert response.status_code == (400 if bad_option else 200)
    assert closed == [True, True]


def test_one_sided_results(client):
    response = client.post("/api/compare", data=form_data(left_paths=["a"], right_paths=["b"]), files=pair())
    assert response.status_code == 200
    assert response.json()["summary"]["left_only"] == 1
    assert response.json()["summary"]["right_only"] == 1
