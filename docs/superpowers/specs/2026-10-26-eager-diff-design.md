# Prepare small diffs during comparison, defer large reports

User requests initial comparison to prepare detailed reports so selection does
not recalculate. At >=1000 changed lines require a separate 상세 내용 분석
button; subsequent steering limits detailed display to1000 rows. Preserve full
file equality, strict decoding, normalization and source guards. Light weight
and speed remain highest priorities; add no runtime dependency.

Ruling: changed lines are additions + deletions (a replaced row counts twice).
Display limit1000 includes hunk/context rows, with explicit truncation notice;
exact full-file equality and edit counts remain distinct from displayed rows.
Both compared/original views are prepared, sharing cache when options are the
defaults or raw inputs are identical. Do not put reports in initial HTTP JSON.

## Interfaces / execution plan

- local_compare_diff.prepare_diff(normalized_left, normalized_right, directory,
  *, path, old_exists, new_exists, cancelled=None): bounded Indel distance test;
  >=1000 returns {state:deferred, threshold:1000} without full alignment or page
  publication. Smaller reports are built once, reusing loaded line lists.
- build_diff keeps unlimited default for internal compatibility; production
  uses max_lines=1000. Capped metadata adds display_limit, truncated and
  full_total_lines; total_lines/pages describe stored rows. Additions/deletions
  describe the complete diff. No partial metadata publication.
- Comparison worker precomputes reports under source guards before publishing
  manifest. Results add reports:{compared:{state},original:{state}}. Read-only
  diff requests never build. Deferred response: {state:deferred,threshold:1000,
  display_limit:1000,lines:[]}. Explicit analyze:true starts owned heavy work;
  completed reports reuse cache on subsequent calls. Both views share cache
  via internal cache_mode when safe. No browser paths or source payloads.
- /api/comparison/diff optional analyze is a strict boolean, defaultfalse.
  Manager.diff(..., analyze=False) adds no heavy work to parent read path.
  Non-Windows upload development UI precomputes/caches small reports in its
  initial compare response and uses explicit analysis for deferred reports.
- UI shows 상세 내용 분석 only for deferred data, preserves retry intent and
  stale-request/cancel/session behavior, displays truncation independently of
  pagination, and includes preparing-reports in initial progress.
- Tests:999/1000/1001 changes, duplicates/normalization/both views/one-sided/
  empty/invalid sources, no normalization/matching/worker start on ready read,
  deferred state and explicit analysis,1000-row cap, stale source/cancel/cache
  ownership, real HTTP/worker integration, browser and actual Windows EXE.
