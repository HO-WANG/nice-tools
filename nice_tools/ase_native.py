"""Process-isolated ASE catalogs and character BCP through bundled FreeTDS.

The parent worker owns file review, cancellation and atomic export publication.
DB-Library callbacks and configuration are global, so operations are serialized
here as well as isolated by that worker. No credentials enter a command line,
configuration file, diagnostic message or emitted event.
"""

from contextlib import contextmanager
import ctypes as ct
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading


class AseNativeError(ValueError):
    """A sanitized error suitable for the local application's response."""


ASEError = AseNativeError
_LOCK = threading.Lock()
_EXPECTED_VERSION = "1.5.19"
_ENCODINGS = {"utf-8": "UTF-8", "cp949": "CP949", "euc-kr": "EUC-KR"}
_COLUMN_KEYS = ("position", "name", "type_id", "type_name", "length", "precision", "scale", "nullable", "identity")
_ERROR_CALLBACK = ct.CFUNCTYPE(ct.c_int, ct.c_void_p, ct.c_int, ct.c_int, ct.c_int, ct.c_char_p, ct.c_char_p)
_MESSAGE_CALLBACK = ct.CFUNCTYPE(ct.c_int, ct.c_void_p, ct.c_int32, ct.c_int, ct.c_int, ct.c_char_p, ct.c_char_p, ct.c_char_p, ct.c_int)


def _native_candidates():
    override = os.environ.get("NICE_TOOLS_ASE_NATIVE")
    if override and os.name != "nt":
        return [Path(override)]
    package = Path(__file__).resolve().parent
    return [package / "native" / "sybdb.dll", package.parent / "build" / "ase-native" / "sybdb.dll"]


