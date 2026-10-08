"""표준 라이브러리만 사용하는 실제 HTTP 준비 확인."""

import argparse
import json
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    with urllib.request.urlopen(f"{base}/health", timeout=10) as response:
        assert json.load(response) == {"status": "ok"}
    with urllib.request.urlopen(f"{base}/", timeout=10) as response:
        assert b'id="left-folder"' in response.read()
    for asset in ("styles.css", "app.js"):
        with urllib.request.urlopen(f"{base}/static/{asset}", timeout=10) as response:
            assert response.read()
    boundary = "nice-tools-smoke-boundary"
    fields = {
        "left_paths": '["a.txt", "only-left.txt"]',
        "right_paths": '["a.txt", "only-right.txt"]',
        "sort_lines": "true", "ignore_whitespace": "true",
        "ignore_start": "1", "ignore_end": "1",
    }
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, content in (
        ("left_files", "a.txt", b"Xa bY\nXcdY"),
        ("left_files", "only-left.txt", b"left"),
        ("right_files", "a.txt", b"QcdR\nQabR"),
        ("right_files", "only-right.txt", b"right"),
    ):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(f"{base}/api/compare", data=b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    assert result["summary"] == {"total": 3, "same": 1, "different": 0, "left_only": 1, "right_only": 1, "error": 0}, result
    assert result["results"][0] == {"path": "a.txt", "status": "same"}, result
    print("HTTP smoke: health, tool screen, assets, upload and all four comparison options passed.")


if __name__ == "__main__":
    main()
