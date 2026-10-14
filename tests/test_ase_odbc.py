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


class FakeDriverError(Exception):
    pass


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.closed = False
        self.cancelled = False

    def execute(self, sql, *parameters):
        db = self.connection.db
        self.connection.queries.append((sql, parameters))
        for part, error in db.query_errors.items():
            if part in sql:
                raise error
        if db.query_error:
            if isinstance(db.query_error, Exception):
                raise db.query_error
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
        if self.connection.db.fetch_error:
            raise self.connection.db.fetch_error
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
        self._timeout = None

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        if self.db.timeout_error:
            raise self.db.timeout_error
        self._timeout = value

    def cursor(self):
        if self.db.cursor_error:
            raise self.db.cursor_error
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.close_calls += 1
        self.closed = True


class FakeOdbc:
    version = "5.3.0"
    Error = FakeDriverError

    def __init__(self):
        self.driver_names = ["SQL Server", "SAP ASE ODBC Driver", "Adaptive Server Enterprise"]
        self.server_version = "Adaptive Server Enterprise/16.0 SP04"
        self.connections = []
        self.connect_calls = []
        self.connect_error = None
        self.query_error = None
        self.query_errors = {}
        self.cursor_error = None
        self.timeout_error = None
        self.fetch_error = None
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
            if isinstance(self.connect_error, Exception):
                raise self.connect_error
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
        assert "SERVER=ase.internal;" in connection_string
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


def test_sap_connection_string_does_not_wrap_ordinary_connection_values(odbc):
    adapter, db = odbc
    session = adapter.connect({**CONNECTION, "host": "192.0.2.23", "password": "sample!P=@value}word"})
    session.close()
    assert db.connect_calls[0][0] == (
        "DRIVER={Adaptive Server Enterprise};SERVER=192.0.2.23;PORT=5000;"
        "UID=reader;PWD=sample!P=@value}word;DATABASE=master;CHARSET=UTF8;"
    )
    assert len(db.connect_calls) == 1


@pytest.mark.parametrize("value,encoded", [
    ("", ""), ("ordinary", "ordinary"), ("contains=equals!@", "contains=equals!@"),
    ("closing}brace", "closing}brace"), ("middle{brace", "middle{brace"),
    ("{leading}", "{{leading}}}"), ("part;UID=wrong}", "{part;UID=wrong}}}"),
    (" leading", "{ leading}"), ("trailing ", "{trailing }"), (" ", "{ }"),
    ("inner space", "inner space"),
])
def test_user_and_password_values_escape_only_odbc_grammar_boundaries(odbc, value, encoded):
    adapter, db = odbc
    # Empty user names remain invalid; an empty DB password is valid.
    request = {**CONNECTION, "password": value}
    if value:
        request["user"] = value
    session = adapter.connect(request)
    session.close()
    text = db.connect_calls[0][0]
    assert "PWD=" + encoded + ";DATABASE=master;" in text
    assert "UID=" + (encoded if value else "reader") + ";PWD=" in text
    assert "DRIVER={Adaptive Server Enterprise};" in text


def test_im002_diagnostic_describes_name_interpretation_without_raw_driver_message(odbc):
    adapter, db = odbc
    db.connect_error = FakeDriverError("IM002", "[Microsoft ODBC Driver Manager] PWD=private-password arbitrary raw error")
    with pytest.raises(ValueError) as error:
        adapter.connect(CONNECTION)
    message = str(error.value)
    assert "SQLSTATE: IM002" in message and "단계: connect" in message
    assert "데이터 원본/드라이버 이름 해석 실패" in message
    assert "private-password" not in message and "arbitrary raw error" not in message


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


@pytest.mark.parametrize("state,hint", [("08001", "연결 협상"), ("28000", "인증"),
                                        ("HYT00", "제한 시간"), ("HYT01", "제한 시간"), ("IM002", "드라이버")])
def test_connect_diagnostic_reports_safe_sqlstate_and_possible_reason(odbc, state, hint):
    adapter, db = odbc
    db.connect_error = FakeDriverError(state, "[SAP ODBC] PWD=private-password; native 4002 SELECT confidential")
    with pytest.raises(ValueError) as error:
        adapter.connect(CONNECTION)
    message = str(error.value)
    assert "단계: connect" in message
    assert "SQLSTATE: " + state in message
    assert hint in message
    assert "private-password" not in message and "confidential" not in message
    assert "4002" not in message and "[SAP ODBC]" not in message
    assert len(db.connect_calls) == 1 and db.connections == []


