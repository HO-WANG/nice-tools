"""Warm-process line matching only; excludes decoding, EXE/UI and page I/O.

Run: .venv/bin/python scripts/benchmark_diff.py
Each case runs in a fresh child; RSS includes generated input and imports.
"""
from pathlib import Path
import subprocess
import sys


PROGRAM = '''
import json, resource, sys, time
from nice_tools.diff import changed_groups
case=sys.argv[1]
if case=='sparse-repeated':
 old=['common record|123|한글']*100_000
 new=old.copy()
 for i in range(129):new[i*99_999//128]=f'changed {i}'
elif case=='dense-shifted':
 old=[];new=[]
 for i in range(10_000):
  old.extend([f'deleted {i}',f'shared {i}'])
  new.extend([f'shared {i}',f'inserted {i}'])
elif case=='dense-anchored-shifts':
 old=[];new=[]
 for i in range(5_000):
  old.extend([f'anchor {i}',f'deleted {i}',f'shared {i}',f'after {i}'])
  new.extend([f'anchor {i}',f'shared {i}',f'inserted {i}',f'after {i}'])
else:
 old=[f'original {i}' for i in range(20_000)]
 new=[f'replacement {i}' for i in range(20_000)]
start=time.perf_counter()
groups=changed_groups(old,new)
elapsed=time.perf_counter()-start
deleted=sum(i2-i1 for g in groups for tag,i1,i2,j1,j2 in g if tag in ('delete','replace'))
added=sum(j2-j1 for g in groups for tag,i1,i2,j1,j2 in g if tag in ('insert','replace'))
expected={'sparse-repeated':129,'dense-shifted':10000,'dense-anchored-shifts':5000,'all-different':20000}[case]
assert (added,deleted)==(expected,expected),(case,added,deleted)
rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024*1024 if sys.platform=='darwin' else 1024)
print(json.dumps({'case':case,'lines_per_side':len(old),'seconds':round(elapsed,4),
 'peak_RSS_MiB':round(rss,2),'added':added,'deleted':deleted}))
'''


if __name__ == '__main__':
    if sys.platform == 'win32':
        raise SystemExit('This developer benchmark uses POSIX resource RSS.')
    root=Path(__file__).resolve().parents[1]
    for case in ('sparse-repeated','dense-shifted','dense-anchored-shifts','all-different'):
        subprocess.run([sys.executable,'-c',PROGRAM,case],cwd=root,check=True)
