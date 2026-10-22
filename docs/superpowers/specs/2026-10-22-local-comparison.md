# Local directory comparison performance

The user approved keeping the web UI while Python directly reads two PC folders, reducing repeated processing and retaining the single Windows EXE distribution. Existing comparison options, complete strict automatic decoding, full-file comparisons and detailed diffs remain required. No proprietary dependencies are added.

## Data flow

The local-only JSON API creates a session and opens owned Windows native dialogs. Folder selections mint opaque tokens; HTTP cannot supply arbitrary filesystem roots. Starting a job sends those grants and comparison options to an owned worker. Two comparison worker slots keep separate tabs from creating unbounded active comparison processes. Progress and cancellation stay responsive. Non-Windows source development retains the existing upload flow after an explicit unsupported-native status.

The worker enumerates relative paths without following descendant symlinks/junctions. Directory identities, no-follow traversal and Windows read-sharing guards detect replacement and prevent source editing during reads. All results refer to complete files; a per-file read failure is an error row, while a failed scan does not publish a partial successful comparison. File count remains 1,000 across the selected folders; byte sizes remain uncapped.

Exact byte equality is a fast path only after one complete strict text validation. Otherwise the engine streams Unicode normalization into owned UTF-8 line-record caches. Sorting uses bounded runs and bounded-fan-in merges. Unicode whitespace removal uses the same Python isspace set through a compiled regex. UTF-8/BOM streams validate as they normalize; legacy detection samples only choose candidates whose full content is then strictly validated.

Selecting a result reuses normalized caches for a diff; original mode lazily prepares its own untransformed caches. Source fingerprints and missing counterparts are checked again before use. Diff records are stored as 200-row pages with metadata published last. Cached page reads validate metadata and sources directly in the parent without another frozen-worker bootstrap. Matching still holds the selected pair's normalized line lists in memory; normalization/sorting memory depends on the largest line and configured buffers. Caches use temporary disk storage.

## Ownership and failure

The session owns selected roots and up to two completed job caches. Cancellation and worker registration share a lock, preventing late worker starts. Process ownership includes the job so one cancellation cannot kill another job's diff. Eviction and session close terminate owned workers before cache deletion. Abandoned session/start/picker responses are cleaned up. Abandoning a diff HTTP request discards that response while its tracked job may finish a reusable cache; page/mode navigation does not invalidate the session. Duplicate busy diff builds return retry guidance instead of consuming all HTTP worker threads waiting on locks.

Only fixed error codes cross the worker and API boundary for known changed/busy/file-count conditions; unexpected native messages and file values are not logged. Actual file/diff values are returned only as the requested diff page. No source files, options, session tokens or result contents are persisted in browser storage.

## Verification

Tests compare streamed results with existing option/encoding semantics, including invalid tails, Unicode separators, very long lines, duplicates, external-sort merges and cancellation cleanup. Integration tests use real temporary folders, HTTP opaque tokens and owned subprocesses. Event-driven regressions cover spawn/cancel races, session cleanup, eviction, stale one-sided results and queued-directory replacement. Mocked native browser tests verify no multipart requests, state/progress/cancel, server paging, retries/stale responses and mobile layouts; existing upload browser checks remain valid in non-Windows development.

The Windows release runs the full source suite and existing Java/BCP checks, builds one EXE, verifies isolated startup and assets, then directly exercises that actual frozen comparison worker with mixed encodings, invalid identical binary text, a final-byte difference beyond 10MiB, complete diff pages and stale-file rejection. Native dialogs use the existing tested Windows dialog primitive; actual user-PC throughput remains workload dependent.
