import ctypes as ct
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import socket
import time

import pytest


OBJECT = {
    "database": "연구]db", "owner": "other]owner", "owner_id": 7,
    "id": 42, "name": "order] history", "kind": "U",
}
COLUMNS = [{
    "position": 1, "name": "id", "type_id": 56, "type_name": "int",
    "length": 4, "precision": 0, "scale": 0,
    "nullable": False, "identity": False,
}]
CONNECTION = {"host": "example.internal", "port": 5000, "user": "reader", "password": "private-value"}


def native_module():
    try:
        return importlib.import_module("nice_tools.ase_native")
    except ImportError:
        pytest.fail("The native ASE bridge is not implemented")


class CatalogSession:
    instances = []
    observed_object = OBJECT
    observed_columns = COLUMNS

    def __init__(self, *args, **kwargs):
        self.queries = []
        self.transfers = []
        self.timings = {"native_load_seconds": 0.125, "login_seconds": 1.5, "setup_seconds": 0.75}
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def query(self, sql):
        self.queries.append(sql)
        if "sysdatabases" in sql:
            return [{"id": "5", "name": "연구]db"}]
        if "syscolumns" in sql:
            return [dict(c) for c in self.observed_columns]
        if "sysobjects" in sql:
            return [dict(self.observed_object)]
        return []

    def bulk_copy(self, obj, columns, path, options):
        self.transfers.append((obj, columns, path, options))
        return {"rows": 3, "direction": options["direction"]}


@pytest.fixture
def catalogs(monkeypatch):
    module = native_module()
    CatalogSession.instances = []
    CatalogSession.observed_object = dict(OBJECT)
    CatalogSession.observed_columns = [dict(c) for c in COLUMNS]
    monkeypatch.setattr(module, "_Session", CatalogSession)
    return module


def payload(operation, **changes):
    return {"operation": operation, "connection": dict(CONNECTION), "object": dict(OBJECT), **changes}


def test_missing_native_library_is_reported_without_install_instructions(monkeypatch, tmp_path):
    module = native_module()
    monkeypatch.setattr(module, "_native_candidates", lambda: [tmp_path / "missing.dll"])
    result = module.native_status()
    assert result["available"] is False
    assert "password" not in str(result).lower()
    assert "missing" in result["detail"].lower() or "unavailable" in result["detail"].lower()


def test_database_discovery_returns_catalog_ids_and_unicode_names(catalogs):
    assert catalogs.run_operation(payload("databases"))["databases"] == [{"id": 5, "name": "연구]db"}]


def test_column_discovery_quotes_database_and_scopes_by_owner_and_object_ids(catalogs):
    result = catalogs.run_operation(payload("columns"))
    assert result["object"] == OBJECT
    assert result["columns"] == COLUMNS
    sql = "\n".join(CatalogSession.instances[-1].queries)
    assert "[연구]]db]" in sql
    assert "o.uid = 7" in sql
    assert "o.id = 42" in sql
    assert CONNECTION["password"] not in sql


@pytest.mark.parametrize("value", ["7; DROP TABLE x", True, -1, 2147483648])
def test_owner_ids_cannot_be_interpreted_as_sql(catalogs, value):
    with pytest.raises(ValueError, match="(?i)(owner|id|integer)"):
        catalogs.run_operation(payload("objects", database="safe", owner_id=value))
    assert not CatalogSession.instances


@pytest.mark.parametrize("owner", [{}, {"owner_id": None}])
def test_object_search_without_owner_queries_all_owners(catalogs, owner):
    result = catalogs.run_operation(payload("objects", database="연구]db", **owner))
    assert result["objects"] == [OBJECT]
    sql = CatalogSession.instances[-1].queries[-1]
    assert "[연구]]db].dbo.sysobjects" in sql
    assert "u.uid = o.uid" in sql
    assert "WHERE o.uid =" not in sql
    assert "o.type IN ('U', 'V', 'P')" in sql
    assert "LIKE" not in sql


