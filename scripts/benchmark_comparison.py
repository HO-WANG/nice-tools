"""Run from the repository root: .venv/bin/python scripts/benchmark_comparison.py.

Synthetic cloud benchmark only: no browser upload, native picker, or worker
startup is measured. Both paths include disk source reads; the streaming path
also creates normalized caches. Temporary test files and caches are removed.
"""
import json
from pathlib import Path
from statistics import median
import subprocess
import sys
import tempfile
import time
import types

repository = Path.cwd()
sys.path.insert(0, str(repository))
from nice_tools.comparison import CompareOptions
from nice_tools.local_compare_engine import compare_files

BASELINE_REF = "ee4f21aff93e395bc27f9550c583aa8243ba10d1"
baseline = types.ModuleType("nice_tools.baseline_comparison")
baseline.__package__ = "nice_tools"
sys.modules[baseline.__name__] = baseline
exec(subprocess.check_output(["git", "show", f"{BASELINE_REF}:nice_tools/comparison.py"], text=True), baseline.__dict__)
line = b"alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau\n"
content = (line * (10 * 1024 * 1024 // len(line) + 1))[:10 * 1024 * 1024]
with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    left, right = folder / "left", folder / "right"
    left.write_bytes(content)
    cases = [
        ("identical_default", content, CompareOptions()),
        ("identical_whitespace_sort", content, CompareOptions(sort_lines=True, ignore_whitespace=True)),
        ("late_difference_default", content[:-1] + b"X", CompareOptions()),
        ("late_difference_whitespace_sort", content[:-1] + b"X", CompareOptions(sort_lines=True, ignore_whitespace=True)),
    ]
    output = []
    for name, rhs, options in cases:
        right.write_bytes(rhs)
        old_times, new_times = [], []
        for attempt in range(3):
            start = time.perf_counter()
            old = baseline.compare_directories({"file": left.read_bytes()}, {"file": right.read_bytes()}, options)
            old_times.append(time.perf_counter() - start)
            with tempfile.TemporaryDirectory(dir=folder) as cache:
                start = time.perf_counter()
                new = compare_files(left, right, options, Path(cache))
                new_times.append(time.perf_counter() - start)
            assert (old["results"][0]["status"] == "same") == new["identical"]
        output.append({
            "case": name,
            "bytes_each": len(content),
            "baseline_seconds_median_3": round(median(old_times), 4),
            "streaming_seconds_median_3": round(median(new_times), 4),
        })
    print(json.dumps(output, indent=2))
