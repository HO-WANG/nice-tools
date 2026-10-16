"""IQ adapter coverage at the proprietary ODBC boundary."""
import importlib
import os
import sys
from decimal import Decimal
import pytest

CONNECTION = {'host': 'iq.internal', 'user': 'reader', 'password': 'private-password'}


def adapter():
    try:
        return importlib.import_module('nice_tools.iq_odbc')
    except ImportError:
        pytest.fail('IQ ODBC adapter is not implemented')


class NativeError(Exception):
    pass


class Cursor:
    def __init__(self, db):
        self.db = db
        self.closed = False
        self.description = [('이름',), ('amount',)]
        self.rows = [('한글', Decimal('123.4500')), ('', None)]

    def execute(self, sql):
        self.db.sql.append(sql)
        if self.db.execute_error:
            raise self.db.execute_error
        return self

    def fetchone(self):
        if self.db.identity_error:
            raise self.db.identity_error
        return (self.db.version,)

    def fetchmany(self, size):
        if self.db.fetch_error:
            raise self.db.fetch_error
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, db):
        self.db = db
        self.closed = self.rolled_back = False
        self.cursors = []
        self.timeout = None

    def cursor(self):
        cursor = Cursor(self.db)
        if self.db.no_result:
            cursor.description = None
        if self.db.empty:
            cursor.rows = []
        self.cursors.append(cursor)
        return cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class Odbc:
    Error = NativeError
    SQL_ATTR_ACCESS_MODE = 101
    SQL_MODE_READ_ONLY = 1
    pooling = True

    def __init__(self):
        self.names = ['SQL Server', 'FreeTDS', 'SAP ASE ODBC Driver', 'SQL Anywhere 17', 'SAP IQ 16.1', 'SAP IQ 16.0']
        self.version = 'SAP IQ/16.1.050'
        self.connections = []
        self.calls = []
        self.sql = []
        self.execute_error = self.identity_error = self.fetch_error = self.connect_error = None
        self.no_result = self.empty = False

    def drivers(self):
        assert self.pooling is False
        return self.names

    def connect(self, text, **kwargs):
        assert self.pooling is False
        self.calls.append((text, kwargs))
        if self.connect_error:
            raise self.connect_error
        connection = Connection(self)
        self.connections.append(connection)
        return connection


@pytest.fixture
def db(monkeypatch):
    module = adapter()
    library = Odbc()
    monkeypatch.setitem(sys.modules, 'pyodbc', library)
    return module, library


def export_payload(tmp_path, **extra):
    path = tmp_path / '.nice-tools-stage'
    path.touch()
    info = path.stat()
    return {'operation': 'export', 'connection': dict(CONNECTION), 'sql': 'SELECT name, amount FROM t',
            'options': {}, 'path': str(path), 'stage_identity': [info.st_dev, info.st_ino], **extra}


def test_discovery_filters_drivers_and_recommends_latest_iq(db):
    module, library = db
    result = module.status()
    assert result == {'available': True, 'module_available': True,
                      'drivers': ['SQL Anywhere 17', 'SAP IQ 16.1', 'SAP IQ 16.0'],
                      'driver': 'SAP IQ 16.1', 'detail': result['detail']}
    library.names = ['Adaptive Server IQ 15', 'Sybase IQ 16', 'SQL Anywhere 9']
    assert module.status()['driver'] == 'Sybase IQ 16'
    library.names = ['SQL Anywhere 9', 'SQL Anywhere 17', 'SAP ASE ODBC Driver', 'FreeTDS IQ']
    assert module.status()['driver'] == 'SQL Anywhere 17'


def test_missing_module_and_driver_are_graceful(db, monkeypatch):
    module, library = db
    library.names = ['SAP ASE ODBC Driver']
    assert module.status()['available'] is False
    monkeypatch.setitem(sys.modules, 'pyodbc', None)
    assert module.status()['module_available'] is False


@pytest.mark.parametrize('change', [{'dsn': 'local'}, {'sql_ini_path': 'secret'}, {'port': True}, {'port': 0},
    {'host': 'x;PWD=bad'}, {'user': 'a\n'}, {'password': 'x\0'}, {'database': 'a\x7f'}, {'query_timeout': 2}])
def test_connection_validation(db, change):
    module, _ = db
    with pytest.raises(ValueError):
        module.connection_data(CONNECTION | change)


def test_connection_values_are_preserved_and_odbc_quoted(db):
    module, _ = db
    normalized = module.connection_data(CONNECTION | {'database': 'db};UID=other', 'server_name': '{engine}',
        'user': ' a;b}', 'password': '{secret};x', 'port': '2639'})
    assert normalized['port'] == 2639
    text = module.build_connection_string(normalized, 'SAP IQ 16;name}')
    assert text == ('DRIVER={SAP IQ 16;name}}};HOST=iq.internal:2639;UID={ a;b}}};'
                    'PWD={{secret}};x};DBN={db}};UID=other};ENG={{engine}}};')


