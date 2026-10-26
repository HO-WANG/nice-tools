"""Exact line alignment, including cases too large for native traceback matrices."""

import json
import random
import subprocess
import sys

import pytest

from nice_tools import diff
from nice_tools import _line_alignment


def _lcs_length(old, new):
    # Deliberately independent scalar dynamic-programming oracle.
    row = [0] * (len(new) + 1)
    for left in old:
        previous = 0
        for j, right in enumerate(new, 1):
            saved = row[j]
            row[j] = previous + 1 if left == right else max(row[j], row[j - 1])
            previous = saved
    return row[-1]


def _check_alignment(old, new, codes, expected_equal):
    i = j = equal = 0
    rebuilt = []
    for tag, i1, i2, j1, j2 in codes:
        assert (i1, j1) == (i, j)
        assert i2 >= i1 and j2 >= j1
        assert i2 > i1 or j2 > j1
        if tag == "equal":
            assert old[i1:i2] == new[j1:j2]
            equal += i2 - i1
        else:
            assert tag == ("insert" if i1 == i2 else "delete" if j1 == j2 else "replace")
        rebuilt.extend(new[j1:j2] if tag != "delete" else [])
        i, j = i2, j2
    assert (i, j) == (len(old), len(new))
    assert rebuilt == new
    assert equal == expected_equal


def test_bounded_alignment_matches_independent_minimality_oracle():
    rng = random.Random(94812)
    pairs = [([], []), (["a"], []), ([], ["a"]),
             (["a", "b", "a"], ["b", "a", "b"])]
    for _ in range(120):
        pairs.append(([rng.choice("abcde") for _ in range(rng.randrange(40))],
                      [rng.choice("abcde") for _ in range(rng.randrange(40))]))
    for old, new in pairs:
        codes = diff.bounded_lcs_opcodes(old, new)
        _check_alignment(old, new, codes, _lcs_length(old, new))


@pytest.mark.parametrize("mode", ["sparse", "dense", "uncached-dense"])
def test_fallback_strategies_preserve_exact_lcs_and_reconstruct(monkeypatch, mode):
    monkeypatch.setattr(_line_alignment, "_NATIVE_ALIGNMENT_CELLS", 0)
    if mode != "sparse":
        monkeypatch.setattr(_line_alignment, "_SPARSE_MATCH_BASE", 0)
        monkeypatch.setattr(_line_alignment, "_SPARSE_MATCH_FACTOR", 0)
    if mode == "uncached-dense":
        monkeypatch.setattr(_line_alignment, "_MASK_CACHE_BYTES", 1)
    rng = random.Random(51412)
    for _ in range(120):
        old = [rng.choice("abcde") for _ in range(rng.randrange(80))]
        new = [rng.choice("abcde") for _ in range(rng.randrange(80))]
        _check_alignment(old, new, diff.bounded_lcs_opcodes(old, new),
                         _lcs_length(old, new))


@pytest.mark.parametrize("case", ["repeated", "reversed", "disjoint"])
def test_large_repeated_reordered_and_disjoint_alignments(case):
    if case == "repeated":
        old = ["a"] * 3000 + ["b"] * 3000
        new = ["b"] * 3000 + ["a"] * 3000
        equal = 3000
    elif case == "reversed":
        old = [str(i) for i in range(12000)]
        new = list(reversed(old))
        equal = 1
    else:
        old = [f"old {i}" for i in range(12000)]
        new = [f"new {i}" for i in range(12000)]
        equal = 0
    _check_alignment(old, new, diff.LineMatcher(None, old, new).get_opcodes(), equal)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX address-space safety limit")
def test_large_shifted_records_fit_bounded_address_space():
    program = '''
import json, resource
resource.setrlimit(resource.RLIMIT_AS, (160 * 1024**2, 160 * 1024**2))
from nice_tools.diff import LineMatcher
old = []; new = []
for i in range(20000):
    old.extend([f"anchor {i}", f"deleted {i}", f"shared {i}", f"after {i}"])
    new.extend([f"anchor {i}", f"shared {i}", f"inserted {i}", f"after {i}"])
codes = LineMatcher(None, old, new).get_opcodes()
equal = sum(i2-i1 for tag,i1,i2,j1,j2 in codes if tag == "equal")
assert equal == 60000, equal
print(json.dumps({"equal": equal}))
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["equal"] == 60000
