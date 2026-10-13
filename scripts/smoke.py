"""표준 라이브러리만 사용하는 실제 HTTP 준비 확인."""

import argparse
import json
import urllib.request
import urllib.error
import socket
from pathlib import Path


def ase_checks(base, require_native=False):
    with urllib.request.urlopen(f"{base}/api/ase/status", timeout=30) as response:
        assert response.headers["Cache-Control"] == "no-store"
        status = json.load(response)
    if require_native:
        assert status.get("available") and "1.5.19" in status.get("version", ""), status
        # A reserved, non-listening loopback socket exercises native login and
        # DB-Library error callbacks without contacting a real database.
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            password = "smoke-password-never-log"
            payload = {"host": "127.0.0.1", "port": unused.getsockname()[1], "user": "smoke", "password": password}
            request = urllib.request.Request(f"{base}/api/ase/connect", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "X-Nice-Tools": "ase"})
            try:
                urllib.request.urlopen(request, timeout=30)
            except urllib.error.HTTPError as response:
                assert response.code == 400
                assert response.headers["Cache-Control"] == "no-store"
                detail = response.read().decode()
                assert password not in detail
            else:
                raise AssertionError("Native connection to a refused port unexpectedly succeeded")
        print("ASE EXE smoke: bundled DB-Library loading and sanitized connection refusal passed; no live ASE transfer tested.")
    else:
        print("ASE HTTP smoke: local driver status passed (native availability:", status.get("available"), ").")


def jasypt_checks(base):
    reference = Path(__file__).resolve().parents[1] / 'tests/fixtures/jasypt.json'
    cases = json.loads(reference.read_text(encoding='utf-8'))['cases']

    def call(config):
        request = urllib.request.Request(f'{base}/api/jasypt', data=json.dumps(config).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.headers['Cache-Control'] == 'no-store'
            return json.load(response)['result']

    for case in cases:
        config = {key: value for key, value in case.items() if key not in ('text', 'ciphertext')}
        assert call(config | {'operation': 'decrypt', 'text': case['ciphertext']}) == case['text']
        encrypted = call(config | {'operation': 'encrypt', 'text': case['text'], 'wrap_enc': True})
        assert encrypted.startswith('ENC(')
        assert call(config | {'operation': 'decrypt', 'text': encrypted}) == case['text']
    print('Jasypt HTTP smoke: Java reference ciphertexts and AES/DES round trips in 12 configurations passed.')


def upload_request(base, route, fields, files):
    boundary = "nice-tools-smoke-boundary"
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, content in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(f"{base}/api/{route}", data=b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    return result


def compare_request(base, fields, files, expected):
    result = upload_request(base, "compare", fields, files)
    assert result["summary"] == expected, result
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--large-files", action="store_true", help="Verify files >10 MiB and total requests >50 MiB")
    parser.add_argument("--require-ase", action="store_true", help="Require the bundled native ASE driver (Windows release validation)")
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    with urllib.request.urlopen(f"{base}/health", timeout=10) as response:
        assert json.load(response) == {"status": "ok"}
    with urllib.request.urlopen(f"{base}/", timeout=10) as response:
        page = response.read()
        assert b'id="left-folder"' in page and b'id="diff-panel"' in page and b'id="jasypt-tool"' in page and b'id="ase-tool"' in page
    for asset in ("styles.css", "app.js", "tools.js", "ase.css", "ase.js"):
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
    diff = upload_request(base, "diff", {"left_paths": '["a.txt"]', "right_paths": '["a.txt"]'}, [
        ("left_files", "a.txt", b"context\nold\nend"),
        ("right_files", "a.txt", b"context\nnew\nend"),
    ])
    assert (diff["additions"], diff["deletions"]) == (1, 1), diff
    assert [line["text"] for line in diff["lines"] if line["kind"] in ("delete", "add")] == ["old", "new"], diff
    normalized = upload_request(base, "diff", {
        "left_paths": '["a.txt"]', "right_paths": '["a.txt"]',
        "sort_lines": "true", "ignore_whitespace": "true", "ignore_start": "1", "ignore_end": "1",
    }, [("left_files", "a.txt", b"Xa bY\nXcdY"), ("right_files", "a.txt", b"QcdR\nQabR")])
    assert normalized["identical"] and not normalized["lines"], normalized
    one_side = upload_request(base, "diff", {"left_paths": '[]', "right_paths": '["new.txt"]'}, [
        ("right_files", "new.txt", b"added"),
    ])
    assert one_side["additions"] == 1 and not one_side["old_exists"], one_side
    repeated = upload_request(base, "diff", {"left_paths": '["repeated.txt"]', "right_paths": '["repeated.txt"]'}, [
        ("left_files", "repeated.txt", b"common line\n" * 10000),
        ("right_files", "repeated.txt", b"changed first\n" + b"common line\n" * 9998 + b"changed last\n"),
    ])
    assert (repeated["additions"], repeated["deletions"]) == (2, 2)
    assert len(repeated["lines"]) <= 16
    many_edits = ["common line"] * 10000
    for i in range(129):
        many_edits[i * 9999 // 128] = f"edit {i}"
    repeated_fallback = upload_request(base, "diff", {"left_paths": '["repeated.txt"]', "right_paths": '["repeated.txt"]'}, [
        ("left_files", "repeated.txt", b"common line\n" * 10000),
        ("right_files", "repeated.txt", "\n".join(many_edits).encode()),
    ])
    assert (repeated_fallback["additions"], repeated_fallback["deletions"]) == (129, 129)
    print("HTTP smoke: health, tool screen, assets, comparison options and unified file diffs passed.")
    jasypt_checks(base)
    ase_checks(base, args.require_ase)
    if args.large_files:
        prefix = b"a" * (10 * 1024 * 1024 + 1)
        compare_request(base, {"left_paths": '["large.txt"]', "right_paths": '["large.txt"]'}, [
            ("left_files", "large.txt", prefix + b"x"),
            ("right_files", "large.txt", prefix + b"y"),
        ], {"total": 1, "same": 0, "different": 1, "left_only": 0, "right_only": 0, "error": 0})
        large_diff = upload_request(base, "diff", {"left_paths": '["large.txt"]', "right_paths": '["large.txt"]'}, [
            ("left_files", "large.txt", prefix + b"\nold"),
            ("right_files", "large.txt", prefix + b"\nnew"),
        ])
        assert [line["text"] for line in large_diff["lines"] if line["kind"] in ("delete", "add")] == ["old", "new"]
        content = b"a" * (9 * 1024 * 1024)
        paths = [f"file{i}.txt" for i in range(3)]
        compare_request(base, {"left_paths": json.dumps(paths), "right_paths": json.dumps(paths)}, [
            (side + "_files", path, content) for side in ("left", "right") for path in paths
        ], {"total": 3, "same": 3, "different": 0, "left_only": 0, "right_only": 0, "error": 0})
        print("Large-file smoke: comparison and diff past 10 MiB and a 54 MiB upload request.")


if __name__ == "__main__":
    main()
