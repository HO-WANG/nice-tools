"""Session-owned directory grants, native workers and disposable diff caches."""
from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time

from .ase_jobs import AseManager
from .local_compare_worker import ComparisonError, SAFE_ERRORS, execute, folder_descriptor, options_data, relative_path


IDLE_SECONDS = 30 * 60
MAX_SESSIONS = 8


class SessionExpired(ValueError):
    pass


class ComparisonManager(AseManager):
    """Reuse the established Windows Job/POSIX process-tree owner only.

    HTTP can address only opaque grants minted after the native dialog. Heavy
    file reads run in owned workers; only metadata and a diff page use JSON.
    """

    def __init__(self, worker_command=None):
        command = worker_command or ([sys.executable, '--comparison-worker'] if getattr(sys, 'frozen', False)
                                     else [sys.executable, '-m', 'nice_tools.local_compare_worker'])
        super().__init__(worker_command=command)
        self.process_jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='directory-compare')

    def status(self):
        return {'native_available': sys.platform == 'win32', 'file_limit': 1000, 'diff_page_size': 200}

    def _session(self, token):
        with self.lock:
            session = self.sessions.get(token) if isinstance(token, str) else None
            if session is None or self.closed:
                raise SessionExpired('폴더 선택 정보가 만료되었습니다. 폴더를 다시 선택하세요.')
            session['touched'] = time.monotonic()
            return session

    def create_session(self):
        with self.lock:
            expired = [token for token, item in self.sessions.items()
                       if time.monotonic() - item['touched'] > IDLE_SECONDS]
        for token in expired:
            self.close_session(token)
        with self.lock:
            if self.closed:
                raise SessionExpired('앱을 다시 실행하세요.')
            if len(self.sessions) >= MAX_SESSIONS:
                raise ValueError('열려 있는 비교 화면이 너무 많습니다. 사용하지 않는 화면을 닫으세요.')
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {'touched':time.monotonic(), 'selections':{}, 'jobs':{}, 'picking':False}
        return {'session': token}

    def _request(self, payload, session, progress=None, *, owner_job=None):
        # Cancellation and worker registration share this lock: a cancelled or
        # evicted job cannot start a worker after its owner snapshots processes.
        with self.lock:
            self._session(session)
            if owner_job is not None:
                if (owner_job['cancelled']
                        or self._job(session, owner_job['job']) is not owner_job):
                    raise ValueError('비교 작업이 취소되었거나 만료되었습니다.')
            process = self._spawn(payload, session)
            if owner_job is not None:
                self.process_jobs[process] = owner_job
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=True))
            process.stdin.close()
            process.stdin = None
            result = error = None
            for line in process.stdout:
                event = json.loads(line)
                if event.get('event') == 'progress' and progress is not None:
                    progress(event)
                elif event.get('event') == 'result':
                    result = event.get('result')
                elif event.get('event') == 'error':
                    error = event
            code = process.wait()
            if code or error is not None or not isinstance(result, dict):
                safe_code = error.get('code') if isinstance(error, dict) else None
                if isinstance(safe_code, str) and safe_code in SAFE_ERRORS:
                    raise ComparisonError(safe_code)
                # Native exceptions (including ValueError subclasses) may
                # contain source text; only allowlisted static codes survive.
                raise ValueError('파일 비교 작업이 중지되었거나 실패했습니다.')
            self._session(session)
            return result
        except (OSError, json.JSONDecodeError):
            raise ValueError('파일 비교 프로세스의 응답을 읽지 못했습니다.') from None
        finally:
            try:
                self._release_worker(process)
            finally:
                with self.lock:
                    self.process_jobs.pop(process, None)

    def _choose_directory(self, session, initial, side):
        return self._request({'operation':'pick-directory', 'initial_directory':initial}, session)

    def pick_directory(self, token, side):
        if side not in ('left', 'right'):
            raise ValueError('왼쪽 또는 오른쪽 폴더를 선택하세요.')
        session = self._session(token)
        with self.lock:
            if session['picking'] or any(job['state'] in ('queued','running') for job in session['jobs'].values()):
                raise ValueError('진행 중인 폴더 선택이나 비교가 끝난 뒤 다시 선택하세요.')
            session['picking'] = True
            previous = session['selections'].get(side)
        try:
            selected = self._choose_directory(token, previous['path'] if previous else None, side)
            if selected.get('cancelled'):
                self._session(token)
                return {'cancelled': True}
            descriptor = folder_descriptor(selected['path'])
            if descriptor['identity'] != selected.get('identity'):
                raise ValueError('선택한 폴더가 변경되었습니다. 다시 선택하세요.')
            choice = {'token': secrets.token_urlsafe(32), **descriptor, 'name':str(selected.get('name',Path(descriptor['path']).name)),
                      'file_count':selected['file_count'], 'total_bytes':selected['total_bytes']}
            with self.lock:
                self._session(token)
                session['selections'][side] = choice
            return {'cancelled':False, 'selection':{key:value for key,value in choice.items() if key!='identity'}}
        finally:
            with self.lock:
                session['picking'] = False

    def _job(self, token, handle):
        session = self._session(token)
        job = session['jobs'].get(handle) if isinstance(handle, str) else None
        if job is None:
            raise ComparisonError('job_expired')
        return job

    def start(self, token, left, right, options):
        normalized = options_data(options)
        with self.lock:
            session = self._session(token)
            if session['picking'] or any(job['state'] in ('queued','running') for job in session['jobs'].values()):
                raise ValueError('진행 중인 작업이 끝난 뒤 비교하세요.')
            selections = session['selections']
            if any(side not in selections or selections[side]['token'] != value
                   for side,value in (('left',left),('right',right))):
                raise ValueError('폴더 두 개를 다시 선택하세요.')
            if selections['left']['file_count'] + selections['right']['file_count'] > 1000:
                raise ValueError('파일은 두 폴더 합계 1,000개까지 비교할 수 있습니다.')
            descriptors = {side: {key: selections[side][key] for key in ('path', 'identity')}
                           for side in ('left', 'right')}
            folder = tempfile.TemporaryDirectory(prefix='nice-tools-comparison-')
            handle, old, old_processes = self._register_job(token, session, descriptors, normalized, folder)
        self._retire_jobs(old, old_processes)
        return {'job': handle}

    def _register_job(self, token, session, descriptors, normalized, folder):
        # Caller holds self.lock; registration and close/cancel are atomic.
        # At most two completed comparison caches per screen; old handles
        # are removed before cleanup so no new operation can claim them.
        old = list(session['jobs'].items())[:-1]
        for key, previous in old:
            session['jobs'].pop(key)
            previous['cancelled'] = True
            previous['future'].cancel()
        old_processes = [process for process, owner in self.process_jobs.items()
                         if any(owner is previous for _, previous in old)]
        handle = secrets.token_urlsafe(24)
        job = {'job':handle, 'state':'queued', 'phase':'scanning', 'completed':0, 'total':0,
               'elapsed_seconds':0.0, 'cancelled':False, 'cache_dir':folder.name, 'folder':folder,
               'use_lock':threading.RLock(), 'started':time.monotonic()}
        session['jobs'][handle] = job
        payload = {'operation':'compare','cache_dir':folder.name, **descriptors, 'options':vars(normalized)}
        job['future'] = self.executor.submit(self._run_job, token, handle, job, payload)
        return handle, old, old_processes

    def _retire_jobs(self, old, old_processes):
        for process in old_processes:
            self._terminate(process)
        for _, previous in old:
            with previous['use_lock']:
                previous['folder'].cleanup()

    def start_upload(self, token, left, right, options):
        """Development fallback: stage once and reuse the same owned-job path."""
        if sys.platform == 'win32':
            raise ValueError('Windows에서는 폴더 선택을 사용하세요.')
        normalized = options_data(options)
        if (not isinstance(left, dict) or not isinstance(right, dict)
                or not left or not right or len(left) + len(right) > 1000):
            raise ValueError('비교할 두 폴더의 파일을 확인하세요.')
        with self.lock:
            session = self._session(token)
            if session['picking'] or any(job['state'] in ('queued','running') for job in session['jobs'].values()):
                raise ValueError('진행 중인 작업이 끝난 뒤 비교하세요.')
            session['picking'] = True
        folder = None
        registered = False
        try:
            folder = tempfile.TemporaryDirectory(prefix='nice-tools-comparison-upload-')
            root = Path(folder.name).resolve()
            descriptors = {}
            for side, files in (('left', left), ('right', right)):
                base = root / 'inputs' / side
                base.mkdir(parents=True)
                for relative, content in files.items():
                    relative_path(relative)
                    if not isinstance(content, bytes):
                        raise ValueError('업로드 파일을 읽을 수 없습니다.')
                    target = base.joinpath(*relative.split('/'))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open('xb') as output:
                        output.write(content)
                descriptors[side] = folder_descriptor(base)
            with self.lock:
                self._session(token)
                handle, old, old_processes = self._register_job(token, session, descriptors, normalized, folder)
                registered = True
            self._retire_jobs(old, old_processes)
            return {'job': handle}
        finally:
            with self.lock:
                session['picking'] = False
            if folder is not None and not registered:
                folder.cleanup()

    def _run_job(self, token, handle, job, payload):
        failure = failure_code = None
        with job['use_lock']:
            try:
                with self.lock:
                    if job['cancelled'] or token not in self.sessions:
                        return
                    job['state'] = 'running'
                def progress(event):
                    with self.lock:
                        for name in ('completed','total'):
                            if type(event.get(name)) is int and event[name]>=0:
                                job[name] = event[name]
                        if event.get('phase') in ('scanning','comparing','preparing-reports'):
                            job['phase'] = event['phase']
                        if isinstance(event.get('path'),str):
                            job['path'] = event['path']
                result = self._request(payload, token, progress, owner_job=job)
                with self.lock:
                    if not job['cancelled'] and token in self.sessions:
                        job.update(result, state='succeeded', phase='done')
            except ComparisonError as error:
                failure, failure_code = str(error), error.code
            except Exception:
                failure = '비교 작업을 완료하지 못했습니다. 폴더·파일 상태와 임시 저장 공간을 확인하세요.'
            finally:
                if job['cancelled'] or failure:
                    job['folder'].cleanup()
                with self.lock:
                    job['elapsed_seconds'] = max(0.0,time.monotonic()-job['started'])
                    if job['cancelled']:
                        job.update(state='cancelled',phase='done',message='비교를 취소했습니다.')
                    elif failure:
                        job.update(state='failed',phase='done',message=failure)
                        if failure_code is not None:
                            job['code'] = failure_code

    def job(self, token, handle):
        with self.lock:
            job = self._job(token,handle)
            result = {key:job[key] for key in ('job','state','phase','completed','total','elapsed_seconds','path','message','code','summary','results') if key in job}
            if job['state'] in ('queued','running'):
                result['elapsed_seconds'] = max(0.0,time.monotonic()-job['started'])
            return result

    def cancel(self, token, handle):
        with self.lock:
            job = self._job(token,handle)
            if job['state'] not in ('queued','running'):
                return self.job(token,handle)
            job['cancelled'] = True
            job['future'].cancel()
            processes = [process for process,owner in self.process_jobs.items() if owner is job]
        for process in processes:
            self._terminate(process)
        with job['use_lock']:
            job['folder'].cleanup()
            with self.lock:
                job.update(state='cancelled',phase='done',message='비교를 취소했습니다.')
        return self.job(token,handle)

    def diff(self, token, handle, path, mode='compared', page=0, analyze=False):
        relative_path(path)
        if mode not in ('compared','original') or type(page) is not int or page<0 or type(analyze) is not bool:
            raise ValueError('변경 내용의 보기와 페이지를 확인하세요.')
        job = self._job(token,handle)
        # HTTP abandonment does not stop the owned build. Reject a duplicate
        # promptly so waiting requests cannot occupy the whole HTTP thread pool.
        if not job['use_lock'].acquire(blocking=False):
            raise ComparisonError('busy')
        try:
            with self.lock:
                if self._job(token,handle) is not job or job['state']!='succeeded':
                    raise ValueError('완료된 비교 결과에서 파일을 선택하세요.')
            payload = {'operation':'diff','cache_dir':job['cache_dir'],
                       'path':path,'mode':mode,'page':page}
            # Every complete report and preview was published by initial
            # comparison. A lookup is strictly metadata/cache I/O, even when
            # an older caller still supplies analyze=True.
            result = execute(payload)
            with self.lock:
                if self._job(token, handle) is not job or job['state'] != 'succeeded':
                    raise ValueError('비교 결과가 만료되었습니다. 다시 비교하세요.')
            return result
        finally:
            job['use_lock'].release()

    def close_session(self, token):
        with self.lock:
            session = self.sessions.pop(token,None) if isinstance(token,str) else None
            if session is None:
                return {'ok':True}
            jobs = list(session['jobs'].values())
            processes = [process for process,owner in self.processes.items() if owner==token]
            for job in jobs:
                job['cancelled'] = True
                job['future'].cancel()
        for process in processes:
            self._terminate(process)
        for job in jobs:
            with job['use_lock']:
                job['folder'].cleanup()
        return {'ok':True}

    def close(self):
        with self.lock:
            self.closed = True
            tokens = list(self.sessions)
        for token in tokens:
            self.close_session(token)
        self.executor.shutdown(wait=True,cancel_futures=True)


manager = ComparisonManager()
atexit.register(manager.close)
