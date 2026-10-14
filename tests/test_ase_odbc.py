"""Persistent ASE ODBC behavior at the unavailable external driver boundary."""

import importlib
import sys
import threading

import pytest


CONNECTION = {"host": "ase.internal", "port": 5000, "user": "reader", "password": "private-password"}
OBJECT = {"database": "연구]db", "owner": "other]owner", "owner_id": 7, "id": 42, "name": "order] history", "kind": "U"}
COLUMNS = [{"position": 1, "name": "id", "type_id": 56, "type_name": "int", "length": 4,
            "precision": 0, "scale": 0, "nullable": False, "identity": False}]


def module():
    try:
        return importlib.import_module("nice_tools.ase_odbc")
    except ImportError:
        pytest.fail("The persistent ASE ODBC adapter is not implemented")


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.closed = False
        self.cancelled = False

    def execute(self, sql, *parameters):
        db = self.connection.db
        self.connection.queries.append((sql, parameters))
        if db.query_error:
            raise RuntimeError(db.query_error)
        if "sysdatabases" in sql and db.block:
            db.entered.set()
            assert db.release.wait(5)
        if "@@version" in sql:
            self.rows = [(db.server_version,)]
        elif "sysdatabases" in sql:
            self.rows = list(db.databases)
        elif "syscolumns" in sql:
            self.rows = [tuple(c[key] for key in ("position", "name", "type_id", "type_name", "length", "precision", "scale", "nullable", "identity")) for c in db.columns]
        elif "sysobjects" in sql and "o.id" in sql.split("FROM", 1)[0]:
            obj = db.object
            self.rows = [(obj["id"], obj["owner_id"], obj["owner"], obj["name"], obj["kind"])]
        elif "sysusers" in sql:
            self.rows = [(7, "other]owner")]
        return self

    def fetchall(self):
        return self.rows

    def cancel(self):
        self.cancelled = True
        self.connection.db.release.set()

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, db):
        self.db = db
        self.queries = []
        self.cursors = []
        self.closed = False
        self.close_calls = 0
        self.timeout = None

    def cursor(self):
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.close_calls += 1
        self.closed = True


class FakeOdbc:
    version = "5.3.0"

    def __init__(self):
        self.driver_names = ["SQL Server", "SAP ASE ODBC Driver", "Adaptive Server Enterprise"]
        self.server_version = "Adaptive Server Enterprise/16.0 SP04"
        self.connections = []
        self.connect_calls = []
        self.connect_error = None
        self.query_error = None
        self.databases = [(5, "연구]db")]
        self.object = dict(OBJECT)
        self.columns = [dict(c) for c in COLUMNS]
        self.block = False
        self.block_connect = False
        self.connect_entered = threading.Event()
        self.connect_release = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()

    def drivers(self):
        return list(self.driver_names)

    def connect(self, connection_string, **options):
        self.connect_calls.append((connection_string, options))
        if self.connect_error:
            raise RuntimeError(self.connect_error)
        connection = FakeConnection(self)
        self.connections.append(connection)
        if self.block_connect:
            self.connect_entered.set()
            assert self.connect_release.wait(5)
        return connection


@pytest.fixture
def odbc(monkeypatch):
    adapter = module()
    fake = FakeOdbc()
    monkeypatch.setattr(adapter, "_pyodbc", lambda: fake)
    monkeypatch.setattr(adapter, "_odbc_settings", lambda: {})
    return adapter, fake


def payload(operation, **changes):
    return {"operation": operation, "connection": dict(CONNECTION), "object": dict(OBJECT), **changes}


def test_connect_selects_registered_ase_driver_and_verifies_server(odbc):
    adapter, db = odbc
    session = adapter.connect(CONNECTION)
    try:
        assert session.driver == "Adaptive Server Enterprise"
        connection_string, options = db.connect_calls[0]
        assert "DRIVER={Adaptive Server Enterprise}" in connection_string
        assert "SERVER={ase.internal}" in connection_string
        assert "PORT=5000" in connection_string
        assert "CHARSET=UTF8" in connection_string
        assert options == {"autocommit": True, "timeout": 15}
        assert db.connections[0].timeout == 60
        queries = [q[0].upper() for q in db.connections[0].queries]
        assert any("SET QUOTED_IDENTIFIER ON" in q for q in queries)
        assert any("@@VERSION" in q for q in queries)
    finally:
        session.close()
    assert db.connections[0].closed
    assert all(c.closed for c in db.connections[0].cursors)


