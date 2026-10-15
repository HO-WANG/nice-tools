"""Character BCP through the user's installed SAP executable.

Metadata is revalidated through a fresh ODBC connection before file transfers.
Only executable paths and nonsecret settings enter argv or temporary files;
the process runner supplies the password to the client's terminal prompt.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
import tempfile


class AseClientError(ValueError):
    """A secret-free error suitable for the local application's response."""


ASEError = AseClientError
_ENCODINGS = {"utf-8": "utf8", "cp949": "cp949", "euc-kr": "eucksc"}
_COLUMN_KEYS = ("position", "name", "type_id", "type_name", "length", "precision", "scale", "nullable", "identity")
_COPIED = re.compile(r"^\s*([0-9][0-9,]*)\s+rows?\s+copied\.?\s*$", re.I)
_ERROR_LINE = re.compile(
    r"(?:\b(?:CT|CS|DB)[- ]LIBRARY\s+error\b|\b(?:error|failed|failure)\s*:|"
    r"\b[1-9][0-9,]*\s+rows?\s+(?:failed|rejected)\b|\b(?:bulk\s+copy|bcp)\s+(?:failed|failure)\b|"
    r"(?:^|\n)\s*Error(?:\s|:))", re.I
)
_SERVER_ERROR = re.compile(r"\bMsg\s+([0-9]+)\s*,\s*(?:Level|Severity)\s+([0-9]+)", re.I)


def _client_config():
    from .ase_clients import client_config
    return client_config()


def client_status():
    from .ase_clients import client_status as status
    return status()


def _run_client(*args, **kwargs):
    from .ase_process import run_client
    return run_client(*args, **kwargs)


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 255 or any(c in value for c in "\0\r\n\x1b"):
        raise AseClientError("A database, owner or object name is invalid.")
    return "[" + value.replace("]", "]]") + "]"


def _id(value, label="id", minimum=0, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AseClientError(f"The {label} must be an integer.")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        raise AseClientError(f"The {label} must be an integer.")
    value = int(value)
    if not minimum <= value <= maximum:
        raise AseClientError(f"The {label} integer is outside the supported range.")
    return value


def _flag(value):
    if value in (False, 0, "0"):
        return False
    if value in (True, 1, "1"):
        return True
    raise AseClientError("ASE returned invalid column flags.")


def _object(value):
    if not isinstance(value, dict):
        raise AseClientError("Select an ASE object before continuing.")
    for key in ("database", "owner", "name"):
        _identifier(value.get(key))
    if value.get("kind") not in ("U", "V", "P"):
        raise AseClientError("This object kind is not supported.")
    return {"database": value["database"], "owner": value["owner"],
            "owner_id": _id(value.get("owner_id"), "owner id", minimum=1),
            "id": _id(value.get("id"), "object id", minimum=1), "name": value["name"], "kind": value["kind"]}


def _column(row):
    try:
        result = {key: row[key] for key in _COLUMN_KEYS}
        for key in ("position", "type_id", "length", "precision", "scale"):
            value = result[key]
            if value is None or (key in ("precision", "scale") and isinstance(value, str) and not value.strip()):
                value = 0
            result[key] = _id(value, f"column {key}")
        result["nullable"], result["identity"] = _flag(result["nullable"]), _flag(result["identity"])
        if result["position"] < 1:
            raise ValueError
        for key in ("name", "type_name"):
            _identifier(result[key])
        return result
    except (KeyError, TypeError, ValueError):
        raise AseClientError("The reviewed column schema is invalid or incomplete.") from None


def _options(raw):
    from .ase_files import parse_options
    return parse_options(raw)


def _connection(raw):
    if not isinstance(raw, dict):
        raise AseClientError("ASE connection settings are invalid.")
    host = raw.get("host")
    if not isinstance(host, str) or not host or len(host) > 253 or any(c in host for c in "\r\n\0[]#;=,"):
        raise AseClientError("The ASE host is invalid.")
    try:
        host.encode("ascii")
    except UnicodeError:
        raise AseClientError("The ASE host must be an IP address or ASCII hostname.") from None
    for key in ("user", "password"):
        value = raw.get(key)
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value) or (key == "user" and not value):
            raise AseClientError("The ASE login settings are invalid.")
    return {"host": host, "port": _id(raw.get("port", 5000), "port", 1, 65535),
            "user": raw["user"], "password": raw["password"],
            "login_timeout": _id(raw.get("login_timeout", 15), "login timeout", 1),
            "query_timeout": _id(raw.get("query_timeout", 60), "query timeout", 1)}


def _has_error(text):
    if _ERROR_LINE.search(text):
        return True
    return any(int(match[1]) >= 11 for match in _SERVER_ERROR.findall(text))


def _elapsed(result):
    elapsed = result.get("elapsed_seconds")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
        raise AseClientError("The SAP client execution time is invalid.")
    return elapsed


def _common_args(connection, ini_path, charset):
    return ["-I", str(ini_path), "-S", "NICE_TOOLS", "-U", connection["user"], "-J", charset, "-z", "us_english"]


def _write_interfaces(folder, connection):
    path = folder / "sql.ini"
    endpoint = f"NLWNSCK,{connection['host']},{connection['port']}"
    path.write_text(f"[NICE_TOOLS]\nmaster={endpoint}\nquery={endpoint}\n", encoding="ascii")
    return path


