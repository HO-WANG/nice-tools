"""Synthetic 10/200 MiB full initial comparison in a fresh source worker.

Includes startup, guarded local reads, normalization, exact matching, both
complete/preview report writes and metadata publication. Excludes fixture
creation, browser/HTTP, native picker, Windows EXE extraction and antivirus.
No original/user files are read. Temporary inputs/reports are removed.
Run: .venv/bin/python scripts/benchmark_initial_comparison.py
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

PROGRAM = r'''
import json, resource, sys, time
from pathlib import Path
from nice_tools.local_compare_worker import execute
phases=[]
started=time.perf_counter()
def progress(event):
 phase=event.get('phase')
 if not phases or (phases[-1]['phase'],phases[-1]['file'])!=(phase,event.get('path')):
  phases.append({'phase':phase,'file':event.get('path'),'at_seconds':round(time.perf_counter()-started,3)})
result=execute(json.loads(sys.stdin.read()),progress)
rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024*1024 if sys.platform=='darwin' else 1024)
print(json.dumps({'summary':result['summary'],'phases':phases,'worker_seconds':round(time.perf_counter()-started,3),'peak_RSS_MiB':round(rss,2)}),flush=True)
'''


def main():
    if sys.platform == 'win32':
        raise SystemExit('Developer benchmark uses POSIX resource RSS; not EXE timings.')
    with tempfile.TemporaryDirectory(prefix='nice-tools-initial-benchmark-') as temporary:
        root = Path(temporary)
        left, right = root / 'left', root / 'right'
        left.mkdir()
        right.mkdir()
        total_bytes = 0
        counts = {}
        # Unique 100-byte ASCII records. Every tenth row is replaced, while
        # the common order stays intact; no sorting or whitespace options.
        for mib in (10, 200):
            count = mib * 1024 * 1024 // 100
            name = f'{mib:03d}MiB.txt'
            counts[name] = count
            for folder, marker in ((left, 'old'), (right, 'new')):
                with (folder / name).open('wb') as output:
                    for start in range(0, count, 10_000):
                        lines = []
                        for index in range(start, min(count, start + 10_000)):
                            label = marker if index % 10 == 0 else 'same'
                            record = f'{index:012d}|{label}|'.encode()
                            lines.append(record + b'x' * (99 - len(record)) + b'\n')
                        output.writelines(lines)
                total_bytes += (folder / name).stat().st_size
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from nice_tools.local_compare_worker import folder_descriptor, _report_directory
        from nice_tools.local_compare_diff import read_page
        cache = root / 'cache'
        payload = {'operation':'compare','left':folder_descriptor(left),'right':folder_descriptor(right),
                   'options':{},'cache_dir':str(cache)}
        started = time.perf_counter()
        child = subprocess.run([sys.executable, '-c', PROGRAM], input=json.dumps(payload),
                               text=True, capture_output=True, check=True, timeout=240,
                               cwd=Path(__file__).resolve().parents[1])
        result = json.loads(child.stdout)
        assert result['summary'] == {'total':2,'same':0,'different':2,'left_only':0,'right_only':0,'error':0}, result
        lookup_started = time.perf_counter()
        for name, count in counts.items():
            directory = _report_directory(cache, name, 'compared')
            preview = read_page(directory, 0)
            full = read_page(directory / 'full', 0)
            assert preview['displayed_changes'] == 1000 and preview['truncated']
            assert preview['additions'] == preview['deletions'] == (count + 9) // 10
            last = read_page(directory / 'full', full['pages'] - 1)
            assert last['total_lines'] == preview['full_total_lines']
        lookup_seconds = time.perf_counter() - lookup_started
        result.update(source_bytes_both_folders=total_bytes,
                      process_seconds=round(time.perf_counter()-started,3),
                      six_cached_page_reads_seconds=round(lookup_seconds,6),
                      cache_bytes=sum(path.stat().st_size for path in cache.rglob('*') if path.is_file()))
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
