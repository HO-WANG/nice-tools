# Preserve identical records in large diffs

The reported CT9701 row is deleted and re-added when neighboring records
change. The current native unit-cost Levenshtein fallback can prefer two
substitutions over deletion + equal + insertion. Reproduced without hidden
characters at the real HTTP and cached-page boundaries, including duplicates.

## Approach

Use the existing pinned RapidFuzz dependency. Keep bounded Myers for small
edits. For larger edits, obtain the existing native Levenshtein alignment and
calculate its insertion/deletion cost. A bounded, matrix-free Indel distance
check proves whether that cost is optimal. Reuse an optimal result, otherwise
obtain native Indel opcodes, which preserve a longest common subsequence.
This is the implementer's choice within the requested correction.

Unbounded Python Myers would harm dense-change performance. Unconditional
native Indel was tested and rejected: the 100,000-line repeated sparse case
used about 1.2GiB versus about 32MiB for the guarded approach. Dense cases
requiring realignment can still use additional native matching memory; no
constant-memory claim. Keep complete inputs, existing normalization/options,
context, line numbers, paged caches, cancellation and single-EXE distribution.
Reordered/duplicate records can have multiple valid alignments.

## Verification / execution plan

- Reproduce three failing HTTP/duplicate/cache regressions before changing code.
- Change only the large-edit fallback and run all diff tests.
- Benchmark 100k sparse repeated and 20k dense/shifted/disjoint cases in fresh
  processes, verifying counts as well as elapsed time and peak RSS.
- Run the full source suite and source-worker integration, then actual Windows
  EXE verification including the reported shifted record and exact counts.
