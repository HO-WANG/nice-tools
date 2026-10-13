# Project rules

## Distribution

- The user requires every end-user release to be **one Windows 10/11 x64 executable: `nice-tools.exe`**.
- Bundle Python, runtime dependencies and all HTML/CSS/JavaScript assets into that executable. End users only need their existing browser.
- Start on `127.0.0.1` with an available port and open the local UI automatically. User files stay on the user's PC.
- Keep source-based commands for development. Do not present Python installation, virtualenvs or wheelhouse folders as the normal end-user deployment.
- Build Windows executables on Windows. Before distributing, exercise the actual EXE from a directory containing only that file, including page/assets and a real comparison request.
- A passing source test suite or Linux packaging check does not prove the Windows EXE works. Report any unrun native checks explicitly.
- Do not introduce runtime CDN or Internet dependencies.

## Workspace

Use the existing isolated cloud checkout. Do not create a Git worktree unless the user explicitly asks.

## File comparison

The user requires no fixed byte-size cap for individual files or complete upload requests. Compare complete contents, including differences beyond the former 10 MiB limit. Keep large-file and large-request regression checks in source tests and executable verification. Actual capacity depends on the PC's memory and temporary storage.

## Validation

Run the source suite with the project's virtualenv: `.venv/bin/python -m pytest -q` on Linux or `.venv\Scripts\python -m pytest -q` on Windows. Windows CI must run source tests, build the single EXE and verify the executable before uploading it as the distribution artifact.
