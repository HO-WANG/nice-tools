"""Synthetic, warm-cache engine timing; excludes UI and EXE startup.

Run from the repository: .venv/bin/python scripts/benchmark_text_inspection.py
Each file has 8/128MiB of allowed text and one forbidden final character.
Dense findings require additional formatting/disk work. RSS is Linux/macOS
whole-child-process peak memory; Windows reports null when resource is absent.
"""
from pathlib import Path
import subprocess
import sys
import tempfile

PROGRAM = '''
from pathlib import Path
import json, sys, time
from nice_tools.text_inspection_engine import scan_file, read_page
path=Path(sys.argv[1]); cache=Path(sys.argv[2]); encoding=sys.argv[3]
started=time.perf_counter()
result=scan_file(path,cache,{'strict_mode':True,'encoding':encoding})
elapsed=time.perf_counter()-started
row=read_page(cache,0)['results'][0]
assert result['findings']==1 and row['column']==result['characters']
try:
 import resource
 divisor=1024*1024 if sys.platform=='darwin' else 1024
 rss=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/divisor,2)
except ImportError:
 rss=None
print(json.dumps({'MiB':round(result['file_size']/1024/1024,2),'encoding':encoding,'seconds':round(elapsed,3),'peak_RSS_MiB':rss,'findings':result['findings'],'last_column':row['column']}))
'''


def main():
    repository=Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='nice-tools-inspection-benchmark-') as name:
        root=Path(name)
        for kind,block in [('ASCII',b'Abc|123!?'*100_000),
                           ('Hangul',('한글한ㄱ|123'*50_000).encode())]:
            for target in (8,128):
                source=root/f'{kind}-{target}.txt'
                with source.open('wb') as output:
                    remaining=target*1024*1024
                    while remaining>=len(block):
                        output.write(block);remaining-=len(block)
                    output.write(b'A'*remaining);output.write('中'.encode())
                for encoding in ('utf-8','auto'):
                    cache=root/f'{kind}-{target}-{encoding}-cache'
                    result=subprocess.check_output([sys.executable,'-c',PROGRAM,str(source),str(cache),encoding],
                                                   cwd=repository,text=True)
                    print(kind,result.strip(),flush=True)


if __name__=='__main__':
    main()