@pytest.mark.parametrize("kind", ["U", "V", "P"])
def test_object_search_filters_owner_and_kind_in_database(catalogs, kind):
    catalogs.run_operation(payload("objects", database="safe", owner_id=7, kind=kind))
    sql = CatalogSession.instances[-1].queries[-1]
    assert "o.uid = 7" in sql
    assert f"o.type = '{kind}'" in sql
    assert "o.type IN" not in sql


@pytest.mark.parametrize("search,pattern", [
    ("order", "%order%"),
    ("OrDeR", "%OrDeR%"),
    ("%_[!'", "%!%!_![!!''%"),
    ("' OR 1=1 --", "%'' OR 1=1 --%"),
    ("주문", "%주문%"),
])
def test_object_name_search_quotes_sql_and_treats_wildcards_literally(catalogs, search, pattern):
    catalogs.run_operation(payload("objects", database="safe", owner_id=7, search=search))
    sql = CatalogSession.instances[-1].queries[-1]
    assert f"lower(o.name) LIKE lower('{pattern}') ESCAPE '!'" in sql


@pytest.mark.parametrize("invalid", [
    {"owner_id": value} for value in [True, False, 0, -1, 2147483648, "7", 1.5]
] + [
    {"search": value} for value in [None, 1, True, "bad\x00search", "x" * 256]
] + [
    {"kind": value} for value in [None, "", "u", "X", True, 1, []]
])
def test_object_search_rejects_invalid_filters_before_login(catalogs, invalid):
    with pytest.raises(ValueError):
        catalogs.run_operation(payload("objects", database="safe", **{"owner_id": 7, **invalid}))
    assert not CatalogSession.instances


@pytest.mark.parametrize("changed", [{"id": 43}, {"owner": "dbo"}, {"name": "another"}, {"kind": "V"}])
def test_replaced_or_renamed_object_cannot_start_a_transfer(catalogs, tmp_path, changed):
    CatalogSession.observed_object = {**OBJECT, **changed}
    with pytest.raises(ValueError, match="(?i)(object|changed|reload)"):
        catalogs.run_operation(payload("transfer", path=str(tmp_path / "in.bcp"), columns=COLUMNS, options={"direction": "in"}))
    assert not CatalogSession.instances[-1].transfers


@pytest.mark.parametrize("changed", [{"name": "other"}, {"length": 8}, {"nullable": True}, {"type_id": 52}])
def test_changed_reviewed_schema_cannot_start_a_transfer(catalogs, tmp_path, changed):
    CatalogSession.observed_columns = [{**COLUMNS[0], **changed}]
    with pytest.raises(ValueError, match="(?i)(schema|column|changed|review)"):
        catalogs.run_operation(payload("transfer", path=str(tmp_path / "in.bcp"), columns=COLUMNS, options={"direction": "in"}))
    assert not CatalogSession.instances[-1].transfers


@pytest.mark.parametrize("kind,direction,type_id", [("P", "out", 56), ("V", "in", 56), ("U", "in", 80)])
def test_procedures_view_in_and_timestamp_in_are_not_bulk_targets(catalogs, tmp_path, kind, direction, type_id):
    obj = {**OBJECT, "kind": kind}
    columns = [{**COLUMNS[0], "type_id": type_id}]
    CatalogSession.observed_object = obj
    CatalogSession.observed_columns = columns
    with pytest.raises(ValueError):
        catalogs.run_operation(payload("transfer", object=obj, path=str(tmp_path / "in.bcp"), columns=columns, options={"direction": direction}))
    assert not any(s.transfers for s in CatalogSession.instances)


def test_view_export_and_append_import_forward_complete_reviewed_schema(catalogs, tmp_path):
    for kind, direction in [("V", "out"), ("U", "in")]:
        obj = {**OBJECT, "kind": kind}
        CatalogSession.observed_object = obj
        result = catalogs.run_operation(payload("transfer", object=obj, path=str(tmp_path / "한글.bcp"), columns=COLUMNS, options={"direction": direction}))
        assert result == {"rows": 3, "direction": direction}
        assert CatalogSession.instances[-1].transfers[0][1] == COLUMNS


