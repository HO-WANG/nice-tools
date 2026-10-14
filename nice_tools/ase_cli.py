"""ASE catalogs and character BCP through the user's installed SAP clients.

Only the executable paths and nonsecret settings enter argv or temporary files.
The process runner supplies the password to the client's terminal prompt. Catalog
rows use an explicit escaped protocol, never isql's whitespace-aligned columns.
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
_FRAME_COUNTS = {"V": 1, "D": 2, "W": 2, "O": 5, "C": 9}
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


def _object_row(row, database):
    return _object({"database": database, "id": row[0], "owner_id": row[1], "owner": row[2], "name": row[3], "kind": row[4]})


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


def _escaped_sql(expression):
    # Keep text in the negotiated client charset. Binary-to-hex conversion would
    # expose the server's charset and make a CP949 server look like UTF-8 bytes.
    result = f"isnull(convert(varchar(512), {expression}), '')"
    for source, replacement in (("'^'", "^^"), ("'|'", "^p"), ("char(13)", "^r"), ("char(10)", "^n"), ("char(9)", "^t")):
        result = f"str_replace({result}, {source}, '{replacement}')"
    return result


def _select(tag, expressions):
    # Build independently to retain the exact field delimiters and a sufficiently
    # wide result type on ASE even when catalog names contain escaped characters.
    parts = ["'~NT~" + tag + "|'"]
    for index, expression in enumerate(expressions):
        if index:
            parts.append("'|'")
        parts.append(_escaped_sql(expression))
    parts.append("'|~END~'")
    # Below the legacy 2 KiB-page varchar limit (1960 bytes). Missing end markers
    # make any result-side truncation an explicit failure rather than a bad name.
    return "SELECT convert(varchar(1800), " + " + ".join(parts) + ") "


def _objects_query(database, owner_id=None, object_id=None, search="", kind="all"):
    db = _identifier(database)
    predicates = ["o.type IN ('U', 'V', 'P')" if kind == "all" else f"o.type = '{kind}'"]
    if owner_id is not None:
        predicates.append(f"o.uid = {owner_id}")
    if object_id is not None:
        predicates.append(f"o.id = {object_id}")
    if search:
        pattern = search.replace("!", "!!").replace("%", "!%").replace("_", "!_").replace("[", "![").replace("'", "''")
        predicates.append(f"lower(o.name) LIKE lower('%{pattern}%') ESCAPE '!'")
    return (_select("O", ("o.id", "o.uid", "u.name", "o.name", "rtrim(o.type)")) +
            f"FROM {db}.dbo.sysobjects o JOIN {db}.dbo.sysusers u ON u.uid = o.uid " +
            f"WHERE {' AND '.join(predicates)} ORDER BY o.type, u.name, o.name")


def _columns_query(obj):
    db = _identifier(obj["database"])
    return (_select("C", ("c.colid", "c.name", "c.type", "t.name", "c.length", "c.prec", "c.scale",
                          "CASE WHEN (c.status & 8) <> 0 THEN 1 ELSE 0 END",
                          "CASE WHEN (c.status & 128) <> 0 THEN 1 ELSE 0 END")) +
            f"FROM {db}.dbo.syscolumns c JOIN {db}.dbo.systypes t ON t.usertype = c.usertype " +
            f"JOIN {db}.dbo.sysobjects o ON o.id = c.id WHERE o.id = {obj['id']} AND o.uid = {obj['owner_id']} " +
            "AND o.type IN ('U', 'V') ORDER BY c.colid")


def _unescape(value):
    mapping = {"^": "^", "p": "|", "r": "\r", "n": "\n", "t": "\t"}
    result, index = [], 0
    while index < len(value):
        char = value[index]
        if char == "^":
            index += 1
            if index >= len(value) or value[index] not in mapping:
                raise AseClientError("SAP isql returned an invalid catalog frame.")
            char = mapping[value[index]]
        result.append(char)
        index += 1
    return "".join(result)


def _has_error(text):
    if _ERROR_LINE.search(text):
        return True
    return any(int(match[1]) >= 11 for match in _SERVER_ERROR.findall(text))


def _parse_catalog(text, allowed):
    records = {tag: [] for tag in allowed}
    begun = ended = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "~NTBEGIN~":
            if begun or ended:
                raise AseClientError("SAP isql returned an invalid catalog frame.")
            begun = True
        elif line == "~NTEND~":
            if not begun or ended:
                raise AseClientError("SAP isql returned an invalid catalog frame.")
            ended = True
        elif line.startswith("~NT~"):
            if not begun or ended or not line.endswith("|~END~"):
                raise AseClientError("SAP isql returned a truncated catalog frame.")
            values = line[4:-6].split("|")
            tag, fields = values[0], values[1:]
            if tag not in allowed or len(fields) != _FRAME_COUNTS[tag]:
                raise AseClientError("SAP isql returned an invalid catalog frame.")
            records[tag].append([_unescape(field) for field in fields])
        elif line:
            if _has_error(line):
                raise AseClientError("SAP isql reported an ASE catalog error.")
            # Nocount + headers disabled means there are no legitimate table
            # headings or row summaries between our markers. Reject corruption.
            raise AseClientError("SAP isql returned unexpected catalog output.")
    if not begun or not ended:
        raise AseClientError("SAP isql did not return a complete catalog result.")
    return records


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


def _catalog(connection, queries, allowed, clients, register=None):
    with tempfile.TemporaryDirectory(prefix="nice-tools-isql-") as directory:
        folder = Path(directory)
        ini_path = _write_interfaces(folder, connection)
        query_path, output_path = folder / "query.sql", folder / "result.txt"
        sql = ("SET QUOTED_IDENTIFIER ON\nSET NOCOUNT ON\ngo\nSELECT '~NTBEGIN~'\n" +
               _select("V", ("@@version",)) + "\n" + "\n".join(queries) + "\nSELECT '~NTEND~'\ngo\nexit\n")
        query_path.write_text(sql, encoding="utf-8")
        argv = [clients["isql_path"], *_common_args(connection, ini_path, "utf8"),
                "-l", str(connection["login_timeout"]), "-t", str(connection["query_timeout"]),
                "-b", "-n", "-m", "11", "-w", "2048", "--retserverror", "-i", str(query_path), "-o", str(output_path)]
        console = _CopyOutput()
        result = _run_client(argv, connection["password"], input_text="",
                             timeout=connection["login_timeout"] + connection["query_timeout"] + 10,
                             register=register, _parse_output=console.feed)
        console.finish()
        if result.get("returncode") != 0 or console.failed:
            raise AseClientError("SAP isql could not connect or complete the catalog query.")
        try:
            text = output_path.read_text(encoding="utf-8-sig", errors="strict")
        except (OSError, UnicodeError):
            raise AseClientError("SAP isql did not return a valid UTF-8 catalog result.") from None
        rows = _parse_catalog(text, allowed | {"V"})
        versions = rows.pop("V")
        if len(versions) != 1 or not any(name in versions[0][0].lower() for name in ("adaptive server", "sap ase")):
            raise AseClientError("This connection does not identify itself as SAP Adaptive Server Enterprise.")
        return rows, _elapsed(result)


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
        result = _run_client(argv, connection["password"], input_text="", timeout=None,
                             _parse_output=output.feed, register=register)
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
    """Run one catalog or transfer operation using installed SAP executables."""
    if not isinstance(payload, dict):
        raise AseClientError("The ASE operation is invalid.")
    operation = payload.get("operation")
    if operation not in ("databases", "owners", "objects", "columns", "transfer"):
        raise AseClientError("The ASE operation is not supported.")
    connection = _connection(payload.get("connection"))
    obj = options = expected = database = owner_id = None
    search, kind = "", "all"
    if operation in ("owners", "objects"):
        database = payload.get("database")
        _identifier(database)
        if operation == "objects":
            owner_id = payload.get("owner_id")
            if owner_id is not None:
                if isinstance(owner_id, bool) or not isinstance(owner_id, int):
                    raise AseClientError("The owner id must be an integer.")
                owner_id = _id(owner_id, "owner id", minimum=1)
            search = payload.get("search", "")
            if not isinstance(search, str) or any(c in search for c in "\0\r\n\x1b") or len(search) > 255:
                raise AseClientError("The object search must be one line up to 255 characters.")
            kind = payload.get("kind", "all")
            if kind not in ("all", "U", "V", "P"):
                raise AseClientError("The object kind is not supported.")
    if operation in ("columns", "transfer"):
        obj = _object(payload.get("object"))
    if operation == "transfer":
        options = _options(payload.get("options", {}))
        if obj["kind"] == "P" or (obj["kind"] == "V" and options["direction"] == "in"):
            raise AseClientError("BCP IN requires a table; BCP OUT supports tables and views.")
        raw_columns = payload.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            raise AseClientError("Review the complete column schema before bulk copy.")
        expected = [_column(column) for column in raw_columns]
        if options["direction"] == "in" and any(c["type_id"] == 80 or c["type_name"].lower() == "timestamp" for c in expected):
            raise AseClientError("Timestamp columns are not supported for BCP IN.")
        path = payload.get("path")
        if not isinstance(path, str) or not path or "\0" in path:
            raise AseClientError("The selected file path is invalid.")
        _target(obj)
    clients = _client_config()
    if operation == "databases":
        rows, elapsed = _catalog(connection, [_select("D", ("dbid", "name")) + "FROM master.dbo.sysdatabases ORDER BY name"], {"D"}, clients, register)
        return {"databases": [{"id": _id(row[0]), "name": row[1]} for row in rows["D"]], "timings": {"client_seconds": elapsed}}
    if operation == "owners":
        db = _identifier(database)
        query = _select("W", ("u.uid", "u.name"))
        query += f"FROM {db}.dbo.sysusers u JOIN {db}.dbo.sysobjects o ON o.uid = u.uid WHERE o.type IN ('U', 'V', 'P') GROUP BY u.uid, u.name ORDER BY u.name"
        rows, elapsed = _catalog(connection, [query], {"W"}, clients, register)
        return {"owners": [{"id": _id(row[0]), "name": row[1]} for row in rows["W"]], "timings": {"client_seconds": elapsed}}
    if operation == "objects":
        rows, elapsed = _catalog(connection, [_objects_query(database, owner_id, search=search, kind=kind)], {"O"}, clients, register)
        return {"objects": [_object_row(row, database) for row in rows["O"]], "timings": {"client_seconds": elapsed}}
    queries = [_objects_query(obj["database"], obj["owner_id"], obj["id"])]
    if obj["kind"] != "P":
        queries.append(_columns_query(obj))
    rows, elapsed = _catalog(connection, queries, {"O", "C"}, clients, register)
    if len(rows["O"]) != 1 or _object_row(rows["O"][0], obj["database"]) != obj:
        raise AseClientError("The selected object changed or is unavailable. Reload its metadata.")
    columns = [_column(dict(zip(_COLUMN_KEYS, row))) for row in rows["C"]]
    if operation == "columns":
        return {"object": obj, "columns": columns, "timings": {"client_seconds": elapsed}}
    if columns != expected:
        raise AseClientError("The reviewed column schema changed. Review the file again before bulk copy.")
    return _copy(connection, obj, columns, payload["path"], options, clients, emit, register)
