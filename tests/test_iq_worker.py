"""Single-request IQ worker framing and secret handling."""
import importlib
import io
import json

import pytest


def worker():
    try:
        return importlib.import_module('nice_tools.iq_worker')
    except ImportError:
        pytest.fail('IQ worker is not implemented')


def run(module, payload):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    output = io.StringIO()
    code = module.main(io.BytesIO(raw), output)
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


def test_status_protocol_is_json_and_flushed(monkeypatch):
    module = worker()
    monkeypatch.setattr(module.iq_odbc, 'run_operation', lambda payload, emit: {'available': False, 'drivers': []})
    class Output(io.StringIO):
        flushes = 0
        def flush(self):
            self.flushes += 1
            super().flush()
    output = Output()
    assert module.main(io.BytesIO(b'{"operation":"status"}'), output) == 0
    assert json.loads(output.getvalue()) == {'event': 'result', 'result': {'available': False, 'drivers': []}}
    assert output.flushes == 1


def test_progress_and_result_redact_values_without_changing_keys_or_numbers(monkeypatch):
    module = worker()
    def operation(payload, emit):
        emit({'phase': 'writing', 'rows': 1, 'bytes_written': 11, 'message': '1 private'})
        return {'rows': 1, 'bytes_written': 11, 'columns': ['1'], 'path': '1-file'}
    monkeypatch.setattr(module.iq_odbc, 'run_operation', operation)
    code, events = run(module, {'operation': 'export', 'connection': {'password': '1'}})
    assert code == 0
    assert events[0]['event'] == 'progress'
    assert events[0]['rows'] == 1 and events[0]['bytes_written'] == 11
    assert '1' not in events[0]['message']
    assert events[1]['result']['rows'] == 1 and '1' not in events[1]['result']['columns'][0]
    code, events = run(module, {'operation': 'export', 'connection': {'password': 'result'}})
    assert code == 0 and events[1]['event'] == 'result' and 'result' in events[1]


def test_redaction_marker_cannot_itself_contain_password(monkeypatch):
    module = worker()
    monkeypatch.setattr(module.iq_odbc, 'run_operation', lambda payload, emit: {'detail': 'redacted-value'})
    code, events = run(module, {'operation': 'status', 'connection': {'password': 'redacted'}})
    assert code == 0
    assert 'redacted' not in events[0]['result']['detail']


@pytest.mark.parametrize('raw', [b'bad json', b'[]', b'{} {}', b'{"x":NaN}', b'\xff'])
def test_invalid_protocol_is_one_safe_error(raw):
    module = worker()
    code, events = run(module, raw)
    assert code == 1 and len(events) == 1 and events[0]['event'] == 'error'
    assert 'Traceback' not in events[0]['detail']


def test_oversized_request_rejected():
    module = worker()
    code, events = run(module, b' ' * (module.MAX_PAYLOAD_BYTES + 1))
    assert code == 1 and len(events) == 1
    assert 'limit' in events[0]['detail']


@pytest.mark.parametrize('error', [RuntimeError('SELECT secret FROM /private/path'),
    ValueError('SELECT secret FROM /private/path'), OSError('SELECT secret FROM /private/path')])
def test_unexpected_errors_do_not_expose_sql_or_path(monkeypatch, error):
    module = worker()
    def operation(payload, emit):
        raise error
    monkeypatch.setattr(module.iq_odbc, 'run_operation', operation)
    code, events = run(module, {'operation': 'export'})
    assert code == 1
    assert events == [{'event': 'error', 'detail': 'IQ operation failed.'}]


def test_known_safe_diagnostics_are_preserved_and_redacted(monkeypatch):
    module = worker()
    def operation(payload, emit):
        raise module.iq_odbc.IqOdbcError('Authentication failed secret', stage='connect', sqlstate='28000')
    monkeypatch.setattr(module.iq_odbc, 'run_operation', operation)
    code, events = run(module, {'operation': 'connect', 'connection': {'password': 'secret'}})
    assert code == 1
    assert '28000' in events[0]['detail'] and 'secret' not in events[0]['detail']


def test_library_prints_cannot_corrupt_protocol(monkeypatch, capsys):
    module = worker()
    def operation(payload, emit):
        import sys
        print('private-password')
        print('private-password', file=sys.stderr)
        emit({'phase': 'connecting'})
        return {'driver': 'SAP IQ'}
    monkeypatch.setattr(module.iq_odbc, 'run_operation', operation)
    code, events = run(module, {'operation': 'connect'})
    assert code == 0 and [event['event'] for event in events] == ['progress', 'result']
    captured = capsys.readouterr()
    assert 'private-password' not in captured.out + captured.err


def test_native_picker_operation_is_delegated(monkeypatch):
    module = worker()
    from nice_tools import ase_picker
    monkeypatch.setattr(ase_picker, 'pick_directory', lambda initial: {'cancelled': False, 'path': initial})
    code, events = run(module, {'operation': 'pick-directory', 'initial_directory': '/tmp'})
    assert code == 0 and events[0]['result'] == {'cancelled': False, 'path': '/tmp'}


def test_unknown_operation_is_safe():
    module = worker()
    code, events = run(module, {'operation': 'DROP secret'})
    assert code == 1 and 'DROP' not in events[0]['detail']


def test_worker_accepts_full_valid_sql_after_json_escaping(monkeypatch):
    module = worker()
    from nice_tools.iq_sql import validate_select
    sql = validate_select('SELECT 1 /*' + '\x7f' * (1024*1024-20) + '*/')
    monkeypatch.setattr(module.iq_odbc,'run_operation',lambda payload,emit: {'rows': 0, 'bytes_written': 0, 'columns': []})
    code, events=run(module,{'operation':'export','connection':{'password':'p'},'sql':sql})
    assert code==0 and events[-1]['event']=='result'
