import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nice_tools import local_compare_api
from nice_tools.local_compare_jobs import ComparisonManager
from nice_tools.local_compare_worker import folder_descriptor


def test_real_http_token_selection_worker_diff_and_stale_file(tmp_path,monkeypatch):
    left,right=tmp_path/'left',tmp_path/'right'
    left.mkdir();right.mkdir()
    (left/'data.txt').write_text('Xa bY\nXcdY',encoding='utf-8')
    (right/'data.txt').write_bytes('QcdR\nQa bR'.encode('cp949'))
    manager=ComparisonManager()
    def choose(session,initial,side):
        path=left if side=='left' else right
        return {**folder_descriptor(path),'cancelled':False,'name':path.name,'file_count':1,'total_bytes':10}
    monkeypatch.setattr(manager,'_choose_directory',choose)
    monkeypatch.setattr(local_compare_api,'manager',manager)
    app=FastAPI();app.include_router(local_compare_api.router)
    try:
        with TestClient(app,base_url='http://127.0.0.1:8000') as client:
            def call(route,data):
                response=client.post('/api/comparison/'+route,json=data,headers={'X-Nice-Tools':'comparison'})
                assert response.headers['cache-control']=='no-store'
                assert response.status_code==200,response.text
                return response.json()
            session=call('session',{})['session']
            a=call('pick-directory',{'session':session,'side':'left'})['selection']['token']
            b=call('pick-directory',{'session':session,'side':'right'})['selection']['token']
            handle=call('start',{'session':session,'left':a,'right':b,'options':{
                'sort_lines':True,'ignore_whitespace':True,'ignore_start':1,'ignore_end':1}})['job']
            deadline=time.monotonic()+8
            while True:
                result=call('job',{'session':session,'job':handle})
                if result['state'] not in ('queued','running'):break
                assert time.monotonic()<deadline
                time.sleep(.01)
            assert result['state']=='succeeded' and result['summary']['same']==1
            query={'session':session,'job':handle,'path':'data.txt','mode':'compared','page':0}
            assert call('diff',query)['identical']
            original=call('diff',{**query,'mode':'original'})
            assert original['additions']==original['deletions']==2
            (right/'data.txt').write_bytes(b'changed\n')
            stale=client.post('/api/comparison/diff',json=query,headers={'X-Nice-Tools':'comparison'})
            assert stale.status_code==400 and stale.json()['code']=='changed'
            assert str(tmp_path) not in stale.text and 'Xa bY' not in stale.text
            call('close',{'session':session})
        assert not manager.sessions and not manager.processes
    finally:
        manager.close()
