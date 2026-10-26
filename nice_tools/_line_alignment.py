"""Exact LCS traceback without an input-sized quadratic alignment matrix.

Small rectangles use RapidFuzz. Sparse matches use Hunt--Szymanski; dense
rectangles use Hirschberg with bit-parallel LCS rows. The latter retains two
bit rows, linear position lists and a byte-bounded cache of symbol masks.
"""

from array import array
from bisect import bisect_left
from collections import OrderedDict

from rapidfuzz.distance import Indel


_NATIVE_ALIGNMENT_CELLS = 4_000_000
_SPARSE_MATCH_BASE = 200_000
_SPARSE_MATCH_FACTOR = 4
_MASK_CACHE_BYTES = 8 * 1024 * 1024


def _sparse_pairs(a, start, stop, positions):
    """Recover increasing matches with at most a linear-budget match count."""
    tails = []
    tail_nodes = []
    source = array("q")
    target = array("q")
    previous = array("q")
    for i in range(start, stop):
        for j in reversed(positions.get(a[i], ())):
            length = bisect_left(tails, j)
            if length < len(tails) and tails[length] == j:
                continue
            node = len(source)
            source.append(i)
            target.append(j)
            previous.append(tail_nodes[length - 1] if length else -1)
            if length == len(tails):
                tails.append(j)
                tail_nodes.append(node)
            else:
                tails[length] = j
                tail_nodes[length] = node
    path = []
    node = tail_nodes[-1] if tail_nodes else -1
    while node >= 0:
        path.append((source[node], target[node]))
        node = previous[node]
    yield from reversed(path)


def _bitset_row(a, start, stop, b, left, right, reverse=False):
    """Encode the increments of one LCS DP row as bits, with bounded masks."""
    width = right - left
    positions = {}
    order = range(right - 1, left - 1, -1) if reverse else range(left, right)
    for offset, j in enumerate(order):
        positions.setdefault(b[j], []).append(offset)
    masks = OrderedDict()
    cache_bytes = 0
    state = 0
    order = range(stop - 1, start - 1, -1) if reverse else range(start, stop)
    for i in order:
        token = a[i]
        if token not in positions:
            continue
        mask = masks.get(token)
        if mask is None:
            packed = bytearray((width + 7) // 8)
            for offset in positions[token]:
                packed[offset >> 3] |= 1 << (offset & 7)
            mask = int.from_bytes(packed, "little")
            size = mask.__sizeof__()
            if size <= _MASK_CACHE_BYTES:
                while masks and cache_bytes + size > _MASK_CACHE_BYTES:
                    _, evicted = masks.popitem(last=False)
                    cache_bytes -= evicted.__sizeof__()
                masks[token] = mask
                cache_bytes += size
        else:
            masks.move_to_end(token)
        union = state | mask
        state = union & ~(union - ((state << 1) | 1))
    return state


def _split_column(forward, backward, width):
    """Find the maximizing Hirschberg cut without materializing integer rows."""
    prefix = 0
    suffix = backward.bit_count()
    best = suffix
    split = 0
    front = forward.to_bytes((width + 7) // 8, "little")
    back = backward.to_bytes((width + 7) // 8, "little")
    for offset in range(width):
        prefix += (front[offset >> 3] >> (offset & 7)) & 1
        reverse_offset = width - offset - 1
        suffix -= (back[reverse_offset >> 3] >> (reverse_offset & 7)) & 1
        score = prefix + suffix
        if score > best:
            best = score
            split = offset + 1
    return split


def _lcs_pairs(a, start, stop, b, left, right):
    while start < stop and left < right and a[start] == b[left]:
        yield start, left
        start += 1
        left += 1
    suffix = 0
    while start < stop and left < right and a[stop - 1] == b[right - 1]:
        suffix += 1
        stop -= 1
        right -= 1
    n, m = stop - start, right - left
    if n and m:
        if n * m <= _NATIVE_ALIGNMENT_CELLS:
            for op in Indel.opcodes(a[start:stop], b[left:right]):
                if op.tag == "equal":
                    yield from zip(range(start + op.src_start, start + op.src_end),
                                   range(left + op.dest_start, left + op.dest_end))
        elif n < m:
            for j, i in _lcs_pairs(b, left, right, a, start, stop):
                yield i, j
        else:
            positions = {}
            for j in range(left, right):
                positions.setdefault(b[j], []).append(j)
            matches = sum(len(positions.get(a[i], ())) for i in range(start, stop))
            if matches <= max(_SPARSE_MATCH_BASE, _SPARSE_MATCH_FACTOR * (n + m)):
                yield from _sparse_pairs(a, start, stop, positions)
            elif m == 1:
                for i in range(start, stop):
                    if a[i] == b[left]:
                        yield i, left
                        break
            else:
                del positions
                middle = start + n // 2
                forward = _bitset_row(a, start, middle, b, left, right)
                backward = _bitset_row(a, middle, stop, b, left, right, reverse=True)
                split = left + _split_column(forward, backward, m)
                del forward, backward
                yield from _lcs_pairs(a, start, middle, b, left, split)
                yield from _lcs_pairs(a, middle, stop, b, split, right)
    for offset in range(suffix):
        yield stop + offset, right + offset


def bounded_lcs_opcodes(old: list[str], new: list[str]) -> list:
    """Return an exact minimum insertion/deletion script in bounded workspace.

    Removing globally unmatchable lines cannot change the LCS. Keep their
    original positions so the resulting script still accounts for every line.
    This often makes large shifted-record comparisons a simple linear scan.
    """
    common = set(old)
    common.intersection_update(new)
    old_indices = array("q", (i for i, line in enumerate(old) if line in common))
    new_indices = array("q", (j for j, line in enumerate(new) if line in common))
    del common
    a = [old[i] for i in old_indices]
    b = [new[j] for j in new_indices]
    codes = []
    i = j = 0
    for ai, bj in _lcs_pairs(a, 0, len(a), b, 0, len(b)):
        next_i, next_j = old_indices[ai], new_indices[bj]
        if next_i > i or next_j > j:
            tag = "insert" if next_i == i else "delete" if next_j == j else "replace"
            codes.append((tag, i, next_i, j, next_j))
        if codes and codes[-1][0] == "equal" and codes[-1][2] == next_i and codes[-1][4] == next_j:
            _, begin_i, _, begin_j, _ = codes[-1]
            codes[-1] = ("equal", begin_i, next_i + 1, begin_j, next_j + 1)
        else:
            codes.append(("equal", next_i, next_i + 1, next_j, next_j + 1))
        i, j = next_i + 1, next_j + 1
    if i < len(old) or j < len(new):
        tag = "insert" if i == len(old) else "delete" if j == len(new) else "replace"
        codes.append((tag, i, len(old), j, len(new)))
    return codes
