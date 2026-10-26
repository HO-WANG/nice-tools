"""Initial comparison retains full reports and a change-counted preview."""

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


def rows(tmp_path, metadata, *, full=False):
    directory = tmp_path / 'report'
    if full:
        directory /= 'full'
        metadata = diff.read_page(directory, 0)
    return [row for page in range(metadata['pages'])
            for row in diff.read_page(directory, page)['lines']]


@pytest.mark.parametrize('count', [999, 1000, 1001])
@pytest.mark.parametrize('side', ['old', 'new', 'both'])
def test_exact_changed_line_boundary_is_ready_and_complete(tmp_path, count, side):
    old = None if side == 'new' else [f'old {i}' for i in range(count if side == 'old' else count // 2)]
    new = None if side == 'old' else [f'new {i}' for i in range(count if side == 'new' else count - count // 2)]
    result = prepare(tmp_path, old, new)
    assert result['state'] == 'ready'
    assert result['additions'] + result['deletions'] == count
    assert result['change_limit'] == 1000
    assert result['displayed_changes'] == min(count, 1000)
    assert result['total_lines'] == min(count, 1000) + 1
    assert result['full_total_lines'] == count + 1
    assert result['truncated'] is (count > 1000)
    full = diff.read_page(tmp_path / 'report' / 'full', 0)
    assert result['full_pages'] == full['pages']
    assert (full['additions'], full['deletions']) == (result['additions'], result['deletions'])
    assert len(rows(tmp_path, result, full=True)) == count + 1


def test_replacement_preview_keeps_first_thousand_changes_and_full_report(tmp_path):
    result = prepare(tmp_path, ['old'] * 600, ['new'] * 600)
    displayed = rows(tmp_path, result)
    assert (result['additions'], result['deletions']) == (600, 600)
    assert result['displayed_changes'] == 1000
    assert [row['kind'] for row in displayed] == ['hunk'] + ['delete'] * 600 + ['add'] * 400
    full = rows(tmp_path, result, full=True)
    assert [row['kind'] for row in full] == ['hunk'] + ['delete'] * 600 + ['add'] * 600
    assert full[:len(displayed)] == displayed


def test_ready_reuses_each_normalized_read_and_preserves_duplicate_lcs(tmp_path, monkeypatch):
    original = diff._read_lines
    reads = []
    def once(path, cancelled):
        assert path not in reads
        reads.append(path)
        return original(path, cancelled)
    monkeypatch.setattr(diff, '_read_lines', once)
    shared = '202609|1011156045|CT9701|101'
    result = prepare(tmp_path, ['old'] * 600 + ['before', 'removed', shared, 'after'],
                     ['new'] * 600 + ['before', shared, 'inserted', 'after'])
    assert result['state'] == 'ready'
    assert (result['additions'], result['deletions']) == (601, 601)
    assert [row for row in rows(tmp_path, result, full=True) if row['text'] == shared] == [
        {'kind': 'context', 'text': shared, 'old_line': 603, 'new_line': 602}]


@pytest.mark.parametrize('changes', [400, 500, 501])
def test_context_and_headings_do_not_consume_change_budget(tmp_path, changes):
    old = [f'line {i}' for i in range(changes * 20)]
    new = old.copy()
    for index in range(changes):
        new[index * 20] = f'changed {index}'
    result = prepare(tmp_path, old, new)
    displayed = rows(tmp_path, result)
    full = rows(tmp_path, result, full=True)
    assert (result['additions'], result['deletions']) == (changes, changes)
    assert result['displayed_changes'] == min(1000, changes * 2)
    assert sum(row['kind'] in ('add', 'delete') for row in displayed) == result['displayed_changes']
    assert result['total_lines'] == len(displayed) > 1000
    assert result['full_total_lines'] == len(full)
    assert result['truncated'] is (changes > 500)
    if changes <= 500:
        assert displayed == full
        assert displayed[-1]['kind'] == 'context'
    else:
        assert displayed[-1]['kind'] == 'add'
        assert displayed == full[:len(displayed)]


def test_cached_first_view_and_full_pages_do_not_read_or_match_again(tmp_path, monkeypatch):
    result = prepare(tmp_path, ['old'] * 600, ['new'] * 600)
    (tmp_path / 'old').unlink()
    (tmp_path / 'new').unlink()
    def forbidden(*args, **kwargs):
        raise AssertionError('cached reports must not read or align')
    monkeypatch.setattr(diff, '_read_lines', forbidden)
    monkeypatch.setattr(diff, 'changed_groups', forbidden)
    assert len(rows(tmp_path, result)) == 1001
    assert len(rows(tmp_path, result, full=True)) == 1201


def test_explicit_row_capped_build_retains_legacy_compatibility(tmp_path):
    result = diff.build_diff(source(tmp_path, 'old', ['old'] * 600),
                             source(tmp_path, 'new', ['new'] * 800), tmp_path / 'report',
                             path='a.txt', old_exists=True, new_exists=True, max_lines=1000)
    assert (result['additions'], result['deletions']) == (800, 600)
    assert result['full_total_lines'] == 1401
    assert result['total_lines'] == len(rows(tmp_path, result)) == 1000
    assert result['display_limit'] == 1000 and result['truncated'] is True


def test_change_capped_build_can_omit_full_store(tmp_path):
    result = diff.build_diff(source(tmp_path, 'old', ['old'] * 3),
                             source(tmp_path, 'new', ['new'] * 3), tmp_path / 'report',
                             path='a.txt', old_exists=True, new_exists=True, max_changes=4)
    assert result['displayed_changes'] == 4
    assert result['total_lines'] == 5
    assert result['full_total_lines'] == 7
    assert result['truncated'] is True
    assert not (tmp_path / 'report' / 'full').exists()


@pytest.mark.parametrize('invalid', [b'line\n' * 1001 + b'\xff\n', b'line\n' * 1001 + b'incomplete'])
def test_preparation_validates_complete_source(tmp_path, invalid):
    path = tmp_path / 'invalid'
    path.write_bytes(invalid)
    with pytest.raises((UnicodeDecodeError, ValueError)):
        diff.prepare_diff(path, None, tmp_path / 'report', path='a', old_exists=True, new_exists=False)
    assert not (tmp_path / 'report').exists()


def test_preparation_cancellation_does_not_publish_directory(tmp_path):
    with pytest.raises(CancelledError):
        prepare(tmp_path, ['old'] * 500, ['new'] * 500, cancelled=lambda: True)
    assert not (tmp_path / 'report').exists()


@pytest.mark.parametrize('store', ['', 'full'])
def test_cancellation_removes_both_partial_stores(tmp_path, store):
    with pytest.raises(CancelledError):
        prepare(tmp_path, None, ['line'] * 1201,
                cancelled=lambda: any((tmp_path / 'report' / store).glob('page-*.json')))
    assert not (tmp_path / 'report').exists()



def test_cancellation_after_full_publication_removes_entire_cache(tmp_path):
    with pytest.raises(CancelledError):
        prepare(tmp_path, None, ['line'] * 1201,
                cancelled=lambda: (tmp_path / 'report' / 'full' / 'metadata.json').exists())
    assert not (tmp_path / 'report').exists()

def test_full_metadata_is_published_before_preview_metadata(tmp_path, monkeypatch):
    original = diff._write_json
    published = []
    def observe(path, value):
        if path.name == 'metadata.json':
            published.append(path.parent.name)
            if path.parent.name == 'report':
                full = diff.read_page(path.parent / 'full', 6)
                assert len(full['lines']) == 2
        original(path, value)
    monkeypatch.setattr(diff, '_write_json', observe)
    prepare(tmp_path, None, ['line'] * 1201)
    assert published == ['full', 'report']


@pytest.mark.parametrize('store,filename', [('report', 'metadata.json'), ('full', 'page-00000001.json')])
def test_write_failure_removes_both_stores(tmp_path, monkeypatch, store, filename):
    original = diff._write_json
    def fail(path, value):
        if path.parent.name == store and path.name == filename:
            raise OSError('disk full')
        original(path, value)
    monkeypatch.setattr(diff, '_write_json', fail)
    with pytest.raises(OSError, match='disk full'):
        prepare(tmp_path, None, ['line'] * 1201)
    assert not (tmp_path / 'report').exists()


@pytest.mark.parametrize('old,new,identical', [(None, [], False), ([], [], True), (None, None, False)])
def test_empty_reports_are_ready_with_empty_preview_and_full_page(tmp_path, old, new, identical):
    result = prepare(tmp_path, old, new)
    assert (result['state'], result['identical'], result['pages'], result['full_pages']) == (
        'ready', identical, 1, 1)
    assert result['total_lines'] == result['full_total_lines'] == result['displayed_changes'] == 0
    assert result['truncated'] is False
    assert rows(tmp_path, result) == rows(tmp_path, result, full=True) == []
