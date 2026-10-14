import importlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from nice_tools import jasypt


MASTER = "profile-test-master"
PLAINTEXT = "db-secret-한글-with-spaces "
DEFAULTS = {"algorithm": "PBEWithMD5AndDES", "iterations": 1000,
            "salt_generator": "random", "iv_generator": "none", "output_type": "base64"}


def loader():
    assert importlib.util.find_spec("nice_tools.ase_profiles") is not None, "encrypted ASE profiles are unavailable"
    return importlib.import_module("nice_tools.ase_profiles")


def encrypted(text=PLAINTEXT, **settings):
    return jasypt.transform({"operation": "encrypt", "text": text,
                            "password": MASTER, "wrap_enc": True, **settings})


def write_profile(tmp_path, *, ciphertext=None, settings=None, name="local", **fields):
    settings = settings or {}
    values = {"host": "127.0.0.1", "port": "5000", "user": "sa",
              "password": ciphertext if ciphertext is not None else encrypted(**settings), **fields}
    contents = ""
    if settings:
        contents += "[jasypt]\n" + "".join(f"{key}={value}\n" for key, value in settings.items()) + "\n"
    contents += f"[{name}]\n" + "".join(f"{key}={value}\n" for key, value in values.items())
    path = tmp_path / "ase-connections.txt"
    path.write_text(contents, encoding="utf-8")
    return path


def selected(path):
    return loader().public_profiles(path)["profiles"][0]["id"]


def test_public_profiles_exposes_metadata_and_default_settings_without_password(tmp_path):
    ciphertext = encrypted()
    path = write_profile(tmp_path, ciphertext=ciphertext, database="sampledb")
    result = loader().public_profiles(path)
    assert result["path"] == str(path.resolve())
    assert result["exists"] is True
    assert result["jasypt"] == DEFAULTS
    profile = result["profiles"][0]
    assert len(profile["id"]) == 64
    assert {key: value for key, value in profile.items() if key != "id"} == {
        "name": "local", "host": "127.0.0.1", "port": 5000, "user": "sa", "database": "sampledb"}
    public = json.dumps(result, ensure_ascii=False)
    assert "password" not in public and ciphertext not in public
    assert MASTER not in public and PLAINTEXT not in public


@pytest.mark.parametrize("settings", [
    {},
    {"algorithm": "pbewithmd5anddes", "iterations": 27, "salt_generator": "zero", "iv_generator": "random", "output_type": "hexadecimal"},
    {"algorithm": jasypt.AES_ALGORITHM, "iterations": 21, "salt_generator": "random", "iv_generator": "random", "output_type": "base64"},
    {"algorithm": jasypt.AES_ALGORITHM, "iterations": 15, "salt_generator": "zero", "iv_generator": "random", "output_type": "hexadecimal"},
    {"algorithm": jasypt.AES_ALGORITHM},
])
def test_decrypts_real_jasypt_with_configured_settings(tmp_path, settings):
    path = write_profile(tmp_path, settings=settings, database=" sampledb ", user=" sa ", host=" 127.0.0.1 ")
    result = loader().decrypt_connection(selected(path), MASTER, path)
    assert result == {"connection": {"host": "127.0.0.1", "port": 5000, "user": "sa", "password": PLAINTEXT},
                      "database": "sampledb", "profile_name": "local"}


def test_empty_decrypted_database_password_is_valid(tmp_path):
    path = write_profile(tmp_path, ciphertext=encrypted(""))
    assert loader().decrypt_connection(selected(path), MASTER, path)["connection"]["password"] == ""


def test_missing_file_allows_manual_fallback_but_cannot_decrypt(tmp_path):
    path = tmp_path / "ase-connections.txt"
    assert loader().public_profiles(path) == {"path": str(path.resolve()), "exists": False,
                                               "profiles": [], "jasypt": DEFAULTS, "clients": {}, "last_profile_id": None}
    with pytest.raises(ValueError):
        loader().decrypt_connection("old-id", MASTER, path)


