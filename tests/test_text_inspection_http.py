import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nice_tools import text_inspection_api
from nice_tools.text_inspection_jobs import InspectionManager


def test_real_upload_scan_and_paged_context_http(tmp_path,monkeypatch):
    manager=InspectionManager()
    monkeypatch.setattr(manager,'status',lambda:{'native_available':False,'page_size':200})
    monkeypatch.setattr(text_inspection_api,'manager',manager)
    app=FastAPI();app.include_router(text_inspection_api.router)
    headers={'X-Nice-Tools':'text-inspection'}
    try:
        with TestClient(app,base_url='http://127.0.0.1:8000') as client:
            def post(route,data):
                response=client.post('/api/text-inspection/'+route,json=data,headers=headers)
                assert response.status_code==200,response.text
                assert response.headers['cache-control']=='no-store'
                return response.json()
            session=post('session',{})['session']
            content='first\u00a0x\u200b\r\nnext\x00\u3164中😀'
            response=client.post('/api/text-inspection/upload',data={'session':session},
                                 files={'file':('sample.txt',content.encode('utf-8-sig'),'text/plain')},headers=headers)
            assert response.status_code==200,response.text
            token=response.json()['selection']['token']
            job=post('start',{'session':session,'file':token,'options':{}})['job']
            deadline=time.monotonic()+8
            while True:
                result=post('job',{'session':session,'job':job})
                if result['state'] not in ('queued','running'):break
                assert time.monotonic()<deadline;time.sleep(.01)
            assert result['state']=='succeeded' and result['summary']['findings']==4,result
            assert result['summary']['bom'] and result['summary']['common_whitespace']['crlf']==1
            page=post('page',{'session':session,'job':job,'page':0})
            assert [row['codepoint'] for row in page['results']]==['U+00A0','U+200B','U+0000','U+3164']
            assert [(row['line'],row['column']) for row in page['results']]==[(1,6),(1,8),(2,5),(2,6)]
            assert '\u200b' not in page['results'][0]['after']
            strict=post('start',{'session':session,'file':token,'options':{'encoding':'UTF8','strict_mode':True}})['job']
            deadline=time.monotonic()+8
            while True:
                result=post('job',{'session':session,'job':strict})
                if result['state'] not in ('queued','running'):break
                assert time.monotonic()<deadline;time.sleep(.01)
            assert result['state']=='succeeded' and result['summary']['findings']==6,result
            assert result['summary']['kinds']['strict']==2
            upload=client.post('/api/text-inspection/upload',data={'session':session},
                               files={'file':('surrogate.txt',b'+2AA-\0','text/plain')},headers=headers)
            invalid_token=upload.json()['selection']['token']
            invalid=post('start',{'session':session,'file':invalid_token,'options':{'encoding':'utf-7'}})['job']
            deadline=time.monotonic()+8
            while True:
                failed=post('job',{'session':session,'job':invalid})
                if failed['state'] not in ('queued','running'):break
                assert time.monotonic()<deadline;time.sleep(.01)
            assert failed['state']=='failed' and failed.get('code')=='encoding'
            page_error=client.post('/api/text-inspection/page',json={'session':session,'job':invalid,'page':0},headers=headers)
            assert page_error.status_code==400 and isinstance(page_error.json()['detail'],str)
            post('close',{'session':session})
        assert not manager.sessions and not manager.processes
    finally:manager.close()
