"""Failures keep safe, actionable evidence across the real worker boundary."""
import errno
import io
import json
from pathlib import Path
import sys
import time

import pytest

from nice_tools import local_compare_jobs as jobs
from nice_tools import local_compare_worker as worker


def comparison_payload(tmp_path):
    descriptors = {}
    for side, content in (('left', 'old\n'), ('right', 'new\n')):
        folder = tmp_path / side
        folder.mkdir()
        (folder / '한글.txt').write_text(content, encoding='utf-8')
        descriptors[side] = worker.folder_descriptor(folder)
    return {'operation': 'compare', 'cache_dir': str(tmp_path / 'cache'), **descriptors, 'options': {}}


def settle(manager, token, handle):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = manager.job(token, handle)
        if result['state'] not in ('queued', 'running'):
            return result
        time.sleep(.005)
    pytest.fail('Comparison did not settle')


def launch(manager, tmp_path, monkeypatch):
    payload = comparison_payload(tmp_path)
    token = manager.create_session()['session']
    def choose(session, initial, side):
        return {**payload[side], 'cancelled': False, 'file_count': 1, 'total_bytes': 4}
    monkeypatch.setattr(manager, '_choose_directory', choose)
    left = manager.pick_directory(token, 'left')['selection']['token']
    right = manager.pick_directory(token, 'right')['selection']['token']
    handle = manager.start(token, left, right, {})['job']
    return token, handle


@pytest.mark.parametrize(('error', 'code', 'number'), [
    (MemoryError('private-memory'), 'memory', None),
    (OSError(errno.ENOMEM, 'private-memory'), 'memory', errno.ENOMEM),
    (OSError(errno.ENOSPC, 'private-cache-path'), 'temp_storage', errno.ENOSPC),
    (OSError(errno.EMFILE, 'private-file-name'), 'resource', errno.EMFILE),
    (PermissionError(errno.EACCES, 'private-cache-path'), 'temp_access', errno.EACCES),
    (ValueError('private-file-text'), 'comparison_failed', None),
])
def test_worker_main_emits_only_static_codes_and_numeric_os_evidence(monkeypatch, error, code, number):
    def fail(*args):
        raise error
    monkeypatch.setattr(worker, 'execute', fail)
    output = io.StringIO()
    assert worker.main(io.StringIO('{}'), output) == 1
    event = json.loads(output.getvalue())
    assert event['event'] == 'error' and event.get('code') == code
    assert event.get('errno') == number
    assert 'private-' not in output.getvalue()
    assert set(event) <= {'event', 'code', 'detail', 'errno', 'winerror', 'exit_code'}


@pytest.mark.parametrize(('error', 'code'), [
    (MemoryError('private'), 'memory'),
    (OSError(errno.ENOSPC, 'private'), 'temp_storage'),
    (OSError(errno.ENOMEM, 'private'), 'memory'),
    (OSError(errno.EMFILE, 'private'), 'resource'),
])
@pytest.mark.parametrize('where', ['compare', 'report'])
def test_resource_failures_abort_instead_of_publishing_per_file_success(tmp_path, monkeypatch, error, code, where):
    payload = comparison_payload(tmp_path)
    def fail(*args, **kwargs):
        raise error
    if where == 'compare':
        monkeypatch.setattr(worker.engine, 'compare_files', fail)
    else:
        monkeypatch.setattr(worker, '_prepare_reports', fail)
    with pytest.raises(worker.ComparisonError) as caught:
        worker.execute(payload)
    assert caught.value.code == code
    assert not (Path(payload['cache_dir']) / 'manifest.json').exists()


def test_invalid_text_stays_a_per_file_error(tmp_path):
    payload = comparison_payload(tmp_path)
    for side in ('left', 'right'):
        (Path(payload[side]['path']) / '한글.txt').write_bytes(b'bad\0text')
    result = worker.execute(payload)
    assert result['summary']['error'] == 1


@pytest.mark.parametrize(('worker_tail', 'code', 'exit_code'), [
    ('sys.exit(17)', 'worker_exit', 17),
    ('sys.exit(0)', 'worker_protocol', 0),
    ("print('not-json', flush=True)", 'worker_protocol', None),
    ("print('[]', flush=True)", 'worker_protocol', None),
    ("print(json.dumps({'event':'error','code':'private-code','detail':'private-secret'}),flush=True);sys.exit(1)", 'comparison_failed', 1),
    ("print(json.dumps({'event':'error','code':'temp_storage','errno':28,'winerror':'private-secret','exit_code':False,'detail':'private-secret'}),flush=True);sys.exit(1)", 'temp_storage', 1),
])
def test_failed_owned_job_retains_phase_path_and_sanitized_process_evidence(tmp_path, monkeypatch, capsys, worker_tail, code, exit_code):
    script = tmp_path / 'worker.py'
    script.write_text("import sys,json\njson.load(sys.stdin)\n"
                      "print(json.dumps({'event':'progress','phase':'preparing-reports','path':'한글.txt','completed':0,'total':1}),flush=True)\n"
                      + worker_tail + '\n', encoding='utf-8')
    manager = jobs.ComparisonManager(worker_command=[sys.executable, str(script)])
    try:
        token, handle = launch(manager, tmp_path, monkeypatch)
        result = settle(manager, token, handle)
        assert result['state'] == 'failed' and result.get('code') == code
        assert result.get('failure_phase') == 'preparing-reports'
        assert result['path'] == '한글.txt'
        if exit_code is not None:
            assert result.get('exit_code') == exit_code
        if code == 'temp_storage':
            assert result['errno'] == 28 and 'winerror' not in result
        assert 'private-' not in json.dumps(result)
        assert not Path(manager.sessions[token]['jobs'][handle]['cache_dir']).exists()
        assert not manager.processes and not manager.process_jobs
        console = capsys.readouterr().out
        assert code in console and 'preparing-reports' in console
        assert '한글.txt' not in console and 'private-' not in console
    finally:
        manager.close()