@pytest.mark.parametrize("key", ["wrong-test-key", "", "한글", None, "key\t"])
def test_master_key_errors_are_sanitized(tmp_path, key):
    ciphertext = encrypted()
    path = write_profile(tmp_path, ciphertext=ciphertext)
    with pytest.raises(ValueError) as exc:
        loader().decrypt_connection(selected(path), key, path)
    message = str(exc.value)
    assert ciphertext not in message and MASTER not in message and PLAINTEXT not in message
    if key:
        assert key not in message


@pytest.mark.parametrize("value", ["plain-db-secret", "", "ENC()", "ENC(  )", "ENC(ciphertext", "ciphertext)", "ENC(ENC(nested-token))"])
def test_profiles_require_nonempty_enc_password_without_echoing_it(tmp_path, value):
    path = write_profile(tmp_path, ciphertext=value)
    with pytest.raises(ValueError) as exc:
        loader().public_profiles(path)
    assert "plain-db-secret" not in str(exc.value)
    assert "nested-token" not in str(exc.value)
    if len(value) > 8:
        assert value not in str(exc.value)


@pytest.mark.parametrize("contents", [
    "[local]\nhost=localhost\nport=5000\nuser=sa\npassword=ENC(token-to-hide)\nmaster_password=master-to-hide\n",
    "[jasypt]\npassword=master-to-hide\n[local]\nhost=localhost\nport=5000\nuser=sa\npassword=ENC(token-to-hide)\n",
    "[jasypt] password=master-to-hide\n[local]\nhost=localhost\nport=5000\nuser=sa\npassword=ENC(token-to-hide)\n",
    "[local] password=plain-token-to-hide\nhost=localhost\nport=5000\nuser=sa\npassword=ENC(token-to-hide)\n",
    "[jasypt] password=master-to-hide]\n[local]\nhost=localhost\nport=5000\nuser=sa\npassword=ENC(token-to-hide)\n",
    "[jasypt]\nalgorithm=unknown-token-to-hide\n",
    "[jasypt]\niterations=0\n",
    "[jasypt]\niterations=1000001\n",
    "[jasypt]\niterations=one-token-to-hide\n",
    "[jasypt]\nsalt_generator=other-token-to-hide\n",
    "[jasypt]\niv_generator=other-token-to-hide\n",
    "[jasypt]\noutput_type=other-token-to-hide\n",
    "[jasypt]\nalgorithm=PBEWITHHMACSHA512ANDAES_256\niv_generator=none\n",
    "[DEFAULT]\npassword=ENC(token-to-hide)\n",
    "[DEFAULT]\n",
    "[local]\nhost=localhost\nport=5000\nuser=sa\npassword=ENC(token-to-hide)\npassword=plain-token-to-hide\n",
    "[local]\npassword=ENC(token-to-hide)\n[local]\npassword=plain-token-to-hide\n",
    "[local]\nhost=localhost\nport=5000\nuser=sa\npassword=ENC(token-to-hide)\nline-without-equals-token-to-hide\n",
    "[local]\nhost=invalid!token-to-hide\nport=5000\nuser=sa\npassword=ENC(token-to-hide)\n",
    "[local]\nhost=localhost\nport=65536\nuser=sa\npassword=ENC(token-to-hide)\n",
    "[local]\nhost=localhost\nport=5000\nuser=\npassword=ENC(token-to-hide)\n",
    "[local]\nhost=localhost\nport=5000\npassword=ENC(token-to-hide)\n",
])
def test_malformed_configuration_is_rejected_without_echoing_lines(tmp_path, contents):
    path = tmp_path / "ase-connections.txt"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        loader().public_profiles(path)
    assert "token-to-hide" not in str(exc.value)
    assert "master-to-hide" not in str(exc.value)
    assert not isinstance(exc.value, __import__("configparser").Error)


def test_interpolation_is_disabled_and_inline_comment_characters_are_data(tmp_path):
    path = write_profile(tmp_path, user="user%name#tag;value")
    profile = loader().public_profiles(path)["profiles"][0]
    assert profile["user"] == "user%name#tag;value"
    assert loader().decrypt_connection(profile["id"], MASTER, path)["connection"]["user"] == profile["user"]


def test_bom_and_unicode_profile_names_with_markup_remain_plain_metadata(tmp_path):
    name = '개발 <img src=x onerror=alert(1)>'
    path = write_profile(tmp_path, name=name, database="데이터베이스")
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    profile = loader().public_profiles(path)["profiles"][0]
    assert profile["name"] == name and profile["database"] == "데이터베이스"
    assert loader().decrypt_connection(profile["id"], MASTER, path)["profile_name"] == name


