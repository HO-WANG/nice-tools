"""Fresh-process normalized-input report builds and cached first-page reads.

Run: .venv/bin/python scripts/benchmark_report_cache.py
Build seconds include strict normalized reads, matching, and both paged stores.
Input creation, decoding/normalization, HTTP, Windows EXE and UI are excluded.
Peak RSS includes imports and generated inputs; process seconds also include
interpreter startup and fixture creation. All fixtures use LF on every OS.
"""
from pathlib import Path
import subprocess
import sys
import time


PROGRAM = r'''
import json, resource, sys, tempfile, time
from pathlib import Path
from nice_tools.local_compare_diff import prepare_diff, read_page
case = sys.argv[1]
count = 100_000 if case in ('sparse-repeated', 'one-sided') else 20_000
with tempfile.TemporaryDirectory() as temporary:
 root = Path(temporary)
 old, new = root / 'old', root / 'new'
 if case != 'one-sided':
  with old.open('w', encoding='utf-8', newline='\n') as output:
   for i in range(count):
    output.write(('common record|123|한글' if case == 'sparse-repeated' else f'original {i}') + '\n')
 with new.open('w', encoding='utf-8', newline='\n') as output:
  for i in range(count):
   if case == 'sparse-repeated':
    line = f'changed {i}' if i % 775 == 0 else 'common record|123|한글'
   else:
    line = f'replacement {i}'
   output.write(line + '\n')
 source_bytes = new.stat().st_size + (old.stat().st_size if old.exists() else 0)
 started = time.perf_counter()
 metadata = prepare_diff(old if old.exists() else None, new, root / 'report',
                         path='benchmark.txt', old_exists=old.exists(), new_exists=True)
 elapsed = time.perf_counter() - started
 started = time.perf_counter()
 first = read_page(root / 'report', 0)
 first_seconds = time.perf_counter() - started
 assert metadata['state'] == 'ready'
 assert metadata['displayed_changes'] == min(1000, metadata['additions'] + metadata['deletions'])
 full = read_page(root / 'report' / 'full', metadata['full_pages'] - 1)
 assert full['total_lines'] == metadata['full_total_lines']
 cache_bytes = sum(p.stat().st_size for p in (root / 'report').rglob('*') if p.is_file())
 rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024*1024 if sys.platform == 'darwin' else 1024)
 print(json.dumps({'case': case, 'lines_per_present_side': count, 'normalized_bytes': source_bytes,
                   'build_seconds': round(elapsed, 4), 'first_cached_page_seconds': round(first_seconds, 6),
                   'peak_RSS_MiB': round(rss, 2), 'cache_bytes': cache_bytes,
                   'changes': metadata['additions'] + metadata['deletions'],
                   'preview_rows': metadata['total_lines'], 'full_rows': metadata['full_total_lines']}), flush=True)
'''


if __name__ == '__main__':
    if sys.platform == 'win32':
        raise SystemExit('This developer benchmark uses POSIX resource RSS.')
    root = Path(__file__).resolve().parents[1]
    for case in ('sparse-repeated', 'all-different', 'one-sided'):
        started = time.perf_counter()
        subprocess.run([sys.executable, '-c', PROGRAM, case], cwd=root, check=True)
        print(f'{case} process seconds: {time.perf_counter() - started:.4f}', flush=True)
