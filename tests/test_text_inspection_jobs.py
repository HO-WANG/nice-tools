import importlib
import io
from pathlib import Path
import sys
import time

import pytest


def module():
    try:return importlib.import_module('nice_tools.text_inspection_jobs')
    except ModuleNotFoundError:pytest.fail('Hidden character job service is missing')


def finished(manager,session,job):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        result=manager.job(session,job)
        if result['state'] in ('succeeded','failed','cancelled'):return result
        time.sleep(.01)
    pytest.fail('Inspection did not settle')


def chosen(manager,tmp_path,monkeypatch):
    from nice_tools.text_inspection_worker import file_descriptor
    path=tmp_path/'sample.txt'
    path.write_text('a\u00a0b\u200bc\x00\r\nnext',encoding='utf-8')
    session=manager.create_session()['session']
    monkeypatch.setattr(manager,'_choose_file',lambda token: {'cancelled':False,'source':file_descriptor(path)})
    token=manager.pick_file(session)['selection']['token']
    return session,token,path


def test_actual_owned_worker_scan_and_pages_preserve_source(tmp_path,monkeypatch):
    manager=module().InspectionManager()
    try:
        session,token,path=chosen(manager,tmp_path,monkeypatch)
        original=path.read_bytes()
        job=manager.start(session,token,{})['job']
        result=finished(manager,session,job)
        assert result['state']=='succeeded' and result['summary']['findings']==3,result
        page=manager.page(session,job,0)
        assert [row['codepoint'] for row in page['results']]==['U+00A0','U+200B','U+0000']
        assert path.read_bytes()==original
        cache=Path(manager.sessions[session]['jobs'][job]['cache_dir'])
        path.write_bytes(b'changed')
        with pytest.raises(ValueError):manager.page(session,job,0)
        manager.close_session(session)
        assert not cache.exists() and not manager.processes
    finally:manager.close()


def test_upload_source_and_results_are_cleaned_on_close(tmp_path):
    manager=module().InspectionManager()
    try:
        session=manager.create_session()['session']
        selection=manager.select_upload(session,io.BytesIO('한글\u200b'.encode()),'../shown.txt')['selection']
        assert selection['name']=='shown.txt'
        job=manager.start(session,selection['token'],{})['job']
        assert finished(manager,session,job)['summary']['findings']==1
        source=Path(manager.sessions[session]['selections']['file']['source']['path'])
        assert source.exists()
        manager.close_session(session)
        assert not source.exists()
    finally:manager.close()


def test_uploaded_source_survives_canonical_temp_path_alias(tmp_path,monkeypatch):
    service=module();manager=service.InspectionManager()
    real,alias=tmp_path/'real-temp',tmp_path/'temp-alias'
    real.mkdir()
    try:alias.symlink_to(real,target_is_directory=True)
    except OSError:pytest.skip('Directory alias creation unavailable')
    factory=service.tempfile.TemporaryDirectory
    def aliased(*args,**kwargs):
        kwargs['dir']=alias
        return factory(*args,**kwargs)
    monkeypatch.setattr(service.tempfile,'TemporaryDirectory',aliased)
    try:
        session=manager.create_session()['session']
        selected=manager.select_upload(session,io.BytesIO(b'A\n'),'data.txt')['selection']
        path=Path(manager.sessions[session]['selections']['file']['source']['path'])
        assert path.exists(),'Canonical selection was deleted as an obsolete upload'
        job=manager.start(session,selected['token'],{})['job']
        assert finished(manager,session,job)['state']=='succeeded'
    finally:manager.close()


def test_file_grants_are_session_owned(tmp_path,monkeypatch):
    manager=module().InspectionManager()
    try:
        session,token,path=chosen(manager,tmp_path,monkeypatch)
        another=manager.create_session()['session']
        with pytest.raises(ValueError):manager.start(another,token,{})
        assert not manager.sessions[another]['jobs']
    finally:manager.close()


def test_cancel_inherited_owned_worker_cleanup(tmp_path,monkeypatch):
    script=tmp_path/'waiting.py'
    script.write_text("import json,sys,time\njson.load(sys.stdin)\nprint(json.dumps({'event':'progress','phase':'scanning','completed':0,'total':10}),flush=True)\ntime.sleep(60)\n",encoding='utf-8')
    manager=module().InspectionManager(worker_command=[sys.executable,str(script)])
    try:
        session,token,path=chosen(manager,tmp_path,monkeypatch)
        job=manager.start(session,token,{})['job']
        deadline=time.monotonic()+3
        while not manager.processes and time.monotonic()<deadline:time.sleep(.01)
        assert manager.processes
        cache=Path(manager.sessions[session]['jobs'][job]['cache_dir'])
        assert manager.cancel(session,job)['state']=='cancelled'
        assert not cache.exists() and not manager.processes
    finally:manager.close()


def test_session_close_racing_start_cannot_leave_an_unowned_cache(tmp_path,monkeypatch):
    import threading
    service=module();manager=service.InspectionManager()
    session,token,path=chosen(manager,tmp_path,monkeypatch)
    entered,release,closing,closed=(threading.Event() for _ in range(4))
    original=manager._session
    lookup=[False]
    def paused(value):
        result=original(value)
        if threading.current_thread().name=='inspection-start-test' and not lookup[0]:
            lookup[0]=True;entered.set();assert release.wait(3)
        return result
    monkeypatch.setattr(manager,'_session',paused)
    folders=[]
    factory=service.tempfile.TemporaryDirectory
    def tracked(*args,**kwargs):
        result=factory(*args,**kwargs);folders.append(result);return result
    monkeypatch.setattr(service.tempfile,'TemporaryDirectory',tracked)
    failures=[]
    def start():
        try:manager.start(session,token,{})
        except ValueError as error:failures.append(error)
    def close():
        closing.set();manager.close_session(session);closed.set()
    starter=threading.Thread(target=start,name='inspection-start-test')
    closer=threading.Thread(target=close)
    starter.start()
    try:
        assert entered.wait(2)
        closer.start();assert closing.wait(2)
        closed.wait(.5)
        release.set();starter.join(3);closer.join(3)
        assert not starter.is_alive() and not closer.is_alive()
        assert not manager.sessions and not manager.processes
        assert all(not Path(folder.name).exists() for folder in folders)
    finally:
        release.set();starter.join(3)
        if closer.ident is not None:closer.join(3)
        manager.close()
