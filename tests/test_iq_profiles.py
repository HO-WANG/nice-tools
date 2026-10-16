"""IQ profile persistence exercises real encrypted files and atomic updates."""
import importlib
import importlib.util
import json
import re
import sys

import pytest

from nice_tools import jasypt

SECRET = 'iq-private-한글 '
KEY = 'nice1234!!'
DEFAULTS = {'algorithm': 'PBEWithMD5AndDES', 'iterations': 1000,
            'salt_generator': 'random', 'iv_generator': 'none', 'output_type': 'base64'}


def module():
    assert importlib.util.find_spec('nice_tools.iq_profiles') is not None, 'IQ saved profiles are unavailable'
    return importlib.import_module('nice_tools.iq_profiles')


def raw(**changes):
    return {'host': 'localhost', 'port': 2638, 'user': 'dba', 'password': SECRET,
            'database': 'warehouse', 'server_name': 'engine', 'driver': 'SQL Anywhere 17', **changes}


def encrypted(**settings):
    return jasypt.transform({'operation': 'encrypt', 'text': SECRET,
                             'password': KEY, 'wrap_enc': True, **settings})


def fixture_file(tmp_path, *, name='old', settings=None):
    settings = settings or {}
    text = '# keep document\n'
    if settings:
        text += '[jasypt]\n' + ''.join(f'{k}={v}\n' for k, v in settings.items()) + '\n'
    text += f'[{name}]\nhost=localhost\nport=2638\nuser=dba\npassword={encrypted(**settings)}\ndatabase=warehouse\nserver_name=engine\ndriver=SQL Anywhere 17\n'
    path = tmp_path / 'iq-connections.txt'
    path.write_text(text, encoding='utf-8')
    return path


def test_preparation_does_not_write_and_save_roundtrips_all_iq_fields(tmp_path):
    store = module()
    path = tmp_path / 'iq-connections.txt'
    assert store.public_profiles(path) == {'path': str(path.resolve()), 'exists': False,
        'profiles': [], 'jasypt': DEFAULTS, 'last_profile_id': None}
    prepared = store.prepare_connection(raw(), path)
    assert not path.exists()
    assert prepared['connection'] == raw()
    assert prepared['settings'] == DEFAULTS and prepared['ciphertext'].startswith('ENC(')
    saved = store.save_connection(prepared, path)
    assert saved['saved'] is True and saved['profile_name'] == 'localhost-dba'
    listing = store.public_profiles(path)
    assert listing['last_profile_id'] == saved['profile_id']
    assert store.decrypt_connection(saved['profile_id'], path) == {'connection': raw(), 'profile_name': 'localhost-dba'}
    public = json.dumps(listing)
    assert SECRET not in public and KEY not in public and prepared['ciphertext'] not in public
    assert 'password' not in public and SECRET not in path.read_text()


@pytest.mark.parametrize('settings', [{}, {'algorithm': jasypt.AES_ALGORITHM, 'iterations': 21,
    'salt_generator': 'zero', 'iv_generator': 'random', 'output_type': 'hexadecimal'}])
def test_existing_jasypt_settings_decrypt_and_survive_save(tmp_path, settings):
    store = module()
    path = fixture_file(tmp_path, settings=settings)
    selected = store.public_profiles(path)['profiles'][0]
    assert store.decrypt_connection(selected['id'], path)['connection'] == raw()
    result = store.save_connection(store.prepare_connection(raw(password='changed'), path), path)
    assert store.decrypt_connection(result['profile_id'], path)['connection']['password'] == 'changed'
    assert len(store.public_profiles(path)['profiles']) == 1
    for key, value in settings.items():
        assert f'{key}={value}' in path.read_text()


def test_upsert_uses_engine_database_and_port_but_replaces_driver(tmp_path):
    store = module()
    path = tmp_path / 'iq-connections.txt'
    names = []
    for changes in ({}, {'database': 'db2'}, {'server_name': 'engine2'}, {'port': 2640}):
        saved = store.save_connection(store.prepare_connection(raw(**changes), path), path)
        names.append(saved['profile_name'])
    assert len(set(names)) == 4
    saved = store.save_connection(store.prepare_connection(raw(driver='SAP IQ 16', password='replacement'), path), path)
    profiles = store.public_profiles(path)['profiles']
    assert len(profiles) == 4
    decoded = store.decrypt_connection(saved['profile_id'], path)['connection']
    assert decoded == raw(driver='SAP IQ 16', password='replacement')


def test_bom_comments_other_profiles_and_remember_are_preserved(tmp_path):
    store = module()
    path = fixture_file(tmp_path)
    original = path.read_bytes()
    path.write_bytes(b'\xef\xbb\xbf' + original + b'\n# trailing comment\n')
    store.save_connection(store.prepare_connection(raw(user='new'), path), path)
    contents = path.read_bytes()
    assert contents.startswith(b'\xef\xbb\xbf' + original)
    assert b'# trailing comment' in contents
    listing = store.public_profiles(path)
    old = next(p for p in listing['profiles'] if p['name'] == 'old')
    result = store.remember_connection(old['id'], path)
    assert result['profile_name'] == 'old'
    assert store.public_profiles(path)['last_profile_id'] == result['profile_id']
    assert original in path.read_bytes()


