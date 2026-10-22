import importlib
import json
from pathlib import Path
import sys
import time

import pytest


def jobs():
    try:
        return importlib.import_module('nice_tools.local_compare_jobs')
    except ModuleNotFoundError:
        pytest.fail('Native comparison job ownership is not implemented')


def ready(manager, session, job):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = manager.job(session, job)
        if result['state'] in ('succeeded', 'failed', 'cancelled'):
            return result
        time.sleep(.01)
    pytest.fail('Comparison did not settle')


def select_pair(manager, tmp_path, monkeypatch):
    left, right = tmp_path / 'left', tmp_path / 'right'
    left.mkdir(); right.mkdir()
    (left / 'data.txt').write_bytes(b'old\n')
    (right / 'data.txt').write_bytes(b'new\n')
    session = manager.create_session()['session']
    from nice_tools.local_compare_worker import folder_descriptor
    def choose(token, initial, side):
        root = left if side == 'left' else right
        return {**folder_descriptor(root), 'cancelled':False,'name':root.name,'file_count':1,'total_bytes':4}
    monkeypatch.setattr(manager, '_choose_directory', choose)
    a = manager.pick_directory(session, 'left')['selection']['token']
    b = manager.pick_directory(session, 'right')['selection']['token']
    return session, a, b


def test_real_worker_compare_and_diff_reuse_owned_cache(tmp_path, monkeypatch):
    manager = jobs().ComparisonManager()
    try:
        session,left,right = select_pair(manager,tmp_path,monkeypatch)
        handle = manager.start(session,left,right,{})['job']
        result = ready(manager,session,handle)
        assert result['state']=='succeeded' and result['summary']['different']==1
        page = manager.diff(session,handle,'data.txt','compared',0)
        assert page['additions']==page['deletions']==1
        assert len(page['lines'])==3
        path = Path(manager.sessions[session]['jobs'][handle]['cache_dir'])
        assert path.exists()
        manager.close_session(session)
        assert not path.exists() and not manager.processes
        with pytest.raises(jobs().SessionExpired): manager.job(session,handle)
    finally:
        manager.close()


def test_selection_tokens_belong_to_the_session(tmp_path, monkeypatch):
    manager = jobs().ComparisonManager()
    try:
        session,left,right = select_pair(manager,tmp_path,monkeypatch)
        other = manager.create_session()['session']
        with pytest.raises(ValueError): manager.start(other,left,right,{})
        assert not manager.sessions[other]['jobs']
    finally:
        manager.close()


def test_abandoned_picker_cannot_install_a_late_selection(tmp_path,monkeypatch):
    import threading
    manager = jobs().ComparisonManager()
    session = manager.create_session()['session']
    started,release = threading.Event(),threading.Event()
    root = tmp_path / 'root'; root.mkdir()
    from nice_tools.local_compare_worker import folder_descriptor
    def choose(*args):
        started.set(); assert release.wait(3)
        return {**folder_descriptor(root),'cancelled':False,'name':'root','file_count':0,'total_bytes':0}
    monkeypatch.setattr(manager,'_choose_directory',choose)
    errors=[]
    def pick():
        try: manager.pick_directory(session,'left')
        except ValueError as error: errors.append(error)
    task=threading.Thread(target=pick);task.start()
    try:
        assert started.wait(2)
        manager.close_session(session)
    finally:
        release.set();task.join(3);manager.close()
    assert errors and not manager.sessions


def test_cancel_kills_owned_worker_and_removes_cache_before_terminal(tmp_path,monkeypatch):
    script=tmp_path/'wait-worker.py'
    script.write_text("import json,sys,time\np=json.load(sys.stdin)\nprint(json.dumps({'event':'progress','phase':'comparing','completed':0,'total':1}),flush=True)\ntime.sleep(60)\n",encoding='utf-8')
    manager = jobs().ComparisonManager(worker_command=[sys.executable,str(script)])
    try:
        session,left,right=select_pair(manager,tmp_path,monkeypatch)
        handle=manager.start(session,left,right,{})['job']
        deadline=time.monotonic()+4
        while not manager.processes and time.monotonic()<deadline: time.sleep(.01)
        assert manager.processes
        cache=Path(manager.sessions[session]['jobs'][handle]['cache_dir'])
        result=manager.cancel(session,handle)
        assert result['state']=='cancelled'
        assert not cache.exists() and not manager.processes
    finally:
        manager.close()