def test_browsing_reuses_one_connection_and_reports_zero_new_login_time(odbc):
    adapter, db = odbc
    session = adapter.connect(CONNECTION)
    try:
        first = session.run_operation(payload("databases"))
        second = session.run_operation(payload("owners", database="연구]db"))
        third = session.run_operation(payload("objects", database="연구]db"))
        assert first["databases"] == [{"id": 5, "name": "연구]db"}]
        assert second["owners"] == [{"id": 7, "name": "other]owner"}]
        assert third["objects"] == [OBJECT]
        assert len(db.connect_calls) == 1
        assert first["timings"]["login_seconds"] >= 0
        assert second["timings"]["login_seconds"] == 0
        assert third["timings"]["login_seconds"] == 0
        assert all(result["timings"]["query_seconds"] >= 0 for result in (first, second, third))
    finally:
        session.close()


def test_odbc_braces_escape_passwords_without_changing_their_value(odbc):
    adapter, db = odbc
    secret = "part;PWD=wrong}tail{value"
    session = adapter.connect({**CONNECTION, "password": secret})
    session.close()
    text = db.connect_calls[0][0]
    assert "PWD={part;PWD=wrong}}tail{value};" in text
    assert text.count("DRIVER=") == 1


@pytest.mark.parametrize("version", ["Microsoft SQL Server 2022", "SAP SQL Anywhere 17", "", None])
def test_non_ase_server_is_rejected_and_connection_closed(odbc, version):
    adapter, db = odbc
    db.server_version = version
    with pytest.raises(ValueError, match="(?i)(ase|adaptive)"):
        adapter.connect(CONNECTION)
    assert db.connections[0].closed
    assert db.connections[0].close_calls == 1


def test_startup_query_error_closes_connection_once_without_driver_details(odbc):
    adapter, db = odbc
    db.query_error = "private-password bad startup SQL"
    with pytest.raises(ValueError) as error:
        adapter.connect(CONNECTION)
    assert "private-password" not in str(error.value)
    assert db.connections[0].close_calls == 1


def test_driver_status_is_read_only_and_distinguishes_module_from_driver_availability(odbc):
    adapter, db = odbc
    result = adapter.odbc_status()
    assert result["available"] and result["module_available"]
    assert result["version"] == "5.3.0"
    assert result["driver"] == "Adaptive Server Enterprise"
    assert db.connect_calls == []
    db.driver_names = ["SQL Server", "FreeTDS", "Sybase SQL Anywhere"]
    result = adapter.odbc_status()
    assert not result["available"] and result["module_available"]
    assert result["driver"] is None
    assert db.connect_calls == []


def test_native_module_disables_pooling_before_driver_discovery(monkeypatch):
    adapter = module()
    db = FakeOdbc()
    db.pooling = True
    monkeypatch.setitem(sys.modules, "pyodbc", db)
    assert adapter._pyodbc() is db
    assert db.pooling is False


def test_configured_ase_driver_is_selected_without_silent_fallback(odbc, monkeypatch):
    adapter, db = odbc
    monkeypatch.setattr(adapter, "_odbc_settings", lambda: {"driver": "SAP ASE ODBC Driver"})
    session = adapter.connect(CONNECTION)
    assert session.driver == "SAP ASE ODBC Driver"
    session.close()
    monkeypatch.setattr(adapter, "_odbc_settings", lambda: {"driver": "Missing ASE driver"})
    with pytest.raises(ValueError):
        adapter.connect(CONNECTION)
    assert len(db.connect_calls) == 1


@pytest.mark.parametrize("driver", ["SQL Server", "FreeTDS", "Sybase SQL Anywhere"])
def test_non_ase_registered_drivers_are_never_used(odbc, driver):
    adapter, db = odbc
    with pytest.raises(ValueError):
        adapter.connect(CONNECTION, driver=driver)
    assert db.connect_calls == []


def test_connect_and_query_errors_never_expose_driver_text_or_password(odbc):
    adapter, db = odbc
    db.connect_error = "ODBC DriverConnection PWD=private-password; SELECT confidential"
    with pytest.raises(ValueError) as error:
        adapter.connect(CONNECTION)
    assert "private-password" not in str(error.value)
    assert "confidential" not in str(error.value)
    db.connect_error = None
    session = adapter.connect(CONNECTION)
    db.query_error = "private-password SELECT confidential"
    with pytest.raises(ValueError) as error:
        session.run_operation(payload("databases"))
    assert "private-password" not in str(error.value)
    assert "confidential" not in str(error.value)
    session.close()


