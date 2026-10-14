"""SAP client boundary tests; no installed SAP client or ASE server is assumed."""

import importlib
from pathlib import Path
import sys

import pytest


CONNECTION = {"host": "ase.internal", "port": 5000, "user": "reader", "password": "private-password"}
OBJECT = {"database": "연구db", "owner": "dbo", "owner_id": 7, "id": 42, "name": "orders", "kind": "U"}
COLUMNS = [{"position": 1, "name": "id", "type_id": 56, "type_name": "int", "length": 4,
            "precision": 0, "scale": 0, "nullable": False, "identity": False}]


def cli_module():
    try:
        return importlib.import_module("nice_tools.ase_cli")
    except ImportError:
        pytest.fail("The installed SAP client bridge is not implemented")


def escape(value):
    return str(value).replace("^", "^^").replace("|", "^p").replace("\r", "^r").replace("\n", "^n").replace("\t", "^t")


def frame(tag, *values):
    return "~NT~" + tag + "|" + "|".join(escape(value) for value in values) + "|~END~\n"


def catalog_output(*records):
    return "~NTBEGIN~\n" + frame("V", "Adaptive Server Enterprise/16.0") + "".join(records) + "~NTEND~\n"


class FakeClients:
    def __init__(self):
        self.calls = []
        self.observed_object = dict(OBJECT)
        self.observed_columns = [dict(c) for c in COLUMNS]
        self.catalog_override = None
        self.catalog_returncode = 0
        self.catalog_console = "Password:"
        self.copy_output = "3 rows copied.\nClock Time (ms.): total = 25 Avg = 8 (120 rows per sec.)\n"
        self.copy_returncode = 0
        self.progress = []

    def run(self, argv, password, input_text="", timeout=90, on_output=None, register=None, *, _parse_output=None):
        assert CONNECTION["password"] not in str(argv)
        ini_path = Path(argv[argv.index("-I") + 1])
        ini_text = ini_path.read_text(encoding="ascii")
        assert CONNECTION["password"] not in ini_text
        assert "NLWNSCK,ase.internal,5000" in ini_text
        call = {"argv": list(argv), "password": password, "ini_path": ini_path, "ini": ini_text, "timeout": timeout,
                "input_text": input_text, "register": register}
        self.calls.append(call)
        if Path(argv[0]).name.lower() == "isql.exe":
            query_path = Path(argv[argv.index("-i") + 1])
            output_path = Path(argv[argv.index("-o") + 1])
            call.update(query_path=query_path, output_path=output_path, query=query_path.read_text(encoding="utf-8"))
            query = call["query"]
            if self.catalog_override is not None:
                output = self.catalog_override
            elif "sysdatabases" in query:
                output = catalog_output(frame("D", 5, "연구db"))
            elif "syscolumns" in query:
                obj = self.observed_object
                output = catalog_output(frame("O", obj["id"], obj["owner_id"], obj["owner"], obj["name"], obj["kind"]),
                                        *(frame("C", *(c[key] for key in ("position", "name", "type_id", "type_name", "length", "precision", "scale")), int(c["nullable"]), int(c["identity"])) for c in self.observed_columns))
            elif "sysusers" in query and "sysobjects" in query and "DISTINCT" in query:
                output = catalog_output(frame("W", 7, "dbo"))
            else:
                obj = self.observed_object
                output = catalog_output(frame("O", obj["id"], obj["owner_id"], obj["owner"], obj["name"], obj["kind"]))
            output_path.write_text(output, encoding="utf-8")
            if _parse_output:
                midpoint = len(self.catalog_console) // 2
                _parse_output(self.catalog_console[:midpoint])
                _parse_output(self.catalog_console[midpoint:])
            return {"stdout": self.catalog_console.replace(password, "[hidden]") if password else self.catalog_console,
                    "returncode": self.catalog_returncode, "elapsed_seconds": 0.25}
        if on_output:
            for chunk in self.progress:
                on_output(chunk)
        if _parse_output:
            for chunk in self.progress:
                _parse_output(chunk)
            _parse_output(self.copy_output)
        return {"stdout": self.copy_output.replace(password, "[hidden]") if password else self.copy_output,
                "returncode": self.copy_returncode, "elapsed_seconds": 0.5}