def _verify_manifest(path):
    # Linux's explicit override is for developer verification, never a release.
    if os.name != "nt" and os.environ.get("NICE_TOOLS_ASE_NATIVE"):
        return
    try:
        manifest = json.loads((path.parent / "native-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1 or manifest.get("freetds_version") != _EXPECTED_VERSION:
            raise ValueError
        if manifest.get("patch_revision", 0) < 1:
            raise ValueError
        files = manifest["files"]
        if path.name not in files or not isinstance(files, dict):
            raise ValueError
        for name, digest in files.items():
            if Path(name).name != name or not isinstance(digest, str):
                raise ValueError
            hasher = hashlib.sha256()
            with (path.parent / name).open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            if hasher.hexdigest() != digest:
                raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise AseNativeError("The bundled ASE library is missing its verified build manifest or has changed.") from None


def _bind(lib):
    pointer, integer, string, int32 = ct.c_void_p, ct.c_int, ct.c_char_p, ct.c_int32
    signatures = {
        "dbversion": (string, []), "dbinit": (integer, []), "dbexit": (None, []),
        "dblogin": (pointer, []), "dbloginfree": (None, [pointer]),
        "dbsetlname": (integer, [pointer, string, integer]),
        "dbsetlbool": (integer, [pointer, integer, integer]),
        "dbsetlversion": (integer, [pointer, ct.c_ubyte]),
        "dbsetlogintime": (integer, [integer]), "dbsettime": (integer, [integer]),
        "tdsdbopen": (pointer, [pointer, string, integer]), "dbclose": (None, [pointer]),
        "dbtds": (integer, [pointer]), "dbcmd": (integer, [pointer, string]),
        "dbsqlexec": (integer, [pointer]), "dbresults": (integer, [pointer]),
        "dbnextrow": (integer, [pointer]), "dbnumcols": (integer, [pointer]),
        "dbcolname": (string, [pointer, integer]), "dbcoltype": (integer, [pointer, integer]),
        "dbdata": (pointer, [pointer, integer]), "dbdatlen": (int32, [pointer, integer]),
        "dbcollen": (int32, [pointer, integer]),
        "dbconvert": (int32, [pointer, integer, pointer, int32, integer, pointer, int32]),
        "dberrhandle": (pointer, [_ERROR_CALLBACK]), "dbmsghandle": (pointer, [_MESSAGE_CALLBACK]),
        "bcp_init": (integer, [pointer, string, string, string, integer]),
        "bcp_columns": (integer, [pointer, integer]),
        "bcp_control": (integer, [pointer, integer, int32]),
        "bcp_colfmt": (integer, [pointer, integer, integer, integer, int32, pointer, integer, integer]),
        "bcp_exec": (integer, [pointer, ct.POINTER(int32)]),
    }
    try:
        for name, (restype, argtypes) in signatures.items():
            function = getattr(lib, name)
            function.restype, function.argtypes = restype, argtypes
    except AttributeError:
        raise AseNativeError("The bundled ASE library does not expose the required Sybase DB-Library API.") from None
    return lib


def _load_native():
    for path in _native_candidates():
        if not path.is_file():
            continue
        _verify_manifest(path)
        try:
            # CDLL uses cdecl, including on Windows; WinDLL uses the wrong ABI.
            with os.add_dll_directory(str(path.parent)) if os.name == "nt" else _null_context():
                lib = _bind(ct.CDLL(str(path)))
            version = (lib.dbversion() or b"").decode("ascii", errors="replace")
            if not re.search(r"(?<![0-9.])1\.5\.19(?![0-9.])", version):
                raise AseNativeError("The bundled ASE library has an unsupported version.")
            return lib
        except OSError:
            raise AseNativeError("The bundled ASE library or one of its runtime dependencies is unavailable.") from None
    raise AseNativeError("The bundled ASE library is missing. Use a Windows build that includes ASE support.")


@contextmanager
def _null_context():
    yield


def native_status():
    try:
        lib = _load_native()
        return {"available": True, "version": lib.dbversion().decode("ascii", errors="replace"), "detail": "Bundled FreeTDS character BCP is available."}
    except AseNativeError as exc:
        return {"available": False, "version": None, "detail": str(exc)}


def _identifier(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise AseNativeError("A database, owner or object name is invalid.")
    return "[" + value.replace("]", "]]") + "]"


def _id(value, label="id", minimum=0, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AseNativeError(f"The {label} must be an integer.")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        raise AseNativeError(f"The {label} must be an integer.")
    value = int(value)
    if not minimum <= value <= maximum:
        raise AseNativeError(f"The {label} integer is outside the supported range.")
    return value


def _flag(value):
    if value in (False, 0, "0"):
        return False
    if value in (True, 1, "1"):
        return True
    raise AseNativeError("ASE returned invalid column flags.")


def _object(value):
    if not isinstance(value, dict):
        raise AseNativeError("Select an ASE object before continuing.")
    for key in ("database", "owner", "name"):
        _identifier(value.get(key))
    kind = value.get("kind")
    if kind not in ("U", "V", "P"):
        raise AseNativeError("This object kind is not supported.")
    return {"database": value["database"], "owner": value["owner"],
            "owner_id": _id(value.get("owner_id"), "owner id"),
            "id": _id(value.get("id"), "object id", minimum=1), "name": value["name"], "kind": kind}


def _object_row(row, database):
    return _object({"database": database, "owner": row.get("owner_name", row.get("owner")),
                    "owner_id": row.get("owner_id", row.get("uid")), "id": row["id"],
                    "name": row.get("object_name", row.get("name")), "kind": str(row["kind"]).rstrip()})


def _column(row):
    try:
        result = {key: row[key] for key in _COLUMN_KEYS}
        for key in ("position", "type_id", "length", "precision", "scale"):
            result[key] = _id(result[key] if result[key] is not None else 0, f"column {key}")
        result["nullable"], result["identity"] = _flag(result["nullable"]), _flag(result["identity"])
        if result["position"] < 1:
            raise ValueError
        for key in ("name", "type_name"):
            _identifier(result[key])
        return result
    except (KeyError, TypeError, ValueError):
        raise AseNativeError("The reviewed column schema is invalid or incomplete.") from None


def _options(raw):
    if not isinstance(raw, dict):
        raise AseNativeError("BCP options are invalid.")
    direction = str(raw.get("direction", "")).lower()
    encoding = raw.get("encoding", "utf-8")
    if direction not in ("in", "out") or encoding not in _ENCODINGS:
        raise AseNativeError("Choose a valid BCP direction and encoding.")
    try:
        field, row = bytes.fromhex(raw.get("field_hex", "09")), bytes.fromhex(raw.get("row_hex", "0a"))
    except (TypeError, ValueError):
        raise AseNativeError("BCP terminators must be hexadecimal bytes.") from None
    if not field or not row:
        raise AseNativeError("BCP terminators cannot be empty.")
    keep = raw.get("keep_identity", False)
    if not isinstance(keep, bool):
        raise AseNativeError("The identity option must be true or false.")
    return {"direction": direction, "encoding": encoding, "field_hex": field.hex(), "row_hex": row.hex(),
            "batch_size": _id(raw.get("batch_size", 1000), "batch size", minimum=1), "keep_identity": keep}


def _connection(raw):
    if not isinstance(raw, dict):
        raise AseNativeError("ASE connection settings are invalid.")
    host = raw.get("host")
    if not isinstance(host, str) or not host or any(c in host for c in "\r\n\0[]#;="):
        raise AseNativeError("The ASE host is invalid.")
    for key in ("user", "password"):
        value = raw.get(key)
        if not isinstance(value, str) or "\0" in value or (key == "user" and not value):
            raise AseNativeError("The ASE login settings are invalid.")
    return {"host": host, "port": _id(raw.get("port", 5000), "port", 1, 65535),
            "user": raw["user"], "password": raw["password"],
            "login_timeout": _id(raw.get("login_timeout", 15), "login timeout", 1),
            "query_timeout": _id(raw.get("query_timeout", 60), "query timeout", 1)}


class _Diagnostics:
    def __init__(self, emit=None, batch_size=1000):
        self.emit = emit
        self.batch_size = batch_size
        self.committed = 0
        self.errors = []
        self.error_handler = _ERROR_CALLBACK(self.error_callback)
        self.message_handler = _MESSAGE_CALLBACK(self.message_callback)

    def event(self, event):
        if self.emit is not None:
            try:
                self.emit(event)
            except Exception:
                # Never let a Python exception cross a native callback boundary.
                self.errors.append("Progress reporting failed.")

    def error_callback(self, _db, severity, code, _oserr, _message, _osmessage):
        if code == 20068:  # SYBEBBCI: a completed bulk-copy batch.
            self.committed += self.batch_size
            self.event({"type": "progress", "phase": "copying", "rows": self.committed})
        else:
            message = f"DB-Library error {code} (severity {severity})."
            self.errors.append(message)
            self.event({"type": "log", "level": "error", "message": message})
        return 2  # INT_CANCEL

    def message_callback(self, _db, code, _state, severity, _message, _server, _procedure, _line):
        if code in (5701, 5703) and severity <= 10:
            return 0
        if severity >= 11:
            message = f"ASE error {code} (severity {severity})."
            self.errors.append(message)
            self.event({"type": "log", "level": "error", "message": message})
        return 0

    def check(self, action, result=1):
        if self.errors:
            raise AseNativeError(self.errors[-1])
        if result != 1:
            raise AseNativeError(f"{action} failed in the ASE native library.")


@contextmanager
def _configuration(connection, encoding):
    keys = ("FREETDSCONF", "TDSVER", "TDSHOST", "TDSPORT", "TDSQUERY", "DSQUERY", "TDSDUMP", "TDSDUMPCONFIG", "FREEBCP_DATEFMT")
    previous = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory(prefix="nice-tools-ase-") as directory:
        config = Path(directory) / "freetds.conf"
        config.write_text(
            "[nice_tools_ase]\n"
            f"host = {connection['host']}\nport = {connection['port']}\n"
            f"tds version = 5.0\nclient charset = {_ENCODINGS[encoding]}\n"
            "text size = 2147483647\ndebug flags = 0\n", encoding="utf-8",
        )
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ["FREETDSCONF"] = str(config)
            os.environ["TDSVER"] = "5.0"
            os.environ["FREEBCP_DATEFMT"] = "%Y-%m-%d %H:%M:%S.%z"
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class _Session:
    def __init__(self, connection, encoding="utf-8", emit=None, batch_size=1000):
        self.connection, self.encoding = connection, encoding
        self.lib = _load_native()
        self.diagnostics = _Diagnostics(emit, batch_size)
        self.db = self.login = None
        self.initialized = False
        self.config = None
        self.old_error = self.old_message = None

    def _checked(self, action, function, *arguments):
        result = function(*arguments)
        self.diagnostics.check(action, result)
        return result

    def _encode(self, value):
        try:
            return value.encode(self.encoding)
        except UnicodeError:
            raise AseNativeError("Login or object text cannot be represented in the selected encoding.") from None

    def __enter__(self):
        self.config = _configuration(self.connection, self.encoding)
        self.config.__enter__()
        try:
            self.old_error = self.lib.dberrhandle(self.diagnostics.error_handler)
            self.old_message = self.lib.dbmsghandle(self.diagnostics.message_handler)
            self._checked("Initialize DB-Library", self.lib.dbinit)
            self.initialized = True
            # FreeTDS dbinit replaces the error handler with its default, which
            # can terminate a process. Reinstall ours before any login/API work.
            self.lib.dberrhandle(self.diagnostics.error_handler)
            self.lib.dbmsghandle(self.diagnostics.message_handler)
            self._checked("Set login timeout", self.lib.dbsetlogintime, self.connection["login_timeout"])
            self._checked("Set query timeout", self.lib.dbsettime, self.connection["query_timeout"])
            self.login = self.lib.dblogin()
            if not self.login:
                raise AseNativeError("ASE could not allocate its login settings.")
            for value, setting in ((self.connection["user"], 2), (self.connection["password"], 3), ("nice-tools", 5), (_ENCODINGS[self.encoding], 10)):
                self._checked("Configure ASE login", self.lib.dbsetlname, self.login, self._encode(value), setting)
            self._checked("Enable ASE bulk copy", self.lib.dbsetlbool, self.login, 1, 6)
            self._checked("Select ASE protocol", self.lib.dbsetlversion, self.login, 2)  # DBVERSION_100 => TDS 5.0
            self.diagnostics.event({"type": "status", "phase": "connecting"})
            self.db = self.lib.tdsdbopen(self.login, b"nice_tools_ase", 0)  # Sybase ABI, not Microsoft's.
            self.diagnostics.check("Connect to ASE", 1 if self.db else 0)
            if self.lib.dbtds(self.db) != 7:  # DBTDS_5_0
                raise AseNativeError("The server did not negotiate the ASE TDS 5.0 protocol.")
            self.query("SET QUOTED_IDENTIFIER ON SET NOCOUNT ON SET TEXTSIZE 2147483647")
            version = self.query("SELECT @@version AS server_version")
            server_version = str(version[0].get("server_version", "")) if version else ""
            if "adaptive server" not in server_version.lower() and "sap ase" not in server_version.lower():
                raise AseNativeError("This connection does not identify itself as SAP Adaptive Server Enterprise.")
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            if self.db:
                self.lib.dbclose(self.db)
                self.db = None
            if self.login:
                self.lib.dbloginfree(self.login)
                self.login = None
            if self.initialized:
                self.lib.dbexit()
                self.initialized = False
        finally:
            if self.old_error is not None:
                self.lib.dberrhandle(ct.cast(self.old_error, _ERROR_CALLBACK))
            if self.old_message is not None:
                self.lib.dbmsghandle(ct.cast(self.old_message, _MESSAGE_CALLBACK))
            else:
                self.lib.dbmsghandle(_MESSAGE_CALLBACK())
            if self.config is not None:
                self.config.__exit__(None, None, None)
                self.config = None

    def __exit__(self, *_args):
        self.close()

    def query(self, sql):
        self._checked("Prepare catalog query", self.lib.dbcmd, self.db, self._encode(sql))
        self._checked("Execute catalog query", self.lib.dbsqlexec, self.db)
        rows = []
        while True:
            result = self.lib.dbresults(self.db)
            self.diagnostics.check("Read catalog results", 1 if result in (1, 2) else 0)
            if result == 2:  # NO_MORE_RESULTS
                return rows
            count = self.lib.dbnumcols(self.db)
            names = [(self.lib.dbcolname(self.db, index) or b"").decode(self.encoding) for index in range(1, count + 1)]
            while True:
                row = self.lib.dbnextrow(self.db)
                self.diagnostics.check("Read catalog row", 1 if row in (-1, -2) else 0)
                if row == -2:  # NO_MORE_ROWS
                    break
                rows.append({name: self._cell(index) for index, name in enumerate(names, 1)})

    def _cell(self, index):
        address = self.lib.dbdata(self.db, index)
        length = self.lib.dbdatlen(self.db, index)
        if not address:
            return None
        kind = self.lib.dbcoltype(self.db, index)
        if kind in (39, 47):  # varchar/char catalog names; preserve actual bytes.
            return ct.string_at(address, length).decode(self.encoding)
        capacity = max(length * 4 + 256, 256)
        buffer = ct.create_string_buffer(capacity)
        converted = self.lib.dbconvert(self.db, kind, address, length, 47, buffer, capacity - 1)
        self.diagnostics.check("Convert catalog value", 1 if converted >= 0 else 0)
        return buffer.raw[:converted].decode(self.encoding).rstrip(" ")

    def bulk_copy(self, obj, columns, path, options):
        if options["direction"] == "in" and not Path(path).is_file():
            raise AseNativeError("The reviewed import file is no longer available.")
        # ASE BCP's legacy client limit applies to owner.object. Selecting the
        # already verified database avoids counting its name against that limit.
        self.query("USE " + _identifier(obj["database"]))
        target = ".".join(_identifier(obj[key]) for key in ("owner", "name"))
        if len(self._encode(target)) > 92:
            raise AseNativeError("This object's qualified name exceeds the bundled ASE BCP identifier support.")
        direction = 1 if options["direction"] == "in" else 2
        self.diagnostics.event({"type": "status", "phase": "preparing"})
        self._checked("Prepare bulk copy", self.lib.bcp_init, self.db, self._encode(target), path.encode("utf-8"), None, direction)
        self._checked("Configure host columns", self.lib.bcp_columns, self.db, len(columns))
        # Upstream BCPMAXERRS=0 resets the allowance to ten, whereas one stops
        # at the first rejected row. The bundled patches also propagate failure.
        for setting, value in ((1, 1), (4, options["batch_size"]), (8, int(options["keep_identity"]))):
            self._checked("Configure bulk copy", self.lib.bcp_control, self.db, setting, value)
        buffers = []
        for position in range(1, len(columns) + 1):
            terminator = bytes.fromhex(options["row_hex"] if position == len(columns) else options["field_hex"])
            buffer = ct.create_string_buffer(terminator)
            buffers.append(buffer)
            self._checked("Configure character column", self.lib.bcp_colfmt, self.db, position, 47, 0, -1, buffer, len(terminator), position)
        if direction == 1 and options["keep_identity"]:
            self.query("SET IDENTITY_INSERT " + target + " ON")
        self.diagnostics.event({"type": "status", "phase": "copying"})
        rows = ct.c_int32()
        self._checked("Bulk copy", self.lib.bcp_exec, self.db, ct.byref(rows))
        if rows.value < 0:
            raise AseNativeError("The native bulk-copy row count exceeded its supported range.")
        return {"rows": rows.value, "direction": options["direction"]}


def _objects_query(database, owner_id, object_id=None):
    db = _identifier(database)
    predicate = f"o.uid = {owner_id}" + (f" AND o.id = {object_id}" if object_id is not None else "")
    return (f"SELECT o.id, o.uid AS owner_id, u.name AS owner_name, o.name AS object_name, rtrim(o.type) AS kind "
            f"FROM {db}.dbo.sysobjects o JOIN {db}.dbo.sysusers u ON u.uid = o.uid "
            f"WHERE {predicate} AND o.type IN ('U', 'V', 'P') ORDER BY o.type, o.name")


def _verified_object(session, obj):
    observed = session.query(_objects_query(obj["database"], obj["owner_id"], obj["id"]))
    if len(observed) != 1 or _object_row(observed[0], obj["database"]) != obj:
        raise AseNativeError("The selected object changed or is unavailable. Reload its metadata.")
    return obj


def _columns(session, obj):
    db = _identifier(obj["database"])
    rows = session.query(
        "SELECT c.colid AS [position], c.name, c.type AS type_id, t.name AS type_name, "
        "c.length, c.prec AS [precision], c.scale, "
        "CASE WHEN (c.status & 8) <> 0 THEN 1 ELSE 0 END AS [nullable], "
        "CASE WHEN (c.status & 128) <> 0 THEN 1 ELSE 0 END AS [identity] "
        f"FROM {db}.dbo.syscolumns c JOIN {db}.dbo.systypes t ON t.usertype = c.usertype "
        f"JOIN {db}.dbo.sysobjects o ON o.id = c.id WHERE o.id = {obj['id']} AND o.uid = {obj['owner_id']} "
        "AND o.type IN ('U', 'V') ORDER BY c.colid"
    )
    return [_column(row) for row in rows]


def run_operation(payload, emit=None):
    if not isinstance(payload, dict):
        raise AseNativeError("The ASE operation is invalid.")
    operation = payload.get("operation")
    if operation not in ("databases", "owners", "objects", "columns", "transfer"):
        raise AseNativeError("The ASE operation is not supported.")
    connection = _connection(payload.get("connection"))
    obj, options, expected, database, owner_id = None, None, None, None, None
    if operation in ("owners", "objects"):
        database = payload.get("database")
        _identifier(database)
        if operation == "objects":
            owner_id = _id(payload.get("owner_id"), "owner id")
    if operation in ("columns", "transfer"):
        obj = _object(payload.get("object"))
    if operation == "transfer":
        options = _options(payload.get("options", {}))
        if obj["kind"] == "P" or (obj["kind"] == "V" and options["direction"] == "in"):
            raise AseNativeError("BCP IN requires a table; BCP OUT supports tables and views.")
        raw_columns = payload.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            raise AseNativeError("Review the complete column schema before bulk copy.")
        expected = [_column(column) for column in raw_columns]
        if options["direction"] == "in" and any(c["type_id"] == 80 or c["type_name"].lower() == "timestamp" for c in expected):
            raise AseNativeError("Timestamp columns are not supported for BCP IN.")
        path = payload.get("path")
        if not isinstance(path, str) or not path or "\0" in path:
            raise AseNativeError("The selected file path is invalid.")
    encoding = options["encoding"] if options else "utf-8"
    with _LOCK, _Session(connection, encoding, emit, options["batch_size"] if options else 1000) as session:
        if operation == "databases":
            rows = session.query("SELECT dbid AS id, name FROM master.dbo.sysdatabases ORDER BY name")
            return {"databases": [{"id": _id(row["id"]), "name": row["name"]} for row in rows]}
        if operation == "owners":
            db = _identifier(database)
            rows = session.query(f"SELECT DISTINCT u.uid AS id, u.name FROM {db}.dbo.sysusers u JOIN {db}.dbo.sysobjects o ON o.uid = u.uid WHERE o.type IN ('U', 'V', 'P') ORDER BY u.name")
            return {"owners": [{"id": _id(row["id"]), "name": row["name"]} for row in rows]}
        if operation == "objects":
            return {"objects": [_object_row(row, database) for row in session.query(_objects_query(database, owner_id))]}
        obj = _verified_object(session, obj)
        columns = _columns(session, obj) if obj["kind"] != "P" else []
        if operation == "columns":
            return {"object": obj, "columns": columns}
        if columns != expected:
            raise AseNativeError("The reviewed column schema changed. Review the file again before bulk copy.")
        return session.bulk_copy(obj, columns, payload["path"], options)
