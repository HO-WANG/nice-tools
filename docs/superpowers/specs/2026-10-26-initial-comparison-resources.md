# Initial comparison resource failure and optimization

The user reported two files around10MB and200MB and failure after hundreds of seconds when starting comparison. The exact user's PC error has not been captured. Existing worker errors collapsed memory exhaustion, process exit and IPC failures into one generic message, preventing a reliable diagnosis.

## Findings and changes

- Unrestricted native traceback could allocate a matrix proportional to both line counts. A constrained160MiB subprocess with80k anchored/shifted records reproduced `std::bad_alloc`. This is a demonstrated failure path, not confirmation of the user's particular cause.
- Preserve exact insertion/deletion minimality. Short edits use bounded Myers. Large searches filter globally impossible matches without dropping them from the output, then use native Indel only for rectangles of at most four million cells, sparse Hunt–Szymanski, or bit-parallel Hirschberg. No unused SequenceMatcher index is built.
- One-sided full reports count LF records in128KiB binary chunks, then strictly decode and stream every record into bounded200-row pages. They do not retain all source strings. Legacy preview limits still validate complete input.
- The C JSON encoder serializes one bounded page per write. A200-row repeated-write probe took0.225s versus0.838s with token-wise `json.dump` in the same cloud workspace.
- Report policy is unchanged: full original/compared archives are ready at initial completion; the first1000 addition/deletion entries are cached for display, with context excluded. Sources remain guarded and unchanged; lookups do not re-match.
- Classify initial failure with allowlisted codes and last trusted phase/relative file. Only numeric errno/winerror/exit codes cross the boundary; console records omit filenames, contents, cache paths and raw exceptions. Storage/resource exhaustion fails the job with guidance; invalid text and ordinary unreadable inputs remain per-file errors.

## Validation evidence

Linux/Python3.12 source checks:1718 passed,13 Windows-specific checks skipped. Independent code review found no actionable defect and checked additional randomized LCS lengths against an independent implementation. Source worker verification covers80k anchored shifted records, exact counts, the complete archive beyond1000 changes, legacy encodings, invalid text, late differences and stale originals. Browser checks cover safe phase/code/numeric rendering plus existing cancellation, stale requests, paging and cached reopen behavior.

`scripts/benchmark_initial_comparison.py` created10MiB and200MiB files in each folder (440,401,800 total source bytes),100-byte unique ASCII records with10% replacements, default options. A fresh source worker including guarded local reads, normalization, matching and complete/preview report publication completed in20.782s (20.884s including process startup), peak RSS941.78MiB. Six cached page reads took0.001358s. The normalized files and JSON reports used748,361,405 cache bytes. This excludes fixture creation, HTTP/browser, Windows EXE extraction and antivirus. Memory still scales with both input line lists, line length and output operations; dense exact matching can still be expensive.

Safe constrained-memory comparisons are reproducible with `scripts/benchmark_bounded_alignment.py`. Representative engine-only results:200k sparse repeated lines0.147s/20.9MiB,80k anchored shifts0.077s/39.3MiB,40k repeated-block reorder0.359s/14.9MiB. Unrestricted Indel failed the320MiB process limit for the first two; the repeated case used205.8MiB. These are synthetic cloud measurements, not user-PC speed promises.

Windows CI must pass source/native tests, actual single-EXE/worker/dialog checks and both ZIP/raw z01 byte validation before release publication. Source evidence alone does not prove the Windows executable works.