def test_cancel_before_registration_does_not_launch_worker(tmp_path, monkeypatch):
    import threading
    marker = tmp_path / 'worker-started'
    script = tmp_path / 'marker-worker.py'
    script.write_text(
        "import json,sys\nfrom pathlib import Path\np=json.load(sys.stdin)\n"
        f"Path({str(marker)!r}).write_text('started')\n"
        "print(json.dumps({'event':'result','result':{'summary':{},'results':[]}}),flush=True)\n",
        encoding='utf-8')
    manager = jobs().ComparisonManager(worker_command=[sys.executable, str(script)])
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    original_request = manager._request
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original_request(*args, **kwargs)
    monkeypatch.setattr(manager, '_request', paused)
    cancel_thread = None
    errors = []
    try:
        session, left, right = select_pair(manager, tmp_path, monkeypatch)
        handle = manager.start(session, left, right, {})['job']
        assert entered.wait(3)
        job = manager.sessions[session]['jobs'][handle]
        original_cancel = job['future'].cancel
        def signal_cancel():
            cancelled.set()
            return original_cancel()
        monkeypatch.setattr(job['future'], 'cancel', signal_cancel)
        def cancel():
            try:
                manager.cancel(session, handle)
            except Exception as error:
                errors.append(error)
        cancel_thread = threading.Thread(target=cancel)
        cancel_thread.start()
        assert cancelled.wait(3)
        release.set()
        cancel_thread.join(3)
        assert not cancel_thread.is_alive() and not errors
        assert not marker.exists(), 'Cancelled job launched a worker after cancellation'
        assert manager.job(session, handle)['state'] == 'cancelled'
        assert not Path(job['cache_dir']).exists() and not manager.processes
    finally:
        release.set()
        if cancel_thread:
            cancel_thread.join(3)
        manager.close()


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    pytest.fail('Worker did not reach its synchronization point')


def blocking_manager(tmp_path):
    """Real workers use files as cross-process events, never duration sleeps."""
    paths = {name: tmp_path / name for name in ('hold-compare', 'compare-started', 'diff-started', 'release-diff')}
    script = tmp_path / 'blocking-worker.py'
    script.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from nice_tools.local_compare_worker import execute\np=json.load(sys.stdin)\n"
        f"hold=Path({str(paths['hold-compare'])!r})\n"
        "if p['operation']=='compare' and hold.exists():\n"
        f"    Path({str(paths['compare-started'])!r}).write_text('started')\n"
        "    while hold.exists(): time.sleep(.005)\n"
        "if p['operation']=='diff':\n"
        f"    Path({str(paths['diff-started'])!r}).write_text('started')\n"
        f"    while not Path({str(paths['release-diff'])!r}).exists(): time.sleep(.005)\n"
        "result=execute(p)\nprint(json.dumps({'event':'result','result':result}),flush=True)\n",
        encoding='utf-8')
    return jobs().ComparisonManager(worker_command=[sys.executable, str(script)]), paths


def start_diff_thread(manager, session, handle):
    import threading
    results, errors = [], []
    def run():
        try:
            results.append(manager.diff(session, handle, 'data.txt'))
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    return thread, results, errors


