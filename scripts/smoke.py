"""표준 라이브러리만 사용하는 실제 HTTP 준비 확인."""

import argparse
import json
import hashlib
import urllib.request
import urllib.error
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


def data_source_checks(odbc):
    sources = odbc.get("data_sources")
    assert isinstance(sources, list), "Missing native ODBC DSN discovery metadata"
    for source in sources:
        assert isinstance(source, dict) and set(source) == {"name", "driver"}, "Unexpected DSN metadata fields"
        assert isinstance(source["name"], str) and source["name"], "Invalid DSN name metadata"
        assert isinstance(source["driver"], str) and source["driver"], "Invalid DSN driver metadata"
    assert len({source["name"] for source in sources}) == len(sources), "Duplicate DSN discovery entries"
    return sources


def ase_checks(base):
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
    assert isinstance(status.get("available"), bool), status
    assert "FreeTDS" not in str(status), "Obsolete native driver remains in client status"
    data_source_checks(status["odbc"])
    print("ASE HTTP smoke: ODBC/SAP BCP discovery and optional-owner session validation passed.")


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


def profile_checks(base, directory, clients=None, executable=None, require_odbc=False):
    """Real frozen ODBC discovery/failure; no simulated successful DB login."""
    directory = directory.resolve(strict=True)
    location = directory / 'ase-connections.txt'
    missing = json_request(base, '/api/ase/profiles')
    assert Path(missing['path']).resolve() == location, 'Configuration must be beside the original executable'
    assert missing['exists'] is False and missing['profiles'] == []
    assert not location.exists(), 'Refusing to replace an existing ASE configuration'
    fixed_key = 'nice1234!!'
    password = 'fixture-db-password-never-log!42'
    incompatible_key = 'fixture-incompatible-legacy-key!43'
    encrypted = json_request(base, '/api/jasypt', {
        'operation': 'encrypt', 'text': password, 'password': fixed_key, 'wrap_enc': True,
    })['result']
    bad_cipher = json_request(base, '/api/jasypt', {
        'operation': 'encrypt', 'text': password, 'password': incompatible_key, 'wrap_enc': True,
    })['result']
    client_section = ''
    if clients is not None:
        clients = clients.resolve(strict=True)
        assert (clients / 'bcp.exe').is_file(), 'Missing external developer BCP console fixture'
        client_section = f'[sap_client]\nbcp_path = {clients / "bcp.exe"}\n\n'
    invalid_driver = 'NiceToolsFixtureMissingDriver-65046'
    existing_sources = data_source_checks(json_request(base, '/api/ase/status')['odbc'])
    invalid_dsn = 'NiceToolsMissingDSN-' + hashlib.sha256(str(directory).encode()).hexdigest()[:8]
    assert invalid_dsn not in {source['name'] for source in existing_sources}, 'The isolated invalid DSN unexpectedly exists'
    config = (client_section + f'[odbc]\ndriver = {invalid_driver}\n\n'
              '[jasypt]\nalgorithm = PBEWithMD5AndDES\niterations = 1000\n'
              'salt_generator = random\niv_generator = none\noutput_type = base64\n\n'
              '[패키지 검사 서버]\nhost = fixture.internal\nport = 5000\n'
              f'user = smoke\npassword = {encrypted}\ndatabase = fixture_db\n\n'
              '[패키지 검사 DSN]\nhost = fixture.internal\nport = 5000\n'
              f'user = smoke\npassword = {encrypted}\ndatabase = fixture_db\ndsn = {invalid_dsn}\n')
    created = False
    try:
        with location.open('x', encoding='utf-8', newline='\n') as output:
            created = True
            output.write(config)
        profiles = json_request(base, '/api/ase/profiles')
        assert Path(profiles['path']).resolve() == location and profiles['exists'] is True
        rendered = json.dumps(profiles, ensure_ascii=False)
        assert not any(value in rendered for value in (encrypted, encrypted[4:-1], password, fixed_key))
        assert profiles['odbc'] == {'driver': invalid_driver}
        assert len(profiles['profiles']) == 2
        profile = next(item for item in profiles['profiles'] if item['name'] == '패키지 검사 서버')
        assert set(profile) == {'id', 'name', 'host', 'port', 'user', 'database'}
        assert profile['name'] == '패키지 검사 서버' and profile['database'] == 'fixture_db'
        dsn_profile = next(item for item in profiles['profiles'] if item['name'] == '패키지 검사 DSN')
        assert set(dsn_profile) == {'id', 'name', 'host', 'port', 'user', 'database', 'dsn'}
        assert dsn_profile['name'] == '패키지 검사 DSN' and dsn_profile['dsn'] == invalid_dsn
        status = json_request(base, '/api/ase/status')
        odbc = status['odbc']
        assert isinstance(odbc['drivers'], list) and odbc['available'] is False
        data_source_checks(odbc)
        if require_odbc:
            assert odbc['module_available'] is True, 'The actual pyodbc native extension did not load'
            assert odbc['version'] == '5.3.0', 'The pinned native pyodbc extension did not load'
        if clients is not None:
            assert status['bcp_available'] is True, 'The configured external BCP fixture was not discovered'
        refused = json_request(base, '/api/ase/connect', {'profile_id': profile['id']}, 400)
        detail = sanitized_error(refused, password, fixed_key, encrypted, encrypted[4:-1])
        assert '복호화' not in detail and 'ODBC' in detail, 'The fixed-key profile did not reach driver validation'
        manual = json_request(base, '/api/ase/connect', {'host': 'fixture-new.internal', 'port': 5001,
                              'user': 'other', 'password': password}, 400)
        assert 'ODBC' in sanitized_error(manual, password), 'Manual login did not reach native ODBC validation'
        dsn_refused = json_request(base, '/api/ase/connect', {'profile_id': dsn_profile['id']}, 400)
        assert 'DSN' in sanitized_error(dsn_refused, password, fixed_key, encrypted, encrypted[4:-1]), 'Saved DSN did not reach native DSN validation'
        dsn_manual = json_request(base, '/api/ase/connect', {'host': 'fixture-new.internal', 'port': 5001,
                                  'user': 'other', 'password': password, 'dsn': invalid_dsn}, 400)
        assert 'DSN' in sanitized_error(dsn_manual, password), 'Manual DSN did not reach native DSN validation'
        assert location.read_text(encoding='utf-8-sig') == config, 'A failed login persisted a new profile'
        if executable is not None:
            from verify_bundle import odbc_worker_checks
            odbc_worker_checks(executable, directory, password, require_odbc, invalid_dsn)
        # A legacy ciphertext encrypted with another key must fail before ODBC.
        location.write_text(config.replace(encrypted, bad_cipher), encoding='utf-8')
        stale = next(item for item in json_request(base, '/api/ase/profiles')['profiles'] if item['name'] == '패키지 검사 서버')
        wrong = json_request(base, '/api/ase/connect', {'profile_id': stale['id']}, 400)
        assert '복호화' in sanitized_error(wrong, password, fixed_key, incompatible_key, bad_cipher, bad_cipher[4:-1])
    finally:
        if created:
            location.unlink()
    target = 'actual Windows EXE' if require_odbc else 'development process'
    print(f'ASE {target} smoke: fixed-key profiles, real pyodbc driver/DSN discovery and invalid-driver/DSN errors, failed-login nonpersistence and incompatible-ciphertext handling passed; no live ASE login tested.')


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
    parser.add_argument("--sap-test-clients", type=Path, help="External developer-only Windows console fixtures")
    parser.add_argument("--executable", type=Path, help="Actual executable to exercise native ODBC worker discovery")
    parser.add_argument("--require-odbc", action="store_true", help="Require imported native pyodbc in Windows release verification")
    parser.add_argument("--config-folder", type=Path, help="Verify encrypted profiles in an isolated executable folder")
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    with urllib.request.urlopen(f"{base}/health", timeout=10) as response:
        assert json.load(response) == {"status": "ok"}
    with urllib.request.urlopen(f"{base}/", timeout=10) as response:
        page = response.read()
        assert b'id="left-folder"' in page and b'id="diff-panel"' in page and b'id="jasypt-tool"' in page and b'id="ase-tool"' in page
        assert b'id="ase-search-button"' in page and b'id="ase-catalog-timings"' in page and b'id="ase-out-statistics"' in page
        assert b'id="ase-master-password"' not in page, 'ASE still asks for a profile encryption key'
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
    ase_checks(base)
    if args.config_folder is not None:
        profile_checks(base, args.config_folder, args.sap_test_clients, args.executable, args.require_odbc)
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