@pytest.fixture
def clients(monkeypatch):
    module = cli_module()
    fake = FakeClients()
    monkeypatch.setattr(module, "_client_config", lambda: {"isql_path": "C:/SAP/isql.exe", "bcp_path": "C:/SAP/bcp.exe"})
    monkeypatch.setattr(module, "_run_client", fake.run)
    return module, fake


def payload(operation, **changes):
    return {"operation": operation, "connection": dict(CONNECTION), "object": dict(OBJECT), **changes}


def test_catalog_uses_installed_isql_and_secret_free_utf8_files_then_removes_them(clients):
    module, fake = clients
    registered = lambda handle: None
    result = module.run_operation(payload("databases"), register=registered)
    assert result["databases"] == [{"id": 5, "name": "연구db"}]
    assert result["timings"] == {"client_seconds": 0.25}
    call = fake.calls[0]
    assert call["register"] is registered
    assert call["password"] == CONNECTION["password"]
    assert call["input_text"] == ""
    assert call["argv"][0] == "C:/SAP/isql.exe"
    assert "-P" not in call["argv"]
    assert call["argv"][call["argv"].index("-J") + 1] == "utf8"
    assert call["argv"][call["argv"].index("-z") + 1] == "us_english"
    assert "--retserverror" in call["argv"]
    assert call["argv"][call["argv"].index("-m") + 1] == "11"
    assert "set nocount on" in call["query"].lower()
    assert "set quoted_identifier on" in call["query"].lower()
    assert call["query"].lower().index("set quoted_identifier on") < call["query"].index("go\n") < call["query"].index("~NTBEGIN~")
    assert "@@version" in call["query"]
    assert call["query"].endswith("go\nexit\n")
    assert not call["ini_path"].exists()
    assert not call["query_path"].exists()
    assert not call["output_path"].exists()


def test_framed_catalog_preserves_whitespace_delimiters_and_unicode_names(clients):
    module, fake = clients
    name = "  주문|^\t\n내역  "
    fake.catalog_override = catalog_output(frame("D", 5, name))
    assert module.run_operation(payload("databases"))["databases"] == [{"id": 5, "name": name}]
    assert "replace(" in fake.calls[0]["query"].lower()


def test_catalog_name_containing_error_words_is_data_not_an_sql_error(clients):
    module, fake = clients
    name = "Error: Msg 208, Level 16, failure:"
    fake.catalog_override = catalog_output(frame("D", 5, name))
    assert module.run_operation(payload("databases"))["databases"] == [{"id": 5, "name": name}]


def test_ase_catalog_uses_str_replace_and_grouped_owners(clients):
    module, fake = clients
    fake.catalog_override = catalog_output(frame("W", 7, "dbo"))
    assert module.run_operation(payload("owners", database="연구db"))["owners"] == [{"id": 7, "name": "dbo"}]
    query = fake.calls[0]["query"]
    assert "str_replace(" in query
    assert "GROUP BY u.uid, u.name" in query
    assert "SELECT DISTINCT" not in query


@pytest.mark.parametrize("output", [
    "~NTBEGIN~\n" + frame("D", 5, "valid") + "~NTEND~\n",
    "~NTBEGIN~\n" + frame("V", "Microsoft SQL Server 2022") + frame("D", 5, "valid") + "~NTEND~\n",
    "~NTBEGIN~\n" + frame("V", "SAP SQL Anywhere 17") + frame("D", 5, "valid") + "~NTEND~\n",
    "~NTBEGIN~\n" + frame("V", "Adaptive Server Enterprise/16.0") + frame("V", "Adaptive Server Enterprise/16.0") + "~NTEND~\n",
])
def test_missing_ambiguous_or_non_ase_identity_cannot_become_a_connection(clients, output):
    module, fake = clients
    fake.catalog_override = output
    with pytest.raises(ValueError):
        module.run_operation(payload("databases"))


