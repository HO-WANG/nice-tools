"""Single-operation IQ connections using registered, external ODBC drivers.

Credentials and SQL stay in memory; native diagnostics never cross this module.
The parent owns staging-file cleanup and atomic publication.
"""
from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import stat

from . import iq_files, iq_sql


_SAFE_STATES = frozenset('08001 08003 08004 08006 08S01 28000 HYT00 HYT01 IM002 IM003 IM004 IM005 IM006 IM014 HY000 HY010 HY090 HY092 HYC00 42000 42S02 42S22 40001 07002 07009 22001 22003 22018'.split())
_MESSAGES = {
    'driver-resolution': 'A registered 64-bit SAP IQ or SQL Anywhere ODBC driver is required.',
    'connect': 'IQ connection failed. Check the endpoint and account settings.',
    'startup': 'IQ connection initialization failed.',
    'IQ-identity': 'The connected server could not be verified as SAP IQ.',
    'query': 'IQ SELECT execution failed. Check the query and read permissions.',
    'writing': 'IQ result export failed. Check the data format and available disk space.',
    'stage': 'The reserved export staging file is invalid or changed.',
}


class IqOdbcError(ValueError):
    """Safe application diagnostic; never stores a native exception."""

    def __init__(self, message, *, stage=None, sqlstate=None):
        self.stage, self.sqlstate = stage, sqlstate
        prefix = f'[{stage}' + (f'; SQLSTATE {sqlstate}' if sqlstate else '') + '] ' if stage else ''
        super().__init__(prefix + message)


def _diagnostic(stage, error=None, library=None):
    state = None
    error_type = getattr(library, 'Error', None)
    if isinstance(error_type, type) and issubclass(error_type, BaseException) and isinstance(error, error_type):
        candidate = error.args[0] if error.args else None
        if isinstance(candidate, str) and candidate in _SAFE_STATES:
            state = candidate
    return IqOdbcError(_MESSAGES[stage], stage=stage, sqlstate=state)


def _text(value):
    return isinstance(value, str) and len(value) <= 4096 and not any(
        ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value)


def connection_data(raw) -> dict:
    """Normalize the direct endpoint, rejecting unsupported or unsafe fields."""
    allowed = {'host', 'port', 'database', 'server_name', 'user', 'password', 'driver'}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise IqOdbcError('Enter valid IQ connection fields.')
    host = raw.get('host')
    if not _text(host) or not host or len(host) > 253:
        raise IqOdbcError('Enter an IP address or host name.')
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?', host):
            raise IqOdbcError('Enter an IP address or host name.') from None
    port = raw.get('port', 2638)
    if isinstance(port, str) and re.fullmatch(r'[0-9]{1,5}', port):
        port = int(port)
    if type(port) is not int or not 1 <= port <= 65535:
        raise IqOdbcError('The IQ port must be an integer from 1 to 65535.')
    result = {'host': host, 'port': port}
    for key in ('database', 'server_name', 'user', 'password', 'driver'):
        value = raw.get(key, '' if key not in ('user', 'password') else None)
        if not _text(value) or (key == 'user' and not value):
            raise IqOdbcError('Enter valid IQ connection values.')
        result[key] = value
    return result


def _braced(value):
    return '{' + value.replace('}', '}}') + '}'


def _value(value):
    return _braced(value) if ';' in value or value.startswith('{') or value != value.strip() else value


def build_connection_string(connection, driver) -> str:
    """Build SQL Anywhere/IQ direct HOST, UID/PWD, optional DBN/ENG attributes."""
    connection = connection_data(connection)
    if not _text(driver) or not driver:
        raise IqOdbcError('Select a valid IQ ODBC driver.')
    host = connection['host']
    if ':' in host:
        host = '[' + host + ']'
    fields = [('DRIVER', _braced(driver)), ('HOST', _value(f'{host}:{connection["port"]}')),
              ('UID', _value(connection['user'])), ('PWD', _value(connection['password']))]
    for key, attribute in (('database', 'DBN'), ('server_name', 'ENG')):
        if connection[key]:
            fields.append((attribute, _value(connection[key])))
    return ''.join(key + '=' + value + ';' for key, value in fields)


def _pyodbc():
    import pyodbc
    # Must precede even drivers(): it can allocate the ODBC environment.
    pyodbc.pooling = False
    return pyodbc


def _compatible(name):
    if not _text(name):
        return False
    compact = re.sub(r'\s+', '', name).casefold()
    if any(value in compact for value in ('freetds', 'sqlserver', 'enterprise')) or re.search(r'\bASE\b', name, re.I):
        return False
    return bool(re.search(r'\b(?:SAP|Sybase)\s+IQ\b|\bAdaptive\s+Server\s+IQ\b|\bSQL\s*Anywhere\b', name, re.I))