def test_untrusted_progress_path_does_not_reveal_absolute_or_traversal_path(tmp_path, monkeypatch):
    script = tmp_path / 'worker.py'
    script.write_text("import sys,json\njson.load(sys.stdin)\n"
                      "print(json.dumps({'event':'progress','phase':'comparing','path':'/private/cache/secret'}),flush=True)\n"
                      "sys.exit(3)\n", encoding='utf-8')
    manager = jobs.ComparisonManager(worker_command=[sys.executable, str(script)])
    try:
        token, handle = launch(manager, tmp_path, monkeypatch)
        result = settle(manager, token, handle)
        assert result['state'] == 'failed'
        assert 'path' not in result
    finally:
        manager.close()


def test_source_worker_memory_failure_reaches_job_not_generic(tmp_path, monkeypatch):
    script = tmp_path / 'resource-worker.py'
    script.write_text(
        'import sys\n'
        f'sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n'
        'from nice_tools import local_compare_worker as worker\n'
        "def fail(*args,**kwargs): raise MemoryError('private-memory-path')\n"
        'worker.engine.compare_files=fail\nraise SystemExit(worker.main())\n', encoding='utf-8')
    manager = jobs.ComparisonManager(worker_command=[sys.executable, str(script)])
    try:
        token, handle = launch(manager, tmp_path, monkeypatch)
        result = settle(manager, token, handle)
        assert result['state'] == 'failed' and result.get('code') == 'memory'
        assert result.get('failure_phase') == 'comparing'
        assert result['path'] == '한글.txt'
        assert 'private-' not in json.dumps(result)
    finally:
        manager.close()


@pytest.mark.parametrize(('winerror', 'code'), [(8, 'memory'), (14, 'memory'), (112, 'temp_storage'), (1450, 'resource'), (1455, 'resource'), (5, 'temp_access'), (32, 'temp_access')])
def test_windows_native_codes_are_classified_without_native_message(monkeypatch, winerror, code):
    error = OSError('private-windows-message')
    error.winerror = winerror
    def fail(*args):
        raise error
    monkeypatch.setattr(worker, 'execute', fail)
    output = io.StringIO()
    assert worker.main(io.StringIO('{}'), output) == 1
    event = json.loads(output.getvalue())
    assert event['code'] == code and event['winerror'] == winerror
    assert 'private-' not in output.getvalue()


@pytest.mark.parametrize('where', ['compare', 'report'])
def test_permission_failure_is_terminal_only_for_report_preparation(tmp_path, monkeypatch, where):
    payload = comparison_payload(tmp_path)
    def fail(*args, **kwargs):
        raise PermissionError(errno.EACCES, 'private-cache-path')
    monkeypatch.setattr(worker.engine if where == 'compare' else worker,
                        'compare_files' if where == 'compare' else '_prepare_reports', fail)
    if where == 'report':
        with pytest.raises(worker.ComparisonError) as caught:
            worker.execute(payload)
        assert caught.value.code == 'temp_access'
        assert not (Path(payload['cache_dir']) / 'manifest.json').exists()
    else:
        result = worker.execute(payload)
        assert result['summary']['error'] == 1
        assert 'private-' not in json.dumps(result)


def test_failure_cleanup_error_does_not_hide_original_reason(tmp_path, monkeypatch):
    manager = jobs.ComparisonManager()
    original_cleanup = None
    try:
        def fail(payload, session, progress=None, *, owner_job=None):
            nonlocal original_cleanup
            original_cleanup = owner_job['folder'].cleanup
            def locked():
                raise PermissionError(errno.EACCES, 'private-cleanup-path')
            monkeypatch.setattr(owner_job['folder'], 'cleanup', locked)
            raise MemoryError('private-memory')
        monkeypatch.setattr(manager, '_request', fail)
        token, handle = launch(manager, tmp_path, monkeypatch)
        result = settle(manager, token, handle)
        assert result['state'] == 'failed' and result['code'] == 'memory'
        assert 'private-' not in json.dumps(result)
        assert original_cleanup is not None
        monkeypatch.setattr(manager.sessions[token]['jobs'][handle]['folder'], 'cleanup', original_cleanup)
    finally:
        if original_cleanup is not None:
            original_cleanup()
        manager.close()
