import json
from pathlib import Path
import sys
import time

import pytest


def manager_for(tmp_path, mode='success'):
    from nice_tools.iq_jobs import IQManager
    script = tmp_path / 'iq-fixture.py'
    script.write_text('''import json,sys,time
from pathlib import Path
p=json.load(sys.stdin)
if p['operation']=='connect':
 print(json.dumps({'event':'result','result':{'driver':'SQL Anywhere 17','version':'SAP IQ/16.1'}}),flush=True)
elif p['operation']=='pick-directory':
 print(json.dumps({'event':'result','result':{'cancelled':False,'path':str(Path(__file__).parent/'chosen')}}),flush=True)
else:
 Path(p['path']).write_bytes(b'complete\\n')
 print(json.dumps({'event':'progress','phase':'writing','rows':1,'bytes_written':9}),flush=True)
 Path(__file__).with_name('export-started').touch()
 MODE
 print(json.dumps({'event':'result','result':{'rows':1,'bytes_written':9,'columns':['value']}}),flush=True)
'''.replace(' MODE', {
        'success': ' pass',
        'fail': " print(json.dumps({'event':'error','detail':'query failed '+p['connection']['password']}),flush=True); sys.exit(1)",
        'wait': ' time.sleep(60)',
    }[mode]), encoding='utf-8')
    return IQManager(worker_command=[sys.executable, str(script)])


def session_for(manager, tmp_path):
    token = manager.connect({'host': 'localhost', 'port': 2638, 'user': 'reader',
                             'password': 'iq-private-pass', 'database': 'iqdb'})['session']
    manager.sessions[token]['export_directory'] = str(tmp_path / 'exports')
    return token


def finished(manager, token, handle):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = manager.job(token, handle)
        if job['state'] in {'succeeded', 'failed', 'cancelled'}:
            return job
        time.sleep(.01)
    raise AssertionError('IQ job did not finish')


@pytest.mark.parametrize('name', ['../escape.txt', r'..\escape.txt', 'CON.txt', 'a|b.txt', '', 'x.'])
def test_invalid_filename_creates_no_output(tmp_path, name):
    manager = manager_for(tmp_path)
    try:
        token = session_for(manager, tmp_path)
        with pytest.raises(ValueError):
            manager.start_export(token, 'select 1', name, {})
        assert not (tmp_path / 'exports').exists()
    finally:
        manager.close()


def test_export_streams_public_progress_and_never_overwrites(tmp_path):
    manager = manager_for(tmp_path)
    try:
        token = session_for(manager, tmp_path)
        directory = tmp_path / 'exports'
        directory.mkdir()
        (directory / 'iq-result.txt').write_bytes(b'old')
        handle = manager.start_export(token, 'select 1', 'iq-result.txt', {})['job']
        result = finished(manager, token, handle)
        assert result['state'] == 'succeeded'
        assert result['rows'] == 1 and result['bytes_written'] == 9
        assert result['path'] == str(directory / 'iq-result_1.txt')
        assert result['elapsed_seconds'] >= 0
        assert result['rows_per_second'] is None or result['rows_per_second'] >= 0
        assert (directory / 'iq-result.txt').read_bytes() == b'old'
        assert Path(result['path']).read_bytes() == b'complete\n'
        assert not list(directory.glob('.nice-tools-*'))
        public = json.dumps(result)
        assert 'iq-private-pass' not in public and 'select 1' not in public
        assert 'process' not in public and '_reservation' not in public
    finally:
        manager.close()


@pytest.mark.parametrize('mode', ['fail', 'wait'])
def test_failed_or_cancelled_export_publishes_nothing(tmp_path, mode):
    manager = manager_for(tmp_path, mode)
    try:
        token = session_for(manager, tmp_path)
        handle = manager.start_export(token, 'select 1', 'iq-result.txt', {})['job']
        if mode == 'wait':
            deadline = time.monotonic() + 5
            while not (tmp_path / 'export-started').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert (tmp_path / 'export-started').exists()
            assert manager.job(token, handle)['rows'] == 1
            with pytest.raises(ValueError):
                manager.start_export(token, 'select 2', 'other.txt', {})
            manager.cancel(token, handle)
        result = finished(manager, token, handle)
        assert result['state'] == ('failed' if mode == 'fail' else 'cancelled')
        assert 'iq-private-pass' not in json.dumps(result)
        assert not list((tmp_path / 'exports').iterdir())
        assert not manager.processes
    finally:
        manager.close()


