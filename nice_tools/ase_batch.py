"""Bounded transfer scheduling and collision-safe batch OUT orchestration."""

from __future__ import annotations

import contextlib
import copy
import os
from pathlib import Path
import queue
import re
import secrets
import tempfile
import threading
import time


MAX_BATCH_OBJECTS = 1000
_QUEUE = queue.Queue()
_POOL_LOCK = threading.Lock()
_POOL = []
_PRIVATE = {'cancelled', 'process', 'thread', 'started_at', '_next_item', '_reservation'}


class _Task:
    """Small joinable task handle; queued work owns no thread or subprocess."""
    def __init__(self, function, args):
        self.function, self.args = function, args
        self.done = threading.Event()

    def join(self, timeout=None):
        self.done.wait(timeout)

    def is_alive(self):
        return not self.done.is_set()


def _consume():
    while True:
        task = _QUEUE.get()
        try:
            # The owning manager checks cancellation under its own lock. Run
            # cancelled callbacks too so they release payloads and settle groups.
            task.function(*task.args)
        except Exception:
            # Operation callbacks settle their own jobs and clean up. Keep the
            # fixed pool alive if an unexpected callback error still escapes;
            # exceptions may contain credentials and must never be printed.
            pass
        finally:
            task.function = task.args = None
            task.done.set()
            _QUEUE.task_done()


def submit(function, *args):
    with _POOL_LOCK:
        if not _POOL:
            for index in range(3):
                thread = threading.Thread(target=_consume, name=f'ase-transfer-{index + 1}', daemon=True)
                _POOL.append(thread)
                thread.start()
    task = _Task(function, args)
    _QUEUE.put(task)
    return task


def default_export_directory():
    from .ase_profiles import configuration_path
    return str(configuration_path().parent)


def public_job(job):
    return {key: ([public_job(item) for item in value] if key == 'items' else copy.deepcopy(value))
            for key, value in job.items() if key not in _PRIVATE}


def _component(name):
    # Target identifiers are validated before this conversion. Windows reserves
    # these basenames even when followed by an extension.
    if re.fullmatch(r'(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])', name, re.IGNORECASE):
        return '_' + name
    return name


def available_target(target, number=0):
    while True:
        candidate = target if number == 0 else target.with_name(f'{target.stem}_{number}{target.suffix}')
        if not os.path.lexists(candidate):
            return candidate, number
        number += 1


def publish_available(stage, requested):
    """Atomically claim an unused destination; never replace another file."""
    requested = Path(requested)
    number = 0
    while True:
        candidate, number = available_target(requested, number)
        try:
            if os.name == 'nt':
                # Windows rename is atomic and refuses an existing destination.
                os.rename(stage, candidate)
            else:
                # A hard link is an atomic no-replace publication on the same
                # filesystem; the reserved stage lives beside its destination.
                os.link(stage, candidate)
                with contextlib.suppress(OSError):
                    Path(stage).unlink()
            return str(candidate)
        except FileExistsError:
            number += 1


