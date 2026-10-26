"""Initial reporting and explicit large-report ownership through real workers."""
import time

import pytest

from nice_tools import local_compare_worker as worker
from nice_tools.local_compare_jobs import ComparisonManager


def compare(tmp_path, old, new, **options):
    left, right = tmp_path / 'left', tmp_path / 'right'
    left.mkdir(); right.mkdir()
    for folder, records in ((left, old), (right, new)):
        if records is not None:
            (folder / 'data.txt').write_text(''.join(row + '\n' for row in records), encoding='utf-8')
    cache = tmp_path / 'cache'
    result = worker.execute({'operation': 'compare', 'left': worker.folder_descriptor(left),
                             'right': worker.folder_descriptor(right), 'options': options,
                             'cache_dir': str(cache)})
    return result, {'operation': 'diff', 'cache_dir': str(cache), 'path': 'data.txt', 'page': 0}


def test_small_reports_exist_before_first_selection_for_both_views(tmp_path, monkeypatch):
    result, query = compare(tmp_path, ['a b'], ['ab'], ignore_whitespace=True)
    assert result['results'][0]['reports']['compared']['state'] == 'ready'
    assert result['results'][0]['reports']['original']['state'] == 'ready'
    monkeypatch.setattr(worker.engine, 'normalize_file', lambda *a, **k: pytest.fail('Selection normalized source'))
    monkeypatch.setattr(worker.diff_cache, 'build_diff', lambda *a, **k: pytest.fail('Selection built report'))
    assert worker.execute({**query, 'mode': 'compared'})['identical']
    assert worker.execute({**query, 'mode': 'original'})['additions'] == 1


@pytest.mark.parametrize('changes', [999, 1000, 1001])
def test_exact_threshold_defers_only_large_reports_and_caps_explicit_analysis(tmp_path, changes):
    result, query = compare(tmp_path, None, [f'new {i}' for i in range(changes)])
    state = result['results'][0]['reports']['compared']['state']
    assert state == ('ready' if changes < 1000 else 'deferred')
    page = worker.execute(query)
    if changes >= 1000:
        assert page['state'] == 'deferred' and page['lines'] == []
        assert not list((tmp_path / 'cache' / 'diffs').rglob('metadata.json'))
        page = worker.execute({**query, 'analyze': True})
    assert page['additions'] == changes and page['deletions'] == 0
    assert page['total_lines'] <= 1000 and page['pages'] <= 5
    assert page['truncated'] is (changes >= 1000)
    all_rows = [row for n in range(page['pages'])
                for row in worker.execute({**query, 'page': n})['lines']]
    assert len(all_rows) == min(changes + 1, 1000)
    assert (tmp_path / 'right' / 'data.txt').read_text().endswith(f'new {changes - 1}\n')


def test_first_ready_selection_never_starts_another_worker(tmp_path, monkeypatch):
    manager = ComparisonManager()
    try:
        session = manager.create_session()['session']
        for side, content in (('left', 'old\n'), ('right', 'new\n')):
            root = tmp_path / side
            root.mkdir()
            (root / 'data.txt').write_text(content, encoding='utf-8')
        def choose(token, initial, side):
            root = tmp_path / side
            return {**worker.folder_descriptor(root), 'file_count': 1, 'total_bytes': 4, 'name': side}
        monkeypatch.setattr(manager, '_choose_directory', choose)
        left = manager.pick_directory(session, 'left')['selection']['token']
        right = manager.pick_directory(session, 'right')['selection']['token']
        result = manager.start(session, left, right, {})
        deadline = time.monotonic() + 8
        while manager.job(session, result['job'])['state'] in ('queued', 'running'):
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert manager.job(session, result['job'])['state'] == 'succeeded'
        monkeypatch.setattr(manager, '_request', lambda *a, **k: pytest.fail('Ready lookup launched worker'))
        assert manager.diff(session, result['job'], 'data.txt')['additions'] == 1
        assert manager.diff(session, result['job'], 'data.txt', 'original')['deletions'] == 1
        assert not manager.processes
    finally:
        manager.close()
