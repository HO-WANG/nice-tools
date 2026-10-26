"""Owned read-only text inspection worker and validated cached-page access."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sys

from .local_compare_worker import ComparisonError, fingerprint, folder_descriptor, _directory_guard, _root
from . import text_inspection_engine as engine


def file_descriptor(path):
    path=Path(path).resolve(strict=True)
    return {'path':str(path),'name':path.name,'folder':folder_descriptor(path.parent),
            'fingerprint':fingerprint(path)}


def _check_source(source):
    try:
        path=Path(source['path'])
        root=_root(source['folder'])
        if path.parent!=root or path.name!=source['name'] or fingerprint(path)!=source['fingerprint']:
            raise ComparisonError('changed')
        return path
    except (OSError,KeyError,TypeError,ValueError):
        raise ComparisonError('changed') from None


@contextlib.contextmanager
def guarded_source(source):
    from .ase_worker import _input_guard
    path=_check_source(source)
    with _directory_guard(path.parent),_input_guard(path):
        _check_source(source)
        yield path
        _check_source(source)


def execute(payload,emit=None):
    if not isinstance(payload,dict):
        raise ValueError('텍스트 검사 입력을 확인하세요.')
    operation=payload.get('operation')
    if operation=='pick-file':
        from .ase_picker import pick_file
        picked=pick_file('in',text_only=True)
        return picked if picked.get('cancelled') else {'cancelled':False,'source':file_descriptor(picked['path'])}
    if operation=='scan':
        source=payload['source']
        cache=Path(payload['cache_dir'])
        with guarded_source(source) as path:
            summary=engine.scan_file(path,cache,payload.get('options',{}),emit=emit)
        try:
            with (cache/'source.json').open('x',encoding='utf-8',newline='\n') as output:
                json.dump(source,output,ensure_ascii=True)
        except OSError:
            raise ComparisonError('inspection_cache') from None
        return {'summary':summary}
    if operation=='page':
        cache=Path(payload['cache_dir'])
        try:
            with (cache/'source.json').open(encoding='utf-8') as stream:
                source=json.load(stream)
        except (OSError,ValueError):
            raise ComparisonError('inspection_cache') from None
        with guarded_source(source):
            return engine.read_page(cache,payload.get('page',0))
    raise ValueError('지원하지 않는 텍스트 검사 작업입니다.')


def main(stdin=None,stdout=None):
    stdin=stdin if stdin is not None else getattr(sys.stdin,'buffer',sys.stdin)
    stdout=stdout if stdout is not None else sys.stdout
    def record(value):
        stdout.write(json.dumps(value,ensure_ascii=True,allow_nan=False)+'\n');stdout.flush()
    try:
        raw=stdin.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError
        payload=json.loads(raw)
        result=execute(payload,lambda data:record({'event':'progress',**data}))
        record({'event':'result','result':result})
        return 0
    except ComparisonError as error:
        record({'event':'error','code':error.code})
    except engine.InspectionError as error:
        record({'event':'error','code':'encoding' if error.code=='encoding' else 'inspection_cache'})
    except Exception:
        record({'event':'error'})
    return 1


if __name__=='__main__':
    raise SystemExit(main())
