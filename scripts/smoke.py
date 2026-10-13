"""표준 라이브러리만 사용하는 실제 HTTP 준비 확인."""

import argparse
import json
import urllib.request


def compare_request(base, fields, files, expected):
    boundary = "nice-tools-smoke-boundary"
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, content in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(f"{base}/api/compare", data=b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    assert result["summary"] == expected, result
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--large-files", action="store_true", help="Verify files >10 MiB and total requests >50 MiB")
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    with urllib.request.urlopen(f"{base}/health", timeout=10) as response:
        assert json.load(response) == {"status": "ok"}
    with urllib.request.urlopen(f"{base}/", timeout=10) as response:
        assert b'id="left-folder"' in response.read()
    for asset in ("styles.css", "app.js"):
        with urllib.request.urlopen(f"{base}/static/{asset}", timeout=10) as response:
            assert response.read()
    result = compare_request(base, {
        "left_paths": '["a.txt", "only-left.txt"]',
        "right_paths": '["a.txt", "only-right.txt"]',
        "sort_lines": "true", "ignore_whitespace": "true",
        "ignore_start": "1", "ignore_end": "1",
    }, [
        ("left_files", "a.txt", b"Xa bY\nXcdY"),
        ("left_files", "only-left.txt", b"left"),
        ("right_files", "a.txt", b"QcdR\nQabR"),
        ("right_files", "only-right.txt", b"right"),
    ], {"total": 3, "same": 1, "different": 0, "left_only": 1, "right_only": 1, "error": 0})
    assert result["results"][0] == {"path": "a.txt", "status": "same"}, result
    print("HTTP smoke: health, tool screen, assets, upload and all four comparison options passed.")
    if args.large_files:
        prefix = b"a" * (10 * 1024 * 1024 + 1)
        compare_request(base, {"left_paths": '["large.txt"]', "right_paths": '["large.txt"]'}, [
            ("left_files", "large.txt", prefix + b"x"),
            ("right_files", "large.txt", prefix + b"y"),
        ], {"total": 1, "same": 0, "different": 1, "left_only": 0, "right_only": 0, "error": 0})
        content = b"a" * (9 * 1024 * 1024)
        paths = [f"file{i}.txt" for i in range(3)]
        compare_request(base, {"left_paths": json.dumps(paths), "right_paths": json.dumps(paths)}, [
            (side + "_files", path, content) for side in ("left", "right") for path in paths
        ], {"total": 3, "same": 3, "different": 0, "left_only": 0, "right_only": 0, "error": 0})
        print("Large-file smoke: compared bytes past 10 MiB and a 54 MiB upload request.")


if __name__ == "__main__":
    main()