def test_unsupported_timeout_attribute_is_not_reported_as_a_login_failure(odbc):
    adapter, db = odbc
    db.timeout_error = FakeDriverError("HY092", "PWD=private-password unsupported native attribute")
    with pytest.raises(ValueError) as error:
        adapter.connect(CONNECTION)
    message = str(error.value)
    assert "단계: connection-timeout-setting" in message
    assert "SQLSTATE: HY092" in message
    assert "연결 속성" in message
    assert "private-password" not in message
    assert len(db.connect_calls) == 1 and db.connections[0].close_calls == 1
    assert db.connections[0].queries == []


@pytest.mark.parametrize("action", ["cursor", "execute", "identity"])
def test_post_login_startup_failure_names_the_phase_and_operation(odbc, action):
    adapter, db = odbc
    native_error = FakeDriverError("HY092", "private-password startup detail")
    if action == "cursor":
        db.cursor_error = native_error
        expected_stage, expected_action = "startup", "ODBC 커서 생성"
    elif action == "execute":
        db.query_error = native_error
        expected_stage, expected_action = "startup", "SQL 실행"
    else:
        db.query_errors["@@version"] = native_error
        expected_stage, expected_action = "ASE-identity", "SQL 실행"
    with pytest.raises(ValueError) as error:
        adapter.connect(CONNECTION)
    message = str(error.value)
    assert "단계: " + expected_stage in message
    assert expected_action in message
    assert "SQLSTATE: HY092" in message
    assert "private-password" not in message
    assert db.connections[0].close_calls == 1


@pytest.mark.parametrize("operation,action", [("databases", "execute"), ("objects", "execute"), ("databases", "fetch")])
def test_catalog_diagnostic_preserves_safe_state_without_raw_driver_details(odbc, operation, action):
    adapter, db = odbc
    session = adapter.connect(CONNECTION)
    if action == "execute":
        db.query_error = FakeDriverError("42000", "private-password SELECT confidential")
        expected_action = "SQL 실행"
    else:
        db.fetch_error = FakeDriverError("08S01", "private-password connection lost")
        expected_action = "결과 읽기"
    try:
        with pytest.raises(ValueError) as error:
            session.run_operation(payload(operation, database=OBJECT["database"]))
        message = str(error.value)
        assert "단계: " + ("databases" if operation == "databases" else "query") in message
        assert expected_action in message
        assert "SQLSTATE: " + ("42000" if action == "execute" else "08S01") in message
        assert "private-password" not in message and "confidential" not in message
        assert all(cursor.closed for cursor in db.connections[0].cursors)
    finally:
        session.close()


@pytest.mark.parametrize("state", ["PRIVATE", "zz999", "ZZ999", "08001 secret", "08001\n", b"08001", ("08001",), None])
def test_malformed_or_unknown_sqlstates_never_echo_driver_arguments(odbc, state):
    adapter, db = odbc
    db.connect_error = FakeDriverError(state, "private-password unsafe driver message")
    with pytest.raises(ValueError) as error:
        adapter.connect(CONNECTION)
    message = str(error.value)
    assert "단계: connect" in message
    assert "SQLSTATE" not in message
    assert "private-password" not in message and "unsafe driver message" not in message
    assert "ZZ999" not in message and "PRIVATE" not in message


def test_non_driver_exception_does_not_fabricate_a_sqlstate(odbc):
    adapter, db = odbc
    db.connect_error = RuntimeError("08001", "private-password")
    with pytest.raises(ValueError) as error:
        adapter.connect(CONNECTION)
    assert "단계: connect" in str(error.value)
    assert "SQLSTATE" not in str(error.value)
    assert "private-password" not in str(error.value)


def test_real_pyodbc_error_subclass_exposes_only_its_known_sqlstate(odbc, monkeypatch):
    import pyodbc

    adapter, db = odbc
    monkeypatch.setattr(db, "Error", pyodbc.Error)
    db.connect_error = pyodbc.OperationalError("08001", "PWD=private-password; native confidential")
    with pytest.raises(ValueError) as error:
        adapter.connect(CONNECTION)
    assert "SQLSTATE: 08001" in str(error.value)
    assert "private-password" not in str(error.value)
    assert "confidential" not in str(error.value)
