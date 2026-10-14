"""Persistent, serialized ASE catalog connections through installed SAP ODBC.

Connection strings stay in memory. Only registered SAP/Sybase ASE drivers are
eligible; driver exception text never reaches the application's responses.
"""

from __future__ import annotations

import ipaddress
import re
import threading
import time


class AseOdbcError(ValueError):
    """A secret-free error suitable for the local application's response."""

    def __init__(self, message, *, stage=None, sqlstate=None, action=None, cancelled=False):
        self.stage = stage
        self.sqlstate = sqlstate
        self.cancelled = cancelled
        if stage is not None:
            context = "단계: " + stage
            if sqlstate is not None:
                context += " · SQLSTATE: " + sqlstate
            if action is not None:
                context += " · " + action
            message = "[" + context + "] " + message
        super().__init__(message)


_COLUMN_KEYS = ("position", "name", "type_id", "type_name", "length", "precision", "scale", "nullable", "identity")
_CATALOGS = {"databases", "owners", "objects", "columns"}
_SAFE_SQLSTATES = frozenset({"08001", "08003", "08004", "08006", "08S01", "28000", "HYT00", "HYT01",
                            "IM002", "IM003", "IM004", "IM005", "IM006", "IM014", "HY000", "HY010", "HY090",
                            "HY092", "HYC00", "42000", "42S02", "42S22", "40001", "07002", "07009",
                            "22001", "22003", "22018"})
_STAGE_MESSAGES = {
    "driver-resolution": "SAP ASE ODBC 드라이버를 확인하지 못했습니다. 64비트 드라이버 설치와 설정을 확인하세요.",
    "connect": "ASE ODBC 접속에 실패했습니다. 서버 연결 설정을 확인하세요.",
    "connection-timeout-setting": "DB 연결 후 ODBC 시간 제한 속성을 설정하지 못했습니다.",
    "startup": "DB 연결 후 ODBC 커서 또는 세션 초기화를 완료하지 못했습니다.",
    "ASE-identity": "DB 연결 후 ASE 서버 확인을 완료하지 못했습니다.",
    "databases": "DB 연결 후 데이터베이스 목록을 조회하지 못했습니다.",
    "query": "ASE ODBC 조회를 완료하지 못했습니다.",
}


def _sqlstate(error, library):
    error_type = getattr(library, "Error", None)
    if not isinstance(error_type, type) or not issubclass(error_type, BaseException) or not isinstance(error, error_type):
        return None
    arguments = error.args
    state = arguments[0] if arguments else None
    if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state) and state in _SAFE_SQLSTATES:
        return state
    return None


def _diagnostic(stage, error, library=None, action=None):
    # Never format, search or copy the driver's raw message or connection string.
    # Only a known SQLSTATE in the documented first typed-error argument is safe.
    state = _sqlstate(error, library)
    message = _STAGE_MESSAGES[stage]
    if state in ("08001", "08004", "08006", "08S01", "08003"):
        message += " 서버 주소·포트, 네트워크 경로 또는 연결 협상에 문제가 있을 수 있습니다."
    elif state == "28000":
        message += " 인증이 거부되었을 수 있습니다. DB 사용자·비밀번호와 계정 상태를 확인하세요."
    elif state in ("HYT00", "HYT01"):
        message += " 연결 또는 조회 제한 시간이 지났을 수 있습니다. 네트워크와 서버 응답을 확인하세요."
    elif state in ("IM002", "IM003", "IM004", "IM005", "IM006", "IM014"):
        message += " ODBC 드라이버 등록과 64비트 설치 상태를 확인하세요."
    elif state in ("HY092", "HYC00"):
        message += " 드라이버가 시간 제한 등 요청한 연결 속성 또는 기능을 지원하지 않을 수 있습니다."
    elif state in ("42000", "42S02", "42S22"):
        message += " SQL 실행, 객체 존재 여부 또는 조회 권한에 문제가 있을 수 있습니다."
    return AseOdbcError(message, stage=stage, sqlstate=state, action=action)


def _pyodbc():
    import pyodbc
    # Set this before drivers() can allocate the ODBC environment. A live
    # OdbcSession already reuses its connection; close must release it rather
    # than retain authenticated connections and credentials in a native pool.
    pyodbc.pooling = False
    return pyodbc


def _odbc_settings():
    from .ase_profiles import odbc_settings
    return odbc_settings()


