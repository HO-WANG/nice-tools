# Installed SAP clients and saved connections

## Authorized behavior

The Windows application remains one `nice-tools.exe`. ASE operations now require
the user's existing SAP Open Client installation: catalogs use `isql.exe` and
character-mode IN/OUT use `bcp.exe`. No database driver or SAP client is bundled.
This supersedes the bundled FreeTDS implementation in the October 13 design.

Successful manual connections save host, port, user and a Jasypt `ENC(...)`
password to `ase-connections.txt` beside the executable. Matching host/port/user
updates an existing profile. Other profiles, comments and SAP/Jasypt settings
survive. Failed connections do not write configuration. A save failure keeps the
successful session and presents a warning without overwriting malformed data.

`[preferences] last_profile` remembers the last successful server. The next
launch selects it; the user still enters the Jasypt master key to connect. The
master key never enters a file, environment variable, browser storage or logs.

## Implementation contracts

- `[sap_client] isql_path/bcp_path` accepts explicit absolute paths. Otherwise
  discovery uses PATH and the installed SYBASE/SYBASE_OCS directories. An invalid
  explicit path fails rather than silently using a different executable.
- Metadata invokes isql directly in the application's thread pool, without
  booting another application EXE. File dialogs and transfers retain isolated
  application workers.
- Windows client passwords enter a ConPTY console prompt, with no `-P` argument.
  Children start suspended, join a kill-on-close Windows Job and then resume.
  Cancellation, timeout, disconnect and shutdown own the complete process tree.
- Temporary `sql.ini` and UTF-8 SQL/output files contain only nonsecret metadata.
  Headerless isql results use escaped frames with begin/end and per-row markers.
  A required `@@version` frame verifies ASE identity. Corrupt or truncated
  output, client errors and server errors fail closed.
- BCP uses SAP flags, literal terminators and installed client charset names.
  OUT explicitly raises the default text/image receive limit. Names requiring
  special BCP target quoting are rejected with an explanation.
- Preserve explicit Search, optional owner filtering, exact object/schema
  checks, full-file IN validation, single-use reviews, input fingerprints and
  Windows sharing guards. OUT publishes a temporary result atomically only after
  validation; failed or canceled exports preserve the destination.
- Catalog timings distinguish total application duration and CLI duration.
  Successful OUT statistics distinguish overall elapsed time/rate and bcp.exe
  elapsed time/rate, including that client's startup/login.
- Configuration writes are atomic, serialized and checked for external edits.
  Profile IDs change with file contents; the UI reconciles against the actual
  connected host/port/user so a live session cannot appear to be another server.

## Verification and practical limits

Source tests and mocked browser checks cover discovery, process ownership,
encrypted persistence, reconnect selection, catalog parsing, file validation,
transfer statistics and failure/cancellation behavior. Windows CI compiles
developer-only external console fixtures, exercises actual ConPTY/Job APIs,
then builds and launches the actual one-file EXE from a Unicode/spaces path.
Those fixtures are never bundled or distributed.

There is no live ASE server or actual SAP SDK in the cloud. Fixture results do
not establish SDK flag/version compatibility, server charset/LOB/identity/date
fidelity, literal terminator behavior or performance on the user's PC. Validate
those against the installed client and an authorized test database.
