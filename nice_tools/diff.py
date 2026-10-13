"""선택한 파일의 비교 결과를 줄 번호가 있는 unified diff로 구성."""

from difflib import SequenceMatcher

from rapidfuzz.distance import Levenshtein

from .comparison import CompareOptions, normalize_lines


def myers_opcodes(old: list[str], new: list[str]) -> list | None:
    """Find a short edit script without quadratic repeated-line matching.

    Bound the search frontier, not the input: large edit distances use the
    native edit matcher, which preserves repeated lines at large edit distances.
    """
    n, m = len(old), len(new)
    if not old or not new or set(old).isdisjoint(new):
        tag = "insert" if not old else "delete" if not new else "replace"
        return [(tag, 0, n, 0, m)] if n or m else []
    frontier = {1: 0}
    trace = []
    for distance in range(min(n + m, 256) + 1):
        trace.append(frontier.copy())
        for diagonal in range(-distance, distance + 1, 2):
            down = diagonal == -distance or (
                diagonal != distance and frontier[diagonal - 1] < frontier[diagonal + 1]
            )
            x = frontier[diagonal + 1] if down else frontier[diagonal - 1] + 1
            y = x - diagonal
            while x < n and y < m and old[x] == new[y]:
                x += 1
                y += 1
            frontier[diagonal] = x
            if x >= n and y >= m:
                break
        else:
            continue
        break
    else:
        return None

    # Backtrack runs rather than individual lines, keeping long equal spans small.
    chunks = []
    x, y = n, m
    for distance in range(len(trace) - 1, -1, -1):
        frontier = trace[distance]
        diagonal = x - y
        down = diagonal == -distance or (
            diagonal != distance and frontier[diagonal - 1] < frontier[diagonal + 1]
        )
        previous = diagonal + 1 if down else diagonal - 1
        previous_x = frontier[previous]
        previous_y = previous_x - previous
        equal = min(x - previous_x, y - previous_y)
        if equal > 0:
            chunks.append(("equal", equal))
            x -= equal
            y -= equal
        if distance:
            chunks.append(("insert" if down else "delete", 1))
            if down:
                y -= 1
            else:
                x -= 1

    codes = []
    i = j = 0
    change_start = None

    def flush_change():
        nonlocal change_start
        if change_start is not None:
            i1, j1 = change_start
            tag = "insert" if i == i1 else "delete" if j == j1 else "replace"
            codes.append((tag, i1, i, j1, j))
            change_start = None

    for tag, count in reversed(chunks):
        if tag == "equal":
            flush_change()
            codes.append((tag, i, i + count, j, j + count))
            i += count
            j += count
        else:
            if change_start is None:
                change_start = (i, j)
            if tag == "delete":
                i += count
            else:
                j += count
    flush_change()
    return codes


class LineMatcher(SequenceMatcher):
    def get_opcodes(self):
        codes = myers_opcodes(self.a, self.b)
        if codes is not None:
            return codes
        return [(op.tag, op.src_start, op.src_end, op.dest_start, op.dest_end)
                for op in Levenshtein.opcodes(self.a, self.b)]


def changed_groups(old: list[str], new: list[str]) -> list:
    # Trim common ends first: even large runs of repeated lines then take linear
    # time for local edits. The Myers search also handles distant repeated-line edits.
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    old_end, new_end = len(old), len(new)
    while old_end > prefix and new_end > prefix and old[old_end - 1] == new[new_end - 1]:
        old_end -= 1
        new_end -= 1
    matcher = LineMatcher(None, old[prefix:old_end], new[prefix:new_end])
    groups = [
        [(tag, i1 + prefix, i2 + prefix, j1 + prefix, j2 + prefix)
         for tag, i1, i2, j1, j2 in group]
        for group in matcher.get_grouped_opcodes(3)
    ]
    if groups:
        if prefix:
            groups[0].insert(0, ("equal", max(0, prefix - 3), prefix, max(0, prefix - 3), prefix))
        suffix = min(3, len(old) - old_end)
        if suffix:
            groups[-1].append(("equal", old_end, old_end + suffix, new_end, new_end + suffix))
    return groups


def file_diff(path: str, left: bytes | None, right: bytes | None, options: CompareOptions) -> dict:
    contents = []
    for label, content in (("왼쪽", left), ("오른쪽", right)):
        try:
            contents.append(normalize_lines(content, options) if content is not None else [])
        except UnicodeDecodeError:
            raise ValueError(f"{label}: UTF-8로 읽을 수 없습니다.") from None
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from None
    old, new = contents
    lines = []
    additions = deletions = 0

    def append(kind, text, old_line=None, new_line=None):
        lines.append({"kind": kind, "text": text, "old_line": old_line, "new_line": new_line})

    def span(start, stop):
        length = stop - start
        if length == 0:
            return f"{start},0"
        return str(start + 1) if length == 1 else f"{start + 1},{length}"

    for group in changed_groups(old, new):
        first, last = group[0], group[-1]
        append("hunk", f"@@ -{span(first[1], last[2])} +{span(first[3], last[4])} @@")
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                    append("context", old[i], i + 1, j + 1)
            if tag in ("delete", "replace"):
                for i in range(i1, i2):
                    append("delete", old[i], i + 1)
                    deletions += 1
            if tag in ("insert", "replace"):
                for j in range(j1, j2):
                    append("add", new[j], new_line=j + 1)
                    additions += 1
    return {
        "path": path, "old_exists": left is not None, "new_exists": right is not None,
        "identical": left is not None and right is not None and old == new,
        "additions": additions, "deletions": deletions, "lines": lines,
    }
