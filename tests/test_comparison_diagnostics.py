"""Result lookup failures keep useful static causes without leaking file data."""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nice_tools import local_compare_api as api
from nice_tools import local_compare_worker as worker
from nice_tools.local_compare_jobs import ComparisonManager


@pytest.fixture
def lookup(tmp_path, monkeypatch):
    manager = ComparisonManager()
    monkeypatch.setattr(api, 'manager', manager)
    app = FastAPI(); app.include_router(api.router)
    def prepare(content=b'old\n', name='data.txt'):
        for side, value in (('left', content), ('right', b'new\n')):
            root = tmp_path / side; root.mkdir()
            (root / name).write_bytes(value)
        session = manager.create_session()['session']
        def choose(token, initial, side):
            root = tmp_path / side
            return {**worker.folder_descriptor(root), 'name': side, 'file_count': 1, 'total_bytes': 4}
        monkeypatch.setattr(manager, '_choose_directory', choose)
        left = manager.pick_directory(session, 'left')['selection']['token']
        right = manager.pick_directory(session, 'right')['selection']['token']
        handle = manager.start(session, left, right, {})['job']
        deadline = time.monotonic() + 8
        while manager.job(session, handle)['state'] in ('queued', 'running'):
            assert time.monotonic() < deadline
            time.sleep(.01)
        result = manager.job(session, handle)
        assert result['state'] == 'succeeded'
        query = {'session': session, 'job': handle, 'path': name}
        return query, manager.sessions[session]['jobs'][handle], result
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        yield prepare, client, manager
    manager.close()


@pytest.mark.parametrize('failure,code', [
    ('file', 'file_error'), ('missing', 'report_missing'),
    ('corrupt', 'report_corrupt'), ('page', 'report_page'), ('expired', 'job_expired')])
def test_lookup_reports_specific_static_cause(lookup, failure, code):
    from pathlib import Path
    prepare, client, manager = lookup
    query, job, result = prepare(b'private\0source' if failure == 'file' else b'old\n')
    directory = worker._report_directory(Path(job['cache_dir']), 'data.txt', 'compared')
    if failure == 'file':
        assert result['results'][0]['status'] == 'error'
    elif failure == 'missing':
        (directory / 'metadata.json').unlink()
    elif failure == 'corrupt':
        (directory / 'metadata.json').write_text('{private corrupt text', encoding='utf-8')
    elif failure == 'page':
        query['page'] = 100
    elif failure == 'expired':
        manager.sessions[query['session']]['jobs'].pop(query['job'])
    response = client.post('/api/comparison/diff', json=query, headers={'X-Nice-Tools': 'comparison'})
    assert response.status_code == 400
    assert response.json().get('code') == code
    assert '파일 비교 작업을 완료하지 못했습니다. 폴더와 작업 상태' not in response.text
    assert 'private' not in response.text and job['cache_dir'] not in response.text
    assert response.headers['cache-control'] == 'no-store'
    if failure == 'expired':
        job['folder'].cleanup()


def test_valid_filename_with_invisible_format_character_can_open_its_ready_report(lookup):
    prepare, client, _ = lookup
    query, _, result = prepare(name='data\u200b.txt')
    assert result['results'][0]['reports']['compared']['state'] == 'ready'
    response = client.post('/api/comparison/diff', json=query, headers={'X-Nice-Tools': 'comparison'})
    assert response.status_code == 200
    assert response.json()['additions'] == response.json()['deletions'] == 1


@pytest.mark.parametrize('phase,code', [('source', 'source_unavailable'), ('cache', 'report_access')])
def test_os_access_failures_are_classified_at_the_correct_boundary(lookup, monkeypatch, phase, code):
    prepare, client, _ = lookup
    query, _, _ = prepare()
    def denied(*args, **kwargs):
        raise PermissionError(13, 'private-file-content-and-native-path')
    if phase == 'source':
        monkeypatch.setattr(worker, '_sources', denied)
    else:
        monkeypatch.setattr(worker.diff_cache, '_read_page', denied)
    response = client.post('/api/comparison/diff', json=query, headers={'X-Nice-Tools': 'comparison'})
    assert response.status_code == 400 and response.json()['code'] == code
    assert 'private' not in response.text
