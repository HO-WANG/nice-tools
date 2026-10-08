from starlette.testclient import TestClient

from nice_tools.main import app


def test_tool_screen_and_controls():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert 'lang="ko"' in response.text
        for identifier in ("sidebar-toggle", "left-folder", "right-folder", "sort-lines", "ignore-whitespace", "ignore-start", "ignore-end", "compare-button", "result-body"):
            assert f'id="{identifier}"' in response.text


def test_static_assets_are_available():
    with TestClient(app) as client:
        assert client.get("/static/styles.css").status_code == 200
        script = client.get("/static/app.js")
        assert script.status_code == 200
        assert "javascript" in script.headers["content-type"]


def test_health_does_not_depend_on_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/styles.css").status_code == 200
