# Project rules

## Distribution

- The user requires every end-user release to be **one Windows 10/11 x64 executable: `nice-tools.exe`**.
- Bundle Python, runtime dependencies and all HTML/CSS/JavaScript assets into that executable. End users need their existing browser; ASE tools additionally use the PC's installed SAP Open Client isql.exe and bcp.exe (Windows 10 1809+).
- Start on `127.0.0.1` with an available port and open the local UI automatically. User files stay on the user's PC.
- Keep source-based commands for development. Do not present Python installation, virtualenvs or wheelhouse folders as the normal end-user deployment.
- Build Windows executables on Windows. Before distributing, exercise the actual EXE from a directory containing only that file, including page/assets and a real comparison request.
- A passing source test suite or Linux packaging check does not prove the Windows EXE works. Report any unrun native checks explicitly.
- Do not introduce runtime CDN or Internet dependencies.
- The user-authorized `ase-connections.txt` is optional, app-created or user-maintained data beside the EXE, not an additional runtime dependency or required distribution artifact. Keep the release ZIP to the single EXE.

## Workspace

Use the existing isolated cloud checkout. Do not create a Git worktree unless the user explicitly asks.

## File comparison

The user requires no fixed byte-size cap for individual files or complete upload requests. Compare complete contents, including differences beyond the former 10 MiB limit. Keep large-file and large-request regression checks in source tests and executable verification. Actual capacity depends on the PC's memory and temporary storage.

Automatically detect each file's text encoding before normalization and diff. Decode and validate the complete file strictly; detection samples must never truncate comparison, and replacement/ignore decoding must not hide differences. Keep Korean legacy and mixed-encoding regression checks. Do not promise perfect detection of ambiguous short text or BOM-less Unicode.

## Validation

Jasypt changes must pass the real Java Jasypt 1.9.3 bidirectional check (`scripts/check_jasypt_java.py`), not just Python round trips. Keep the pinned Java reference fixtures and executable checks for both AES and MD5/DES settings. The default is `PBEWithMD5AndDES` with NoIvGenerator; retain AES support with RandomIvGenerator. Java is for developer validation only; ASE uses the PC's SAP utilities and otherwise user PCs need the single EXE and a browser. Never persist passwords/plaintext in logs or browser storage, and keep Jasypt API responses `Cache-Control: no-store` with sanitized validation errors.

Run the source suite with the project's virtualenv: `.venv/bin/python -m pytest -q` on Linux or `.venv\Scripts\python -m pytest -q` on Windows. Windows CI must run source tests, build the single EXE and verify the executable before uploading it as the distribution artifact.

## ASE / BCP

- DB/filter changes must not fetch objects automatically. Search/Enter explicitly queries the server, with an optional owner (all owners by default) and literal, case-insensitive name/kind filters. Owner choices may load on demand. Preserve exact owner/object/schema checks for BCP even when browsing all owners.
- Catalog timing diagnostics must remain secret-free and distinguish total, SAP CLI execution (process start/login/query) and remaining app preparation. Queries invoke installed isql directly without rebooting the frozen EXE; each query still logs in, so do not claim connection pooling or a measured latency improvement without user-PC evidence.
- Read UTF-8 INI profiles from `ase-connections.txt` beside the actual EXE, or repository root for source execution, independently of CWD and bundle extraction path. On successful new connection, atomically save metadata and Jasypt-encrypted DB password (`ENC(...)`), preserving existing sections. Failed login must not save; failed persistence must retain the successful session and show a sanitized warning. Remember the last successful profile for the next launch; require a UI key to connect, never store the master key.
- Resolve the PC's installed `isql.exe` and `bcp.exe` through `[sap_client] isql_path/bcp_path`, then PATH/SYBASE/SYBASE_OCS when unspecified. Explicit invalid paths must fail rather than falling back. Never bundle or download SAP clients, FreeTDS or ODBC drivers, or substitute ordinary INSERTs for BCP.
- Never persist plaintext DB passwords or Jasypt master keys in files, arguments, environment variables, logs or browser storage. Use the owned Windows ConPTY Password: prompt, not `-P`; isolated workers receive credentials through stdin. Keep responses no-store and retain the loopback/custom-header boundary.
- IN appends. Completed batches may survive failure/cancel; show this before execution. Revalidate reviewed schema and complete input file, and hold Windows read-sharing guard through SAP transfer.
- Publish OUT only after success under the parent's cancellation lock; preserve an existing destination on failure/cancel and kill the owned worker/SAP process tree.
- Windows CI must compile developer-only external console fixtures before source tests and exercise native console password input plus actual EXE CLI/catalog/profile/file integration. Fixtures are never distribution artifacts. Source/fixture tests do not prove SAP client or ASE compatibility; report real-server IN/OUT status including charset, identity, LOB and dates.
