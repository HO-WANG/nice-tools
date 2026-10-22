import importlib
import json
from pathlib import Path

import pytest


def worker():
    try:
        return importlib.import_module('nice_tools.local_compare_worker')
    except ModuleNotFoundError:
        pytest.fail('Local directory worker is not implemented')


def request(tmp_path, left, right, **options):
    module = worker()
    return {'operation': 'compare', 'left': module.folder_descriptor(left),
            'right': module.folder_descriptor(right), 'options': options,
            'cache_dir': str(tmp_path / 'cache')}


def test_direct_folder_comparison_and_cached_pages(tmp_path):
    left, right = tmp_path / 'before', tmp_path / 'after'
    left.mkdir(); right.mkdir()
    (left / 'same.txt').write_bytes('한글\n'.encode('cp949'))
    (right / 'same.txt').write_text('한글\n', encoding='utf-8')
    (left / 'changed.txt').write_text('old\n', encoding='utf-8')
    (right / 'changed.txt').write_text('new\n', encoding='utf-8')
    (left / 'only.txt').write_text('alone\n', encoding='utf-8')
    payload = request(tmp_path, left, right)
    events = []
    result = worker().execute(payload, events.append)
    assert result['summary'] == {'total':3,'same':1,'different':1,'left_only':1,'right_only':0,'error':0}
    assert events[-1]['completed'] == 3
    page = worker().execute({'operation':'diff','cache_dir':payload['cache_dir'],
                             'path':'changed.txt','mode':'compared','page':0})
    assert page['additions'] == page['deletions'] == 1
    assert [row['text'] for row in page['lines'] if row['kind'] in ('add','delete')] == ['old','new']
    assert page['pages'] == 1 and page['page_size'] == 200


def test_changed_file_invalidates_cached_diff(tmp_path):
    left, right = tmp_path / 'before', tmp_path / 'after'
    left.mkdir(); right.mkdir()
    (left / 'data.txt').write_bytes(b'old\n')
    (right / 'data.txt').write_bytes(b'new\n')
    payload = request(tmp_path, left, right)
    worker().execute(payload)
    (right / 'data.txt').write_bytes(b'edited again\n')
    with pytest.raises(ValueError, match='변경'):
        worker().execute({'operation':'diff','cache_dir':payload['cache_dir'],
                          'path':'data.txt','mode':'compared','page':0})


def test_new_counterpart_invalidates_one_sided_diff(tmp_path):
    left,right=tmp_path/'before',tmp_path/'after'
    left.mkdir();right.mkdir()
    (left/'data.txt').write_bytes(b'original\n')
    payload=request(tmp_path,left,right)
    worker().execute(payload)
    (right/'data.txt').write_bytes(b'new counterpart\n')
    with pytest.raises(ValueError,match='변경'):
        worker().execute({'operation':'diff','cache_dir':payload['cache_dir'],'path':'data.txt','mode':'original','page':0})


def test_directory_replacement_during_scan_cannot_expose_outside_names(tmp_path,monkeypatch):
    import os
    module=worker()
    root,child,outside=tmp_path/'root',tmp_path/'root'/'child',tmp_path/'outside'
    child.mkdir(parents=True);outside.mkdir()
    (outside/'sensitive-outside-name.txt').write_bytes(b'private')
    probe=tmp_path/'link-probe'
    try:
        probe.symlink_to(outside,target_is_directory=True);probe.unlink()
    except OSError:
        pytest.skip('Directory symlinks unavailable')
    original=os.scandir
    attacked=[]
    def replacement(path):
        actual=Path(os.readlink(f'/proc/self/fd/{path}')) if isinstance(path,int) else Path(path)
        if actual==child and not attacked:
            attacked.append(True)
            child.rename(root/'original-child')
            child.symlink_to(outside,target_is_directory=True)
        return original(path)
    monkeypatch.setattr(os,'scandir',replacement)
    with pytest.raises((ValueError,OSError)):
        module.scan_folder(module.folder_descriptor(root))
    assert attacked


def test_byte_identical_invalid_text_is_not_reported_same(tmp_path):
    left, right = tmp_path / 'before', tmp_path / 'after'
    left.mkdir(); right.mkdir()
    for root in (left, right): (root / 'binary.txt').write_bytes(b'abc\0bad')
    result = worker().execute(request(tmp_path, left, right))
    assert result['summary']['error'] == 1 and result['summary']['same'] == 0


def test_symlink_does_not_read_outside_selected_folder(tmp_path):
    left, right = tmp_path / 'before', tmp_path / 'after'
    left.mkdir(); right.mkdir()
    outside = tmp_path / 'secret.txt'
    outside.write_text('outside-sensitive-value', encoding='utf-8')
    try:
        (left / 'link.txt').symlink_to(outside)
    except OSError:
        pytest.skip('Symlink creation is unavailable')
    result = worker().execute(request(tmp_path, left, right))
    assert result['summary']['error'] == 1
    assert 'outside-sensitive-value' not in json.dumps(result)


@pytest.mark.parametrize('path', ['../secret', '/secret', 'C:/secret', 'sub/../secret', r'sub\secret'])
def test_diff_rejects_unsafe_relative_paths(tmp_path, path):
    with pytest.raises(ValueError):
        worker().execute({'operation':'diff','cache_dir':str(tmp_path), 'path':path, 'mode':'original','page':0})


def test_raw_equal_fast_path_can_show_both_diff_modes_without_normalizing(tmp_path, monkeypatch):
    left, right = tmp_path / 'before', tmp_path / 'after'
    left.mkdir(); right.mkdir()
    for root in (left, right): (root / 'data.txt').write_text('same\n',encoding='utf-8')
    payload = request(tmp_path,left,right,sort_lines=True,ignore_whitespace=True)
    worker().execute(payload)
    from nice_tools import local_compare_engine
    monkeypatch.setattr(local_compare_engine,'normalize_file',lambda *args,**kwargs: pytest.fail('Raw equal files need no normalization'))
    for mode in ('original','compared'):
        page = worker().execute({'operation':'diff','cache_dir':payload['cache_dir'],'path':'data.txt','mode':mode,'page':0})
        assert page['identical'] and page['lines'] == []
