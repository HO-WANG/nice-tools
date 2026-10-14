import importlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from nice_tools import jasypt


MASTER = "nice1234!!"
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
    result = loader().decrypt_connection(selected(path), path)
    assert result == {"connection": {"host": "127.0.0.1", "port": 5000, "user": "sa", "password": PLAINTEXT},
                      "database": "sampledb", "profile_name": "local"}


def test_empty_decrypted_database_password_is_valid(tmp_path):
    path = write_profile(tmp_path, ciphertext=encrypted(""))
    assert loader().decrypt_connection(selected(path), path)["connection"]["password"] == ""


def test_missing_file_allows_manual_fallback_but_cannot_decrypt(tmp_path):
    path = tmp_path / "ase-connections.txt"
    assert loader().public_profiles(path) == {"path": str(path.resolve()), "exists": False,
                                               "profiles": [], "jasypt": DEFAULTS, "clients": {}, "odbc": {}, "last_profile_id": None}
    with pytest.raises(ValueError):
        loader().decrypt_connection("old-id", path)


def test_profiles_with_old_custom_key_require_password_reentry_without_changes(tmp_path):
    old_key = "old-custom-key"
    ciphertext = jasypt.transform({"operation": "encrypt", "text": PLAINTEXT,
                                   "password": old_key, "wrap_enc": True})
    path = write_profile(tmp_path, ciphertext=ciphertext)
    original = path.read_bytes()
    with pytest.raises(ValueError) as exc:
        loader().decrypt_connection(selected(path), path)
    message = str(exc.value)
    assert "다시" in message and "저장" in message
    assert ciphertext not in message and MASTER not in message and PLAINTEXT not in message
    assert old_key not in message
    assert path.read_bytes() == original


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
    assert loader().decrypt_connection(profile["id"], path)["connection"]["user"] == profile["user"]


def test_bom_and_unicode_profile_names_with_markup_remain_plain_metadata(tmp_path):
    name = '개발 <img src=x onerror=alert(1)>'
    path = write_profile(tmp_path, name=name, database="데이터베이스")
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    profile = loader().public_profiles(path)["profiles"][0]
    assert profile["name"] == name and profile["database"] == "데이터베이스"
    assert loader().decrypt_connection(profile["id"], path)["profile_name"] == name


def test_changing_file_invalidates_previously_selected_profile(tmp_path):
    path = write_profile(tmp_path)
    old_id = selected(path)
    path.write_text(path.read_text().replace("127.0.0.1", "127.0.0.2"), encoding="utf-8")
    assert selected(path) != old_id
    with pytest.raises(ValueError) as exc:
        loader().decrypt_connection(old_id, path)
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
        loader().decrypt_connection(selected(path), path)
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
    assert result['clients'] == {'bcp_path': bcp}
    assert result['last_profile_id'] == result['profiles'][0]['id']


def test_successful_manual_connection_save_encrypts_and_remembers_choice(tmp_path):
    module = loader()
    path = tmp_path / 'ase-connections.txt'
    data = {'host': 'server', 'port': 5000, 'user': 'test', 'password': PLAINTEXT}
    prepared = module.prepare_connection(data, path)
    assert not path.exists(), 'preparation must not save a failed/unattempted connection'
    result = module.save_connection(prepared, path)
    raw = path.read_text(encoding='utf-8')
    assert result['saved'] is True
    assert result['profile_name'] == 'server-test'
    assert MASTER not in raw and PLAINTEXT not in raw and 'ENC(' in raw
    public = module.public_profiles(path)
    assert public['last_profile_id'] == result['profile_id']
    assert module.decrypt_connection(result['profile_id'], path)['connection'] == data


