"""Build a reusable, paginated diff from owned normalized text caches.

Normalized files contain strict UTF-8 LF-terminated records, including blank
records; Unicode separators inside records are ordinary content. Matching uses
existing ``changed_groups`` and holds the selected pair's line lists in memory.
Diff JSON is written one page at a time rather than collected for the full pair.
The caller serializes builds and validates original-file fingerprints on reuse.
"""

from concurrent.futures import CancelledError
import json
from pathlib import Path
import shutil
from typing import Callable

from .diff import changed_groups

PAGE_SIZE = 200
_METADATA = 'metadata.json'


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise CancelledError()


def _read_lines(path: Path | None, cancelled: Callable[[], bool] | None) -> list[str]:
    if path is None:
        return []
    lines = []
    # newline='\n' deliberately avoids splitlines/universal newline semantics.
    with path.open('r', encoding='utf-8', errors='strict', newline='\n') as source:
        for record in source:
            _check_cancelled(cancelled)
            if not record.endswith('\n'):
                raise ValueError('Incomplete normalized line record.')
            lines.append(record[:-1])
    return lines


def _write_json(path: Path, value) -> None:
    temporary = path.with_suffix('.tmp')
    with temporary.open('x', encoding='utf-8', newline='\n') as output:
        # Escape controls and Unicode separators for safe JSON transport.
        json.dump(value, output, ensure_ascii=True, separators=(',', ':'))
    temporary.replace(path)


def _span(start: int, stop: int) -> str:
    length = stop - start
    if length == 0:
        return f'{start},0'
    return str(start + 1) if length == 1 else f'{start + 1},{length}'


def _page_path(directory: Path, page: int) -> Path:
    return directory / f'page-{page:08d}.json'


def build_diff(
    left_normalized: Path | None,
    right_normalized: Path | None,
    directory: Path,
    *,
    path: str,
    old_exists: bool,
    new_exists: bool,
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Create a complete cache in a new/empty owned directory.

    Existing nonempty directories are rejected unchanged. Failed or cancelled
    builds remove their owned directory. Metadata is published last, after every
    page has closed. Even an empty diff has page zero. Cancellation is checked
    between reads and emitted rows; callers can kill their owned worker while
    the existing matcher is running.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise FileExistsError('Diff cache directory must be empty.')
    # Claim the empty directory before entering cleanup ownership. Parent callers
    # serialize cache builds; exclusive creation also rejects concurrent claims.
    marker = directory / '.building'
    marker.touch(exist_ok=False)
    try:
        _check_cancelled(cancelled)
        if (left_normalized is not None) != old_exists or (right_normalized is not None) != new_exists:
            raise ValueError('Normalized paths must match file existence.')
        old = _read_lines(left_normalized, cancelled)
        new = _read_lines(right_normalized, cancelled)
        _check_cancelled(cancelled)
        groups = changed_groups(old, new)
        _check_cancelled(cancelled)
        page_rows = []
        pages = total_lines = additions = deletions = 0

        def append(kind, text, old_line=None, new_line=None):
            nonlocal pages, total_lines, additions, deletions
            _check_cancelled(cancelled)
            page_rows.append({'kind': kind, 'text': text, 'old_line': old_line, 'new_line': new_line})
            total_lines += 1
            additions += kind == 'add'
            deletions += kind == 'delete'
            if len(page_rows) == PAGE_SIZE:
                _write_json(_page_path(directory, pages), page_rows)
                pages += 1
                page_rows.clear()

        for group in groups:
            first, last = group[0], group[-1]
            append('hunk', f'@@ -{_span(first[1], last[2])} +{_span(first[3], last[4])} @@')
            for tag, i1, i2, j1, j2 in group:
                if tag == 'equal':
                    for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                        append('context', old[i], i + 1, j + 1)
                if tag in ('delete', 'replace'):
                    for i in range(i1, i2):
                        append('delete', old[i], i + 1)
                if tag in ('insert', 'replace'):
                    for j in range(j1, j2):
                        append('add', new[j], new_line=j + 1)
        if page_rows or not pages:
            _write_json(_page_path(directory, pages), page_rows)
            pages += 1
        metadata = {
            'path': path, 'old_exists': old_exists, 'new_exists': new_exists,
            'identical': old_exists and new_exists and old == new,
            'additions': additions, 'deletions': deletions,
            'total_lines': total_lines, 'page_size': PAGE_SIZE, 'pages': pages,
        }
        _check_cancelled(cancelled)
        marker.unlink()
        _write_json(directory / _METADATA, metadata)
        return metadata
    except BaseException:
        shutil.rmtree(directory)
        raise


def read_page(directory: Path, page: int) -> dict:
    """Read an already built zero-based page without touching normalized files."""
    if type(page) is not int or page < 0:
        raise ValueError('Diff page must be a nonnegative integer.')
    directory = Path(directory)
    with (directory / _METADATA).open(encoding='utf-8') as source:
        metadata = json.load(source)
    if (not isinstance(metadata, dict) or type(metadata.get('pages')) is not int
            or type(metadata.get('total_lines')) is not int or metadata['total_lines'] < 0
            or metadata.get('page_size') != PAGE_SIZE
            or metadata['pages'] != max(1, (metadata['total_lines'] + PAGE_SIZE - 1) // PAGE_SIZE)):
        raise ValueError('Incomplete diff cache metadata.')
    if page >= metadata['pages']:
        raise ValueError('Diff page is out of range.')
    try:
        with _page_path(directory, page).open(encoding='utf-8') as source:
            lines = json.load(source)
    except FileNotFoundError:
        raise ValueError('Incomplete diff cache page.') from None
    expected = min(PAGE_SIZE, max(0, metadata['total_lines'] - page * PAGE_SIZE))
    if not isinstance(lines, list) or len(lines) != expected:
        raise ValueError('Incomplete diff cache page.')
    return metadata | {'page': page, 'lines': lines}
