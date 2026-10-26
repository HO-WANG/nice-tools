"""Cached diff pages preserve the complete existing diff representation."""

from concurrent.futures import CancelledError
import json
from pathlib import Path
import random

import pytest

from nice_tools.comparison import CompareOptions
from nice_tools.diff import file_diff


def module():
    from nice_tools import local_compare_diff
    return local_compare_diff


def normalized(tmp_path, name, lines):
    if lines is None:
        return None
    path = tmp_path / name
    path.write_bytes(''.join(line + '\n' for line in lines).encode('utf-8'))
    return path


def encoded(lines):
    return None if lines is None else ''.join(line + '\n' for line in lines).encode('utf-8')


def build(tmp_path, old, new, name='cache', **kwargs):
    return module().build_diff(normalized(tmp_path, name + '-old', old),
                               normalized(tmp_path, name + '-new', new), tmp_path / name,
                               path='sub/a.txt', old_exists=old is not None,
                               new_exists=new is not None, **kwargs)


def all_rows(directory, metadata):
    return [row for page in range(metadata['pages'])
            for row in module().read_page(directory, page)['lines']]


@pytest.mark.parametrize('old,new', [([], []), (None, []), ([], None), (None, None),
                                     (None, ['one', '', 'two']), (['old'], None),
                                     (['same'], ['same']), (['one', 'old', 'end'], ['one', 'new', 'end'])])