def test_disconnect_reaps_export_and_keeps_unrelated_files(tmp_path):
    manager = manager_for(tmp_path, 'wait')
    try:
        token = session_for(manager, tmp_path)
        handle = manager.start_export(token, 'select 1', 'iq-result.txt', {})['job']
        task = manager.sessions[token]['jobs'][handle]['thread']
        deadline = time.monotonic() + 5
        while not (tmp_path / 'export-started').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        directory = tmp_path / 'exports'
        (directory / 'keep.txt').write_bytes(b'user')
        manager.disconnect(token)
        task.join(timeout=5)
        assert not task.is_alive()
        assert list(directory.iterdir()) == [directory / 'keep.txt']
        assert (directory / 'keep.txt').read_bytes() == b'user'
        assert not manager.processes and token not in manager.sessions
    finally:
        manager.close()


@pytest.mark.parametrize('invalid', [{'rows': 1, 'bytes_written': 9, 'columns': None}, {'rows': 1, 'bytes_written': 9, 'columns': [False]}, {'rows': 10**350, 'bytes_written': 9, 'columns': []}])
def test_invalid_completion_never_publishes(tmp_path, invalid):
    manager = manager_for(tmp_path)
    script = tmp_path / 'iq-fixture.py'
    text = script.read_text()
    script.write_text(text.replace("{'rows':1,'bytes_written':9,'columns':['value']}", repr(invalid)))
    try:
        token = session_for(manager, tmp_path)
        handle = manager.start_export(token, 'select 1', 'iq-result.txt', {})['job']
        result = finished(manager, token, handle)
        assert result['state'] == 'failed'
        assert not list((tmp_path / 'exports').iterdir())
    finally:
        manager.close()


def test_redacted_driver_display_does_not_change_saved_connection(tmp_path):
    manager = manager_for(tmp_path)
    script = tmp_path / 'iq-fixture.py'
    script.write_text(script.read_text().replace("'driver':'SQL Anywhere 17'", "'driver':' Anywhere 17'"))
    try:
        result = manager.connect({'host': 'localhost', 'user': 'u', 'password': 'SQL', 'driver': 'SQL Anywhere 17'})
        assert manager.sessions[result['session']]['connection']['driver'] == 'SQL Anywhere 17'
        assert 'SQL' not in result['driver']
    finally:
        manager.close()


def test_queued_cancel_is_terminal_without_waiting_for_dispatcher(tmp_path, monkeypatch):
    from nice_tools import iq_jobs
    class Task:
        def join(self, timeout=None): pass
        def is_alive(self): return False
    callbacks=[]
    monkeypatch.setattr(iq_jobs, 'submit', lambda callback,*args: callbacks.append((callback,args)) or Task())
    manager=manager_for(tmp_path)
    try:
        token=session_for(manager,tmp_path)
        handle=manager.start_export(token,'select 1','first.txt',{})['job']
        result=manager.cancel(token,handle)
        assert result['state']=='cancelled' and result['phase']=='done'
        manager.start_export(token,'select 2','second.txt',{})
        callback,args=callbacks[0]
        callback(*args)
        assert not (tmp_path/'exports').exists()
    finally:
        manager.close()


def test_failure_is_terminal_only_after_owned_stage_cleanup(tmp_path, monkeypatch):
    import threading
    from nice_tools import iq_jobs
    entered, release = threading.Event(), threading.Event()
    original_cleanup = iq_jobs.ExportReservation.cleanup
    def cleanup(reservation):
        entered.set()
        assert release.wait(5)
        original_cleanup(reservation)
    monkeypatch.setattr(iq_jobs.ExportReservation, 'cleanup', cleanup)
    manager = manager_for(tmp_path, 'fail')
    try:
        token = session_for(manager, tmp_path)
        handle = manager.start_export(token, 'select 1', 'iq-result.txt', {})['job']
        assert entered.wait(5)
        assert manager.job(token, handle)['state'] == 'running'
        release.set()
        result = finished(manager, token, handle)
        assert result['state'] == 'failed'
        assert not list((tmp_path / 'exports').iterdir())
        assert not manager.processes
    finally:
        release.set()
        manager.close()


