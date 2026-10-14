"""Isolated ASE operations with a single-request newline JSON protocol.

Credentials arrive over stdin for isolated file transfers. Catalog and preview
operations reuse the application's ODBC session; BCP remains a child process
owned by the transfer worker's cancellation group.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable


MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
CATALOG_OPERATIONS = {"databases", "owners", "objects", "columns"}


def _module(name: str):
    return importlib.import_module(f"nice_tools.{name}")


def _direction(payload: dict) -> str:
    options = payload.get("options", {})
    if not isinstance(options, dict):
        raise ValueError("Transfer options must be an object.")
    direction = options.get("direction")
    if not isinstance(direction, str) or direction.lower() not in {"in", "out"}:
        raise ValueError("Transfer direction must be 'in' or 'out'.")
    return direction.lower()


def _path(value) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Select a valid local file path.")
    return Path(value)


def target_fingerprint(path: Path) -> dict:
    """Describe the reviewed destination without reading its contents."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False}
    if not path.is_file():
        raise ValueError("The output destination must be a file.")
    return {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _check_target(path: Path, expected) -> None:
    if not isinstance(expected, dict) or not isinstance(expected.get("exists"), bool):
        raise ValueError("Review the output destination before starting the export.")
    if expected["exists"]:
        if not isinstance(expected.get("size"), int) or not isinstance(expected.get("mtime_ns"), int):
            raise ValueError("Review the output destination before starting the export.")
        expected = {key: expected[key] for key in ("exists", "size", "mtime_ns")}
    else:
        expected = {"exists": False}
    if target_fingerprint(path) != expected:
        raise ValueError("The output destination changed since preview. Select and preview it again.")


def _check_input(path: Path, expected) -> None:
    if not isinstance(expected, dict) or not all(key in expected for key in ("size", "mtime_ns", "sha256")):
        raise ValueError("Preview the input file before starting the import.")
    if not isinstance(expected["size"], int) or not isinstance(expected["mtime_ns"], int) or not isinstance(expected["sha256"], str):
        raise ValueError("Preview the input file before starting the import.")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if before.st_size != expected["size"] or before.st_mtime_ns != expected["mtime_ns"]:
            raise ValueError("The input file changed since preview. Preview it again.")
        while chunk := source.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
        after = os.fstat(source.fileno())
    current = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or current.st_size != after.st_size
        or current.st_mtime_ns != after.st_mtime_ns
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
        or digest.hexdigest() != expected["sha256"]
    ):
        raise ValueError("The input file changed since preview. Preview it again.")


def _load_windows_file_api():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _last_windows_error():
    return ctypes.get_last_error()


@contextlib.contextmanager
def _input_guard(path: Path):
    """Prevent Windows writes/deletes from verification through SAP BCP import.

    Source development on other platforms does not promise a filesystem lock.
    The shipped application is Windows-only.
    """
    if sys.platform != "win32":
        yield
        return
    library = _load_windows_file_api()
    library.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    library.CreateFileW.restype = ctypes.c_void_p
    library.CloseHandle.argtypes = [ctypes.c_void_p]
    library.CloseHandle.restype = ctypes.c_int32
    handle = library.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x80, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        error = _last_windows_error()
        if error in (32, 33):
            raise ValueError("Close applications editing the input file, then preview it again.")
        raise OSError(error, "The input file could not be opened safely. Select and preview it again.")
    try:
        yield
    finally:
        library.CloseHandle(handle)


def _preview(payload: dict, emit: Callable, catalog=None) -> dict:
    direction = _direction(payload)
    path = _path(payload.get("path"))
    files = _module("ase_files")
    options = files.parse_options(payload.get("options", {}))
    metadata = (catalog or _module("ase_odbc").run_operation)({**payload, "operation": "columns"}, emit=emit)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("columns"), list):
        raise ValueError("ASE did not return valid object metadata.")
    result = {**metadata, "path": str(path), "warnings": []}
    if direction == "in":
        result.update(files.inspect_file(path, metadata["columns"], options, emit))
    else:
        result["out_target_fingerprint"] = target_fingerprint(path)
    return result