def _drivers(library):
    try:
        names = library.drivers()
        if not isinstance(names, (list, tuple)) or any(not isinstance(name, str) for name in names):
            raise ValueError
        return list(dict.fromkeys(name for name in names if _compatible(name)))
    except Exception as error:
        raise _diagnostic('driver-resolution', error, library) from None


def _driver(names, configured=''):
    if configured:
        for name in names:
            if configured.casefold() == name.casefold():
                return name
        raise _diagnostic('driver-resolution')
    if not names:
        raise _diagnostic('driver-resolution')
    return max(names, key=lambda name: (bool(re.search(r'\bIQ\b', name, re.I)),
                                       tuple(int(n) for n in re.findall(r'\d+', name)), name.casefold()))


def status() -> dict:
    result = {'available': False, 'module_available': False, 'drivers': [], 'driver': '',
              'detail': 'The bundled ODBC module could not be loaded.'}
    try:
        library = _pyodbc()
    except Exception:
        return result
    result['module_available'] = True
    try:
        result['drivers'] = _drivers(library)
        result['driver'] = _driver(result['drivers'])
    except IqOdbcError as error:
        result['detail'] = str(error)
    else:
        result.update(available=True, detail='Connect through an installed 64-bit IQ ODBC driver.')
    return result


@contextlib.contextmanager
def _stage(payload):
    """Adopt only the parent's empty reservation, never truncate or unlink it."""
    descriptor = None
    try:
        path, identity = payload.get('path'), payload.get('stage_identity')
        if not isinstance(path, str) or not path or '\0' in path:
            raise ValueError
        if not isinstance(identity, (list, tuple)) or len(identity) != 2 or any(type(n) is not int for n in identity):
            raise ValueError
        def check(info):
            if (not stat.S_ISREG(info.st_mode) or info.st_size != 0 or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != tuple(identity)):
                raise ValueError
        check(os.lstat(path))
        descriptor = os.open(path, os.O_WRONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        check(os.fstat(descriptor))
        check(os.lstat(path))
        output = os.fdopen(descriptor, 'wb')
        descriptor = None
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise _diagnostic('stage') from None
    with output:
        yield output


def _connected_operation(payload, connection, output, emit):
    library = None
    native = cursor = None
    stage = 'driver-resolution'
    try:
        library = _pyodbc()
        driver = _driver(_drivers(library), connection['driver'])
        emit({'phase': 'connecting'})
        stage = 'connect'
        native = library.connect(build_connection_string(connection, driver), autocommit=False, timeout=15,
                                 attrs_before={getattr(library, 'SQL_ATTR_ACCESS_MODE', 101):
                                               getattr(library, 'SQL_MODE_READ_ONLY', 1)})
        stage = 'startup'
        native.timeout = 0
        cursor = native.cursor()
        stage = 'IQ-identity'
        cursor.execute('SELECT @@version')
        row = cursor.fetchone()
        version = row[0] if row is not None and len(row) else None
        if not isinstance(version, str) or not re.search(r'\bIQ\b', version, re.I) or len(version) > 4096:
            raise _diagnostic(stage)
        version = ' '.join(version.split())
        if output is None:
            return {'driver': driver, 'version': version}
        cursor.close()
        cursor = None
        stage = 'startup'
        cursor = native.cursor()
        emit({'phase': 'querying'})
        stage = 'query'
        cursor.execute(payload['sql'])
        if not cursor.description:
            raise _diagnostic(stage)
        stage = 'writing'
        emit({'phase': 'writing'})
        return iq_files.write_result(cursor, output, payload['options'], emit=emit)
    except IqOdbcError:
        raise
    except Exception as error:
        raise _diagnostic(stage, error, library) from None
    finally:
        if cursor is not None:
            with contextlib.suppress(Exception):
                cursor.close()
        if native is not None:
            with contextlib.suppress(Exception):
                native.rollback()
            with contextlib.suppress(Exception):
                native.close()


def run_operation(payload, emit=None) -> dict:
    """Execute one bounded worker request; credentials never leave memory."""
    if not isinstance(payload, dict):
        raise IqOdbcError('Worker request must be an object.')
    emit = emit or (lambda record: None)
    operation = payload.get('operation')
    if operation == 'status':
        return status()
    if operation == 'pick-directory':
        from .ase_picker import pick_directory
        return pick_directory(payload.get('initial_directory'))
    if operation not in ('connect', 'export'):
        raise IqOdbcError('Unknown IQ worker operation.')
    connection = connection_data(payload.get('connection'))
    if operation == 'connect':
        return _connected_operation(payload, connection, None, emit)
    payload = {**payload, 'sql': iq_sql.validate_select(payload.get('sql')),
               'options': iq_files.parse_options(payload.get('options', {}))}
    with _stage(payload) as output:
        return _connected_operation(payload, connection, output, emit)
