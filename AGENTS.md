# Project rules

## Distribution

- The user requires every end-user release to be **one Windows 10/11 x64 executable: `nice-tools.exe`**.
- Bundle Python, runtime dependencies and all HTML/CSS/JavaScript assets into that executable. End users only need their existing browser.
- Start on `127.0.0.1` with an available port and open the local UI automatically. User files stay on the user's PC.
- Keep source-based commands for development. Do not present Python installation, virtualenvs or wheelhouse folders as the normal end-user deployment.
- Build Windows executables on Windows. Before distributing, exercise the actual EXE from a directory containing only that file, including page/assets and a real comparison request.
- A passing source test suite or Linux packaging check does not prove the Windows EXE works. Report any unrun native checks explicitly.
- Do not introduce runtime CDN or Internet dependencies.
- The user-authorized `ase-connections.txt` is optional, user-maintained data beside the EXE, not an additional runtime dependency or required distribution artifact. Keep the release ZIP to the single EXE.

## Workspace

Use the existing isolated cloud checkout. Do not create a Git worktree unless the user explicitly asks.

## File comparison

The user requires no fixed byte-size cap for individual files or complete upload requests. Compare complete contents, including differences beyond the former 10 MiB limit. Keep large-file and large-request regression checks in source tests and executable verification. Actual capacity depends on the PC's memory and temporary storage.

Automatically detect each file's text encoding before normalization and diff. Decode and validate the complete file strictly; detection samples must never truncate comparison, and replacement/ignore decoding must not hide differences. Keep Korean legacy and mixed-encoding regression checks. Do not promise perfect detection of ambiguous short text or BOM-less Unicode.

## Validation

Jasypt changes must pass the real Java Jasypt 1.9.3 bidirectional check (`scripts/check_jasypt_java.py`), not just Python round trips. Keep the pinned Java reference fixtures and executable checks for both AES and MD5/DES settings. The default is `PBEWithMD5AndDES` with NoIvGenerator; retain AES support with RandomIvGenerator. Java is for developer validation only; user PCs still need only the single EXE and a browser. Never persist passwords/plaintext in logs or browser storage, and keep Jasypt API responses `Cache-Control: no-store` with sanitized validation errors.

Run the source suite with the project's virtualenv: `.venv/bin/python -m pytest -q` on Linux or `.venv\Scripts\python -m pytest -q` on Windows. Windows CI must run source tests, build the single EXE and verify the executable before uploading it as the distribution artifact.

## ASE / BCP

- Read optional UTF-8 INI profiles from `ase-connections.txt` beside the actual EXE, or the repository root for source execution, independently of the current directory and bundle extraction path. Connection metadata and Jasypt-encrypted DB passwords (`ENC(...)`) may persist in that user-owned file. Never persist plaintext DB passwords or the Jasypt master key in files, arguments, logs or browser storage; keep decrypted credentials in memory and worker stdin only. Obtain the master key from the UI at connection time and keep ASE JSON responses no-store with the loopback/custom-header boundary.
- Use the bundled, checksum-verified FreeTDS Sybase DB-Library with TDS 5.0 and isolated workers of the same EXE. Do not replace character BCP with ordinary INSERT statements.
- IN appends. Completed batches may survive failure/cancel; show this before execution. Revalidate reviewed schema and the complete input file, and hold the Windows read-sharing guard through native transfer.
- Publish OUT only after success under the parent's cancellation lock; preserve an existing destination on failure/cancel.
- Preserve native source pins, checksum guards and precision/binary/failure-propagation patches. Verify the full application's native binding, not a hand-picked subset of symbols.
- Source/mock/native-load tests do not prove ASE compatibility. Report whether real ASE IN/OUT was tested, including server charset, identity, LOB and date fidelity.