def test_server_error_is_fatal_even_if_native_return_value_succeeds():
    module = native_module()
    events = []
    diagnostics = module._Diagnostics(events.append)
    diagnostics.message_callback(None, 2714, 1, 16, b"private-value SELECT secret", b"server", b"proc", 1)
    with pytest.raises(ValueError, match="2714"):
        diagnostics.check("Read catalog")
    assert "private-value" not in str(events)
    assert "SELECT" not in str(events)


def test_native_error_and_batch_notification_are_distinguished():
    module = native_module()
    events = []
    diagnostics = module._Diagnostics(events.append, batch_size=1000)
    diagnostics.error_callback(None, 0, 20068, 0, b"batch copied", b"")
    diagnostics.check("Copy")
    assert events[-1]["rows"] == 1000
    diagnostics.error_callback(None, 0, 20018, 0, b"raw password", b"")
    with pytest.raises(ValueError, match="20018"):
        diagnostics.check("Copy")
    assert "raw password" not in str(events)


class NativeLibrary:
    """DB-Library boundary fake; no server or credentials leave this process."""

    def __init__(self):
        self.error_handler = self.message_handler = None
        self.queries = []
        self.login_values = {}
        self.controls = []
        self.formats = []
        self.host_count = 2
        self.closed = False
        self.fail_copy = False
        self.rows = []
        self.results_pending = False
        self.row_index = -1
        self.cells = []

    def dberrhandle(self, callback):
        self.error_handler = callback
        return None

    def dbmsghandle(self, callback):
        self.message_handler = callback
        return None

    def dbinit(self):
        assert "TDSDUMP" not in os.environ
        self.config_text = Path(os.environ["FREETDSCONF"]).read_text(encoding="utf-8")
        self.error_handler = None  # Real dbinit resets the global error handler.
        return 1

    def dbexit(self):
        pass

    def dblogin(self):
        return 101

    def dbloginfree(self, _login):
        pass

    def dbsetlname(self, _login, value, setting):
        self.login_values[setting] = value
        return 1

    def dbsetlbool(self, *_args):
        return 1

    def dbsetlversion(self, _login, version):
        assert version == 2
        return 1

    def dbsetlogintime(self, *_args):
        return 1

    def dbsettime(self, *_args):
        return 1

    def tdsdbopen(self, _login, server, msdblib):
        assert server == b"nice_tools_ase"
        assert msdblib == 0
        return 102

    def dbclose(self, _db):
        self.closed = True

    def dbtds(self, _db):
        return 7

    def dbcmd(self, _db, sql):
        self.queries.append(sql.decode("utf-8"))
        return 1

    def dbsqlexec(self, _db):
        sql = self.queries[-1]
        self.rows = [{"server_version": "Adaptive Server Enterprise/16.0"}] if "@@version" in sql else []
        self.row_index = -1
        self.results_pending = True
        return 1

    def dbresults(self, _db):
        if self.results_pending:
            self.results_pending = False
            return 1
        return 2

    def dbnumcols(self, _db):
        return len(self.rows[0]) if self.rows else 0

    def dbcolname(self, _db, index):
        return list(self.rows[0])[index - 1].encode("utf-8")

    def dbnextrow(self, _db):
        self.row_index += 1
        if self.row_index >= len(self.rows):
            return -2
        self.cells = [ct.create_string_buffer(v.encode("utf-8")) for v in self.rows[self.row_index].values()]
        return -1

    def dbdata(self, _db, index):
        return ct.addressof(self.cells[index - 1])

    def dbdatlen(self, _db, index):
        return len(self.cells[index - 1].value)

    def dbcoltype(self, _db, _index):
        return 39

    def bcp_init(self, _db, target, path, error_path, direction):
        self.target = target
        self.path = path
        assert error_path is None
        self.direction = direction
        return 1

    def bcp_columns(self, _db, count):
        assert count == self.host_count
        return 1

    def bcp_control(self, _db, field, value):
        self.controls.append((field, value))
        return 1

    def bcp_colfmt(self, _db, position, kind, prefix, length, terminator, termlen, target_position):
        self.formats.append((position, kind, prefix, length, ct.string_at(terminator, termlen), target_position))
        return 1

    def bcp_exec(self, db, rows):
        if self.fail_copy:
            self.message_handler(db, 8152, 1, 16, b"private-value SQL row data", b"server", b"", 1)
        ct.cast(rows, ct.POINTER(ct.c_int32)).contents.value = 3
        return 1


