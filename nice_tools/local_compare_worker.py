"""Owned local-directory worker; source contents never cross HTTP or JSON IPC."""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

from .comparison import CompareOptions
from .limits import MAX_FILES
from . import local_compare_engine as engine
from . import local_compare_diff as diff_cache


CHANGED = '파일 또는 폴더가 변경되었습니다. 폴더를 다시 선택하고 비교하세요.'
SAFE_ERRORS = {'changed': CHANGED,
               'busy': '변경 내용을 계산하고 있습니다. 잠시 후 다시 시도하세요.',
               'file_limit': '파일은 두 폴더 합계 1,000개까지 비교할 수 있습니다.'}


class ComparisonError(ValueError):
    def __init__(self, code):
        if not isinstance(code,str) or code not in SAFE_ERRORS:
            raise ValueError('올바르지 않은 비교 오류 코드입니다.')
        self.code=code
        super().__init__(SAFE_ERRORS[code])


def relative_path(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or re.match(r'^[A-Za-z]:', value)
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('폴더 안의 올바른 상대 경로를 선택하세요.')
    return value


def options_data(raw):
    if not isinstance(raw, dict) or set(raw) - {'sort_lines', 'ignore_whitespace', 'ignore_start', 'ignore_end'}:
        raise ValueError('파일 비교 옵션을 확인하세요.')
    return CompareOptions(**raw)


def fingerprint(path, *, directory=False):
    info = Path(path).lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('링크 또는 일반 파일이 아닌 항목은 비교할 수 없습니다.')
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def folder_descriptor(path):
    path = Path(path).resolve(strict=True)
    stamp = fingerprint(path, directory=True)
    return {'path': str(path), 'identity': stamp[:2]}


def _root(descriptor):
    if not isinstance(descriptor, dict) or set(descriptor) != {'path', 'identity'}:
        raise ValueError('선택한 폴더 정보를 확인하세요.')
    path = Path(descriptor['path'])
    if not path.is_absolute() or fingerprint(path, directory=True)[:2] != descriptor['identity']:
        raise ComparisonError('changed')
    return path


@contextlib.contextmanager
def _directory_guard(path):
    # A read/list handle without delete sharing prevents Windows directory
    # renames while source identities and content are examined.
    if sys.platform != 'win32':
        yield
        return
    library = ctypes.WinDLL('kernel32', use_last_error=True)
    library.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    library.CreateFileW.restype = ctypes.c_void_p
    library.CloseHandle.argtypes = [ctypes.c_void_p]
    library.CloseHandle.restype = ctypes.c_int32
    handle = library.CreateFileW(str(path), 0x1, 0x3, None, 3, 0x02000000 | 0x00200000, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ValueError('폴더를 안전하게 읽을 수 없습니다. 사용 중인 프로그램과 권한을 확인하세요.')
    try:
        yield
    finally:
        library.CloseHandle(handle)


def scan_folder(descriptor):
    root = _root(descriptor)
    entries = {}
    identities = {root: descriptor['identity']}
    pending = [root]
    def check_chain(folder):
        while True:
            if fingerprint(folder,directory=True)[:2] != identities[folder]:
                raise ComparisonError('changed')
            if folder==root:
                break
            folder=folder.parent
    # Keep Windows ancestor handles until enumeration completes; on POSIX use
    # no-follow directory descriptors so scandir cannot follow a swapped link.
    with contextlib.ExitStack() as guards:
        while pending:
            folder = pending.pop()
            guards.enter_context(_directory_guard(folder))
            check_chain(folder)
            descriptor_fd = None
            try:
                if os.name != 'nt':
                    descriptor_fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                    info=os.fstat(descriptor_fd)
                    if [info.st_dev,info.st_ino] != identities[folder]:
                        raise ComparisonError('changed')
                with os.scandir(descriptor_fd if descriptor_fd is not None else folder) as items:
                    for item in items:
                        path = folder / item.name
                        relative = relative_path(path.relative_to(root).as_posix())
                        info = item.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                            entries[relative] = {'error': '링크는 따라가지 않습니다. 일반 파일을 선택하세요.'}
                        elif stat.S_ISDIR(info.st_mode):
                            # Windows DirEntry.stat caches can omit identity
                            # fields; lstat supplies the actual directory ID.
                            identities[path]=fingerprint(path,directory=True)[:2]
                            pending.append(path)
                        elif stat.S_ISREG(info.st_mode):
                            entries[relative] = {'fingerprint': fingerprint(path)}
                        else:
                            entries[relative] = {'error': '일반 파일이 아닌 항목은 비교할 수 없습니다.'}
                        if len(entries) > MAX_FILES:
                            raise ComparisonError('file_limit')
                check_chain(folder)
            finally:
                if descriptor_fd is not None:
                    os.close(descriptor_fd)
    _root(descriptor)
    return entries


def _source(descriptor, relative, expected):
    root = _root(descriptor)
    path = root
    parts = PurePosixPath(relative_path(relative)).parts
    for part in parts[:-1]:
        path /= part
        fingerprint(path, directory=True)
    path /= parts[-1]
    if fingerprint(path) != expected:
        raise ComparisonError('changed')
    return path


def _absent_source(descriptor, relative):
    root = _root(descriptor)
    path = root
    for part in PurePosixPath(relative_path(relative)).parts[:-1]:
        path /= part
        try:
            info=path.lstat()
        except (FileNotFoundError,NotADirectoryError):
            return
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ComparisonError('changed')
        if not stat.S_ISDIR(info.st_mode):
            return
    if os.path.lexists(root.joinpath(*PurePosixPath(relative).parts)):
        raise ComparisonError('changed')


@contextlib.contextmanager
def _sources(descriptors, relative, entry):
    from .ase_worker import _input_guard
    paths = []
    with contextlib.ExitStack() as stack:
        for side in ('left', 'right'):
            expected = entry.get(side)
            if expected is None:
                _absent_source(descriptors[side],relative)
                paths.append(None)
                continue
            root = _root(descriptors[side])
            path = _source(descriptors[side], relative, expected)
            parent = path.parent
            while parent != root:
                stack.enter_context(_directory_guard(parent))
                parent = parent.parent
            stack.enter_context(_input_guard(path))
            _source(descriptors[side], relative, expected)
            paths.append(path)
        yield paths
        for side in ('left', 'right'):
            if entry.get(side) is not None:
                _source(descriptors[side], relative, entry[side])
            else:
                _absent_source(descriptors[side],relative)


def _write_manifest(directory, manifest):
    temporary = directory / 'manifest.tmp'
    with temporary.open('x', encoding='utf-8', newline='\n') as output:
        json.dump(manifest, output, ensure_ascii=True)
    temporary.replace(directory / 'manifest.json')


def _compare(payload, emit):
    descriptors = {side: payload[side] for side in ('left', 'right')}
    options = options_data(payload.get('options', {}))
    cache = Path(payload['cache_dir'])
    cache.mkdir(parents=True, exist_ok=True)
    if (cache / 'manifest.json').exists():
        raise ValueError('이미 완료된 비교 결과를 덮어쓸 수 없습니다.')
    with contextlib.ExitStack() as stack:
        for descriptor in descriptors.values():
            stack.enter_context(_directory_guard(_root(descriptor)))
        emit({'phase': 'scanning', 'completed': 0, 'total': 0})
        inventories = {side: scan_folder(descriptor) for side, descriptor in descriptors.items()}
        if sum(map(len, inventories.values())) > MAX_FILES:
            raise ComparisonError('file_limit')
        paths = sorted(inventories['left'].keys() | inventories['right'].keys())
        summary = dict.fromkeys(('total', 'same', 'different', 'left_only', 'right_only', 'error'), 0)
        results, entries = [], {}
        for index, relative in enumerate(paths):
            emit({'phase': 'comparing', 'completed': index, 'total': len(paths), 'path': relative})
            item = {'path': relative}
            entry = {side: inventories[side].get(relative, {}).get('fingerprint') for side in ('left', 'right')}
            errors = [inventories[side].get(relative, {}).get('error') for side in ('left', 'right')]
            if any(errors):
                item.update(status='error', message=' / '.join(error for error in errors if error))
            elif relative not in inventories['right']:
                item['status'] = 'left_only'
            elif relative not in inventories['left']:
                item['status'] = 'right_only'
            else:
                try:
                    with _sources(descriptors, relative, entry) as (left, right):
                        entry['pair'] = engine.compare_files(left, right, options, cache / 'pairs' / str(index))
                    item['status'] = 'same' if entry['pair']['identical'] else 'different'
                except (ValueError, OSError):
                    # No native exception, data value or absolute source path.
                    item.update(status='error', message='텍스트를 읽을 수 없거나 파일이 변경되었습니다. 인코딩·권한을 확인하고 다시 비교하세요.')
            entries[relative] = {**entry, 'status': item['status']}
            summary[item['status']] += 1
            summary['total'] += 1
            results.append(item)
        # Folder moves/replacements invalidate the complete published result.
        for descriptor in descriptors.values():
            _root(descriptor)
        _write_manifest(cache, {'folders': descriptors, 'options': payload.get('options', {}), 'entries': entries})
        emit({'phase': 'comparing', 'completed': len(paths), 'total': len(paths)})
        return {'summary': summary, 'results': results}


def _diff(payload):
    relative = relative_path(payload.get('path'))
    mode, page = payload.get('mode', 'compared'), payload.get('page', 0)
    if mode not in ('compared', 'original') or type(page) is not int or page < 0:
        raise ValueError('변경 내용의 보기와 페이지를 확인하세요.')
    cache = Path(payload['cache_dir'])
    with (cache / 'manifest.json').open(encoding='utf-8') as source:
        manifest = json.load(source)
    entry = manifest['entries'].get(relative)
    if entry is None or entry['status'] == 'error':
        raise ValueError('정상적으로 비교된 파일을 선택하세요.')
    key = hashlib.sha256((mode + '\0' + relative).encode('utf-8')).hexdigest()
    directory = cache / 'diffs' / key
    descriptors = manifest['folders']
    with contextlib.ExitStack() as stack:
        for descriptor in descriptors.values():
            stack.enter_context(_directory_guard(_root(descriptor)))
        with _sources(descriptors, relative, entry) as sources:
            if not (directory / 'metadata.json').exists():
                pair = entry.get('pair', {})
                normalized = []
                if pair.get('raw_identical') or (mode=='compared' and pair.get('identical')):
                    # Validation occurred during comparison; source identities
                    # were just rechecked. Empty caches represent equal texts.
                    empty = cache / 'equal.empty'
                    empty.touch(exist_ok=True)
                    normalized = [empty, empty]
                else:
                    for side, source in zip(('left', 'right'), sources):
                        existing = pair.get(side + '_normalized') if mode == 'compared' else None
                        if source is None:
                            normalized.append(None)
                        elif existing:
                            normalized.append(Path(existing))
                        else:
                            folder = cache / 'extra' / key
                            folder.mkdir(parents=True, exist_ok=True)
                            target = folder / (side + '.txt')
                            if not target.exists():
                                options = CompareOptions() if mode == 'original' else options_data(manifest['options'])
                                engine.normalize_file(source, target, options, encoding=pair.get(side + '_encoding'))
                            normalized.append(target)
                diff_cache.build_diff(*normalized, directory, path=relative,
                                      old_exists=sources[0] is not None, new_exists=sources[1] is not None)
            result = diff_cache.read_page(directory, page)
        return result


def execute(payload, emit=None):
    if not isinstance(payload, dict):
        raise ValueError('비교 작업의 입력을 확인하세요.')
    operation = payload.get('operation')
    if operation == 'pick-directory':
        from .ase_picker import pick_directory
        result = pick_directory(payload.get('initial_directory'))
        if result.get('cancelled'):
            return result
        descriptor = folder_descriptor(result['path'])
        with _directory_guard(_root(descriptor)):
            entries = scan_folder(descriptor)
        return {**descriptor, 'cancelled': False, 'name': Path(descriptor['path']).name,
                'file_count': len(entries), 'total_bytes': sum(item.get('fingerprint', [0,0,0])[2] for item in entries.values())}
    if operation == 'compare':
        return _compare(payload, emit or (lambda data: None))
    if operation == 'diff':
        return _diff(payload)
    raise ValueError('지원하지 않는 파일 비교 작업입니다.')


def main(stdin=None, stdout=None):
    stdin = stdin if stdin is not None else getattr(sys.stdin, 'buffer', sys.stdin)
    stdout = stdout if stdout is not None else sys.stdout
    def record(value):
        stdout.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + '\n')
        stdout.flush()
    try:
        payload = json.loads(stdin.read(1024 * 1024 + 1))
        result = execute(payload, lambda data: record({'event': 'progress', **data}))
        record({'event': 'result', 'result': result})
        return 0
    except Exception as error:
        detail = str(error) if isinstance(error, ValueError) else '파일 비교 작업을 완료하지 못했습니다. 파일 상태와 임시 저장 공간을 확인하세요.'
        record({'event': 'error', 'detail': detail,
                **({'code':error.code} if isinstance(error,ComparisonError) else {})})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