@pytest.mark.parametrize("output", [
    "", "~NTBEGIN~\n", "~NTEND~\n", "~NTBEGIN~\n~NTBEGIN~\n~NTEND~\n",
    catalog_output("~NT~D|5|truncated\n"), catalog_output(frame("D", 5, "bad") + "~NT~D|6|bad^x|~END~\n"),
    catalog_output(frame("D", 5, "valid") + "~NT~UNKNOWN|6|bad|~END~\n"),
    catalog_output(frame("D", 5, "valid") + "Msg 102, Level 15, State 1:\nSyntax error\n"),
])
def test_partial_invalid_or_server_error_output_never_becomes_empty_success(clients, output):
    module, fake = clients
    fake.catalog_override = output
    with pytest.raises(ValueError):
        module.run_operation(payload("databases"))


def test_server_error_exit_is_sanitized_and_temp_files_removed(clients):
    module, fake = clients
    fake.catalog_returncode = 1
    fake.catalog_console = "Msg 4002, Level 14, private-password SELECT private data"
    with pytest.raises(ValueError) as error:
        module.run_operation(payload("databases"))
    assert CONNECTION["password"] not in str(error.value)
    assert "SELECT" not in str(error.value)
    assert not fake.calls[0]["ini_path"].exists()


@pytest.mark.parametrize("password,message", [
    ("error", "CT-LIBRARY error: connection unexpectedly closed\n"),
    ("Error", "Error: connection unexpectedly closed\n"),
    ("Msg", "Msg 208, Level 16, State 1:\n"),
])
def test_catalog_console_error_is_fatal_before_overlapping_password_redaction(clients, password, message):
    module, fake = clients
    # The SQL output file is complete and the process incorrectly exits zero.
    # Its fatal console diagnostic must still prevent a successful connection.
    fake.catalog_console = message
    with pytest.raises(ValueError):
        module.run_operation(payload("databases", connection={**CONNECTION, "password": password}))
    assert not fake.calls[0]["ini_path"].exists()


def test_database_and_optional_owner_search_use_explicit_literal_filters(clients):
    module, fake = clients
    result = module.run_operation(payload("objects", database="연구db", search="%_[!'", kind="U"))
    assert result["objects"] == [OBJECT]
    query = fake.calls[0]["query"]
    assert "[연구db].dbo.sysobjects" in query
    assert "o.uid =" not in query
    assert "o.type = 'U'" in query
    assert "lower(o.name) LIKE lower('%!%!_![!!''%') ESCAPE '!'" in query


def test_columns_verify_owner_object_and_schema_in_one_isql_process(clients):
    module, fake = clients
    result = module.run_operation(payload("columns"))
    assert result["object"] == OBJECT
    assert result["columns"] == COLUMNS
    assert len(fake.calls) == 1
    query = fake.calls[0]["query"]
    assert "o.uid = 7" in query and "o.id = 42" in query


def test_nullable_ase_precision_and_scale_normalize_to_zero(clients):
    module, fake = clients
    obj = OBJECT
    fake.catalog_override = catalog_output(
        frame("O", obj["id"], obj["owner_id"], obj["owner"], obj["name"], obj["kind"]),
        frame("C", 1, "id", 56, "int", 4, "", " ", 0, 0),
    )
    assert module.run_operation(payload("columns"))["columns"] == COLUMNS


@pytest.mark.parametrize("changes", [
    {"owner_id": True}, {"owner_id": "7"}, {"owner_id": 0}, {"search": "a\ngo\n"}, {"kind": "U' OR 1=1"},
])
def test_invalid_search_filters_fail_before_starting_clients(clients, changes):
    module, fake = clients
    with pytest.raises(ValueError):
        module.run_operation(payload("objects", database="safe", **changes))
    assert fake.calls == []