def test_connection_identity_readonly_options_and_cleanup(db):
    module, library = db
    assert module.run_operation({'operation': 'connect', 'connection': CONNECTION}) == {
        'driver': 'SAP IQ 16.1', 'version': library.version}
    assert library.calls[0][1] == {'autocommit': False, 'timeout': 15, 'attrs_before': {101: 1}}
    assert 'HOST=iq.internal:2638;' in library.calls[0][0]
    assert library.sql == ['SELECT @@version']
    connection = library.connections[0]
    assert connection.timeout == 0
    assert connection.closed and connection.rolled_back and all(c.closed for c in connection.cursors)


@pytest.mark.parametrize('driver', ['SAP ASE ODBC Driver', 'unregistered IQ', 'FreeTDS', 'SAP IQ 19'])
def test_explicit_driver_must_be_compatible_and_registered(db, driver):
    module, library = db
    with pytest.raises(ValueError):
        module.run_operation({'operation': 'connect', 'connection': CONNECTION | {'driver': driver}})
    assert not library.calls


@pytest.mark.parametrize('failure', ['version', 'identity_error', 'execute_error', 'fetch_error'])
def test_all_native_failures_release_connection_and_sanitize(db, tmp_path, failure):
    module, library = db
    if failure == 'version':
        library.version = 'SQL Anywhere 17'
    else:
        setattr(library, failure, NativeError('42000', 'private-password SELECT secret /private/path'))
    with pytest.raises(ValueError) as error:
        module.run_operation(export_payload(tmp_path))
    assert 'private-password' not in str(error.value)
    assert 'SELECT secret' not in str(error.value)
    assert '/private/path' not in str(error.value)
    connection = library.connections[0]
    assert connection.closed and connection.rolled_back and all(c.closed for c in connection.cursors)
    if failure != 'version':
        assert '42000' in str(error.value)


@pytest.mark.parametrize('error, expected', [(NativeError('28000', 'password'), '28000'),
    (NativeError('P4SSW', 'password'), None), (RuntimeError('28000', 'password'), None)])
def test_only_known_sqlstate_from_typed_native_errors_is_exposed(db, error, expected):
    module, library = db
    library.connect_error = error
    with pytest.raises(ValueError) as captured:
        module.run_operation({'operation': 'connect', 'connection': CONNECTION})
    assert getattr(captured.value, 'sqlstate', None) == expected
    assert 'password' not in str(captured.value)


def test_export_writes_complete_euckr_file_and_reports_numeric_progress(db, tmp_path):
    module, library = db
    payload = export_payload(tmp_path)
    events = []
    result = module.run_operation(payload, events.append)
    expected = '한글|123.4500\n'.encode('euc-kr') + b'\0|\n'
    assert open(payload['path'], 'rb').read() == expected
    assert result == {'rows': 2, 'bytes_written': len(expected), 'columns': ['이름', 'amount']}
    assert [event['phase'] for event in events if 'phase' in event][:3] == ['connecting', 'querying', 'writing']
    assert any(event.get('rows') == 2 and event.get('bytes_written') == len(expected) for event in events)
    assert library.connections[0].closed


@pytest.mark.parametrize('header', [False, True])
def test_zero_rows_respect_header_option(db, tmp_path, header):
    module, library = db
    library.empty = True
    payload = export_payload(tmp_path, options={'include_header': header})
    result = module.run_operation(payload)
    data = open(payload['path'], 'rb').read()
    assert data == ('이름|amount\n'.encode('euc-kr') if header else b'')
    assert result['rows'] == 0


def test_select_validation_prevents_native_work(db, tmp_path):
    module, library = db
    with pytest.raises(ValueError):
        module.run_operation(export_payload(tmp_path, sql='SELECT 1; DELETE t'))
    assert not library.calls


def test_no_result_set_rejected_and_stage_fd_closed(db, tmp_path, monkeypatch):
    module, library = db
    library.no_result = True
    payload = export_payload(tmp_path)
    descriptors = []
    original_open = os.open
    def capture(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        descriptors.append(fd)
        return fd
    monkeypatch.setattr(os, 'open', capture)
    with pytest.raises(ValueError):
        module.run_operation(payload)
    assert descriptors
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize('case', ['wrong-identity', 'nonempty', 'symlink', 'hardlink', 'directory'])
def test_unrelated_or_changed_stage_is_never_modified(db, tmp_path, case):
    module, library = db
    payload = export_payload(tmp_path)
    path = tmp_path / '.nice-tools-stage'
    if case == 'wrong-identity':
        payload['stage_identity'][1] += 1
    elif case == 'nonempty':
        path.write_bytes(b'keep')
    elif case == 'symlink':
        target = tmp_path / 'unrelated'
        target.write_bytes(b'keep')
        path.unlink()
        path.symlink_to(target)
    elif case == 'hardlink':
        os.link(path, tmp_path / 'other-link')
    else:
        path.unlink()
        path.mkdir()
    with pytest.raises(ValueError):
        module.run_operation(payload)
    assert path.exists()
    assert not library.calls
    if case == 'nonempty' or case == 'symlink':
        assert path.read_bytes() == b'keep'


@pytest.mark.parametrize('value', ['x\u0085', 'x\u009f', 'x\ud800'])
def test_non_ascii_control_and_invalid_unicode_connection_values_are_rejected(db, value):
    module, _ = db
    with pytest.raises(ValueError):
        module.connection_data(CONNECTION | {'password': value})