def test_cancelling_new_compare_preserves_older_job_diff(tmp_path, monkeypatch):
    manager, events = blocking_manager(tmp_path)
    diff_thread = None
    try:
        session, left, right = select_pair(manager, tmp_path, monkeypatch)
        old_handle = manager.start(session, left, right, {})['job']
        assert ready(manager, session, old_handle)['state'] == 'succeeded'
        diff_thread, results, errors = start_diff_thread(manager, session, old_handle)
        wait_until(events['diff-started'].exists)
        events['hold-compare'].touch()
        new_handle = manager.start(session, left, right, {})['job']
        wait_until(events['compare-started'].exists)
        assert manager.cancel(session, new_handle)['state'] == 'cancelled'
        events['release-diff'].touch()
        diff_thread.join(5)
        assert not diff_thread.is_alive()
        assert not errors, 'Cancelling a new comparison killed another job’s diff'
        assert results[0]['additions'] == results[0]['deletions'] == 1
        assert not manager.processes
    finally:
        events['release-diff'].touch()
        events['hold-compare'].unlink(missing_ok=True)
        manager.close()
        if diff_thread:
            diff_thread.join(5)


def test_busy_diff_fails_promptly_with_retry_guidance(tmp_path, monkeypatch):
    import threading
    manager, events = blocking_manager(tmp_path)
    first_thread = second_thread = None
    done, errors = threading.Event(), []
    try:
        session, left, right = select_pair(manager, tmp_path, monkeypatch)
        handle = manager.start(session, left, right, {})['job']
        assert ready(manager, session, handle)['state'] == 'succeeded'
        first_thread, first_results, first_errors = start_diff_thread(manager, session, handle)
        wait_until(events['diff-started'].exists)
        def second():
            try:
                manager.diff(session, handle, 'data.txt', 'original')
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        second_thread = threading.Thread(target=second)
        second_thread.start()
        assert done.wait(2), 'Another diff request waited on the active build lock'
        assert len(errors) == 1 and isinstance(errors[0], ValueError)
        assert '다시' in str(errors[0])
        events['release-diff'].touch()
        first_thread.join(5)
        assert first_results and not first_errors
        assert manager.diff(session, handle, 'data.txt', 'original')['additions'] == 1
    finally:
        events['release-diff'].touch()
        manager.close()
        for thread in (first_thread, second_thread):
            if thread:
                thread.join(5)


def test_evicting_old_job_kills_its_diff_and_cleans_cache(tmp_path, monkeypatch):
    import threading
    manager, events = blocking_manager(tmp_path)
    diff_thread = start_thread = None
    done, handles, start_errors = threading.Event(), [], []
    try:
        session, left, right = select_pair(manager, tmp_path, monkeypatch)
        old_handle = manager.start(session, left, right, {})['job']
        assert ready(manager, session, old_handle)['state'] == 'succeeded'
        old_cache = Path(manager.sessions[session]['jobs'][old_handle]['cache_dir'])
        second_handle = manager.start(session, left, right, {})['job']
        assert ready(manager, session, second_handle)['state'] == 'succeeded'
        diff_thread, _, diff_errors = start_diff_thread(manager, session, old_handle)
        wait_until(events['diff-started'].exists)
        def third():
            try:
                handles.append(manager.start(session, left, right, {})['job'])
            except Exception as error:
                start_errors.append(error)
            finally:
                done.set()
        start_thread = threading.Thread(target=third)
        start_thread.start()
        assert done.wait(3), 'Eviction waited for the old diff instead of terminating its worker'
        diff_thread.join(3)
        assert diff_errors and not diff_thread.is_alive()
        assert handles and not start_errors
        assert ready(manager, session, handles[0])['state'] == 'succeeded'
        assert not old_cache.exists()
        assert old_handle not in manager.sessions[session]['jobs']
        assert not manager.processes and not manager.process_jobs
    finally:
        events['release-diff'].touch()
        manager.close()
        for thread in (diff_thread, start_thread):
            if thread:
                thread.join(5)