def test_autosave_preserves_comments_other_profiles_client_paths_and_crypto(tmp_path):
    settings = {'algorithm': jasypt.AES_ALGORITHM, 'iterations': 32, 'iv_generator': 'random'}
    path = write_profile(tmp_path, settings=settings)
    before = f'# permanent user comment\n[sap_client]\nisql_path={tmp_path / "isql.exe"}\nbcp_path={tmp_path / "bcp.exe"}\n\n' + path.read_text()
    path.write_text(before, encoding='utf-8')
    module = loader()
    data = {'host': 'other-server', 'port': 5010, 'user': 'other', 'password': 'new-secret'}
    saved = module.save_connection(module.prepare_connection(data, path), path)
    after = path.read_text()
    assert after.startswith(before)
    public = module.public_profiles(path)
    assert len(public['profiles']) == 2
    assert public['jasypt']['algorithm'] == jasypt.AES_ALGORITHM
    assert module.decrypt_connection(saved['profile_id'], path)['connection'] == data
    assert module.decrypt_connection(public['profiles'][0]['id'], path)['connection']['password'] == PLAINTEXT


def test_autosave_reuses_identity_and_updates_password_without_duplicate(tmp_path):
    module = loader()
    path = write_profile(tmp_path)
    data = {'host': '127.0.0.1', 'port': 5000, 'user': 'sa', 'password': 'new-password'}
    saved = module.save_connection(module.prepare_connection(data, path), path)
    public = module.public_profiles(path)
    assert len(public['profiles']) == 1
    assert saved['profile_name'] == '127.0.0.1-sa'
    assert module.decrypt_connection(saved['profile_id'], path)['connection']['password'] == 'new-password'


