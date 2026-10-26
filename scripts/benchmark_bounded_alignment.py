"""Safe, isolated LCS-engine benchmarks (no decoding, report I/O, EXE or UI).

Run with the project virtualenv. Each child has a 320 MiB address-space limit
and a 30-second timeout; large native traceback matrices fail safely. Reported
RSS includes imports and generated input. Matching uses complete line lists.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys


PROGRAM = '''
import json, random, resource, sys, time
resource.setrlimit(resource.RLIMIT_AS, (320 * 1024**2, 320 * 1024**2))
from rapidfuzz.distance import Indel, Levenshtein
from nice_tools._line_alignment import bounded_lcs_opcodes
case, algorithm = sys.argv[1:]
if case == 'sparse-repeated':
    old = ['common record|123|한글'] * 200000
    new = old.copy()
    for i in range(129):
        new[i * (len(new)-1) // 128] = f'changed {i}'
    expected = len(old) - 129
elif case == 'anchored-shifts':
    old = []; new = []
    for i in range(20000):
        old.extend([f'anchor {i}', f'deleted {i}', f'shared {i}', f'after {i}'])
        new.extend([f'anchor {i}', f'shared {i}', f'inserted {i}', f'after {i}'])
    expected = 60000
elif case == 'dense-repeated':
    old = ['a'] * 20000 + ['b'] * 20000
    new = ['b'] * 20000 + ['a'] * 20000
    expected = 20000
else:
    rng = random.Random(1)
    old = [str(i) for i in range(10000)] * 10
    new = old.copy()
    rng.shuffle(old); rng.shuffle(new)
    expected = len(old) - Indel.distance(old, new) // 2
started = time.perf_counter()
try:
    if algorithm == 'bounded':
        codes = bounded_lcs_opcodes(old, new)
        equal = sum(i2-i1 for tag,i1,i2,j1,j2 in codes if tag == 'equal')
    else:
        engine = Indel if algorithm == 'native-indel' else Levenshtein
        codes = engine.opcodes(old, new)
        equal = sum(op.src_end-op.src_start for op in codes if op.tag == 'equal')
    if expected is not None and algorithm != 'native-levenshtein':
        assert equal == expected, (equal, expected)
    status = 'ok'
except MemoryError:
    status = 'MemoryError'
    equal = None
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps(dict(case=case, algorithm=algorithm, status=status,
    lines_per_side=len(old), seconds=round(time.perf_counter()-started, 4),
    peak_RSS_MiB=round(rss / (1024**2 if sys.platform == 'darwin' else 1024), 2),
    equal_lines=equal)))
'''


def main():
    if sys.platform == "win32":
        raise SystemExit("This developer benchmark requires POSIX resource limits.")
    cases = ("sparse-repeated", "anchored-shifts", "dense-repeated", "mixed-shuffled")
    algorithms = ("bounded", "native-indel", "native-levenshtein")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=cases, action="append")
    parser.add_argument("--algorithm", choices=algorithms, action="append")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for case in args.case or cases:
        for algorithm in args.algorithm or algorithms:
            try:
                result = subprocess.run([sys.executable, "-c", PROGRAM, case, algorithm],
                                        cwd=root, text=True, capture_output=True, timeout=30)
            except subprocess.TimeoutExpired:
                print(json.dumps(dict(case=case, algorithm=algorithm, status="timeout-30s")))
                continue
            if result.returncode:
                raise RuntimeError(result.stderr)
            print(result.stdout.strip(), flush=True)


if __name__ == "__main__":
    main()