def test_search_is_parameterized_with_optional_owner_and_literal_wildcards(odbc):
    adapter, db = odbc
    session = adapter.connect(CONNECTION)
    try:
        session.run_operation(payload("objects", database="연구]db", search="%_[!' OR 1=1 --", kind="U"))
        query, parameters = db.connections[0].queries[-1]
        assert "[연구]]db].dbo.sysobjects" in query
        assert "o.uid = ?" not in query
        assert "o.type = ?" in query
        assert "lower(o.name) LIKE lower(?) ESCAPE '!'" in query
        assert "OR 1=1" not in query
        assert parameters == ("U", "%!%!_![!!' OR 1=1 --%")
        session.run_operation(payload("objects", database="연구]db", owner_id=7))
        query, parameters = db.connections[0].queries[-1]
        assert "o.uid = ?" in query
        assert parameters == (7,)
    finally:
        session.close()


def test_exact_object_and_columns_are_checked_on_the_persistent_connection(odbc):
    adapter, db = odbc
    session = adapter.connect(CONNECTION)
    try:
        result = session.run_operation(payload("columns"))
        assert result["object"] == OBJECT
        assert result["columns"] == COLUMNS
        queries = db.connections[0].queries[-2:]
        assert all("o.id = ?" in query and "o.uid = ?" in query for query, _ in queries)
        assert queries[0][1] == (7, 42)
        assert queries[1][1] == (42, 7)
        assert len(db.connect_calls) == 1
    finally:
        session.close()


@pytest.mark.parametrize("changed", [{"owner": "dbo"}, {"id": 43}, {"name": "renamed"}, {"kind": "V"}])
def test_replaced_object_cannot_pass_metadata_verification(odbc, changed):
    adapter, db = odbc
    session = adapter.connect(CONNECTION)
    db.object.update(changed)
    with pytest.raises(ValueError, match="(?i)(object|metadata|changed)"):
        session.run_operation(payload("columns"))
    session.close()


@pytest.mark.parametrize("changes", [{"owner_id": True}, {"owner_id": "7"}, {"owner_id": 0},
                                       {"search": 1}, {"search": "bad\0search"}, {"kind": "U' OR 1=1"}])
def test_invalid_search_is_rejected_before_query(odbc, changes):
    adapter, db = odbc
    session = adapter.connect(CONNECTION)
    before = len(db.connections[0].queries)
    with pytest.raises(ValueError):
        session.run_operation(payload("objects", database="safe", **changes))
    assert len(db.connections[0].queries) == before
    session.close()


def test_fresh_worker_operation_always_closes_its_temporary_connection(odbc):
    adapter, db = odbc
    assert adapter.run_operation(payload("columns"))["columns"] == COLUMNS
    assert db.connections[0].closed
    db.object["name"] = "changed"
    with pytest.raises(ValueError):
        adapter.run_operation(payload("columns"))
    assert db.connections[1].closed


def test_cancel_interrupts_cursor_without_waiting_for_catalog_lock(odbc):
    adapter, db = odbc
    session = adapter.connect(CONNECTION)
    db.block = True
    failures = []

    def catalog():
        try:
            session.run_operation(payload("databases"))
        except ValueError as error:
            failures.append(error)

    thread = threading.Thread(target=catalog)
    thread.start()
    assert db.entered.wait(3)
    session.cancel()
    thread.join(3)
    assert not thread.is_alive()
    assert len(failures) == 1
    assert any(cursor.cancelled for cursor in db.connections[0].cursors)
    assert "private-password" not in str(failures[0])
    session.close()


def test_close_cancels_active_query_and_prevents_future_queries(odbc):
    adapter, db = odbc
    session = adapter.connect(CONNECTION)
    db.block = True
    failures = []

    def catalog():
        try:
            session.run_operation(payload("databases"))
        except ValueError as error:
            failures.append(error)

    thread = threading.Thread(target=catalog)
    thread.start()
    assert db.entered.wait(3)
    session.close()
    thread.join(3)
    assert not thread.is_alive() and len(failures) == 1
    assert db.connections[0].closed
    with pytest.raises(ValueError):
        session.run_operation(payload("databases"))
    session.close()


def test_cancel_during_login_closes_the_unpublished_connection(odbc):
    adapter, db = odbc
    db.block_connect = True
    session = adapter.OdbcSession(CONNECTION)
    failures = []

    def login():
        try:
            session.open()
        except ValueError as error:
            failures.append(error)

    thread = threading.Thread(target=login)
    thread.start()
    assert db.connect_entered.wait(3)
    session.cancel()
    db.connect_release.set()
    thread.join(3)
    assert not thread.is_alive() and len(failures) == 1
    assert db.connections[0].closed and db.connections[0].close_calls == 1
    assert all("@@version" not in query for query, _ in db.connections[0].queries)
    session.close()
    assert db.connections[0].close_calls == 1
