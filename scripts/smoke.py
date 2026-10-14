"""표준 라이브러리만 사용하는 실제 HTTP 준비 확인."""

import argparse
import json
import urllib.request
import urllib.error
import socket
from pathlib import Path


def json_request(base, route, payload=None, error_code=None):
    headers = {"Content-Type": "application/json", "X-Nice-Tools": "ase"}
    request = urllib.request.Request(f"{base}{route}", headers=headers,
                                     data=json.dumps(payload).encode() if payload is not None else None)
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as response:
        with response:
            assert response.code == error_code, f"{route}: unexpected HTTP {response.code}"
            assert response.headers["Cache-Control"] == "no-store", route
            return json.load(response)
    else:
        with response:
            assert error_code is None, f"{route}: expected HTTP {error_code}"
            assert response.headers["Cache-Control"] == "no-store", route
            return json.load(response)


def sanitized_error(result, *secrets):
    assert isinstance(result.get("detail"), str), "Missing sanitized error detail"
    rendered = json.dumps(result, ensure_ascii=False)
    assert not any(secret in rendered for secret in secrets), "Private values appeared in an error response"
    return result["detail"]


def ase_checks(base, require_native=False):
    with urllib.request.urlopen(f"{base}/api/ase/status", timeout=30) as response:
        assert response.headers["Cache-Control"] == "no-store"
        status = json.load(response)
    # Optional owner/filter validation must reach session validation even when
    # no live ASE is configured. A build with the old required-owner boundary
    # would fail here before inspecting the deliberately absent session.
    missing_session = json_request(base, '/api/ase/objects', {
        'session': 'smoke-absent-session', 'database': 'smoke_db',
        'owner_id': None, 'search': 'customer', 'kind': 'all',
    }, 400)
    assert '접속 정보가 만료' in sanitized_error(missing_session), 'All-owner object search did not reach session validation'
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
    default_case = next(case for case in cases if case['algorithm'] == 'PBEWithMD5AndDES'
                        and case['iterations'] == 1000 and case['salt_generator'] == 'random'
                        and case['iv_generator'] == 'none' and case['output_type'] == 'base64')
    defaults = {'password': default_case['password']}
    explicit = {key: value for key, value in default_case.items() if key not in ('text', 'ciphertext')}
    assert call(defaults | {'operation': 'decrypt', 'text': default_case['ciphertext']}) == default_case['text']
    encrypted = call(defaults | {'operation': 'encrypt', 'text': default_case['text'], 'wrap_enc': True})
    assert call(explicit | {'operation': 'decrypt', 'text': encrypted}) == default_case['text']
    encrypted = call(explicit | {'operation': 'encrypt', 'text': default_case['text']})
    assert call(defaults | {'operation': 'decrypt', 'text': encrypted}) == default_case['text']
    print('Jasypt HTTP smoke: Java reference ciphertexts, 12 AES/DES configurations and omitted MD5/DES defaults passed.')


def profile_checks(base, directory, require_native=False):
    """Exercise a fixture beside an isolated EXE; never replace an existing file."""
    directory = directory.resolve(strict=True)
    assert directory.is_dir(), 'The EXE fixture directory must exist'
    location = directory / 'ase-connections.txt'
    missing = json_request(base, '/api/ase/profiles')
    assert Path(missing['path']).resolve() == location, 'The app did not read profiles beside its original executable'
    assert missing['exists'] is False and missing['profiles'] == [], 'The isolated EXE directory must start without a configuration'
    assert not location.exists(), 'Refusing to replace an existing ASE configuration'
    master = 'fixture-master-only-in-memory!42'
    password = 'fixture-db-password-never-log!42'
    wrong_master = 'fixture-incorrect-master!43'
    encrypted = json_request(base, '/api/jasypt', {
        'operation': 'encrypt', 'text': password, 'password': master, 'wrap_enc': True,
    })['result']
    assert encrypted.startswith('ENC(') and encrypted.endswith(')'), 'Expected wrapped Jasypt ciphertext'
    with socket.socket() as unused:
        unused.bind(('127.0.0.1', 0))
        port = unused.getsockname()[1]
        config = (
            '[jasypt]\nalgorithm = PBEWithMD5AndDES\niterations = 1000\n'
            'salt_generator = random\niv_generator = none\noutput_type = base64\n\n'
            '[패키지 검사 서버]\nhost = 127.0.0.1\n'
            f'port = {port}\nuser = smoke\npassword = {encrypted}\ndatabase = smoke_db\n'
        )
        created = False
        try:
            with location.open('x', encoding='utf-8', newline='\n') as output:
                created = True
                output.write(config)
            profiles = json_request(base, '/api/ase/profiles')
            assert Path(profiles['path']).resolve() == location and profiles['exists'] is True
            rendered = json.dumps(profiles, ensure_ascii=False)
            assert not any(value in rendered for value in (encrypted, encrypted[4:-1], password, master)), 'Profile metadata exposed password material'
            assert len(profiles['profiles']) == 1, 'Expected the isolated encrypted profile'
            profile = profiles['profiles'][0]
            assert set(profile) == {'id', 'name', 'host', 'port', 'user', 'database'}, 'Unexpected private profile fields'
            assert profile['name'] == '패키지 검사 서버' and profile['host'] == '127.0.0.1'
            assert profile['port'] == port and profile['user'] == 'smoke' and profile['database'] == 'smoke_db'
            assert profiles['jasypt'] == {'algorithm': 'PBEWithMD5AndDES', 'iterations': 1000,
                                         'salt_generator': 'random', 'iv_generator': 'none', 'output_type': 'base64'}
            wrong = json_request(base, '/api/ase/connect', {'profile_id': profile['id'], 'master_password': wrong_master}, 400)
            detail = sanitized_error(wrong, master, wrong_master, password, encrypted, encrypted[4:-1])
            assert '복호화' in detail, 'An incorrect master key should fail before ASE login'
            if require_native:
                refused = json_request(base, '/api/ase/connect', {'profile_id': profile['id'], 'master_password': master}, 400)
                detail = sanitized_error(refused, master, password, encrypted, encrypted[4:-1])
                assert 'DB-Library error' in detail or 'ASE error' in detail, 'The valid master key did not reach native ASE login'
                assert '복호화' not in detail, 'Valid encrypted settings failed during decryption'
        finally:
            if created:
                location.unlink()
    print('ASE profile EXE smoke: original EXE folder, encrypted configuration and sanitized wrong-key checks passed'
          + ('; native login refusal passed; no live ASE transfer tested.' if require_native else '.'))


