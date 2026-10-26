"""Reuse native job ownership for read-only, single-file character inspection."""
from __future__ import annotations

import atexit
import ntpath
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time

from .local_compare_jobs import ComparisonManager,SessionExpired
from .local_compare_worker import ComparisonError
from .text_inspection_worker import execute,file_descriptor
from .text_inspection_engine import validate_options


class InspectionManager(ComparisonManager):
    def __init__(self,worker_command=None):
        command=worker_command or ([sys.executable,'--text-inspection-worker'] if getattr(sys,'frozen',False)
                                   else [sys.executable,'-m','nice_tools.text_inspection_worker'])
        super().__init__(worker_command=command)

    def status(self):
        return {'native_available':sys.platform=='win32','page_size':200}

    def create_session(self):
        result=super().create_session()
        with self.lock:
            session=self._session(result['session'])
            session.update(selection_lock=threading.RLock(),uploads=[])
        return result

    def _selection_start(self,token):
        with self.lock:
            session=self._session(token)
            if session['picking'] or any(job['state'] in ('queued','running') for job in session['jobs'].values()):
                raise ValueError('진행 중인 작업이 끝난 뒤 파일을 선택하세요.')
            session['picking']=True
        return session

    def _install_selection(self,token,session,source,name=None):
        # Verify native worker metadata again before minting the public grant.
        current=file_descriptor(source['path'])
        if current!=source:raise ComparisonError('changed')
        choice={'token':secrets.token_urlsafe(32),'path':source['path'],'name':name or source['name'],
                'size':source['fingerprint'][2],'source':source}
        with self.lock:
            self._session(token)
            session['selections']['file']=choice
        return {'cancelled':False,'selection':{key:choice[key] for key in ('token','path','name','size')}}

    def _choose_file(self,token):
        return self._request({'operation':'pick-file'},token)

    def pick_file(self,token):
        session=self._selection_start(token)
        try:
            with session['selection_lock']:
                self._session(token)
                picked=self._choose_file(token)
                if picked.get('cancelled'):
                    self._session(token)
                    return {'cancelled':True}
                result=self._install_selection(token,session,picked['source'])
                self._cleanup_uploads(session)
                return result
        finally:
            with self.lock:session['picking']=False

    def select_upload(self,token,source,filename):
        if not isinstance(filename,str) or not filename or len(filename)>1024:
            raise ValueError('텍스트 파일을 선택하세요.')
        session=self._selection_start(token)
        folder=None
        try:
            with session['selection_lock']:
                self._session(token)
                folder=tempfile.TemporaryDirectory(prefix='nice-tools-inspection-input-')
                path=Path(folder.name)/'input.txt'
                with self.lock:
                    self._session(token)
                    session['uploads'].append({'folder':folder,'path':str(path)})
                source.seek(0)
                with path.open('xb') as destination:
                    while True:
                        self._session(token)
                        chunk=source.read(128*1024)
                        if not chunk:break
                        if not isinstance(chunk,bytes):raise ValueError('파일 입력을 확인하세요.')
                        destination.write(chunk)
                result=self._install_selection(token,session,file_descriptor(path),ntpath.basename(filename))
                self._cleanup_uploads(session)
                folder=None
                return result
        finally:
            if folder is not None:folder.cleanup()
            with self.lock:session['picking']=False

    def _cleanup_uploads(self,session):
        with self.lock:
            retained={job['source_path'] for job in session['jobs'].values()
                      if job['state'] in ('queued','running','succeeded')}
            choice=session['selections'].get('file')
            if choice:retained.add(choice['path'])
            obsolete=[upload for upload in session['uploads'] if upload['path'] not in retained]
            session['uploads']=[upload for upload in session['uploads'] if upload['path'] in retained]
        for upload in obsolete:upload['folder'].cleanup()

    def start(self,token,file,options):
        options=validate_options(options)
        with self.lock:
            session=self._session(token)
            if session['picking'] or any(job['state'] in ('queued','running') for job in session['jobs'].values()):
                raise ValueError('진행 중인 작업이 끝난 뒤 검사하세요.')
            selected=session['selections'].get('file')
            if selected is None or selected['token']!=file:raise ValueError('파일을 다시 선택하세요.')
            old=list(session['jobs'].items())[:-1]
            for key,previous in old:
                session['jobs'].pop(key)
                previous['cancelled']=True;previous['future'].cancel()
            processes=[process for process,owner in self.process_jobs.items() if any(owner is previous for _,previous in old)]
            folder=tempfile.TemporaryDirectory(prefix='nice-tools-text-inspection-')
            handle=secrets.token_urlsafe(24)
            job={'job':handle,'state':'queued','phase':'scanning','completed':0,'total':selected['size'],
                 'elapsed_seconds':0.0,'cancelled':False,'cache_dir':folder.name,'folder':folder,
                 'use_lock':threading.RLock(),'started':time.monotonic(),'source_path':selected['path']}
            session['jobs'][handle]=job
            payload={'operation':'scan','source':selected['source'],'cache_dir':folder.name,'options':options}
            job['future']=self.executor.submit(self._run_job,token,handle,job,payload)
        for process in processes:self._terminate(process)
        for _,previous in old:
            with previous['use_lock']:previous['folder'].cleanup()
        with session['selection_lock']:self._cleanup_uploads(session)
        return {'job':handle}

    def job(self,token,handle):
        result=super().job(token,handle)
        if result['state']=='cancelled':result['message']='검사를 취소했습니다.'
        elif result['state']=='failed' and not result.get('code'):
            result['message']='텍스트 검사를 완료하지 못했습니다. 파일 상태와 인코딩·임시 저장 공간을 확인하세요.'
        return result

    def page(self,token,handle,page=0):
        if type(page) is not int or page<0:raise ValueError('결과 페이지를 확인하세요.')
        job=self._job(token,handle)
        if not job['use_lock'].acquire(blocking=False):raise ComparisonError('busy')
        try:
            with self.lock:
                if self._job(token,handle) is not job or job['state']!='succeeded':
                    raise ValueError('검사가 완료된 결과를 선택하세요.')
            return execute({'operation':'page','cache_dir':job['cache_dir'],'page':page})
        finally:job['use_lock'].release()

    def close_session(self,token):
        with self.lock:session=self.sessions.get(token) if isinstance(token,str) else None
        result=super().close_session(token)
        if session is not None:
            with session['selection_lock']:
                for upload in session['uploads']:upload['folder'].cleanup()
                session['uploads'].clear()
        return result


manager=InspectionManager()
atexit.register(manager.close)
