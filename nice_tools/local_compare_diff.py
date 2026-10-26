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
MAX_DIFF_CHANGES = 1000
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


def _load_lines(left_normalized, right_normalized, old_exists, new_exists, cancelled):
    _check_cancelled(cancelled)
    if (left_normalized is not None) != old_exists or (right_normalized is not None) != new_exists:
        raise ValueError('Normalized paths must match file existence.')
    old = _read_lines(left_normalized, cancelled)
    new = _read_lines(right_normalized, cancelled)
    _check_cancelled(cancelled)
    return old, new


def prepare_diff(
    left_normalized: Path | None,
    right_normalized: Path | None,
    directory: Path,
    *,
    path: str,
    old_exists: bool,
    new_exists: bool,
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Build the complete report and first 1000 changed entries during compare."""
    metadata = build_diff(left_normalized, right_normalized, directory, path=path,
                          old_exists=old_exists, new_exists=new_exists,
                          cancelled=cancelled, max_changes=MAX_DIFF_CHANGES, keep_full=True)
    return metadata | {'state': 'ready'}


def build_diff(
    left_normalized: Path | None,
    right_normalized: Path | None,
    directory: Path,
    *,
    path: str,
    old_exists: bool,
    new_exists: bool,
    cancelled: Callable[[], bool] | None = None,
    max_lines: int | None = None,
    max_changes: int | None = None,
    keep_full: bool = False,
) -> dict:
    """Create paginated reports in a new/empty owned directory.

    By default retain the complete existing representation. ``max_changes``
    limits additions/deletions in the preview, excluding headings/context. If
    changes exceed that budget, stop immediately after the last allowed change;
    otherwise retain every context row. ``keep_full`` streams an independent
    complete report under ``full/`` before publishing preview metadata.
    ``max_lines`` retains the legacy all-row cap and cannot combine with a
    change cap. Failure/cancellation removes the entire owned directory.
    """
    return _build_diff(
        lambda: _load_lines(left_normalized, right_normalized, old_exists, new_exists, cancelled),
        directory, path=path, old_exists=old_exists, new_exists=new_exists,
        cancelled=cancelled, max_lines=max_lines, max_changes=max_changes, keep_full=keep_full)


class _PageWriter:
    """Retain at most one 200-row page while streaming a report to disk."""

    def __init__(self, directory):
        self.directory = directory
        self.rows = []
        self.pages = 0
        self.total_lines = 0

    def append(self, row):
        self.rows.append(row)
        self.total_lines += 1
        if len(self.rows) == PAGE_SIZE:
            self._flush()

    def _flush(self):
        _write_json(_page_path(self.directory, self.pages), self.rows)
        self.pages += 1
        self.rows.clear()

    def finish(self):
        if self.rows or not self.pages:
            self._flush()
        return {'total_lines': self.total_lines, 'page_size': PAGE_SIZE, 'pages': self.pages}


def _build_diff(load_lines, directory, *, path, old_exists, new_exists, cancelled,
                max_lines, max_changes, keep_full):
    for limit in (max_lines, max_changes):
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError('Diff display limit must be a nonnegative integer.')
    if max_lines is not None and max_changes is not None:
        raise ValueError('Choose either a row limit or a change limit.')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise FileExistsError('Diff cache directory must be empty.')
    # Claim before taking cleanup ownership; reject concurrent builders unchanged.
    marker = directory / '.building'
    marker.touch(exist_ok=False)
    try:
        _check_cancelled(cancelled)
        old, new = load_lines()
        _check_cancelled(cancelled)
        groups = changed_groups(old, new)
        _check_cancelled(cancelled)
        # Count compact opcodes before writing to distinguish exactly-at-limit
        # reports (which keep trailing context) from genuinely truncated ones.
        additions = deletions = 0
        full_total_lines = len(groups)
        for group in groups:
            _check_cancelled(cancelled)
            for tag, i1, i2, j1, j2 in group:
                _check_cancelled(cancelled)
                if tag == 'equal':
                    full_total_lines += i2 - i1
                if tag in ('delete', 'replace'):
                    deletions += i2 - i1
                    full_total_lines += i2 - i1
                if tag in ('insert', 'replace'):
                    additions += j2 - j1
                    full_total_lines += j2 - j1
        change_truncated = max_changes is not None and additions + deletions > max_changes
        displayed_changes = 0
        preview = _PageWriter(directory)
        full = None
        if keep_full:
            (directory / 'full').mkdir()
            full = _PageWriter(directory / 'full')

        def preview_available():
            return ((max_lines is None or preview.total_lines < max_lines)
                    and (not change_truncated or displayed_changes < max_changes))

        def append(kind, text, old_line=None, new_line=None):
            nonlocal displayed_changes
            _check_cancelled(cancelled)
            show = preview_available()
            if full is None and not show:
                return
            row = {'kind': kind, 'text': text, 'old_line': old_line, 'new_line': new_line}
            if full is not None:
                full.append(row)
            if show:
                preview.append(row)
                if kind in ('add', 'delete'):
                    displayed_changes += 1

        def display_stop(start, stop, *, change=False):
            if full is not None:
                return stop
            if not preview_available():
                return start
            if max_lines is not None:
                return min(stop, start + max_lines - preview.total_lines)
            if change and change_truncated:
                return min(stop, start + max_changes - displayed_changes)
            return stop

        for group in groups:
            _check_cancelled(cancelled)
            first, last = group[0], group[-1]
            append('hunk', f'@@ -{_span(first[1], last[2])} +{_span(first[3], last[4])} @@')
            for tag, i1, i2, j1, j2 in group:
                _check_cancelled(cancelled)
                if tag == 'equal':
                    for i in range(i1, display_stop(i1, i2)):
                        j = j1 + i - i1
                        append('context', old[i], i + 1, j + 1)
                if tag in ('delete', 'replace'):
                    for i in range(i1, display_stop(i1, i2, change=True)):
                        append('delete', old[i], i + 1)
                if tag in ('insert', 'replace'):
                    for j in range(j1, display_stop(j1, j2, change=True)):
                        append('add', new[j], new_line=j + 1)
        common = {
            'path': path, 'old_exists': old_exists, 'new_exists': new_exists,
            'identical': old_exists and new_exists and old == new,
            'additions': additions, 'deletions': deletions,
        }
        metadata = common | preview.finish()
        if max_lines is not None:
            metadata.update(display_limit=max_lines, truncated=full_total_lines > preview.total_lines,
                            full_total_lines=full_total_lines)
        if max_changes is not None:
            metadata.update(change_limit=max_changes, displayed_changes=displayed_changes,
                            truncated=change_truncated, full_total_lines=full_total_lines)
        if full is not None:
            full_metadata = common | full.finish()
            _check_cancelled(cancelled)
            _write_json(directory / 'full' / _METADATA, full_metadata)
            metadata.update(full_total_lines=full.total_lines, full_pages=full.pages)
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
