"""Encrypted IQ connections beside the executable, independent of ASE settings.

Ordinary INI metadata stays plain. Values with surrounding whitespace or an
initial double quote use JSON string quoting so significant ODBC values survive.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading

from . import jasypt
from .ase_profiles import _ini_lines, _update_section
from .iq_odbc import connection_data


FILENAME = 'iq-connections.txt'
PROFILE_PASSWORD = 'nice1234!!'
MAX_CONFIG_BYTES = 1024 * 1024
MAX_PROFILES = 100
JASYPT_FIELDS = ('algorithm', 'iterations', 'salt_generator', 'iv_generator', 'output_type')
RESERVED_SECTIONS = {'jasypt', 'preferences'}
METADATA_FIELDS = ('host', 'port', 'user', 'database', 'server_name', 'driver')
IDENTITY_FIELDS = ('host', 'port', 'user', 'database', 'server_name')
PROFILE_FIELDS = set(METADATA_FIELDS) | {'password'}
REQUIRED_FIELDS = {'host', 'port', 'user', 'password'}
SAVE_ERROR = '접속은 성공했지만 IQ 연결 정보를 저장하지 못했습니다. 설정 파일의 형식·권한과 변경 여부를 확인하세요.'
CONFIG_ERROR = ('iq-connections.txt의 형식이 올바르지 않습니다. 프로필의 host·port·user·password=ENC(...)와 '
                '[jasypt] 설정을 확인하세요. 중복 항목·알 수 없는 항목·DEFAULT·평문 비밀번호는 사용할 수 없습니다.')
READ_ERROR = 'iq-connections.txt를 읽을 수 없습니다. 파일 위치·권한과 UTF-8 형식을 확인하세요.'
PROFILE_ERROR = 'IQ 접속 프로필이 없거나 파일이 변경되었습니다. 프로필 목록을 새로 불러오세요.'
DECRYPT_ERROR = 'IQ 비밀번호를 복호화할 수 없습니다. DB 비밀번호를 다시 입력해 접속하고 저장하세요. Jasypt 설정도 확인하세요.'
_SAVE_LOCK = threading.RLock()


def configuration_path() -> Path:
    try:
        root = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
                else Path(__file__).resolve().parents[1])
        return root / FILENAME
    except (OSError, RuntimeError):
        raise ValueError(READ_ERROR) from None


def _profile_path(path: Path | None) -> Path:
    try:
        return (configuration_path() if path is None else Path(path)).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ValueError(READ_ERROR) from None


def _settings(values: dict) -> dict:
    if set(values) - set(JASYPT_FIELDS):
        raise ValueError(CONFIG_ERROR)
    options = dict(values)
    if 'iterations' in options:
        if not re.fullmatch(r'[0-9]{1,7}', str(options['iterations'])):
            raise ValueError(CONFIG_ERROR)
        options['iterations'] = int(options['iterations'])
    try:
        settings = jasypt.validate_payload({'operation': 'decrypt', 'text': '',
                    'password': 'metadata-validation', **options})
    except (ValueError, TypeError):
        raise ValueError(CONFIG_ERROR) from None
    return {key: settings[key] for key in JASYPT_FIELDS}


def _read_raw(location: Path) -> bytes | None:
    try:
        with location.open('rb') as stream:
            return stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(READ_ERROR) from None


def _decode(value: str) -> str:
    if value.startswith('"'):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise ValueError(CONFIG_ERROR) from None
        if not isinstance(value, str):
            raise ValueError(CONFIG_ERROR)
    return value


def _encode(value) -> str:
    value = str(value)
    return json.dumps(value, ensure_ascii=False) if value != value.strip() or value.startswith('"') else value


def _parse(raw: bytes) -> tuple[str, configparser.ConfigParser]:
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(CONFIG_ERROR)
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        raise ValueError(READ_ERROR) from None
    if '\x00' in text:
        raise ValueError(CONFIG_ERROR)
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=None,
        strict=True, empty_lines_in_values=False, default_section='\x00')
    parser.SECTCRE = re.compile(r'\[(?P<header>[^\[\]\r\n]+)\]\Z')
    try:
        parser.read_string(text)
    except configparser.Error:
        raise ValueError(CONFIG_ERROR) from None
    if any(name.casefold() == 'default' for name in parser.sections()):
        raise ValueError(CONFIG_ERROR)
    return text, parser


def _load(path: Path | None) -> dict:
    location = _profile_path(path)
    raw = _read_raw(location)
    document = {'path': location, 'raw': raw, 'text': '', 'exists': raw is not None,
                'settings': _settings({}), 'profiles': [], 'last_profile': ''}
    if raw is None:
        return document
    text, parser = _parse(raw)
    document['text'] = text
    document['settings'] = _settings(dict(parser['jasypt']) if parser.has_section('jasypt') else {})
    preferences = dict(parser['preferences']) if parser.has_section('preferences') else {}
    if set(preferences) - {'last_profile'}:
        raise ValueError(CONFIG_ERROR)
    last = _decode(preferences.get('last_profile', ''))
    if len(last) > 255 or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in last):
        raise ValueError(CONFIG_ERROR)
    document['last_profile'] = last
    names = [name for name in parser.sections() if name not in RESERVED_SECTIONS]
    if len(names) > MAX_PROFILES:
        raise ValueError(CONFIG_ERROR)
    for name in names:
        values = dict(parser[name])
        if (not name.strip() or len(name) > 255 or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name)
                or set(values) - PROFILE_FIELDS or not REQUIRED_FIELDS <= set(values)
                or any('\r' in value or '\n' in value for value in values.values())):
            raise ValueError(CONFIG_ERROR)
        ciphertext = values.pop('password')
        if not re.fullmatch(r'ENC\([^()\r\n]+\)', ciphertext) or not ciphertext[4:-1].strip():
            raise ValueError(CONFIG_ERROR)
        try:
            values = {key: _decode(value) for key, value in values.items()}
            if not re.fullmatch(r'[0-9]{1,5}', values['port']):
                raise ValueError(CONFIG_ERROR)
            connection = connection_data({**values, 'password': ''})
        except (ValueError, TypeError, UnicodeError):
            raise ValueError(CONFIG_ERROR) from None
        public = {'id': hashlib.sha256(raw + b'\x00' + name.encode('utf-8')).hexdigest(),
                  'name': name, **{key: connection[key] for key in METADATA_FIELDS}}
        document['profiles'].append({'public': public, 'ciphertext': ciphertext})
    return document


def public_profiles(path: Path | None = None) -> dict:
    document = _load(path)
    profiles = [item['public'] for item in document['profiles']]
    selected = next((item['id'] for item in profiles if item['name'] == document['last_profile']), None)
    return {'path': str(document['path']), 'exists': document['exists'], 'profiles': profiles,
            'jasypt': document['settings'], 'last_profile_id': selected}


def decrypt_connection(profile_id: str, path: Path | None = None) -> dict:
    document = _load(path)
    selected = next((item for item in document['profiles'] if item['public']['id'] == profile_id), None)
    if not isinstance(profile_id, str) or selected is None:
        raise ValueError(PROFILE_ERROR)
    metadata = selected['public']
    try:
        password = jasypt.transform({'operation': 'decrypt', 'text': selected['ciphertext'],
                                    'password': PROFILE_PASSWORD, **document['settings']})
        connection = connection_data({**{key: metadata[key] for key in METADATA_FIELDS}, 'password': password})
    except (ValueError, TypeError, UnicodeError):
        raise ValueError(DECRYPT_ERROR) from None
    return {'connection': connection, 'profile_name': metadata['name']}


def prepare_connection(raw: dict, path: Path | None = None) -> dict:
    """Validate/encrypt in memory; callers save only after successful login."""
    connection = connection_data(raw)
    try:
        settings = _load(path)['settings']
    except ValueError:
        settings = _settings({})
    ciphertext = jasypt.transform({'operation': 'encrypt', 'text': connection['password'],
                                  'password': PROFILE_PASSWORD, 'wrap_enc': True, **settings})
    return {'connection': connection, 'ciphertext': ciphertext, 'settings': settings}


def _canonical_name(connection: dict, profiles: list[dict], existing: dict | None) -> str:
    identity = tuple(connection[key] for key in IDENTITY_FIELDS)
    digest = hashlib.sha256(repr(identity).encode('utf-8')).hexdigest()[:12]
    base = f"{connection['host']}-{connection['user']}"
    base = base.replace('%', '%25').replace('[', '%5B').replace(']', '%5D')
    if len(base) > 255:
        base = base[:242] + '-' + digest
    occupied = {item['public']['name'] for item in profiles
                if existing is None or item['public']['name'] != existing['name']}
    if base not in occupied:
        return base
    suffix = f"-{connection['port']}"
    candidate = base[:255 - len(suffix)] + suffix
    if candidate not in occupied:
        return candidate
    suffix += '-' + digest
    candidate = base[:255 - len(suffix)] + suffix
    counter = 1
    while candidate in occupied:
        counter += 1
        numbered = suffix + f'-{counter}'
        candidate = base[:255 - len(numbered)] + numbered
    return candidate


def _rename_section(text: str, old_name: str, new_name: str) -> str:
    if old_name == new_name:
        return text
    lines, headers, _ = _ini_lines(text)
    for index, name in headers:
        if name == old_name:
            ending = '\r\n' if lines[index].endswith('\r\n') else '\n' if lines[index].endswith('\n') else ''
            lines[index] = f'[{new_name}]{ending}'
            return ''.join(lines)
    raise ValueError(SAVE_ERROR)


def _write_document(document: dict, text: str) -> None:
    location = document['path']
    raw = (b'\xef\xbb\xbf' if document['raw'] and document['raw'].startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(SAVE_ERROR)
    temporary = None
    try:
        # Atomic replacement only needs directory write access on POSIX;
        # respect an existing file's explicit read-only state as well.
        if document['raw'] is not None and not location.stat().st_mode & 0o222:
            raise ValueError(SAVE_ERROR)
        descriptor, temporary = tempfile.mkstemp(prefix='.iq-connections-', suffix='.tmp', dir=location.parent)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if _read_raw(location) != document['raw']:
            raise ValueError(SAVE_ERROR)
        os.replace(temporary, location)
        temporary = None
    except (OSError, ValueError):
        raise ValueError(SAVE_ERROR) from None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except OSError:
                pass


def _saved_result(name: str, path: Path | None) -> dict:
    profile = next((item for item in public_profiles(path)['profiles'] if item['name'] == name), None)
    if profile is None:
        raise ValueError(SAVE_ERROR)
    return {'saved': True, 'profile_id': profile['id'], 'profile_name': name}


def save_connection(prepared: dict, path: Path | None = None) -> dict:
    with _SAVE_LOCK:
        try:
            document = _load(path)
            if document['settings'] != prepared['settings']:
                raise ValueError(SAVE_ERROR)
            connection = connection_data(prepared['connection'])
            ciphertext = prepared['ciphertext']
            if not isinstance(ciphertext, str) or not re.fullmatch(r'ENC\([^()\r\n]+\)', ciphertext):
                raise ValueError(SAVE_ERROR)
            # Validate the prepared token before replacing any user file. This
            # also prevents a modified prepared payload from saving the wrong
            # password after the caller connected successfully.
            password = jasypt.transform({'operation': 'decrypt', 'text': ciphertext,
                'password': PROFILE_PASSWORD, **document['settings']})
            if password != connection['password']:
                raise ValueError(SAVE_ERROR)
            identity = tuple(connection[key] for key in IDENTITY_FIELDS)
            existing = next((item['public'] for item in document['profiles']
                if tuple(item['public'][key] for key in IDENTITY_FIELDS) == identity), None)
            if existing is None and len(document['profiles']) >= MAX_PROFILES:
                raise ValueError(SAVE_ERROR)
            name = _canonical_name(connection, document['profiles'], existing)
            original = _rename_section(document['text'], existing['name'], name) if existing else document['text']
            values = {key: _encode(connection[key]) for key in METADATA_FIELDS}
            values['password'] = ciphertext
            text = _update_section(original, name, values)
            text = _update_section(text, 'preferences', {'last_profile': _encode(name)})
            _write_document(document, text)
            return _saved_result(name, path)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ValueError(SAVE_ERROR) from None


def remember_connection(profile_id: str, path: Path | None = None) -> dict:
    with _SAVE_LOCK:
        try:
            document = _load(path)
            profile = next((item['public'] for item in document['profiles'] if item['public']['id'] == profile_id), None)
            if profile is None:
                raise ValueError(PROFILE_ERROR)
            text = _update_section(document['text'], 'preferences', {'last_profile': _encode(profile['name'])})
            _write_document(document, text)
            return _saved_result(profile['name'], path)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ValueError(SAVE_ERROR) from None