def _ase_driver(name):
    if not isinstance(name, str) or any(word in name.lower().replace(" ", "") for word in ("freetds", "sqlanywhere", "sqlserver")):
        return False
    return bool(re.search(r"adaptive\s*server\s*enterprise", name, re.I) or
                (re.search(r"\b(?:sap|sybase)\b", name, re.I) and re.search(r"\base\b", name, re.I)))


def _registered_drivers(library):
    try:
        drivers = library.drivers()
        if not isinstance(drivers, (list, tuple)) or any(not isinstance(driver, str) for driver in drivers):
            raise ValueError
        return list(dict.fromkeys(drivers))
    except Exception as error:
        raise _diagnostic("driver-resolution", error, library) from None


def _select_driver(drivers, configured=None):
    if configured is not None:
        if not isinstance(configured, str) or not configured or not _ase_driver(configured):
            raise AseOdbcError("SAP ASE용 64비트 ODBC 드라이버를 선택하세요.")
        matches = [name for name in drivers if name.casefold() == configured.casefold() and _ase_driver(name)]
        if not matches:
            raise AseOdbcError("설정한 SAP ASE 64비트 ODBC 드라이버가 설치되어 있지 않습니다.")
        return matches[0]
    eligible = [name for name in drivers if _ase_driver(name)]
    if not eligible:
        raise AseOdbcError("SAP ASE용 64비트 ODBC 드라이버가 필요합니다. SAP Open Client의 ODBC 드라이버를 설치하세요.")
    return next((name for name in eligible if name.casefold() == "adaptive server enterprise"), eligible[0])


def _valid_dsn_name(name):
    return (isinstance(name, str) and bool(name.strip()) and len(name) <= 255 and
            not any(ord(character) < 32 or ord(character) == 127 for character in name))


def _registered_data_sources(library, drivers):
    try:
        # pyodbc 5.3 dataSources() takes no arguments. It enumerates the current
        # process's user/system DSNs and therefore the application's bitness.
        sources = library.dataSources()
        if not isinstance(sources, dict):
            raise ValueError
    except Exception as error:
        raise AseOdbcError("등록된 64비트 ODBC DSN 목록을 읽지 못했습니다.", stage="driver-resolution",
                           sqlstate=_sqlstate(error, library)) from None
    eligible = {name.casefold(): name for name in drivers if _ase_driver(name)}
    result = []
    for name, description in sources.items():
        if not _valid_dsn_name(name) or not isinstance(description, str):
            continue
        driver = eligible.get(description.casefold())
        if driver is not None:
            result.append({"name": name, "driver": driver})
    return result


def _resolve_dsn(requested, sources):
    matches = [source for source in sources if source["name"].casefold() == requested.casefold()]
    if len(matches) != 1:
        raise AseOdbcError("입력한 DSN을 등록된 SAP ASE 64비트 ODBC DSN에서 확인할 수 없습니다. DSN 이름과 드라이버를 확인하세요.")
    return matches[0]


def odbc_status():
    result = {"available": False, "detail": "", "driver": None, "drivers": [], "data_sources": [],
              "module_available": False, "version": None}
    try:
        library = _pyodbc()
    except Exception:
        result["detail"] = "프로그램의 ODBC 모듈을 읽지 못했습니다. ODBC가 포함된 Windows EXE를 사용하세요."
        return result
    result.update(module_available=True, version=getattr(library, "version", None))
    try:
        result["drivers"] = _registered_drivers(library)
        result["driver"] = _select_driver(result["drivers"], _odbc_settings().get("driver"))
        result.update(available=True, detail="설치된 SAP ASE ODBC 드라이버로 접속합니다.")
    except AseOdbcError as error:
        result["detail"] = str(error)
    except Exception:
        result["detail"] = "ASE ODBC 드라이버 설정을 읽지 못했습니다."
    try:
        result["data_sources"] = _registered_data_sources(library, result["drivers"])
    except AseOdbcError as error:
        # DSN discovery is optional for a connection entered as host/port. A
        # failed list must not turn an otherwise available driver into an error.
        result["data_sources_detail"] = str(error)
    return result


status = odbc_status


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 255 or "\0" in value:
        raise AseOdbcError("A database, owner or object name is invalid.")
    return "[" + value.replace("]", "]]") + "]"


