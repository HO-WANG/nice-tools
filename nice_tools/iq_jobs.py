"""Owned IQ workers and atomic, cancellable SELECT-result publication."""

import atexit
import contextlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

from .ase_batch import default_export_directory, publish_available, submit
from .ase_export import ExportReservation, cleanup_owned_path, file_identity
from .ase_jobs import AseManager
from .iq_files import parse_options
from .iq_sql import validate_select


def filename(value):
    if (not isinstance(value, str) or not value or len(value) > 180
            or value != value.strip() or value.endswith(('.', ' '))
            or ntpath.basename(value) != value
            or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', value)
            or re.fullmatch(r'(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])', value.split('.')[0], re.I)):
        raise ValueError('파일명에는 경로 또는 Windows 예약 이름을 사용할 수 없습니다.')
    return value if Path(value).suffix else value + '.txt'


class IQManager(AseManager):
    """Reuse only the existing owned-process/dispatcher lifecycle primitives."""

    def __init__(self, worker_command=None):
        command = worker_command or ([sys.executable, '--iq-worker'] if getattr(sys, 'frozen', False)
                                     else [sys.executable, '-m', 'nice_tools.iq_worker'])
        super().__init__(worker_command=command)

    def status(self):
        from .iq_odbc import status
        return {**status(), 'export_directory': default_export_directory()}

    def _request(self, payload, token=None, timeout=25):
        process = self._spawn(payload, token)
        try:
            output, _ = process.communicate(json.dumps(payload, ensure_ascii=True), timeout=timeout)
            result = error = None
            for line in output.splitlines():
                record = json.loads(line)
                if record.get('event') == 'result':
                    result = record.get('result')
                elif record.get('event') == 'error':
                    error = record.get('detail', 'IQ 작업을 완료하지 못했습니다.')
            if process.returncode != 0 or error or not isinstance(result, dict):
                raise ValueError(self._redact(error or 'IQ 작업을 완료하지 못했습니다.', payload.get('connection', {})))
            return result
        except subprocess.TimeoutExpired:
            self._terminate(process)
            raise ValueError('IQ 연결 확인 시간이 초과되었습니다.') from None
        except (OSError, json.JSONDecodeError):
            raise ValueError('IQ 작업 프로세스 응답을 확인하지 못했습니다.') from None
        finally:
            self._release_worker(process)

    def connect(self, raw):
        from .iq_odbc import connection_data
        connection = connection_data(raw)
        result = self._request({'operation': 'connect', 'connection': connection})
        if not isinstance(result.get('driver'), str):
            raise ValueError('IQ 드라이버 확인 응답이 올바르지 않습니다.')
        # Public worker strings may be password-redacted. Keep the original
        # explicit driver (or auto selection), never reuse display text as config.
        with self.lock:
            if self.closed:
                raise ValueError('IQ 연결 요청이 취소되었습니다.')
            token = secrets.token_urlsafe(32)
            directory = default_export_directory()
            self.sessions[token] = {'connection': connection, 'jobs': {}, 'touched': time.monotonic(),
                                    'export_directory': directory}
        return {'session': token, 'driver': self._redact(result['driver'], connection), 'export_directory': directory,
                'version': self._redact(result.get('version', ''), connection)}

    def _cancel_job(self, job):
        queued = job.get('phase') == 'queued'
        super()._cancel_job(job)
        if queued:
            job.update(state='cancelled', phase='done', message='내보내기 취소됨')

    def pick_directory(self, token):
        session = self._session(token)
        with self.lock:
            if self._running(session):
                raise ValueError('내보내기가 진행 중입니다.')
            initial = session['export_directory']
        result = self._request({'operation': 'pick-directory', 'initial_directory': initial}, token, timeout=None)
        if result.get('cancelled'):
            return {'cancelled': True}
        path = Path(result['path']).resolve()
        if not path.is_dir():
            raise ValueError('선택한 폴더를 사용할 수 없습니다.')
        with self.lock:
            self._session(token)
            if self._running(session):
                raise ValueError('내보내기가 진행 중입니다.')
            session['export_directory'] = str(path)
        return {'cancelled': False, 'path': str(path)}

    def start_export(self, token, sql, name, options):
        session = self._session(token)
        sql, name, options = validate_select(sql), filename(name), parse_options(options)
        with self.lock:
            self._session(token)
            job = self._new_job(session)
            root = Path(session['export_directory']).resolve()
            job.update(phase='queued', message='실행 대기 중', rows=0, bytes_written=0,
                       elapsed_seconds=0.0, rows_per_second=None, path=str(root / name))
            job['thread'] = submit(self._run_export, token, session, job, sql, options, root)
        return {'job': job['job']}

    def _run_export(self, token, session, job, sql, options, root):
        process = reservation = stage = identity = None
        payload = {}
        connection = session['connection']
        started = time.monotonic()
        try:
            with self.lock:
                if job['cancelled'] or self.closed or token not in self.sessions:
                    job['cancelled'] = True
                    return
                if root.resolve() != root:
                    raise ValueError('출력 폴더가 변경되었습니다.')
                root.mkdir(parents=True, exist_ok=True)
                if root.resolve() != root:
                    raise ValueError('출력 폴더가 변경되었습니다.')
                descriptor, stage = tempfile.mkstemp(prefix='.nice-tools-iq-', suffix='.tmp', dir=root)
                identity = file_identity(os.fstat(descriptor))
                os.close(descriptor)
                reservation = ExportReservation(root, stage, identity)
                job.update(phase='connecting', message='IQ에 접속하는 중')
                payload = {'operation': 'export', 'connection': connection, 'sql': sql,
                           'path': stage, 'stage_identity': identity, 'options': options}
            process = self._spawn(payload, token)
            with self.lock:
                job['process'] = process
                if job['cancelled']:
                    self._terminate(process)
            process.stdin.write(json.dumps(payload, ensure_ascii=True))
            process.stdin.close()
            result = error = None
            for line in process.stdout:
                record = json.loads(line)
                with self.lock:
                    if record.get('event') == 'progress':
                        phase = record.get('phase')
                        if phase in {'connecting', 'querying', 'writing'}:
                            job['phase'] = phase
                        if isinstance(record.get('message'), str):
                            job['message'] = self._redact(record['message'], connection)
                        for key in ('rows', 'bytes_written'):
                            if type(record.get(key)) is int and record[key] >= 0:
                                job[key] = record[key]
                        elapsed = max(0.0, time.monotonic() - started)
                        job['elapsed_seconds'] = elapsed
                        job['rows_per_second'] = job['rows'] / elapsed if elapsed else None
                    elif record.get('event') == 'result':
                        result = record.get('result')
                    elif record.get('event') == 'error':
                        error = self._redact(record.get('detail', 'IQ 내보내기에 실패했습니다.'), connection)
            process.wait()
            if process.returncode != 0 or error or not isinstance(result, dict):
                raise ValueError(error or 'IQ 내보내기 프로세스가 정상적으로 끝나지 않았습니다.')
            if (type(result.get('rows')) is not int or result['rows'] < 0
                    or type(result.get('bytes_written')) is not int or result['bytes_written'] < result['rows']
                    or not isinstance(result.get('columns'), list)
                    or not all(isinstance(value, str) for value in result['columns'])):
                raise ValueError('내보낸 행 수 또는 파일 크기를 확인하지 못했습니다.')
            with self.lock:
                if job['cancelled'] or self.closed or token not in self.sessions:
                    job['cancelled'] = True
                    return
                reservation.validate()
                if Path(stage).stat().st_size != result['bytes_written']:
                    raise ValueError('결과 파일 크기가 일치하지 않아 저장하지 않았습니다.')
                elapsed = max(0.0, time.monotonic() - started)
                rate = result['rows'] / elapsed if elapsed else None
                if rate is not None and not math.isfinite(rate):
                    rate = None
                public = {'rows': result['rows'], 'bytes_written': result['bytes_written'],
                          'columns': [self._redact(value, connection) for value in result['columns']],
                          'path': job['path'], 'elapsed_seconds': elapsed, 'rows_per_second': rate}
                public['path'] = publish_available(stage, job['path'])
                job.update(public, result=public, state='succeeded', phase='done', message='내보내기 완료')
        except Exception as error:
            with self.lock:
                if not job['cancelled']:
                    detail = str(error) if isinstance(error, ValueError) else 'IQ 결과를 저장하지 못했습니다. 연결과 출력 폴더를 확인하세요.'
                    job.update(state='failed', phase='done', message=self._redact(detail, connection))
        finally:
            if process is not None:
                with contextlib.suppress(Exception):
                    self._release_worker(process)
            if reservation is not None:
                reservation.cleanup()
                reservation.close()
            elif stage is not None and identity is not None:
                cleanup_owned_path(stage, identity)
            with self.lock:
                job['process'] = None
                if job['state'] != 'succeeded':
                    job['elapsed_seconds'] = max(0.0, time.monotonic() - started)
                if job['cancelled']:
                    job.update(state='cancelled', phase='done', message='내보내기 취소됨')
            payload.clear()


manager = IQManager()
atexit.register(manager.close)