def test_malformed_existing_file_is_never_overwritten_on_save(tmp_path):
    module = loader()
    path = tmp_path / 'ase-connections.txt'
    original = b'[bad]\npassword=plaintext-existing-secret\n'
    path.write_bytes(original)
    prepared = module.prepare_connection({'host': 'localhost', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, path)
    with pytest.raises(ValueError):
        module.save_connection(prepared, path)
    assert path.read_bytes() == original


def test_crypto_changed_during_connect_is_not_overwritten(tmp_path):
    module = loader()
    path = write_profile(tmp_path)
    prepared = module.prepare_connection({'host': 'localhost', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, path)
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
    assert module.decrypt_connection(saved['profile_id'], path)['connection']['password'] == PLAINTEXT


@pytest.mark.parametrize('contents', [
    '[sap_client]\nbcp_path=relative/bcp.exe\n',
    '[sap_client]\npassword=hidden-secret\n',
    '[preferences]\nmaster_password=hidden-secret\n',
    '[sap_client]\nbcp_path=/opt/sap/bin/other.exe\n',
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
        loader().prepare_connection({'host': 'localhost', 'port': 5000, 'user': user, 'password': 'db-secret'}, path)
    assert 'injected-secret' not in str(exc.value)
    assert not path.exists()


def test_atomic_save_replace_error_preserves_original_and_removes_temporary_files(tmp_path, monkeypatch):
    module = loader()
    path = write_profile(tmp_path)
    before = path.read_bytes()
    prepared = module.prepare_connection({'host': 'new-server', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, path)
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
    prepared = module.prepare_connection({'host': 'new-server', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, path)
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
    prepared = module.prepare_connection({'host': 'new-server', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, path)
    with path.open('a', encoding='utf-8') as stream:
        stream.write('\n# added by editor\n[another]\nhost=third-server\nport=5000\nuser=test\npassword=' + encrypted('third-secret') + '\n')
    result = module.save_connection(prepared, path)
    profiles = module.public_profiles(path)['profiles']
    assert len(profiles) == 3
    third = next(item for item in profiles if item['name'] == 'another')
    assert module.decrypt_connection(third['id'], path)['connection']['password'] == 'third-secret'
    assert module.decrypt_connection(result['profile_id'], path)['connection']['password'] == 'new-secret'


def test_removed_profile_after_atomic_write_returns_safe_save_error(tmp_path, monkeypatch):
    module = loader()
    path = write_profile(tmp_path)
    prepared = module.prepare_connection({'host': 'new-server', 'port': 5000, 'user': 'test', 'password': 'new-secret'}, path)
    replace = module.os.replace
    def replace_and_external_delete(source, target):
        replace(source, target)
        Path(target).write_text('# all profiles externally removed\n')
    monkeypatch.setattr(module.os, 'replace', replace_and_external_delete)
    with pytest.raises(ValueError) as exc:
        module.save_connection(prepared, path)
    assert 'new-secret' not in str(exc.value)


def test_odbc_settings_are_global_and_exclude_obsolete_isql_path(tmp_path):
    path = write_profile(tmp_path)
    path.write_text('[odbc]\ndriver=SAP ASE ODBC Driver\n[sap_client]\nisql_path=invalid-old-relative-path\n\n' + path.read_text())
    public = loader().public_profiles(path)
    assert public['odbc'] == {'driver': 'SAP ASE ODBC Driver'}
    assert public['clients'] == {}
    assert len(public['profiles']) == 1
    assert loader().odbc_settings(path) == public['odbc']


def test_odbc_metadata_discovery_does_not_require_valid_profile_data(tmp_path):
    path = tmp_path / 'ase-connections.txt'
    path.write_text('[odbc]\ndriver=SAP ASE ODBC Driver\n[bad-profile]\npassword=old-existing-secret\n')
    assert loader().odbc_settings(path) == {'driver': 'SAP ASE ODBC Driver'}


@pytest.mark.parametrize('field', ['password=hidden-secret', 'master_password=hidden-secret', 'driver=first\n second'])
def test_odbc_settings_reject_secret_or_multiline_values_without_echo(tmp_path, field):
    path = tmp_path / 'ase-connections.txt'
    path.write_text('[odbc]\n' + field + '\n')
    with pytest.raises(ValueError) as exc:
        loader().public_profiles(path)
    assert 'hidden-secret' not in str(exc.value)


def test_manual_save_renames_matching_legacy_section_and_preserves_comments_database(tmp_path):
    module = loader()
    path = write_profile(tmp_path, name='saved-old-id', database='default-db')
    original = '# shared user comment\n[odbc]\ndriver=SAP ASE ODBC Driver\n\n' + path.read_text().replace('[saved-old-id]\n', '[saved-old-id]\n# profile comment\n')
    path.write_text(original)
    data = {'host': '127.0.0.1', 'port': 5000, 'user': 'sa', 'password': 'new-secret'}
    saved = module.save_connection(module.prepare_connection(data, path), path)
    public = module.public_profiles(path)
    assert saved['profile_name'] == '127.0.0.1-sa'
    assert len(public['profiles']) == 1
    assert public['profiles'][0]['database'] == 'default-db'
    assert public['odbc'] == {'driver': 'SAP ASE ODBC Driver'}
    raw = path.read_text()
    assert '[saved-old-id]' not in raw and '[127.0.0.1-sa]' in raw
    assert '# profile comment' in raw and raw.startswith('# shared user comment\n')
    assert public['last_profile_id'] == saved['profile_id']
    assert module.decrypt_connection(saved['profile_id'], path)['connection'] == data


def test_same_host_and_user_different_port_gets_suffix_without_overwriting(tmp_path):
    module = loader()
    path = tmp_path / 'ase-connections.txt'
    first_data = {'host': 'server', 'port': 5000, 'user': 'sa', 'password': 'first-secret'}
    second_data = {**first_data, 'port': 5010, 'password': 'second-secret'}
    first = module.save_connection(module.prepare_connection(first_data, path), path)
    second = module.save_connection(module.prepare_connection(second_data, path), path)
    assert first['profile_name'] == 'server-sa'
    assert second['profile_name'] == 'server-sa-5010'
    second_update = module.save_connection(module.prepare_connection({**second_data, 'password': 'updated-secret'}, path), path)
    public = module.public_profiles(path)
    assert len(public['profiles']) == 2
    assert second_update['profile_name'] == second['profile_name']
    retained = next(item for item in public['profiles'] if item['name'] == first['profile_name'])
    assert module.decrypt_connection(retained['id'], path)['connection']['password'] == 'first-secret'
    assert module.decrypt_connection(second_update['profile_id'], path)['connection']['password'] == 'updated-secret'


@pytest.mark.parametrize('user', ['사용자', 'account[unsafe]header', 'account%5Bunsafe%5D', 'a' * 255])
def test_profile_names_preserve_normal_unicode_and_escape_brackets_with_length_bound(tmp_path, user):
    module = loader()
    path = tmp_path / 'ase-connections.txt'
    data = {'host': 'server', 'port': 5000, 'user': user, 'password': 'secret'}
    first = module.save_connection(module.prepare_connection(data, path), path)
    name = first['profile_name']
    assert name.startswith('server-') and len(name) <= 255
    assert '[' not in name and ']' not in name
    if user == '사용자':
        assert name == 'server-사용자'
    assert module.decrypt_connection(first['profile_id'], path)['connection'] == data
    updated = module.save_connection(module.prepare_connection({**data, 'password': 'updated'}, path), path)
    assert updated['profile_name'] == name
    assert len(module.public_profiles(path)['profiles']) == 1


def test_canonical_name_collision_with_other_endpoint_never_replaces_profile(tmp_path):
    module = loader()
    path = tmp_path / 'ase-connections.txt'
    one = {'host': 'a-b', 'port': 5000, 'user': 'c', 'password': 'one-secret'}
    two = {'host': 'a', 'port': 5000, 'user': 'b-c', 'password': 'two-secret'}
    module.save_connection(module.prepare_connection(one, path), path)
    saved = module.save_connection(module.prepare_connection(two, path), path)
    public = module.public_profiles(path)
    assert len(public['profiles']) == 2
    assert saved['profile_name'] != 'a-b-c'
    assert saved['profile_name'].startswith('a-b-c-5000')
    first = next(item for item in public['profiles'] if item['name'] == 'a-b-c')
    assert module.decrypt_connection(first['id'], path)['connection'] == one
    assert module.decrypt_connection(saved['profile_id'], path)['connection'] == two


def test_port_suffix_collision_preserves_unrelated_named_profile(tmp_path):
    module = loader()
    path = write_profile(tmp_path, name='server-sa-5010', host='other-server', user='other', ciphertext=encrypted('untouched-secret'))
    module.save_connection(module.prepare_connection({'host': 'server', 'port': 5000, 'user': 'sa', 'password': 'first-secret'}, path), path)
    data = {'host': 'server', 'port': 5010, 'user': 'sa', 'password': 'new-secret'}
    saved = module.save_connection(module.prepare_connection(data, path), path)
    public = module.public_profiles(path)
    assert len(public['profiles']) == 3
    assert saved['profile_name'].startswith('server-sa-5010-')
    assert saved['profile_name'] != 'server-sa-5010'
    untouched = next(item for item in public['profiles'] if item['name'] == 'server-sa-5010')
    assert module.decrypt_connection(untouched['id'], path)['connection']['password'] == 'untouched-secret'
    assert module.decrypt_connection(saved['profile_id'], path)['connection'] == data


@pytest.mark.parametrize('settings', [{}, {'algorithm': jasypt.AES_ALGORITHM, 'iterations': 32, 'iv_generator': 'random'}])
def test_optional_dsn_round_trip_preserves_crypto_comments_and_legacy_profiles(tmp_path, settings):
    module = loader()
    path = write_profile(tmp_path, settings=settings, name='legacy', database='default-db')
    original = '# operator comments remain\n' + path.read_text()
    path.write_text(original)
    data = {'host': 'ase-host', 'port': 5000, 'user': 'reader', 'password': 'new-db-secret', 'dsn': ' My ASE DSN '}
    saved = module.save_connection(module.prepare_connection(data, path), path)
    public = module.public_profiles(path)
    fresh = next(item for item in public['profiles'] if item['id'] == saved['profile_id'])
    assert fresh['dsn'] == 'My ASE DSN'
    assert module.decrypt_connection(saved['profile_id'], path)['connection'] == {**data, 'dsn': 'My ASE DSN'}
    legacy = next(item for item in public['profiles'] if item['name'] == 'legacy')
    assert 'dsn' not in legacy
    assert 'dsn' not in module.decrypt_connection(legacy['id'], path)['connection']
    assert legacy['database'] == 'default-db'
    assert path.read_text().startswith(original)
    assert module.public_profiles(path)['jasypt']['algorithm'] == (settings.get('algorithm') or jasypt.DES_ALGORITHM)


def test_dsn_distinguishes_profiles_without_overwriting_direct_or_other_dsn(tmp_path):
    module = loader()
    path = tmp_path / 'ase-connections.txt'
    base = {'host': 'server', 'port': 5000, 'user': 'sa', 'password': 'direct-secret'}
    module.save_connection(module.prepare_connection(base, path), path)
    one = module.save_connection(module.prepare_connection({**base, 'dsn': 'DSN A', 'password': 'dsn-a-secret'}, path), path)
    two = module.save_connection(module.prepare_connection({**base, 'dsn': 'DSN B', 'password': 'dsn-b-secret'}, path), path)
    updated = module.save_connection(module.prepare_connection({**base, 'dsn': 'DSN A', 'password': 'dsn-a-updated'}, path), path)
    public = module.public_profiles(path)
    assert len(public['profiles']) == 3
    assert one['profile_name'] == updated['profile_name']
    assert one['profile_name'] != two['profile_name']
    direct = next(item for item in public['profiles'] if 'dsn' not in item)
    dsn_b = next(item for item in public['profiles'] if item.get('dsn') == 'DSN B')
    assert module.decrypt_connection(direct['id'], path)['connection']['password'] == 'direct-secret'
    assert module.decrypt_connection(dsn_b['id'], path)['connection']['password'] == 'dsn-b-secret'
    assert module.decrypt_connection(updated['profile_id'], path)['connection']['password'] == 'dsn-a-updated'
    assert public['last_profile_id'] == updated['profile_id']


@pytest.mark.parametrize('dsn', ['', '   '])
def test_blank_dsn_is_absent_and_reuses_legacy_connection_identity(tmp_path, dsn):
    module = loader()
    path = write_profile(tmp_path)
    data = {'host': '127.0.0.1', 'port': 5000, 'user': 'sa', 'password': 'updated-secret', 'dsn': dsn}
    saved = module.save_connection(module.prepare_connection(data, path), path)
    public = module.public_profiles(path)
    assert len(public['profiles']) == 1 and 'dsn' not in public['profiles'][0]
    assert 'dsn' not in module.decrypt_connection(saved['profile_id'], path)['connection']


@pytest.mark.parametrize('dsn', ['bad\tprivate-dsn', 'bad\x7fprivate-dsn', 'x' * 256, None, 123])
def test_invalid_manual_dsn_is_sanitized_before_connect_or_save(tmp_path, dsn):
    path = tmp_path / 'ase-connections.txt'
    with pytest.raises(ValueError) as exc:
        loader().prepare_connection({'host': 'localhost', 'port': 5000, 'user': 'test', 'password': 'db-secret', 'dsn': dsn}, path)
    assert 'private-dsn' not in str(exc.value) and 'db-secret' not in str(exc.value)
    assert not path.exists()


@pytest.mark.parametrize('dsn', ['bad\tprivate-dsn', 'bad\x7fprivate-dsn', 'x' * 256])
def test_invalid_profile_dsn_is_rejected_without_echo(tmp_path, dsn):
    path = write_profile(tmp_path, dsn=dsn)
    with pytest.raises(ValueError) as exc:
        loader().public_profiles(path)
    assert 'private-dsn' not in str(exc.value)


def test_global_odbc_dsn_is_rejected(tmp_path):
    path = tmp_path / 'ase-connections.txt'
    path.write_text('[odbc]\ndsn=global-dsn\n')
    with pytest.raises(ValueError):
        loader().public_profiles(path)


def test_existing_aes_dsn_profile_update_preserves_database_and_comments(tmp_path):
    module = loader()
    settings = {'algorithm': jasypt.AES_ALGORITHM, 'iterations': 32, 'iv_generator': 'random'}
    path = write_profile(tmp_path, settings=settings, name='custom-old-name', database='default-db', dsn='ASE DSN')
    path.write_text(path.read_text().replace('[custom-old-name]\n', '[custom-old-name]\n# retained DSN profile comment\n'))
    data = {'host': '127.0.0.1', 'port': 5000, 'user': 'sa', 'password': 'new-secret', 'dsn': 'ASE DSN'}
    saved = module.save_connection(module.prepare_connection(data, path), path)
    public = module.public_profiles(path)
    assert len(public['profiles']) == 1
    assert public['profiles'][0]['dsn'] == 'ASE DSN' and public['profiles'][0]['database'] == 'default-db'
    assert saved['profile_name'] == '127.0.0.1-sa'
    assert '# retained DSN profile comment' in path.read_text()
    assert '[custom-old-name]' not in path.read_text()
    assert module.decrypt_connection(saved['profile_id'], path)['connection'] == data


def test_save_sql_ini_path_preserves_encrypted_profiles_comments_and_odbc_settings(tmp_path):
    module = loader()
    path = write_profile(tmp_path, name='legacy', database='default-db', dsn='Existing ODBC DSN')
    original = '# operator comments\n[odbc]\ndriver=SAP ASE ODBC Driver\n\n' + path.read_text()
    path.write_text(original)
    sql_path = tmp_path / '설치 폴더' / 'sql.ini'
    result = module.save_sql_ini_path(str(sql_path), path)
    public = module.public_profiles(path)
    assert result == {'saved': True}
    assert public['clients']['sql_ini_path'] == str(sql_path)
    assert module.client_settings(path)['sql_ini_path'] == str(sql_path)
    assert path.read_text().startswith(original)
    assert public['odbc'] == {'driver': 'SAP ASE ODBC Driver'}
    profile = public['profiles'][0]
    assert profile['dsn'] == 'Existing ODBC DSN' and profile['database'] == 'default-db'
    assert module.decrypt_connection(profile['id'], path)['connection']['password'] == PLAINTEXT
    assert PLAINTEXT not in path.read_text() and '[ALIAS]' not in path.read_text()


def test_sql_ini_path_update_keeps_bcp_and_legacy_ignored_isql_settings(tmp_path):
    module = loader()
    path = tmp_path / 'ase-connections.txt'
    old_sql, new_sql = tmp_path / 'old.ini', tmp_path / 'new.ini'
    bcp = tmp_path / 'bcp.exe'
    path.write_text(f'[sap_client]\n# SAP client comment\nisql_path=legacy-invalid-path\nbcp_path={bcp}\nsql_ini_path={old_sql}\n')
    module.save_sql_ini_path(str(new_sql), path)
    public = module.public_profiles(path)
    assert public['clients'] == {'bcp_path': str(bcp), 'sql_ini_path': str(new_sql)}
    assert '# SAP client comment' in path.read_text()
    assert 'isql_path=legacy-invalid-path' in path.read_text()
    assert sum(line.startswith('sql_ini_path') for line in path.read_text().splitlines()) == 1


@pytest.mark.parametrize('selected_path', ['relative/sql.ini', 'C-not-an-absolute-path', 'bad\tprivate-path', None])
def test_bad_sql_ini_metadata_path_is_rejected_without_writing(tmp_path, selected_path):
    path = tmp_path / 'ase-connections.txt'
    with pytest.raises(ValueError) as exc:
        loader().save_sql_ini_path(selected_path, path)
    assert 'private-path' not in str(exc.value)
    assert not path.exists()


def test_sql_ini_metadata_save_never_overwrites_malformed_existing_profiles(tmp_path):
    path = tmp_path / 'ase-connections.txt'
    original = b'[bad]\npassword=existing-private-secret\n'
    path.write_bytes(original)
    with pytest.raises(ValueError) as exc:
        loader().save_sql_ini_path(str(tmp_path / 'sql.ini'), path)
    assert path.read_bytes() == original
    assert 'existing-private-secret' not in str(exc.value)


@pytest.mark.parametrize('separator', ['\x80', '\x85', '\x9f', '\u2028', '\u2029'])
def test_sql_ini_path_rejects_c1_and_unicode_line_separators_without_changing_profiles(tmp_path, separator):
    module = loader()
    path = write_profile(tmp_path, name='protected', database='default-db', dsn='Working DSN')
    path.write_text('# preserved operator comment\n' + path.read_text())
    before = path.read_bytes()
    dangerous = str(tmp_path / ('folder' + separator + 'private-path.ini'))
    with pytest.raises(ValueError) as exc:
        module.save_sql_ini_path(dangerous, path)
    assert 'private-path' not in str(exc.value)
    assert path.read_bytes() == before
    public = module.public_profiles(path)
    assert public['profiles'][0]['name'] == 'protected'
    assert public['profiles'][0]['database'] == 'default-db'
    assert module.decrypt_connection(public['profiles'][0]['id'], path)['connection']['password'] == PLAINTEXT
    assert '# preserved operator comment' in path.read_text()