def _output_transfer(payload: dict, emit: Callable, register=None) -> dict:
    target = _path(payload.get("path"))
    expected = payload.get("out_target_fingerprint")
    _check_target(target, expected)
    stage = None
    preserve_stage = False
    try:
        if payload.get("stage_path") is not None:
            candidate = _path(payload["stage_path"])
            if (
                candidate.parent.resolve() != target.parent.resolve()
                or candidate.resolve() == target.resolve()
                or candidate.is_symlink()
                or not candidate.name.startswith(".nice-tools-")
                or not candidate.is_file()
                or candidate.stat().st_size != 0
                or candidate.stat().st_nlink != 1
                or (target.exists() and candidate.samefile(target))
            ):
                raise ValueError("The export staging file must be an empty reserved file beside the destination.")
            # Only adopt cleanup ownership after checking the reservation. An
            # invalid request must never unlink the target or another user file.
            stage = candidate
        else:
            descriptor, filename = tempfile.mkstemp(prefix=".nice-tools-", suffix=".bcp", dir=target.parent)
            os.close(descriptor)
            stage = Path(filename)
        request = {**payload, "path": str(stage)}
        result = _module("ase_cli").run_operation(request, emit, register=register)
        if not isinstance(result, dict):
            raise ValueError("ASE did not return a valid transfer result.")
        _check_target(target, expected)
        result = {**result, "path": str(target)}
        if payload.get("defer_publish") is True:
            # The parent publishes under its cancellation lock, then removes the
            # stage in finally. A killed worker cannot bypass that lock.
            result["stage_path"] = str(stage)
            preserve_stage = True
        else:
            os.replace(stage, target)
        return result
    finally:
        if stage is not None and not preserve_stage:
            with contextlib.suppress(OSError):
                stage.unlink(missing_ok=True)


def execute(payload: dict, emit: Callable | None = None, register=None, catalog=None) -> dict:
    """Execute one operation; SAP errors propagate for protocol sanitization."""
    if not isinstance(payload, dict):
        raise ValueError("Worker request must be a JSON object.")
    emit = emit or (lambda data: None)
    operation = payload.get("operation")
    if operation == "status":
        return _module("ase_cli").client_status()
    if operation == "pick":
        return _module("ase_picker").pick_file(payload.get("direction"), payload.get("suggested_name"))
    if operation == "pick-sql-ini":
        return _module("ase_picker").pick_sql_ini()
    if operation == "preview":
        return _preview(payload, emit, catalog=catalog)
    if operation == "transfer":
        direction = _direction(payload)
        payload = {**payload, "options": {**payload["options"], "direction": direction}}
        if direction == "out":
            return _output_transfer(payload, emit, register=register)
        path = _path(payload.get("path"))
        with _input_guard(path):
            _check_input(path, payload.get("fingerprint"))
            return _module("ase_cli").run_operation(payload, emit, register=register)
    if operation in CATALOG_OPERATIONS:
        return (catalog or _module("ase_odbc").run_operation)(payload, emit=emit)
    raise ValueError("Unknown ASE worker operation.")


def _redact(value, password: str):
    if isinstance(value, str):
        return value.replace(password, "[redacted]") if password else value
    if isinstance(value, dict):
        return {key: _redact(item, password) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, password) for item in value]
    return value


def main(stdin=None, stdout=None) -> int:
    """Read exactly one JSON request and write flushed newline JSON records."""
    stdin = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)
    stdout = stdout if stdout is not None else sys.stdout
    password = ""

    def record(value: dict) -> None:
        stdout.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n")
        stdout.flush()

    try:
        raw = stdin.read(MAX_PAYLOAD_BYTES + 1)
        length = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
        if length > MAX_PAYLOAD_BYTES:
            raise ValueError("Worker request exceeds the metadata limit.")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError("Invalid worker JSON request.") from None
        if not isinstance(payload, dict):
            raise ValueError("Worker request must be a JSON object.")
        connection = payload.get("connection")
        if isinstance(connection, dict) and isinstance(connection.get("password"), str):
            password = connection["password"]

        def emit(data: dict) -> None:
            if not isinstance(data, dict):
                raise ValueError("Invalid ASE progress record.")
            data = dict(data)
            for key in ("message", "detail", "warning", "warnings"):
                if key in data:
                    data[key] = _redact(data[key], password)
            record({**data, "event": "progress"})

        # Keep accidental Python library prints out of both logs and protocol.
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result = execute(payload, emit)
        if not isinstance(result, dict):
            raise ValueError("ASE did not return a valid operation result.")
        record({"event": "result", "result": result})
        return 0
    except Exception as error:
        detail = str(error) if isinstance(error, (ValueError, OSError)) else "ASE operation failed."
        record({"event": "error", "detail": _redact(detail or "ASE operation failed.", password)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