def _id(value, label="id", minimum=0, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or (isinstance(value, str) and not re.fullmatch(r"[0-9]+", value)):
        raise AseOdbcError(f"The {label} must be an integer.")
    value = int(value)
    if not minimum <= value <= maximum:
        raise AseOdbcError(f"The {label} integer is outside the supported range.")
    return value


def _flag(value):
    if value in (False, 0, "0"):
        return False
    if value in (True, 1, "1"):
        return True
    raise AseOdbcError("ASE returned invalid column flags.")


def _object(value):
    if not isinstance(value, dict):
        raise AseOdbcError("Select an ASE object before continuing.")
    for key in ("database", "owner", "name"):
        _identifier(value.get(key))
    if value.get("kind") not in ("U", "V", "P"):
        raise AseOdbcError("This object kind is not supported.")
    return {"database": value["database"], "owner": value["owner"], "name": value["name"], "kind": value["kind"],
            "owner_id": _id(value.get("owner_id"), "owner id", 1), "id": _id(value.get("id"), "object id", 1)}


def _object_row(row, database):
    return _object({"database": database, "id": row[0], "owner_id": row[1], "owner": row[2], "name": row[3], "kind": str(row[4]).rstrip()})


def _column(row):
    try:
        result = dict(zip(_COLUMN_KEYS, row, strict=True))
        for key in ("position", "type_id", "length", "precision", "scale"):
            result[key] = _id(result[key] if result[key] is not None else 0, f"column {key}")
        result["nullable"], result["identity"] = _flag(result["nullable"]), _flag(result["identity"])
        if result["position"] < 1:
            raise ValueError
        for key in ("name", "type_name"):
            _identifier(result[key])
        return result
    except (KeyError, TypeError, ValueError):
        raise AseOdbcError("The reviewed column schema is invalid or incomplete.") from None


def _connection(raw):
    if not isinstance(raw, dict):
        raise AseOdbcError("접속 정보를 입력하세요.")
    host = raw.get("host")
    if not isinstance(host, str) or not host or len(host) > 253:
        raise AseOdbcError("서버 주소를 입력하세요.")
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", host):
            raise AseOdbcError("서버 주소에는 IP 또는 호스트 이름을 입력하세요.") from None
    result = {"host": host, "port": _id(raw.get("port", 5000), "port", 1, 65535),
              "login_timeout": _id(raw.get("login_timeout", 15), "login timeout", 1),
              "query_timeout": _id(raw.get("query_timeout", 60), "query timeout", 1)}
    for key in ("user", "password"):
        value = raw.get(key)
        if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value) or (key == "user" and not value):
            raise AseOdbcError("사용자 또는 비밀번호가 올바르지 않습니다.")
        result[key] = value
    dsn = raw.get("dsn", "")
    if not isinstance(dsn, str) or len(dsn) > 255 or any(ord(character) < 32 or ord(character) == 127 for character in dsn):
        raise AseOdbcError("DSN 이름은 제어 문자를 제외한 255자 이내 문자열로 입력하세요.")
    if dsn.strip():
        result["dsn"] = dsn.strip()
    return result


def _braced(value):
    return "{" + value.replace("}", "}}") + "}"


def _connection_string(settings, driver, dsn=None):
    # pyodbc's **kwargs connection builder does not escape semicolons or braces.
    # Build the ODBC string explicitly; it is used only by SQLDriverConnect.
    prefix = f"DSN={_braced(dsn)};" if dsn is not None else f"DRIVER={_braced(driver)};"
    # Explicit endpoint attributes override the DSN's defaults so catalog and
    # BCP use the same entered server/port. Keep its other working attributes.
    suffix = "" if dsn is not None else "DATABASE={master};CHARSET=UTF8;"
    return (prefix + f"SERVER={_braced(settings['host'])};PORT={settings['port']};"
            f"UID={_braced(settings['user'])};PWD={_braced(settings['password'])};" + suffix)


def _request(payload):
    if not isinstance(payload, dict) or payload.get("operation") not in _CATALOGS:
        raise AseOdbcError("The ASE catalog operation is not supported.")
    operation = payload["operation"]
    database = owner_id = obj = None
    search, kind = "", "all"
    if operation in ("owners", "objects"):
        database = payload.get("database")
        _identifier(database)
    if operation == "objects":
        owner_id = payload.get("owner_id")
        if owner_id is not None:
            if isinstance(owner_id, bool) or not isinstance(owner_id, int):
                raise AseOdbcError("The owner id must be an integer.")
            owner_id = _id(owner_id, "owner id", 1)
        search = payload.get("search", "")
        if not isinstance(search, str) or "\0" in search or len(search) > 255:
            raise AseOdbcError("The object search must be text up to 255 characters.")
        kind = payload.get("kind", "all")
        if kind not in ("all", "U", "V", "P"):
            raise AseOdbcError("The object kind is not supported.")
    if operation == "columns":
        obj = _object(payload.get("object"))
    return operation, database, owner_id, search, kind, obj