def test_native_character_copy_preserves_terminator_bytes_and_uses_fail_fast(monkeypatch, tmp_path):
    module = native_module()
    lib = NativeLibrary()
    monkeypatch.setattr(module, "_load_native", lambda: lib)
    monkeypatch.setenv("TDSDUMP", str(tmp_path / "must-not-exist.log"))
    path = tmp_path / "한글.bcp"
    path.write_bytes(b"1\x00|value\r\n")
    options = module._options({"direction": "in", "field_hex": "007c", "row_hex": "0d0a", "batch_size": 13})
    with module._Session(module._connection(CONNECTION)) as session:
        result = session.bulk_copy(OBJECT, COLUMNS * 2, str(path), options)
    assert result == {"rows": 3, "direction": "in"}
    assert lib.path == str(path).encode("utf-8")
    assert lib.target == b"[other]]owner].[order]] history]"
    assert "USE [연구]]db]" in lib.queries
    assert lib.formats == [(1, 47, 0, -1, b"\x00|", 1), (2, 47, 0, -1, b"\r\n", 2)]
    assert (1, 1) in lib.controls  # zero is coerced to ten by upstream FreeTDS.
    assert (4, 13) in lib.controls
    assert CONNECTION["password"] not in lib.config_text
    assert "2147483647" in lib.config_text
    assert os.environ["TDSDUMP"].endswith("must-not-exist.log")
    assert not (tmp_path / "must-not-exist.log").exists()
    assert lib.closed


def test_native_copy_cannot_claim_success_after_an_ase_error(monkeypatch, tmp_path):
    module = native_module()
    lib = NativeLibrary()
    lib.fail_copy = True
    lib.host_count = 1
    monkeypatch.setattr(module, "_load_native", lambda: lib)
    events = []
    with module._Session(module._connection(CONNECTION), emit=events.append) as session:
        with pytest.raises(ValueError, match="8152"):
            session.bulk_copy(OBJECT, COLUMNS, str(tmp_path / "stage.bcp"), module._options({"direction": "out"}))
    assert lib.closed
    assert "private-value" not in str(events)
    assert "SQL" not in str(events)


def test_native_manifest_detects_modified_or_missing_dependencies(monkeypatch, tmp_path):
    module = native_module()
    monkeypatch.delenv("NICE_TOOLS_ASE_NATIVE", raising=False)
    path = tmp_path / "sybdb.dll"
    path.write_bytes(b"native")
    runtime = tmp_path / "runtime.dll"
    runtime.write_bytes(b"dependency")
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, runtime)}
    (tmp_path / "native-manifest.json").write_text(json.dumps({
        "schema_version": 1, "freetds_version": "1.5.19", "patch_revision": 1, "files": files,
    }), encoding="utf-8")
    module._verify_manifest(path)
    runtime.write_bytes(b"changed")
    with pytest.raises(ValueError, match="(?i)(manifest|changed|verified)"):
        module._verify_manifest(path)


def test_error_handler_is_installed_after_dbinit_resets_it(monkeypatch):
    module = native_module()
    lib = NativeLibrary()
    monkeypatch.setattr(module, "_load_native", lambda: lib)
    events = []
    with module._Session(module._connection(CONNECTION), emit=events.append) as session:
        assert lib.error_handler is not None
        lib.error_handler(session.db, 0, 20068, 0, b"batch", b"")
        assert events[-1]["rows"] == 1000
        lib.error_handler(session.db, 9, 20009, 0, b"secret SQL private-value", b"")
        with pytest.raises(ValueError, match="20009"):
            session.diagnostics.check("Connect")
    assert "private-value" not in str(events)


def test_configuration_pins_fractional_date_format_and_restores_environment(monkeypatch):
    module = native_module()
    monkeypatch.setenv("FREEBCP_DATEFMT", "user-locale-format")
    with module._configuration(module._connection(CONNECTION), "utf-8"):
        assert os.environ.get("FREEBCP_DATEFMT") == "%Y-%m-%d %H:%M:%S.%z"
    assert os.environ["FREEBCP_DATEFMT"] == "user-locale-format"