def test_closing_session_kills_all_job_workers_and_cleans_ownership(tmp_path, monkeypatch):
    manager, events = blocking_manager(tmp_path)
    diff_thread = None
    try:
        session, left, right = select_pair(manager, tmp_path, monkeypatch)
        old_handle = manager.start(session, left, right, {})['job']
        assert ready(manager, session, old_handle)['state'] == 'succeeded'
        diff_thread, _, errors = start_diff_thread(manager, session, old_handle)
        wait_until(events['diff-started'].exists)
        events['hold-compare'].touch()
        manager.start(session, left, right, {})
        wait_until(events['compare-started'].exists)
        caches = [Path(job['cache_dir']) for job in manager.sessions[session]['jobs'].values()]
        assert len(manager.processes) == 2
        manager.close_session(session)
        diff_thread.join(3)
        assert not diff_thread.is_alive() and errors
        assert not manager.sessions and not manager.processes and not manager.process_jobs
        assert not any(path.exists() for path in caches)
    finally:
        events['release-diff'].touch()
        events['hold-compare'].unlink(missing_ok=True)
        manager.close()
        if diff_thread:
            diff_thread.join(5)


def test_cached_pages_skip_worker_startup_and_revalidate_sources(tmp_path, monkeypatch):
    manager = jobs().ComparisonManager()
    try:
        session, left, right = select_pair(manager, tmp_path, monkeypatch)
        (tmp_path / 'right' / 'data.txt').write_text('new\n' * 451, encoding='utf-8')
        handle = manager.start(session, left, right, {})['job']
        assert ready(manager, session, handle)['state'] == 'succeeded'
        first = manager.diff(session, handle, 'data.txt')
        assert first['pages'] >= 3
        def no_worker(*args, **kwargs):
            pytest.fail('Cached page started another worker')
        monkeypatch.setattr(manager, '_request', no_worker)
        second = manager.diff(session, handle, 'data.txt', page=1)
        assert second['page'] == 1 and len(second['lines']) == 200
        assert second['additions'] == first['additions'] == 451
        assert not manager.processes and not manager.process_jobs
        (tmp_path / 'right' / 'data.txt').write_text('changed after comparison\n', encoding='utf-8')
        with pytest.raises(ValueError, match='변경'):
            manager.diff(session, handle, 'data.txt', page=1)
    finally:
        manager.close()


def error_worker(tmp_path, code):
    script = tmp_path / 'error-worker.py'
    event = {'event': 'error', 'code': code, 'detail': 'native-private-source-value'}
    script.write_text('import json,sys\njson.load(sys.stdin)\n'
                      f'print({json.dumps(event)!r}, flush=True)\nsys.exit(1)\n', encoding='utf-8')
    return jobs().ComparisonManager(worker_command=[sys.executable, str(script)])


@pytest.mark.parametrize('code', ['changed', 'file_limit'])
def test_worker_safe_error_codes_are_preserved_without_native_detail(tmp_path, code):
    from nice_tools.local_compare_worker import ComparisonError, SAFE_ERRORS
    manager = error_worker(tmp_path, code)
    try:
        token = manager.create_session()['session']
        with pytest.raises(ComparisonError) as caught:
            manager._request({'operation': 'pick-directory'}, token)
        assert caught.value.code == code
        assert str(caught.value) == SAFE_ERRORS[code]
        assert not manager.processes and not manager.process_jobs
    finally:
        manager.close()


def test_worker_unknown_error_code_cannot_forward_native_detail(tmp_path):
    manager = error_worker(tmp_path, 'unknown-native-error')
    try:
        token = manager.create_session()['session']
        with pytest.raises(ValueError) as caught:
            manager._request({'operation': 'pick-directory'}, token)
        assert 'native-private-source-value' not in str(caught.value)
    finally:
        manager.close()


def test_failed_job_retains_safe_actionable_reason(tmp_path, monkeypatch):
    from nice_tools.local_compare_worker import SAFE_ERRORS
    manager = error_worker(tmp_path, 'file_limit')
    try:
        token, left, right = select_pair(manager, tmp_path, monkeypatch)
        handle = manager.start(token, left, right, {})['job']
        result = ready(manager, token, handle)
        assert result['state'] == 'failed'
        assert result.get('code') == 'file_limit'
        assert result['message'] == SAFE_ERRORS['file_limit']
        assert not Path(manager.sessions[token]['jobs'][handle]['cache_dir']).exists()
    finally:
        manager.close()