def _objects_query(database, owner_id=None, object_id=None, search="", kind="all"):
    predicates, parameters = [], []
    if kind == "all":
        predicates.append("o.type IN ('U', 'V', 'P')")
    else:
        predicates.append("o.type = ?")
        parameters.append(kind)
    for value, expression in ((owner_id, "o.uid"), (object_id, "o.id")):
        if value is not None:
            predicates.append(expression + " = ?")
            parameters.append(value)
    if search:
        pattern = search.replace("!", "!!").replace("%", "!%").replace("_", "!_").replace("[", "![")
        predicates.append("lower(o.name) LIKE lower(?) ESCAPE '!'")
        parameters.append("%" + pattern + "%")
    query = ("SELECT o.id, o.uid, u.name, o.name, rtrim(o.type) "
             f"FROM {_identifier(database)}.dbo.sysobjects o JOIN {_identifier(database)}.dbo.sysusers u ON u.uid = o.uid "
             f"WHERE {' AND '.join(predicates)} ORDER BY o.type, u.name, o.name")
    return query, tuple(parameters)


class OdbcSession:
    def __init__(self, settings, driver=None):
        self._settings = _connection(settings)
        self._explicit_driver = driver
        self.driver = None
        self.login_seconds = 0.0
        self._initial_seconds = 0.0
        self._first_result = True
        self._connection = None
        self._library = None
        self._cursor = None
        self._closed = False
        self._generation = 0
        self._state_lock = threading.Lock()
        self._query_lock = threading.RLock()

    def _check(self, generation=None):
        with self._state_lock:
            if self._closed or (generation is not None and generation != self._generation):
                raise AseOdbcError("ODBC 작업이 취소되었습니다. 다시 접속하세요.", cancelled=True)
            return self._generation

    def open(self):
        with self._query_lock:
            generation = self._check()
            if self._connection is not None:
                return self
            started = time.monotonic()
            connection = None
            attached = False
            stage = "driver-resolution"
            library = None
            try:
                library = _pyodbc()
                self._library = library
                configured = self._explicit_driver if self._explicit_driver is not None else _odbc_settings().get("driver")
                settings = self._settings
                drivers = _registered_drivers(library)
                dsn = None
                if settings.get("dsn"):
                    source = _resolve_dsn(settings["dsn"], _registered_data_sources(library, drivers))
                    dsn, self.driver = source["name"], source["driver"]
                    if configured is not None and _select_driver(drivers, configured).casefold() != self.driver.casefold():
                        raise AseOdbcError("선택한 DSN의 드라이버와 설정한 ODBC 드라이버가 다릅니다. DSN 또는 드라이버 설정을 확인하세요.")
                else:
                    self.driver = _select_driver(drivers, configured)
                login_started = time.monotonic()
                stage = "connect"
                connection = library.connect(_connection_string(settings, self.driver, dsn), autocommit=True, timeout=settings["login_timeout"])
                self.login_seconds = max(0.0, time.monotonic() - login_started)
                self._check(generation)
                stage = "connection-timeout-setting"
                connection.timeout = settings["query_timeout"]
                with self._state_lock:
                    if self._closed or generation != self._generation:
                        raise AseOdbcError("ODBC 작업이 취소되었습니다. 다시 접속하세요.", cancelled=True)
                    self._connection = connection
                    attached = True
                stage = "startup"
                self._query("SET QUOTED_IDENTIFIER ON SET NOCOUNT ON", fields=None, stage=stage)
                stage = "ASE-identity"
                versions = self._query("SELECT @@version", fields=1, stage=stage)
                if len(versions) != 1 or not isinstance(versions[0][0], str) or not any(term in versions[0][0].lower() for term in ("adaptive server", "sap ase")):
                    raise AseOdbcError("This connection does not identify itself as SAP Adaptive Server Enterprise.")
                self._check(generation)
                self._initial_seconds = max(0.0, time.monotonic() - started)
                return self
            except AseOdbcError as error:
                self.close()
                if connection is not None and not attached:
                    self._close_connection(connection)
                if error.stage is not None or error.cancelled:
                    raise
                raise AseOdbcError(str(error), stage=stage) from None
            except Exception as error:
                self.close()
                if connection is not None and not attached:
                    self._close_connection(connection)
                raise _diagnostic(stage, error, library) from None
            finally:
                self._settings = None

    @staticmethod
    def _close_connection(connection):
        try:
            connection.close()
        except Exception:
            pass

    def _query(self, sql, parameters=(), fields=None, stage="query"):
        generation = self._check()
        connection = self._connection
        if connection is None:
            raise AseOdbcError("다시 접속하세요.")
        cursor = None
        action = "ODBC 커서 생성"
        try:
            cursor = connection.cursor()
            with self._state_lock:
                self._cursor = cursor
            self._check(generation)
            action = "SQL 실행"
            cursor.execute(sql, *parameters)
            if fields is None:
                rows = []
            else:
                action = "결과 읽기"
                rows = [tuple(row) for row in cursor.fetchall()]
                if any(len(row) != fields for row in rows):
                    raise AseOdbcError("ASE did not return valid catalog metadata.")
            self._check(generation)
            return rows
        except AseOdbcError:
            raise
        except Exception as error:
            self._check(generation)
            raise _diagnostic(stage, error, self._library, action) from None
        finally:
            with self._state_lock:
                if self._cursor is cursor:
                    self._cursor = None
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass

    def run_operation(self, payload, emit=None):
        operation, database, owner_id, search, kind, obj = _request(payload)
        with self._query_lock:
            generation = self._check()
            started = time.monotonic()
            if operation == "databases":
                rows = self._query("SELECT dbid, name FROM master.dbo.sysdatabases ORDER BY name", fields=2, stage="databases")
                result = {"databases": [{"id": _id(row[0]), "name": row[1]} for row in rows]}
            elif operation == "owners":
                db = _identifier(database)
                rows = self._query(f"SELECT DISTINCT u.uid, u.name FROM {db}.dbo.sysusers u JOIN {db}.dbo.sysobjects o ON o.uid = u.uid WHERE o.type IN ('U', 'V', 'P') ORDER BY u.name", fields=2)
                result = {"owners": [{"id": _id(row[0]), "name": row[1]} for row in rows]}
            elif operation == "objects":
                query, parameters = _objects_query(database, owner_id, search=search, kind=kind)
                rows = self._query(query, parameters, fields=5)
                result = {"objects": [_object_row(row, database) for row in rows]}
            else:
                query, parameters = _objects_query(obj["database"], obj["owner_id"], obj["id"])
                rows = self._query(query, parameters, fields=5)
                if len(rows) != 1 or _object_row(rows[0], obj["database"]) != obj:
                    raise AseOdbcError("The selected object changed or is unavailable. Reload its metadata.")
                columns = []
                if obj["kind"] != "P":
                    db = _identifier(obj["database"])
                    rows = self._query(
                        "SELECT c.colid, c.name, c.type, t.name, c.length, c.prec, c.scale, "
                        "CASE WHEN (c.status & 8) <> 0 THEN 1 ELSE 0 END, "
                        "CASE WHEN (c.status & 128) <> 0 THEN 1 ELSE 0 END "
                        f"FROM {db}.dbo.syscolumns c JOIN {db}.dbo.systypes t ON t.usertype = c.usertype "
                        f"JOIN {db}.dbo.sysobjects o ON o.id = c.id WHERE o.id = ? AND o.uid = ? "
                        "AND o.type IN ('U', 'V') ORDER BY c.colid", (obj["id"], obj["owner_id"]), fields=9)
                    columns = [_column(row) for row in rows]
                result = {"object": obj, "columns": columns}
            self._check(generation)
            elapsed = max(0.0, time.monotonic() - started)
            first = self._first_result
            self._first_result = False
            return {**result, "timings": {"login_seconds": self.login_seconds if first else 0.0,
                                          "query_seconds": elapsed,
                                          "total_seconds": elapsed + (self._initial_seconds if first else 0.0)}}

    def cancel(self):
        with self._state_lock:
            self._generation += 1
            cursor = self._cursor
        if cursor is not None:
            try:
                cursor.cancel()
            except Exception:
                pass

    def close(self):
        with self._state_lock:
            self._closed = True
            self._generation += 1
            cursor = self._cursor
        if cursor is not None:
            try:
                cursor.cancel()
            except Exception:
                pass
        with self._query_lock:
            with self._state_lock:
                connection = self._connection
                self._connection = None
                self._library = None
                self._settings = None
            if connection is not None:
                self._close_connection(connection)


def connect(settings, driver=None):
    return OdbcSession(settings, driver=driver).open()


def run_operation(payload, emit=None, register=None):
    _request(payload)
    session = connect(payload.get("connection"))
    try:
        return session.run_operation(payload, emit)
    finally:
        session.close()