def test_binding_uses_only_symbols_in_the_verified_native_library(monkeypatch):
    path = os.environ.get("NICE_TOOLS_ASE_TEST_NATIVE")
    if not path:
        pytest.skip("Set NICE_TOOLS_ASE_TEST_NATIVE to the verified developer FreeTDS library")
    module = native_module()
    monkeypatch.setenv("NICE_TOOLS_ASE_NATIVE", path)
    status = module.native_status()
    assert status["available"] is True, status["detail"]
    assert "1.5.19" in status["version"]


def test_verified_native_connection_refusal_is_sanitized_and_does_not_exit(monkeypatch):
    path = os.environ.get("NICE_TOOLS_ASE_TEST_NATIVE")
    if not path:
        pytest.skip("Set NICE_TOOLS_ASE_TEST_NATIVE to the verified developer FreeTDS library")
    module = native_module()
    monkeypatch.setenv("NICE_TOOLS_ASE_NATIVE", path)
    events = []
    with socket.socket() as closed_port:
        closed_port.bind(("127.0.0.1", 0))
        connection = {**CONNECTION, "host": "127.0.0.1", "port": closed_port.getsockname()[1], "login_timeout": 1, "query_timeout": 1}
        with pytest.raises(ValueError) as error:
            module.run_operation({"operation": "databases", "connection": connection}, events.append)
    assert "private-value" not in str(error.value)
    assert "private-value" not in str(events)
    assert any(event.get("level") == "error" for event in events)
    assert "error" in str(error.value).lower()


class PhaseClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_native_session_times_library_load_login_and_server_setup_separately(monkeypatch):
    module = native_module()
    clock = PhaseClock()

    class TimedLibrary(NativeLibrary):
        def dbinit(self):
            clock.advance(2.0)  # Client prep must not be labeled as network login.
            return super().dbinit()

        def tdsdbopen(self, *args):
            clock.advance(1.5)
            return super().tdsdbopen(*args)

        def dbsqlexec(self, *args):
            clock.advance(0.5 if "@@version" in self.queries[-1] else 0.25)
            return super().dbsqlexec(*args)

    library = TimedLibrary()

    def load_library():
        clock.advance(0.125)
        return library

    monkeypatch.setattr(module, "_load_native", load_library)
    monkeypatch.setattr(time, "monotonic", clock)
    with module._Session(module._connection(CONNECTION)) as session:
        assert session.timings == {
            "native_load_seconds": 0.125,
            "login_seconds": 1.5,
            "setup_seconds": 0.75,
        }
    assert library.closed


@pytest.mark.parametrize("operation,changes,query_count", [
    ("databases", {}, 1),
    ("owners", {"database": OBJECT["database"]}, 1),
    ("objects", {"database": OBJECT["database"], "owner_id": OBJECT["owner_id"]}, 1),
    ("columns", {}, 2),
])
def test_catalog_timing_contains_requested_queries_and_no_connection_values(monkeypatch, operation, changes, query_count):
    module = native_module()
    clock = PhaseClock()

    class TimedCatalog(CatalogSession):
        def query(self, sql):
            clock.advance(0.625)
            return super().query(sql)

    TimedCatalog.observed_object = dict(OBJECT)
    TimedCatalog.observed_columns = [dict(column) for column in COLUMNS]
    monkeypatch.setattr(module, "_Session", TimedCatalog)
    monkeypatch.setattr(time, "monotonic", clock)
    result = module.run_operation(payload(operation, **changes))
    assert result["timings"] == {
        "native_load_seconds": 0.125,
        "login_seconds": 1.5,
        "setup_seconds": 0.75,
        "query_seconds": query_count * 0.625,
    }
    assert all(isinstance(value, float) and value >= 0 and math.isfinite(value) for value in result["timings"].values())
    for value in (CONNECTION["host"], CONNECTION["user"], CONNECTION["password"]):
        assert value not in json.dumps(result)