def test_pages_match_existing_diff(tmp_path, old, new):
    expected = file_diff('sub/a.txt', encoded(old), encoded(new), CompareOptions())
    metadata = build(tmp_path, old, new)
    assert metadata == {key: value for key, value in expected.items() if key != 'lines'} | {
        'total_lines': len(expected['lines']), 'page_size': 200,
        'pages': max(1, (len(expected['lines']) + 199) // 200)}
    assert all_rows(tmp_path / 'cache', metadata) == expected['lines']
    assert module().read_page(tmp_path / 'cache', 0) == metadata | {
        'page': 0, 'lines': expected['lines'][:200]}


def test_repeated_random_lines_and_large_edit_distance_match_existing_diff(tmp_path):
    rng = random.Random(7391)
    pairs = [([rng.choice(['a', 'b', '', '한글']) for _ in range(rng.randrange(50))],
              [rng.choice(['a', 'b', '', '한글']) for _ in range(rng.randrange(50))])
             for _ in range(70)]
    old = ['repeated'] * 10000
    new = old.copy()
    for index in range(129):
        new[index * 9999 // 128] = f'change {index}'
    pairs.append((old, new))
    for index, (old, new) in enumerate(pairs):
        name = f'cache-{index}'
        metadata = build(tmp_path, old, new, name)
        expected = file_diff('sub/a.txt', encoded(old), encoded(new), CompareOptions())
        assert all_rows(tmp_path / name, metadata) == expected['lines']
        assert (metadata['additions'], metadata['deletions']) == (expected['additions'], expected['deletions'])


def test_pages_are_200_records_and_read_without_sources_or_matcher(tmp_path, monkeypatch):
    metadata = build(tmp_path, None, [str(index) for index in range(451)])
    assert (metadata['pages'], metadata['total_lines']) == (3, 452)
    (tmp_path / 'cache-new').unlink()
    def forbidden(*args, **kwargs):
        raise AssertionError('pages must not rebuild the diff')
    monkeypatch.setattr(module(), 'changed_groups', forbidden)
    pages = [module().read_page(tmp_path / 'cache', number) for number in range(3)]
    assert [len(page['lines']) for page in pages] == [200, 200, 52]
    assert pages[1]['lines'][0]['new_line'] == 200
    assert pages[-1]['lines'][-1]['text'] == '450'


def test_lf_only_records_preserve_unicode_separators_and_control_characters(tmp_path):
    old = ['a\u2028b\u2029c\x85d\v\f\t"\\', '끝']
    new = ['new\u2028line', '끝']
    metadata = build(tmp_path, old, new)
    assert all_rows(tmp_path / 'cache', metadata) == file_diff(
        'sub/a.txt', encoded(old), encoded(new), CompareOptions())['lines']
    for file in (tmp_path / 'cache').glob('*.json'):
        json.loads(file.read_text('utf-8'))


def test_complete_large_line_and_change_after_former_ten_megabyte_cap(tmp_path):
    large = '한' * (4 * 1024 * 1024)
    metadata = build(tmp_path, [large, 'old'], [large, 'new'])
    rows = all_rows(tmp_path / 'cache', metadata)
    assert rows[1]['text'] == large
    assert [row['text'] for row in rows if row['kind'] in ('delete', 'add')] == ['old', 'new']
    assert (metadata['additions'], metadata['deletions']) == (1, 1)


def test_cancellation_removes_owned_partial_cache(tmp_path):
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 20
    with pytest.raises(CancelledError):
        build(tmp_path, None, [str(index) for index in range(1000)], cancelled=cancelled)
    assert not (tmp_path / 'cache').exists()


def test_invalid_utf8_fails_strictly_and_cleans_partial_cache(tmp_path):
    source = tmp_path / 'invalid'
    source.write_bytes(b'valid\n\xff\n')
    with pytest.raises(UnicodeDecodeError):
        module().build_diff(source, None, tmp_path / 'cache', path='a', old_exists=True, new_exists=False)
    assert not (tmp_path / 'cache').exists()


def test_finished_cache_is_never_overwritten(tmp_path):
    original = build(tmp_path, ['old'], ['new'])
    with pytest.raises(FileExistsError):
        build(tmp_path, ['old'], ['other'])
    assert module().read_page(tmp_path / 'cache', 0)['additions'] == original['additions']
    assert [row['text'] for row in all_rows(tmp_path / 'cache', original) if row['kind'] == 'add'] == ['new']


def test_incomplete_cache_and_invalid_page_are_rejected(tmp_path):
    directory = tmp_path / 'cache'
    directory.mkdir()
    with pytest.raises((ValueError, FileNotFoundError)):
        module().read_page(directory, 0)
    metadata = build(tmp_path, [], [])
    for number in (-1, 1, True, '0'):
        with pytest.raises(ValueError):
            module().read_page(directory, number)
    assert metadata['pages'] == 1


@pytest.mark.parametrize('count,expected_sizes', [(199, [200]), (200, [200, 1]), (399, [200, 200])])
def test_exact_page_boundaries(tmp_path, count, expected_sizes):
    metadata = build(tmp_path, None, ['line'] * count)
    assert [len(module().read_page(tmp_path / 'cache', page)['lines'])
            for page in range(metadata['pages'])] == expected_sizes


def test_cancellation_after_page_written_removes_partial_pages(tmp_path):
    saw_page = False
    def cancelled():
        nonlocal saw_page
        saw_page = any((tmp_path / 'cache').glob('page-*.json'))
        return saw_page
    with pytest.raises(CancelledError):
        build(tmp_path, None, ['line'] * 451, cancelled=cancelled)
    assert saw_page
    assert not (tmp_path / 'cache').exists()


def test_missing_page_in_completed_cache_is_rejected(tmp_path):
    build(tmp_path, None, ['line'] * 451)
    for page in (tmp_path / 'cache').glob('page-*.json'):
        page.unlink()
    with pytest.raises(ValueError, match='Incomplete'):
        module().read_page(tmp_path / 'cache', 0)


def test_nonempty_directory_is_preserved_on_rejection(tmp_path):
    directory = tmp_path / 'cache'
    directory.mkdir()
    sentinel = directory / 'original'
    sentinel.write_text('retain', encoding='utf-8')
    with pytest.raises(FileExistsError):
        build(tmp_path, [], [])
    assert sentinel.read_text('utf-8') == 'retain'


def test_large_edit_distance_keeps_identical_record_across_cached_pages(tmp_path):
    shared = '202609|1011156045|CT9701|101'
    old = ([f'old {i}' for i in range(300)] + ['before', 'removed', shared, 'after']
           + [f'old tail {i}' for i in range(300)])
    new = ([f'new {i}' for i in range(300)] + ['before', shared, 'inserted', 'after']
           + [f'new tail {i}' for i in range(300)])
    metadata = build(tmp_path, old, new)
    rows = all_rows(tmp_path / 'cache', metadata)
    assert [row for row in rows if row['text'] == shared] == [
        {'kind': 'context', 'text': shared, 'old_line': 303, 'new_line': 302}]
    assert (metadata['additions'], metadata['deletions']) == (601, 601)
