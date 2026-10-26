"""Eager reports respect edit-distance and stored-row boundaries."""

from concurrent.futures import CancelledError

import pytest

from nice_tools import local_compare_diff as diff


def source(tmp_path, name, lines):
    if lines is None:
        return None
    path = tmp_path / name
    path.write_text(''.join(line + '\n' for line in lines), encoding='utf-8', newline='\n')
    return path


def prepare(tmp_path, old, new, **kwargs):
    return diff.prepare_diff(source(tmp_path, 'old', old), source(tmp_path, 'new', new),
                             tmp_path / 'report', path='a.txt', old_exists=old is not None,
                             new_exists=new is not None, **kwargs)


def rows(tmp_path, metadata):
    return [row for page in range(metadata['pages'])
            for row in diff.read_page(tmp_path / 'report', page)['lines']]


@pytest.mark.parametrize('count,state', [(999, 'ready'), (1000, 'deferred'), (1001, 'deferred')])
@pytest.mark.parametrize('side', ['old', 'new', 'both'])
def test_exact_changed_line_threshold(tmp_path, count, state, side):
    old = None if side == 'new' else [f'old {i}' for i in range(count if side == 'old' else count // 2)]
    new = None if side == 'old' else [f'new {i}' for i in range(count if side == 'new' else count - count // 2)]
    result = prepare(tmp_path, old, new)
    assert result['state'] == state
    if state == 'deferred':
        assert result == {'state': 'deferred', 'threshold': 1000}
        assert not (tmp_path / 'report').exists()
    else:
        assert result['additions'] + result['deletions'] == 999
        assert result['display_limit'] == 1000
        assert result['total_lines'] == result['full_total_lines'] == 1000
        assert result['truncated'] is False


def test_deferred_never_aligns_or_publishes_pages(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('deferred reports must not align or publish')
    monkeypatch.setattr(diff, 'changed_groups', forbidden)
    monkeypatch.setattr(diff, '_write_json', forbidden)
    # Equal lengths prevent a line-count-only shortcut; each substitution costs two.
    assert prepare(tmp_path, ['repeat'] * 500, ['different'] * 500) == {
        'state': 'deferred', 'threshold': 1000}
    assert not (tmp_path / 'report').exists()


def test_ready_reuses_each_normalized_read_and_preserves_duplicate_lcs(tmp_path, monkeypatch):
    original = diff._read_lines
    reads = []
    def once(path, cancelled):
        assert path not in reads
        reads.append(path)
        return original(path, cancelled)
    monkeypatch.setattr(diff, '_read_lines', once)
    result = prepare(tmp_path, ['same'] * 600 + ['before', 'shared', 'after'],
                     ['new'] * 100 + ['same'] * 600 + ['before', 'shared', 'inserted', 'after'])
    assert result['state'] == 'ready'
    assert (result['additions'], result['deletions']) == (101, 0)
    assert any(row['kind'] == 'context' and row['text'] == 'shared' for row in rows(tmp_path, result))


def test_display_cap_includes_context_and_counts_complete_report(tmp_path):
    old = [f'line {i}' for i in range(10000)]
    new = old.copy()
    for index in range(400):
        new[index * 20] = f'changed {index}'
    result = prepare(tmp_path, old, new)
    assert result['state'] == 'ready'
    assert (result['additions'], result['deletions']) == (400, 400)
    assert result['total_lines'] == 1000
    assert result['pages'] == 5
    assert result['full_total_lines'] == 3597
    assert result['truncated'] is True
    displayed = rows(tmp_path, result)
    assert len(displayed) == 1000
    assert {'context', 'hunk', 'add', 'delete'} == {row['kind'] for row in displayed}
    assert len(list((tmp_path / 'report').glob('page-*.json'))) == 5
    with pytest.raises(ValueError, match='out of range'):
        diff.read_page(tmp_path / 'report', 5)


def test_explicit_capped_build_keeps_full_counts_after_display_limit(tmp_path):
    result = diff.build_diff(source(tmp_path, 'old', ['old'] * 600),
                             source(tmp_path, 'new', ['new'] * 800), tmp_path / 'report',
                             path='a.txt', old_exists=True, new_exists=True, max_lines=1000)
    assert (result['additions'], result['deletions']) == (800, 600)
    assert result['full_total_lines'] == 1401
    assert result['total_lines'] == len(rows(tmp_path, result)) == 1000
    assert result['display_limit'] == 1000 and result['truncated'] is True


def test_prepare_validates_complete_source_even_when_already_over_threshold(tmp_path):
    invalid = tmp_path / 'invalid'
    invalid.write_bytes(b'line\n' * 1001 + b'\xff\n')
    with pytest.raises(UnicodeDecodeError):
        diff.prepare_diff(invalid, None, tmp_path / 'report', path='a', old_exists=True, new_exists=False)
    assert not (tmp_path / 'report').exists()


def test_preparation_cancellation_does_not_publish_directory(tmp_path):
    with pytest.raises(CancelledError):
        prepare(tmp_path, ['old'] * 500, ['new'] * 500, cancelled=lambda: True)
    assert not (tmp_path / 'report').exists()


def test_capped_build_cancellation_removes_written_pages(tmp_path):
    with pytest.raises(CancelledError):
        prepare(tmp_path, None, ['line'] * 999,
                cancelled=lambda: any((tmp_path / 'report').glob('page-*.json')))
    assert not (tmp_path / 'report').exists()


@pytest.mark.parametrize('old,new,identical', [(None, [], False), ([], [], True), (None, None, False)])
def test_empty_reports_are_ready_with_one_empty_page(tmp_path, old, new, identical):
    result = prepare(tmp_path, old, new)
    assert (result['state'], result['identical'], result['pages']) == ('ready', identical, 1)
    assert result['total_lines'] == result['full_total_lines'] == 0
    assert result['truncated'] is False
    assert rows(tmp_path, result) == []