class BatchMixin:
    def pick_directory(self, token):
        session = self._session(token)
        with self.lock:
            if self._running(session):
                raise ValueError('진행 중인 전송이 있습니다.')
            initial = session.get('export_directory', default_export_directory())
        result = self._invoke({'operation': 'pick-directory', 'initial_directory': initial}, token, timeout=None)
        if result.get('cancelled'):
            return {'cancelled': True}
        path = Path(result['path']).resolve()
        if not path.is_dir():
            raise ValueError('선택한 폴더를 사용할 수 없습니다.')
        with self.lock:
            self._session(token)
            if self._running(session):
                raise ValueError('진행 중인 전송이 있습니다.')
            session['export_directory'] = str(path)
        return {'cancelled': False, 'path': str(path)}

    def _new_job(self, session):
        if self._running(session):
            raise ValueError('진행 중인 전송이 있습니다.')
        for key in list(session['jobs']):
            if len(session['jobs']) < 8:
                break
            if session['jobs'][key]['state'] != 'running':
                session['jobs'].pop(key)
        job = {'job': secrets.token_urlsafe(32), 'state': 'running', 'phase': 'starting',
               'message': '전송 준비 중', 'events': [], 'cancelled': False, 'process': None}
        session['jobs'][job['job']] = job
        return job

    def start_export(self, token, objects, options, concurrency=3):
        from .ase_cli import _target
        from .ase_files import parse_options
        from .ase_jobs import object_data
        session = self._session(token)
        if type(concurrency) is not int or not 1 <= concurrency <= 3:
            raise ValueError('동시 OUT 전송 수는 1, 2, 3 중 선택하세요.')
        if not isinstance(objects, list) or not 1 <= len(objects) <= MAX_BATCH_OBJECTS:
            raise ValueError(f'OUT 오브젝트는 1~{MAX_BATCH_OBJECTS}개 선택하세요.')
        if not isinstance(options, dict):
            raise ValueError('전송 옵션이 올바르지 않습니다.')
        options = parse_options({**options, 'direction': 'out'})
        validated = []
        seen = set()
        for value in objects:
            obj = object_data(value)
            if obj['kind'] not in ('U', 'V'):
                raise ValueError('OUT은 테이블과 뷰만 가능합니다.')
            _target(obj)  # Validate all objects before creating any directory.
            identity = tuple(obj.items())
            if identity in seen:
                raise ValueError('같은 오브젝트를 중복 선택할 수 없습니다.')
            seen.add(identity)
            validated.append(obj)
        with self.lock:
            self._session(token)
            if self._running(session):
                raise ValueError('진행 중인 전송이 있습니다.')
            directory = Path(session.get('export_directory', default_export_directory())).resolve()
            paths = []
            for obj in validated:
                parent = directory / _component(obj['owner'])
                if parent.is_symlink() or parent.resolve().parent != directory:
                    raise ValueError('소유자 OUT 폴더를 사용할 수 없습니다.')
                paths.append(parent / (_component(obj['name']) + '.txt'))
            job = self._new_job(session)
            job.update(concurrency=concurrency, _next_item=0)
            job['items'] = []
            for obj, path in zip(validated, paths):
                item = {'object': obj, 'path': str(path), 'state': 'queued', 'phase': 'queued',
                        'message': '대기 중', 'rows': None, 'events': [], 'cancelled': False, 'process': None}
                job['items'].append(item)
            self._refresh_batch(job)
            for _ in range(min(concurrency, len(job['items']))):
                self._schedule_next(token, session, job, options, directory)
        return {'job': job['job']}

    def _schedule_next(self, token, session, group, options, directory):
        # Called under the manager lock. Only selected-concurrency work enters
        # the shared queue; a lower setting never blocks a global worker slot.
        if group['cancelled'] or self.closed or token not in self.sessions:
            return
        index = group['_next_item']
        if index < len(group['items']):
            group['_next_item'] += 1
            item = group['items'][index]
            item['thread'] = submit(self._run_batch_item, token, session, group, item, options, directory)

    def start_import(self, token, object_value, file_token, options):
        from .ase_files import parse_options
        from .ase_jobs import object_data
        session = self._session(token)
        obj = object_data(object_value)
        if obj['kind'] != 'U':
            raise ValueError('IN은 테이블 하나만 가능합니다.')
        if not isinstance(options, dict):
            raise ValueError('전송 옵션이 올바르지 않습니다.')
        options = parse_options({**options, 'direction': 'in'})
        with self.lock:
            self._session(token)
            selected = session['files'].get(file_token) if isinstance(file_token, str) else None
            if not selected or selected['direction'] != 'in':
                raise ValueError('IN 파일 하나를 다시 선택하세요.')
            payload = {'operation': 'transfer', 'connection': session['connection'], 'object': obj,
                       'path': selected['path'], 'options': options, 'auto_validate': True}
            job = self._new_job(session)
            job['thread'] = submit(self._run_job, token, session, job, payload)
        return {'job': job['job']}

    @staticmethod
    def _refresh_batch(group):
        states = [item['state'] for item in group['items']]
        group['total'] = len(states)
        group['completed'] = sum(state in ('succeeded', 'failed', 'cancelled') for state in states)
        group['running'] = states.count('running')
        group['failed'] = states.count('failed')
        if group['completed'] == group['total']:
            group.update(state=('cancelled' if group['cancelled'] else 'failed' if group['failed'] else 'succeeded'),
                         phase='done', message='전송 취소됨' if group['cancelled'] else '일부 전송 실패' if group['failed'] else '전송 완료')
        else:
            group['message'] = f"{group['completed']}/{group['total']} 완료"

    def _run_batch_item(self, token, session, group, item, options, directory):
        from .ase_export import ExportReservation, cleanup_owned_path, file_identity
        payload = None
        stage = None
        stage_identity = None
        reservation = None
        try:
            with self.lock:
                if group['cancelled'] or self.closed or token not in self.sessions:
                    item.update(state='cancelled', cancelled=True, phase='done', message='전송 취소됨')
                    return
                item.update(state='running', phase='starting', message='전송 준비 중', started_at=time.monotonic())
                target = Path(item['path'])
                if directory.resolve() != directory or target.parent.is_symlink() or target.parent.resolve().parent != directory:
                    raise ValueError('소유자 OUT 폴더를 사용할 수 없습니다.')
                target.parent.mkdir(parents=True, exist_ok=True)
                if directory.resolve() != directory or target.parent.is_symlink() or target.parent.resolve().parent != directory:
                    raise ValueError('소유자 OUT 폴더를 사용할 수 없습니다.')
                descriptor, stage = tempfile.mkstemp(prefix='.nice-tools-', suffix='.bcp', dir=target.parent)
                stage_identity = file_identity(os.fstat(descriptor))
                os.close(descriptor)
                reservation = ExportReservation(directory, target.parent, stage, stage_identity)
                item["_reservation"] = reservation
                payload = {'operation': 'transfer', 'connection': session['connection'], 'object': item['object'],
                           'path': str(target), 'options': dict(options), 'stage_path': stage,
                           'defer_publish': True, 'no_replace': True, 'auto_validate': True,
                           'output_boundary': reservation.boundary}
                self._refresh_batch(group)
            self._run_job(token, session, item, payload)
        except Exception as error:
            with self.lock:
                detail = self._redact(error, session['connection']) if isinstance(error, (OSError, ValueError)) else '전송을 완료하지 못했습니다.'
                item.update(state='failed', phase='done', message=detail)
        finally:
            if reservation is not None:
                reservation.cleanup()
                reservation.close()
            elif stage and stage_identity is not None:
                cleanup_owned_path(stage, stage_identity)
            if payload is not None:
                payload.clear()
            with self.lock:
                item.pop('_reservation', None)
                if item.get('result'):
                    item.update(path=item['result']['path'], statistics=item['result'].get('statistics'))
                self._refresh_batch(group)
                self._schedule_next(token, session, group, options, directory)

    def _cancel_job(self, job):
        job['cancelled'] = True
        job['message'] = '취소 중'
        for item in job.get('items', [job]):
            item['cancelled'] = True
            if item['state'] == 'queued':
                item.update(state='cancelled', phase='done', message='전송 취소됨')
            if item.get('process'):
                self._terminate(item['process'])
        if 'items' in job:
            self._refresh_batch(job)

    @staticmethod
    def _job_tasks(job):
        return [item['thread'] for item in job.get('items', [job]) if item.get('thread')]