def test_ini_name_escape_long_names_and_collision(tmp_path):
    store = module()
    path = tmp_path / 'iq-connections.txt'
    for user in ('x[%]#;=:', 'x' * 4096):
        result = store.save_connection(store.prepare_connection(raw(user=user), path), path)
        assert len(result['profile_name']) <= 255
        assert '[' not in result['profile_name'] and ']' not in result['profile_name']
        assert store.decrypt_connection(result['profile_id'], path)['connection']['user'] == user


@pytest.mark.parametrize('frozen', [False, True])
def test_location_independent_of_cwd_and_bundle_directory(tmp_path, monkeypatch, frozen):
    store = module()
    root = tmp_path / 'app'
    (root / 'nice_tools').mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
    monkeypatch.setattr(sys, 'executable', str(root / 'nice-tools.exe'))
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path / 'extraction'), raising=False)
    monkeypatch.setattr(store, '__file__', str(root / 'nice_tools' / 'iq_profiles.py'))
    assert store.configuration_path() == root / 'iq-connections.txt'


@pytest.mark.parametrize('contents', [
    '[DEFAULT]\npassword=secret-to-hide\n', '[default]\n',
    '[jasypt]\npassword=secret-to-hide\n', '[jasypt]\niterations=1000001\n',
    '[preferences]\nunknown=secret-to-hide\n', '[odbc]\ndriver=secret-to-hide\n',
    '[p]\nhost=localhost\nport=2638\nuser=dba\npassword=secret-to-hide\n',
    '[p]\nhost=localhost\nport=2638\nuser=dba\npassword=ENC(abc)\npassword=secret-to-hide\n',
    '[p]\nhost=localhost\nport=2638\nuser=dba\npassword=ENC(abc)\nunknown=secret-to-hide\n',
    '[p] secret-to-hide\nhost=localhost\nport=2638\nuser=dba\npassword=ENC(abc)\n',
    '[p]\nhost=localhost\nport=2638\nuser=dba\npassword=ENC(abc)\n driver=secret-to-hide\n',
])
def test_invalid_configs_fail_closed_without_leaking_or_overwriting(tmp_path, contents):
    store = module()
    path = tmp_path / 'iq-connections.txt'
    original = contents.encode()
    path.write_bytes(original)
    with pytest.raises(ValueError) as error:
        store.public_profiles(path)
    assert 'secret-to-hide' not in str(error.value)
    prepared = store.prepare_connection(raw(), path)
    with pytest.raises(ValueError) as error:
        store.save_connection(prepared, path)
    assert str(error.value) == store.SAVE_ERROR
    assert path.read_bytes() == original


def test_changed_ids_and_external_edits_prevent_stale_actions(tmp_path, monkeypatch):
    store = module()
    path = fixture_file(tmp_path)
    old_id = store.public_profiles(path)['profiles'][0]['id']
    path.write_bytes(path.read_bytes() + b'# edit\n')
    with pytest.raises(ValueError):
        store.decrypt_connection(old_id, path)
    prepared = store.prepare_connection(raw(), path)
    def external_edit_before_replace(*args):
        raise AssertionError('replace must not run after an external edit')
    original_read = store._read_raw
    calls = 0
    def edited_read(location):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(path.read_bytes() + b'# external\n')
        return original_read(location)
    monkeypatch.setattr(store, '_read_raw', edited_read)
    monkeypatch.setattr(store.os, 'replace', external_edit_before_replace)
    with pytest.raises(ValueError) as error:
        store.save_connection(prepared, path)
    assert str(error.value) == store.SAVE_ERROR and path.read_bytes().endswith(b'# external\n')
    assert not list(tmp_path.glob('*.tmp'))


def test_settings_changed_after_prepare_cannot_overwrite(tmp_path):
    store = module()
    path = fixture_file(tmp_path)
    prepared = store.prepare_connection(raw(), path)
    path.write_text(path.read_text() + '\n[jasypt]\niterations=12\n')
    original = path.read_bytes()
    with pytest.raises(ValueError, match=store.SAVE_ERROR):
        store.save_connection(prepared, path)
    assert path.read_bytes() == original


def test_failed_replace_cleans_stage_and_keeps_original(tmp_path, monkeypatch):
    store = module()
    path = fixture_file(tmp_path)
    original = path.read_bytes()
    prepared = store.prepare_connection(raw(), path)
    def denied(*args):
        raise PermissionError('secret-to-hide')
    monkeypatch.setattr(store.os, 'replace', denied)
    with pytest.raises(ValueError) as error:
        store.save_connection(prepared, path)
    assert str(error.value) == store.SAVE_ERROR
    assert path.read_bytes() == original and not list(tmp_path.glob('*.tmp'))


def test_size_and_profile_count_limits(tmp_path, monkeypatch):
    store = module()
    path = fixture_file(tmp_path)
    monkeypatch.setattr(store, 'MAX_CONFIG_BYTES', 10)
    with pytest.raises(ValueError):
        store.public_profiles(path)
    monkeypatch.setattr(store, 'MAX_CONFIG_BYTES', 1024 * 1024)
    monkeypatch.setattr(store, 'MAX_PROFILES', 1)
    prepared = store.prepare_connection(raw(user='new'), path)
    with pytest.raises(ValueError, match=store.SAVE_ERROR):
        store.save_connection(prepared, path)