@pytest.mark.parametrize("password", ["with" + chr(code) + "control" for code in (*range(32), 127)])
def test_password_cannot_inject_terminal_input(clients, password):
    module, fake = clients
    with pytest.raises(ValueError):
        module.run_operation(payload("databases", connection={**CONNECTION, "password": password}))
    assert fake.calls == []


@pytest.mark.parametrize("user", ["with\tcontrol", "with\x7fcontrol", "with\x03control"])
def test_user_cannot_inject_terminal_control_characters(clients, user):
    module, fake = clients
    with pytest.raises(ValueError):
        module.run_operation(payload("databases", connection={**CONNECTION, "user": user}))
    assert fake.calls == []


@pytest.mark.parametrize("changes", [{"name": "renamed"}, {"owner": "other"}, {"id": 43}, {"kind": "V"}])
def test_transfer_rechecks_exact_object_before_calling_bcp(clients, tmp_path, changes):
    module, fake = clients
    fake.observed_object.update(changes)
    with pytest.raises(ValueError, match="(?i)object"):
        module.run_operation(payload("transfer", path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction": "out"}))
    assert len(fake.calls) == 1


def test_transfer_rechecks_full_schema_before_calling_bcp(clients, tmp_path):
    module, fake = clients
    fake.observed_columns[0]["length"] = 8
    with pytest.raises(ValueError, match="(?i)schema"):
        module.run_operation(payload("transfer", path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction": "out"}))
    assert len(fake.calls) == 1


@pytest.mark.parametrize("encoding,sap_name", [("utf-8", "utf8"), ("cp949", "cp949"), ("euc-kr", "eucksc")])
def test_out_calls_sap_bcp_character_mode_and_reports_copy_time(clients, tmp_path, encoding, sap_name):
    module, fake = clients
    result = module.run_operation(payload("transfer", path=str(tmp_path / "한글 data.bcp"), columns=COLUMNS,
                                          options={"direction": "out", "encoding": encoding}))
    assert result["rows"] == 3 and result["direction"] == "out"
    assert result["bcp_seconds"] == 0.5 and result["bcp_rows_per_second"] == 6
    argv = fake.calls[1]["argv"]
    assert argv[:3] == ["C:/SAP/bcp.exe", "연구db.dbo.orders", "out"]
    assert argv[3] == str(tmp_path / "한글 data.bcp")
    assert argv[argv.index("-J") + 1] == sap_name
    assert "-c" in argv and "-P" not in argv and "-E" not in argv
    assert "-b" not in argv
    assert argv[argv.index("-T") + 1] == "2147483647"
    assert fake.calls[1]["timeout"] is None


def test_in_uses_append_batch_and_identity_flags_and_reports_progress(clients, tmp_path):
    module, fake = clients
    path = tmp_path / "data.bcp"
    path.write_text("1\n2\n3\n", encoding="utf-8")
    fake.progress = ["1000 rows sent to the server. 1000 rows total sent.\n", "2000 rows sent to the server. 3000 rows total sent.\n"]
    events = []
    result = module.run_operation(payload("transfer", path=str(path), columns=COLUMNS,
                                          options={"direction": "in", "batch_size": 500, "keep_identity": True}), events.append)
    assert result["rows"] == 3
    argv = fake.calls[1]["argv"]
    assert argv[argv.index("-b") + 1] == "500"
    assert argv[argv.index("-m") + 1] == "1"
    assert "-E" in argv
    assert "-T" not in argv
    assert [event["rows"] for event in events if "rows" in event] == [1000, 3000]
    assert CONNECTION["password"] not in str(events)


@pytest.mark.parametrize("output,returncode", [
    ("3 rows copied.\n", 1), ("", 0), ("1000 rows sent to the server. 1000 rows total sent.\n", 0),
    ("3 rows copied.\nCT-LIBRARY error: private-password\n", 0),
    ("3 rows copied.\nError: rejected row\n", 0), ("3 rows copied.\n1 rows failed.\n", 0),
    ("3 rows copied.\nMsg 208, Level 16, State 1:\n", 0),
    ("3 rows copied.\nError in row 2: conversion failed\n", 0),
])
def test_copy_requires_success_exit_final_row_count_and_no_reported_error(clients, tmp_path, output, returncode):
    module, fake = clients
    fake.copy_output, fake.copy_returncode = output, returncode
    with pytest.raises(ValueError) as error:
        module.run_operation(payload("transfer", path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction": "out"}))
    assert CONNECTION["password"] not in str(error.value)


def test_zero_row_out_is_a_valid_completed_export(clients, tmp_path):
    module, fake = clients
    fake.copy_output = "0 rows copied.\n"
    assert module.run_operation(payload("transfer", path=str(tmp_path / "empty.bcp"), columns=COLUMNS,
                                          options={"direction": "out"}))["rows"] == 0


def test_error_in_stream_is_fatal_even_if_returned_output_tail_reports_success(clients, tmp_path):
    module, fake = clients
    fake.progress = ["CT-LIBRARY error: private-password\n", "3 rows copied.\n"]
    with pytest.raises(ValueError):
        module.run_operation(payload("transfer", path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction": "out"}))


def test_literal_control_terminators_are_passed_as_one_argument(clients, tmp_path):
    module, fake = clients
    module.run_operation(payload("transfer", path=str(tmp_path / "data.bcp"), columns=COLUMNS,
                                 options={"direction": "out", "field_hex": "09", "row_hex": "0d0a"}))
    argv = fake.calls[1]["argv"]
    assert argv[argv.index("-t") + 1] == "\t"
    assert argv[argv.index("-r") + 1] == "\r\n"


def test_unsupported_sap_cli_target_name_fails_explicitly_before_bcp(clients, tmp_path):
    module, fake = clients
    obj = {**OBJECT, "name": "order history"}
    fake.observed_object = obj
    with pytest.raises(ValueError, match="(?i)(name|identifier)"):
        module.run_operation(payload("transfer", object=obj, path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction": "out"}))
    assert not any(Path(call["argv"][0]).name.lower() == "bcp.exe" for call in fake.calls)


@pytest.mark.parametrize("password,error", [("rows", False), ("Error", True)])
def test_real_runner_parses_copy_protocol_before_password_redaction(clients, tmp_path, monkeypatch, password, error):
    from nice_tools import ase_process

    module, fake = clients
    # Exercise the real prompt/redaction boundary, including an early error that
    # disappears from the bounded public output tail after later client output.
    transcript = (("Error: rejected row\n" + "copied progress\n" * 1000) if error else "") + "3 rows copied.\n"
    script = tmp_path / "protocol client.py"
    script.write_text(
        "import os, sys\n"
        "print('Password:', end='', flush=True)\n"
        "if os.name == 'nt':\n"
        "    import msvcrt\n"
        "    chars=[]\n"
        "    while True:\n"
        "        char=msvcrt.getwch()\n"
        "        if char in ('\\r', '\\n'): break\n"
        "        chars.append(char)\n"
        "    password=''.join(chars)\n"
        "else:\n"
        "    password=sys.stdin.readline().rstrip('\\r\\n')\n"
        "assert password == " + repr(password) + "\n"
        "print('', flush=True)\n"
        "sys.stdout.write(" + repr(transcript) + ")\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ase_process, "MAX_CLIENT_OUTPUT", 1024)
    public = []

    def boundary(argv, actual_password, **kwargs):
        if Path(argv[0]).name.lower() == "isql.exe":
            return fake.run(argv, actual_password, **kwargs)
        result = ase_process.run_client([sys.executable, "-u", str(script), *argv[1:]], actual_password, **kwargs)
        public.append(result)
        return result

    monkeypatch.setattr(module, "_run_client", boundary)
    request = payload("transfer", connection={**CONNECTION, "password": password}, path=str(tmp_path / "data.bcp"),
                      columns=COLUMNS, options={"direction": "out"})
    if error:
        with pytest.raises(ValueError):
            module.run_operation(request)
        assert public[0]["stdout_truncated"]
    else:
        assert module.run_operation(request)["rows"] == 3
    assert password not in public[0]["stdout"]