def encoding_checks(base):
    texts = [
        ('cp949', '내부망 파일 비교\n설정 값 확인\n'),
        ('utf-16', '내부망 파일 비교\n설정 값 확인\n'),
        ('utf-32', '내부망 파일 비교\n설정 값 확인\n'),
        ('cp932', 'こんにちは世界。設定ファイルを比較してください。\n名前=山田太郎'),
        ('cp1252', 'Café — “Price €12.50”\nRésumé déjà vu.\n'),
    ]
    paths = [f'{encoding}.txt' for encoding, _ in texts]
    files = [(side + '_files', f'{encoding}.txt', text.encode(encoding if side == 'left' else 'utf-8'))
             for side in ('left', 'right') for encoding, text in texts]
    compare_request(base, {'left_paths': json.dumps(paths), 'right_paths': json.dumps(paths)}, files,
                    {'total': 5, 'same': 5, 'different': 0, 'left_only': 0, 'right_only': 0, 'error': 0})
    diff = upload_request(base, 'diff', {'left_paths': '["encoded.txt"]', 'right_paths': '["encoded.txt"]'}, [
        ('left_files', 'encoded.txt', '설정 시작\n원본 값\n설정 끝'.encode('cp949')),
        ('right_files', 'encoded.txt', '설정 시작\n수정 값\n설정 끝'.encode('utf-16')),
    ])
    assert [line['text'] for line in diff['lines'] if line['kind'] in ('delete', 'add')] == ['원본 값', '수정 값'], 'Mixed-encoding diff changed Unicode text'
    assert (diff['additions'], diff['deletions']) == (1, 1), 'Mixed-encoding diff had unexpected changes'
    print('Encoding HTTP smoke: CP949, UTF-16/32, CP932 and CP1252 match UTF-8, with Unicode diffs preserved.')


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
    parser.add_argument("--config-folder", type=Path, help="Verify encrypted profiles in an isolated executable folder")
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    with urllib.request.urlopen(f"{base}/health", timeout=10) as response:
        assert json.load(response) == {"status": "ok"}
    with urllib.request.urlopen(f"{base}/", timeout=10) as response:
        page = response.read()
        assert b'id="left-folder"' in page and b'id="diff-panel"' in page and b'id="jasypt-tool"' in page and b'id="ase-tool"' in page
        assert b'id="ase-search-button"' in page and b'id="ase-catalog-timings"' in page and b'id="ase-out-statistics"' in page
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
    encoding_checks(base)
    jasypt_checks(base)
    ase_checks(base, args.require_ase)
    if args.config_folder is not None:
        profile_checks(base, args.config_folder, args.require_ase)
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
        encoded_left = prefix + '\n끝:원본\n'.encode('cp949')
        encoded_right = prefix + '\n끝:수정\n'.encode('cp949')
        compare_request(base, {"left_paths": '["encoded-large.txt"]', "right_paths": '["encoded-large.txt"]'}, [
            ('left_files', 'encoded-large.txt', encoded_left),
            ('right_files', 'encoded-large.txt', encoded_right),
        ], {"total": 1, "same": 0, "different": 1, "left_only": 0, "right_only": 0, "error": 0})
        encoded_diff = upload_request(base, 'diff', {"left_paths": '["encoded-large.txt"]', "right_paths": '["encoded-large.txt"]'}, [
            ('left_files', 'encoded-large.txt', encoded_left),
            ('right_files', 'encoded-large.txt', encoded_right),
        ])
        assert [line['text'] for line in encoded_diff['lines'] if line['kind'] in ('delete', 'add')] == ['끝:원본', '끝:수정'], 'CP949 suffix beyond 10 MiB was lost or changed'
        content = b"a" * (9 * 1024 * 1024)
        paths = [f"file{i}.txt" for i in range(3)]
        compare_request(base, {"left_paths": json.dumps(paths), "right_paths": json.dumps(paths)}, [
            (side + "_files", path, content) for side in ("left", "right") for path in paths
        ], {"total": 3, "same": 3, "different": 0, "left_only": 0, "right_only": 0, "error": 0})
        print("Large-file smoke: comparison and Unicode CP949 tail diffs past 10 MiB and a 54 MiB upload request.")


if __name__ == "__main__":
    main()