def test_reject_caller_key_and_invalid_password_without_leaking(tmp_path):
    store = module()
    for changes in ({'master_password': 'secret-to-hide'}, {'password': 'secret-to-hide\n'}):
        with pytest.raises(ValueError) as error:
            store.prepare_connection(raw(**changes), tmp_path / 'iq-connections.txt')
        assert 'secret-to-hide' not in str(error.value)


@pytest.mark.parametrize('user,database,engine,driver', [
    (' dba ', ' warehouse ', ' engine ', ' SQL Anywhere 17 '),
    ('"account', '"db', '"engine', '"driver'),
])
def test_exact_metadata_whitespace_and_quotes_roundtrip(tmp_path, user, database, engine, driver):
    store = module()
    path = tmp_path / 'iq-connections.txt'
    connection = raw(user=user, database=database, server_name=engine, driver=driver)
    prepared = store.prepare_connection(connection, path)
    assert prepared['connection'] == connection
    saved = store.save_connection(prepared, path)
    assert store.decrypt_connection(saved['profile_id'], path)['connection'] == connection
    assert store.public_profiles(path)['last_profile_id'] == saved['profile_id']


def test_iq_save_never_touches_ase_file_and_preserves_prelogin_external_edits(tmp_path):
    store = module()
    path = fixture_file(tmp_path)
    ase_path = tmp_path / 'ase-connections.txt'
    ase_path.write_bytes(b'ASE data must stay byte-for-byte identical\n')
    prepared = store.prepare_connection(raw(user='new'), path)
    path.write_bytes(path.read_bytes() + b'# user edit before save\n')
    store.save_connection(prepared, path)
    assert b'# user edit before save\n' in path.read_bytes()
    assert ase_path.read_bytes() == b'ASE data must stay byte-for-byte identical\n'


@pytest.mark.parametrize('password', ['', 'space at end ', '한글😀'])
def test_password_is_exact_for_empty_unicode_and_trailing_space(tmp_path, password):
    store = module()
    path = tmp_path / 'iq-connections.txt'
    saved = store.save_connection(store.prepare_connection(raw(password=password), path), path)
    assert store.decrypt_connection(saved['profile_id'], path)['connection']['password'] == password


def test_old_key_and_malformed_cipher_do_not_leak_or_change_file(tmp_path):
    store = module()
    path = fixture_file(tmp_path)
    wrong_cipher = jasypt.transform({'operation': 'encrypt', 'text': SECRET,
                                     'password': 'old-key-to-hide', 'wrap_enc': True})
    for cipher in [wrong_cipher, 'ENC(secret-to-hide)']:
        original = path.read_text()
        path.write_text(re.sub(r'password=ENC\([^()]+\)', 'password=' + cipher, original))
        before = path.read_bytes()
        profile = store.public_profiles(path)['profiles'][0]
        with pytest.raises(ValueError) as error:
            store.decrypt_connection(profile['id'], path)
        assert 'old-key-to-hide' not in str(error.value) and SECRET not in str(error.value)
        assert cipher not in str(error.value) and path.read_bytes() == before


def test_invalid_utf8_read_errors_are_sanitized(tmp_path):
    store = module()
    path = tmp_path / 'iq-connections.txt'
    path.write_bytes(b'\xffsecret-to-hide')
    with pytest.raises(ValueError) as error:
        store.public_profiles(path)
    assert str(error.value) == store.READ_ERROR


def test_serialized_saves_do_not_lose_profiles(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = module()
    path = tmp_path / 'iq-connections.txt'
    prepared = [store.prepare_connection(raw(user=f'user{index}'), path) for index in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda item: store.save_connection(item, path), prepared))
    assert all(result['saved'] for result in results)
    assert len(store.public_profiles(path)['profiles']) == 8


@pytest.mark.parametrize('ciphertext', ['ENC( )', 'ENC(\x00)', 'ENC(malformed-token-to-hide)'])
def test_invalid_prepared_cipher_cannot_replace_valid_file(tmp_path, ciphertext):
    store = module()
    path = fixture_file(tmp_path)
    prepared = store.prepare_connection(raw(), path)
    prepared['ciphertext'] = ciphertext
    before = path.read_bytes()
    with pytest.raises(ValueError) as error:
        store.save_connection(prepared, path)
    assert str(error.value) == store.SAVE_ERROR
    assert path.read_bytes() == before


def test_readonly_file_is_preserved_even_when_directory_allows_replacement(tmp_path):
    store = module()
    path = fixture_file(tmp_path)
    prepared = store.prepare_connection(raw(), path)
    before = path.read_bytes()
    path.chmod(0o444)
    try:
        with pytest.raises(ValueError) as error:
            store.save_connection(prepared, path)
        assert str(error.value) == store.SAVE_ERROR
        assert path.read_bytes() == before
    finally:
        path.chmod(0o600)