def test_changing_file_invalidates_previously_selected_profile(tmp_path):
    path = write_profile(tmp_path)
    old_id = selected(path)
    path.write_text(path.read_text().replace("127.0.0.1", "127.0.0.2"), encoding="utf-8")
    assert selected(path) != old_id
    with pytest.raises(ValueError) as exc:
        loader().decrypt_connection(old_id, MASTER, path)
    assert MASTER not in str(exc.value)


def test_profile_ids_are_distinct_stable_and_cover_all_file_bytes(tmp_path):
    path = write_profile(tmp_path)
    path.write_text(path.read_text() + "\n[dev]\nhost=localhost\nport=5001\nuser=dev\npassword=" + encrypted() + "\n", encoding="utf-8")
    initial = loader().public_profiles(path)["profiles"]
    assert initial == loader().public_profiles(path)["profiles"]
    assert initial[0]["id"] != initial[1]["id"]
    path.write_bytes(path.read_bytes() + b"\n# change\n")
    assert {item["id"] for item in initial}.isdisjoint(item["id"] for item in loader().public_profiles(path)["profiles"])


def test_default_location_uses_executable_directory_when_frozen(tmp_path, monkeypatch):
    module = loader()
    executable_dir = tmp_path / "exe"
    executable_dir.mkdir()
    path = write_profile(executable_dir)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    write_profile(cwd, host="127.0.0.2")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable_dir / "nice-tools.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(cwd), raising=False)
    assert module.public_profiles()["path"] == str(path.resolve())
    assert module.public_profiles()["profiles"][0]["host"] == "127.0.0.1"


def test_default_source_location_is_repository_root_not_cwd(tmp_path, monkeypatch):
    module = loader()
    root = tmp_path / "repo"
    (root / "nice_tools").mkdir(parents=True)
    path = write_profile(root)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    write_profile(cwd, host="127.0.0.2")
    monkeypatch.chdir(cwd)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(module, "__file__", str(root / "nice_tools" / "ase_profiles.py"))
    assert module.public_profiles()["path"] == str(path.resolve())
    assert module.public_profiles()["profiles"][0]["host"] == "127.0.0.1"


def test_unresolvable_executable_path_errors_do_not_echo_paths(tmp_path, monkeypatch):
    module = loader()
    loop = tmp_path / "path-secret-token"
    resolve = Path.resolve

    def resolve_without_executable(self, *args, **kwargs):
        if self == loop:
            raise RuntimeError("Symlink loop from path-secret-token")
        return resolve(self, *args, **kwargs)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(loop))
    monkeypatch.setattr(Path, "resolve", resolve_without_executable)
    with pytest.raises(ValueError) as exc:
        module.public_profiles()
    assert "path-secret-token" not in str(exc.value)


@pytest.mark.parametrize("text", ["a" * 256, "\x00invalid"])
def test_decrypted_password_is_validated_for_ase_without_echoing_it(tmp_path, text):
    path = write_profile(tmp_path, ciphertext=encrypted(text))
    with pytest.raises(ValueError) as exc:
        loader().decrypt_connection(selected(path), MASTER, path)
    assert text not in str(exc.value)


def test_config_has_structural_bounds_and_invalid_utf8_is_sanitized(tmp_path):
    path = tmp_path / "ase-connections.txt"
    for raw in [b"#" * (1024 * 1024 + 1), b"\xffplain-secret-token", b"[local]\npassword=ENC(secret)\x00"]:
        path.write_bytes(raw)
        with pytest.raises(ValueError) as exc:
            loader().public_profiles(path)
        assert "secret" not in str(exc.value)
    block = "host=localhost\nport=5000\nuser=sa\npassword=ENC(token)\n"
    path.write_text("".join(f"[profile-{index}]\n{block}" for index in range(101)), encoding="utf-8")
    with pytest.raises(ValueError):
        loader().public_profiles(path)