def test_failed_export_prints_sanitized_detail_to_launcher_console(tmp_path, capsys):
    manager=manager_for(tmp_path,'fail')
    try:
        token=session_for(manager,tmp_path)
        handle=manager.start_export(token,'select sensitive_literal','iq-result.txt',{})['job']
        result=finished(manager,token,handle)
        output=capsys.readouterr().out
        assert result['state']=='failed'
        assert '[IQ export failed]' in output and 'query failed' in output
        assert 'iq-private-pass' not in output and 'select sensitive_literal' not in output
    finally:
        manager.close()


def test_failed_connect_prints_error_without_credentials(tmp_path, capsys):
    manager=manager_for(tmp_path)
    script=tmp_path/'iq-fixture.py'
    text=script.read_text()
    text=text.replace("print(json.dumps({'event':'result','result':{'driver':'SQL Anywhere 17','version':'SAP IQ/16.1'}}),flush=True)",
                      "print(json.dumps({'event':'error','detail':'connect failed '+p['connection']['password']}),flush=True); sys.exit(1)")
    script.write_text(text)
    try:
        with pytest.raises(ValueError):
            manager.connect({'host':'localhost','user':'reader','password':'iq-private-pass'})
        output=capsys.readouterr().out
        assert '[IQ connect failed]' in output and 'connect failed' in output
        assert 'iq-private-pass' not in output and not manager.sessions
    finally:
        manager.close()


def test_console_failure_tolerates_unavailable_stdout(tmp_path, monkeypatch):
    from nice_tools import iq_jobs
    manager=manager_for(tmp_path,'fail')
    def unavailable(text): raise OSError('closed output')
    monkeypatch.setattr(iq_jobs,'_write_bcp_output',unavailable)
    try:
        token=session_for(manager,tmp_path)
        handle=manager.start_export(token,'select 1','iq-result.txt',{})['job']
        assert finished(manager,token,handle)['state']=='failed'
        assert not list((tmp_path/'exports').iterdir())
    finally:
        manager.close()


def test_console_redacts_complete_message_before_control_escaping(tmp_path, capsys):
    manager=manager_for(tmp_path)
    try:
        manager._console_error('export','private-password\x1b[31m reason=encoding row 1 column 2',{'password':'private-password'})
        output=capsys.readouterr().out
        assert 'private-password' not in output and '\x1b' not in output
        assert 'reason=encoding' in output and 'row 1' in output
    finally:
        manager.close()


def test_real_iq_worker_delivers_cell_failure_to_parent_console(tmp_path, capsys):
    from nice_tools.iq_jobs import IQManager
    script=tmp_path/'real-iq-worker-fixture.py'
    script.write_text('import sys\nsys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]))+')\n'+'''
from nice_tools import iq_odbc,iq_worker
class Cursor:
 description=[('value',)]
 def execute(self,sql): return self
 def fetchone(self): return ('SAP IQ/16.1',)
 def fetchmany(self,size): return [('ROW-SENSITIVE|VALUE',)]
 def close(self): pass
class Connection:
 def cursor(self): return Cursor()
 def rollback(self): pass
 def close(self): pass
class Odbc:
 class Error(Exception): pass
 def drivers(self): return ['SAP IQ']
 def connect(self,*args,**kwargs): return Connection()
iq_odbc._pyodbc=lambda: Odbc()
raise SystemExit(iq_worker.main())
''',encoding='utf-8')
    manager=IQManager(worker_command=[sys.executable,str(script)])
    try:
        token=session_for(manager,tmp_path)
        handle=manager.start_export(token,'select SENSITIVE_SQL','iq-result.txt',{})['job']
        result=finished(manager,token,handle)
        output=capsys.readouterr().out
        assert result['state']=='failed' and 'reason=field-terminator' in result['message']
        assert '[IQ export failed]' in output and 'row 1' in output and 'column 1' in output
        assert 'field-terminator' in output and 'encoding=euc-kr' in output
        assert not any(secret in output for secret in ['iq-private-pass','SENSITIVE_SQL','ROW-SENSITIVE'])
        assert not list((tmp_path/'exports').iterdir())
    finally:
        manager.close()
