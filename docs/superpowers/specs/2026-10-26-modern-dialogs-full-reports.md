# Modern native selection and complete eager comparison reports

Latest user correction supersedes the prior deferred/report-row policy:
prepare detailed reports during initial comparison for ALL valid text pairs,
including >=1000 changes. Persist the complete detail report in owned disk
pages and a ready preview containing the first1000 addition/deletion entries,
with context/headings excluded from the1000-change budget. No analysis button
or heavy computation at selection/reopen/page time. Exact full counts/equality
remain unchanged; never cap source bytes. Both views retain safe cache sharing.

Full report pages are retained under report/full; top-level report pages are
the preview. Metadata publishes last after both stores close. Preview can have
more than1000 rows because context/headings do not consume change budget.
Use metadata change_limit:1000,displayed_changes,truncated,full_total_lines,
full_pages; total_lines/pages describe preview rows. When full change count is
<=1000 preview includes all context; above1000 stop after1000th changed entry.
prepare_diff always returns ready, with no deferred classifier. Preserve old
unlimited build_diff default for internal callers; production uses max_changes
and keep_full. Existing max_lines may remain compatibility-only if useful.

All app-controlled Windows file/folder dialogs migrate to native Common Item
Dialog using ctypes COM, no new dependency or legacy fallback. Folder uses
IFileOpenDialog+FOS_PICKFOLDERS; open-file uses IFileOpenDialog; save-file uses
IFileSaveDialog. Preserve cancellation, STA lifetime, ownership, filesystem-only
paths, filters, suggested basename, no CWD/history changes and initial folders.
Comparison, ASE OUT folder/IN file, IQ OUT folder and character inspection all
reuse ase_picker public functions. Browser development upload chooser is browser
controlled. Verify real Windows COM and frozen picker open/cancel if feasible;
do not claim human visual review when unavailable.

Execution: independent dialog and engine work; root handles worker/jobs/API/UI
integration, docs/bundle checks. TDD for changed behavior, focused/full source
tests, actual browser and source-worker flows, independent review, actual
Windows single-EXE build/verification and fresh artifact before completion.