def test_global_client_settings_and_last_selection_do_not_become_profiles(tmp_path):
    path = write_profile(tmp_path)
    isql, bcp = str(tmp_path / 'isql.exe'), str(tmp_path / 'bcp.exe')
    path.write_text(f'# retain comment\n[sap_client]\nisql_path={isql}\nbcp_path={bcp}\n\n[preferences]\nlast_profile=local\n\n' + path.read_text(), encoding='utf-8')
    result = loader().public_profiles(path)
    assert len(result['profiles']) == 1
    assert result['clients'] == {'isql_path': isql, 'bcp_path': bcp}
    assert result['last_profile_id'] == result['profiles'][0]['id']


def test_successful_manual_connection_save_encrypts_and_remembers_choice(tmp_path):
    module = loader()
    path = tmp_path / 'ase-connections.txt'
    data = {'host': 'server', 'port': 5000, 'user': 'test', 'password': PLAINTEXT}
    prepared = module.prepare_connection(data, MASTER, path)
    assert not path.exists(), 'preparation must not save a failed/unattempted connection'
    result = module.save_connection(prepared, path)
    raw = path.read_text(encoding='utf-8')
    assert result['saved'] is True
    assert MASTER not in raw and PLAINTEXT not in raw and 'ENC(' in raw
    public = module.public_profiles(path)
    assert public['last_profile_id'] == result['profile_id']
    assert module.decrypt_connection(result['profile_id'], MASTER, path)['connection'] == data


def test_autosave_preserves_comments_other_profiles_client_paths_and_crypto(tmp_path):
    settings = {'algorithm': jasypt.AES_ALGORITHM, 'iterations': 32, 'iv_generator': 'random'}
    path = write_profile(tmp_path, settings=settings)
    before = f'# permanent user comment\n[sap_client]\nisql_path={tmp_path / "isql.exe"}\nbcp_path={tmp_path / "bcp.exe"}\n\n' + path.read_text()
    path.write_text(before, encoding='utf-8')
    module = loader()
    data = {'host': 'other-server', 'port': 5010, 'user': 'other', 'password': 'new-secret'}
    saved = module.save_connection(module.prepare_connection(data, MASTER, path), path)
    after = path.read_text()
    assert after.startswith(before)
    public = module.public_profiles(path)
    assert len(public['profiles']) == 2
    assert public['jasypt']['algorithm'] == jasypt.AES_ALGORITHM
    assert module.decrypt_connection(saved['profile_id'], MASTER, path)['connection'] == data
    assert module.decrypt_connection(public['profiles'][0]['id'], MASTER, path)['connection']['password'] == PLAINTEXT


def test_autosave_reuses_identity_and_updates_password_without_duplicate(tmp_path):
    module = loader()
    path = write_profile(tmp_path)
    data = {'host': '127.0.0.1', 'port': 5000, 'user': 'sa', 'password': 'new-password'}
    saved = module.save_connection(module.prepare_connection(data, MASTER, path), path)
    public = module.public_profiles(path)
    assert len(public['profiles']) == 1
    assert saved['profile_name'] == 'local'
    assert module.decrypt_connection(saved['profile_id'], MASTER, path)['connection']['password'] == 'new-password'