def _target(obj):
    for key in ("database", "owner", "name"):
        value = obj[key]
        if not re.fullmatch(r"[^\W\d][\w@$#]*", value, flags=re.UNICODE):
            raise AseClientError("SAP BCP supports standard identifiers here; this database, owner or object name requires unsupported quoting.")
    return ".".join(obj[key] for key in ("database", "owner", "name"))


def _terminator(raw_hex, encoding):
    try:
        return bytes.fromhex(raw_hex).decode(encoding, errors="strict")
    except (ValueError, UnicodeError):
        raise AseClientError("The BCP terminator cannot be represented in the selected client encoding.") from None


class _CopyOutput:
    def __init__(self, emit=None):
        self.emit = emit
        self.pending = ""
        self.failed = False
        self.rows = None

    def feed(self, text):
        self.pending += text
        lines = self.pending.split("\n")
        self.pending = lines.pop()
        for line in lines:
            self._line(line)
        if len(self.pending) > 65536:
            self.failed = True
            self.pending = ""

    def _line(self, line):
        if _has_error(line):
            self.failed = True
        final = _COPIED.fullmatch(line)
        if final:
            count = final.group(1).replace(",", "")
            if len(count) > 20:
                self.failed = True
            else:
                self.rows = int(count)
        progress = re.search(r"([0-9][0-9,]*)\s+rows?\s+total\s+sent\b", line, re.I)
        if progress and self.emit:
            count = progress.group(1).replace(",", "")
            if len(count) <= 20:
                self.emit({"type": "progress", "phase": "copying", "rows": int(count)})

    def finish(self):
        if self.pending:
            self._line(self.pending)
            self.pending = ""


def _copy(connection, obj, columns, path, options, clients, emit=None, register=None):
    target = _target(obj)
    if options["direction"] == "in" and not Path(path).is_file():
        raise AseClientError("The reviewed import file is no longer available.")
    output = _CopyOutput(emit)
    with tempfile.TemporaryDirectory(prefix="nice-tools-bcp-") as directory:
        ini_path = _write_interfaces(Path(directory), connection)
        argv = [clients["bcp_path"], target, options["direction"], path,
                *_common_args(connection, ini_path, _ENCODINGS[options["encoding"]]), "-c",
                "-t", _terminator(options["field_hex"], options["encoding"]),
                "-r", _terminator(options["row_hex"], options["encoding"]), "-m", "1"]
        if options["direction"] == "in":
            argv.extend(["-b", str(options["batch_size"])])
            if options["keep_identity"]:
                argv.append("-E")
        else:
            # SAP's default text/image receive limit is only 34816 bytes. Use
            # the ASE large-object limit instead of silently shortening exports.
            argv.extend(["-T", "2147483647"])
        if emit:
            emit({"type": "status", "phase": "copying"})

        def client_output(text):
            # run_client removes terminal escapes and redacts credentials across
            # reads before invoking this callback; never forward its raw parser.
            if emit:
                emit({"type": "bcp_output", "message": text})
        result = _run_client(argv, connection["password"], input_text="", timeout=None,
                             on_output=client_output, _parse_output=output.feed, register=register)
        # The trusted parser sees terminal text before credential redaction.
        # A password such as "rows" or "Error" must not alter row counts or hide
        # failures. It emits only numeric progress and retains no public text.
        output.finish()
        if result.get("returncode") != 0 or output.failed:
            raise AseClientError("SAP BCP failed. Completed import batches may remain in the table.")
        rows = output.rows
        if rows is None:
            raise AseClientError("SAP BCP did not report a successful final row count.")
        elapsed = _elapsed(result)
        rate = rows / elapsed if elapsed > 0 else None
        return {"rows": rows, "direction": options["direction"], "bcp_seconds": elapsed,
                "bcp_rows_per_second": rate if rate is None or math.isfinite(rate) else None}


def run_operation(payload, emit=None, register=None):
    """Revalidate an object with ODBC, then perform installed SAP BCP IN/OUT."""
    if not isinstance(payload, dict) or payload.get("operation") != "transfer":
        raise AseClientError("The BCP operation is not supported.")
    connection = _connection(payload.get("connection"))
    obj = _object(payload.get("object"))
    options = _options(payload.get("options", {}))
    if obj["kind"] == "P" or (obj["kind"] == "V" and options["direction"] == "in"):
        raise AseClientError("BCP IN requires a table; BCP OUT supports tables and views.")
    raw_columns = payload.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise AseClientError("Review the complete column schema before bulk copy.")
    expected = [_column(column) for column in raw_columns]
    if options["direction"] == "in" and any(column["type_id"] == 80 or column["type_name"].lower() == "timestamp" for column in expected):
        raise AseClientError("Timestamp columns are not supported for BCP IN.")
    path = payload.get("path")
    if not isinstance(path, str) or not path or "\0" in path:
        raise AseClientError("The selected file path is invalid.")
    _target(obj)
    clients = _client_config()
    from .ase_odbc import run_operation as metadata_operation
    metadata = metadata_operation({"operation": "columns", "connection": connection, "object": obj})
    if not isinstance(metadata, dict) or _object(metadata.get("object")) != obj:
        raise AseClientError("The selected object changed or is unavailable. Reload its metadata.")
    raw_current = metadata.get("columns")
    if not isinstance(raw_current, list):
        raise AseClientError("ASE did not return a complete column schema.")
    columns = [_column(column) for column in raw_current]
    if columns != expected:
        raise AseClientError("The reviewed column schema changed. Review the file again before bulk copy.")
    return _copy(connection, obj, columns, payload["path"], options, clients, emit, register)