def test_malformed_existing_file_is_never_overwritten_on_save(tmp_path):
    module = loader()
    path = tmp_path / 'ase-connections.txt'
    original = b'[bad]\npassword=plaintext-existing-secret\n'
    path.write_bytes(original)
    prepared = module.prepare_connection({'host': 'localhost', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, MASTER, path)
    with pytest.raises(ValueError):
        module.save_connection(prepared, path)
    assert path.read_bytes() == original


def test_crypto_changed_during_connect_is_not_overwritten(tmp_path):
    module = loader()
    path = write_profile(tmp_path)
    prepared = module.prepare_connection({'host': 'localhost', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, MASTER, path)
    changed = '[jasypt]\niterations=31\n' + path.read_text()
    path.write_text(changed)
    with pytest.raises(ValueError):
        module.save_connection(prepared, path)
    assert path.read_text() == changed


def test_saved_profile_success_remembers_and_returns_fresh_id(tmp_path):
    module = loader()
    path = write_profile(tmp_path)
    old = selected(path)
    saved = module.remember_connection(old, path)
    assert saved['saved'] is True
    assert saved['profile_id'] != old
    public = module.public_profiles(path)
    assert public['last_profile_id'] == saved['profile_id']
    assert public['profiles'][0]['name'] == 'local'
    assert module.decrypt_connection(saved['profile_id'], MASTER, path)['connection']['password'] == PLAINTEXT


@pytest.mark.parametrize('contents', [
    '[sap_client]\nisql_path=relative/isql.exe\n',
    '[sap_client]\npassword=hidden-secret\n',
    '[preferences]\nmaster_password=hidden-secret\n',
    '[sap_client]\nisql_path=/opt/sap/bin/other.exe\n',
])
def test_bad_global_fields_cannot_execute_other_tools_or_store_secrets(tmp_path, contents):
    path = tmp_path / 'ase-connections.txt'
    path.write_text(contents)
    with pytest.raises(ValueError) as exc:
        loader().public_profiles(path)
    assert 'hidden-secret' not in str(exc.value)


@pytest.mark.parametrize('user', ['safe\npassword = injected-secret', 'safe\rusername'])
def test_prepare_rejects_line_breaks_before_connect_or_writing_ini(tmp_path, user):
    path = tmp_path / 'ase-connections.txt'
    with pytest.raises(ValueError) as exc:
        loader().prepare_connection({'host': 'localhost', 'port': 5000, 'user': user, 'password': 'db-secret'}, MASTER, path)
    assert 'injected-secret' not in str(exc.value)
    assert not path.exists()


def test_atomic_save_replace_error_preserves_original_and_removes_temporary_files(tmp_path, monkeypatch):
    module = loader()
    path = write_profile(tmp_path)
    before = path.read_bytes()
    prepared = module.prepare_connection({'host': 'new-server', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, MASTER, path)
    def fail_replace(source, target):
        assert 'new-secret' not in Path(source).read_text()
        raise OSError('private-error-secret')
    monkeypatch.setattr(module.os, 'replace', fail_replace)
    with pytest.raises(ValueError) as exc:
        module.save_connection(prepared, path)
    assert 'private-error-secret' not in str(exc.value)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('.ase-connections-*.tmp'))


def test_atomic_save_refuses_external_edit_before_replace(tmp_path, monkeypatch):
    module = loader()
    path = write_profile(tmp_path)
    prepared = module.prepare_connection({'host': 'new-server', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, MASTER, path)
    before = path.read_bytes()
    fsync = module.os.fsync
    def edit_during_write(descriptor):
        fsync(descriptor)
        path.write_bytes(before + b'\n# external editor change\n')
    monkeypatch.setattr(module.os, 'fsync', edit_during_write)
    with pytest.raises(ValueError):
        module.save_connection(prepared, path)
    assert path.read_bytes() == before + b'\n# external editor change\n'
    assert not list(tmp_path.glob('.ase-connections-*.tmp'))


def test_merge_external_profile_added_while_login_is_running(tmp_path):
    module = loader()
    path = write_profile(tmp_path)
    prepared = module.prepare_connection({'host': 'new-server', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, MASTER, path)
    with path.open('a', encoding='utf-8') as stream:
        stream.write('\n# added by editor\n[another]\nhost=third-server\nport=5000\nuser=test\npassword=' + encrypted('third-secret') + '\n')
    result = module.save_connection(prepared, path)
    profiles = module.public_profiles(path)['profiles']
    assert len(profiles) == 3
    third = next(item for item in profiles if item['name'] == 'another')
    assert module.decrypt_connection(third['id'], MASTER, path)['connection']['password'] == 'third-secret'
    assert module.decrypt_connection(result['profile_id'], MASTER, path)['connection']['password'] == 'new-secret'


def test_removed_profile_after_atomic_write_returns_safe_save_error(tmp_path, monkeypatch):
    module = loader()
    path = write_profile(tmp_path)
    prepared = module.prepare_connection({'host': 'new-server', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, MASTER, path)
    replace = module.os.replace
    def replace_and_external_delete(source, target):
        replace(source, target)
        Path(target).write_text('# all profiles externally removed\n')
    monkeypatch.setattr(module.os, 'replace', replace_and_external_delete)
    with pytest.raises(ValueError) as exc:
        module.save_connection(prepared, path)
    assert 'new-secret' not in str(exc.value)
