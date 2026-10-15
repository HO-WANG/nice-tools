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


class FakeClients:
    def __init__(self):
        self.calls = []
        self.observed_object = dict(OBJECT)
        self.observed_columns = [dict(c) for c in COLUMNS]
        self.metadata_calls = []
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
        assert Path(argv[0]).name.lower() == "bcp.exe", "Metadata must not invoke isql.exe"
        if on_output:
            for chunk in self.progress:
                on_output(chunk.replace(password, "[hidden]") if password else chunk)
            on_output(self.copy_output.replace(password, "[hidden]") if password else self.copy_output)
        if _parse_output:
            for chunk in self.progress:
                _parse_output(chunk)
            _parse_output(self.copy_output)
        return {"stdout": self.copy_output.replace(password, "[hidden]") if password else self.copy_output,
                "returncode": self.copy_returncode, "elapsed_seconds": 0.5}


    def metadata(self, request, emit=None, register=None):
        self.metadata_calls.append(request)
        assert request["operation"] == "columns"
        return {"object": dict(self.observed_object), "columns": [dict(column) for column in self.observed_columns]}


@pytest.fixture
def clients(monkeypatch):
    module = cli_module()
    fake = FakeClients()
    import nice_tools
    from types import SimpleNamespace
    backend = SimpleNamespace(run_operation=fake.metadata)
    monkeypatch.setitem(sys.modules, "nice_tools.ase_odbc", backend)
    monkeypatch.setattr(nice_tools, "ase_odbc", backend, raising=False)
    monkeypatch.setattr(module, "_client_config", lambda: {"bcp_path": "C:/SAP/bcp.exe"})
    monkeypatch.setattr(module, "_run_client", fake.run)
    return module, fake


def payload(operation, **changes):
    return {"operation": operation, "connection": dict(CONNECTION), "object": dict(OBJECT), **changes}


@pytest.mark.parametrize("operation", ["databases", "owners", "objects", "columns"])
def test_cli_does_not_handle_catalog_operations(clients, operation):
    module, fake = clients
    with pytest.raises(ValueError):
        module.run_operation(payload(operation))
    assert fake.calls == []
    assert fake.metadata_calls == []


@pytest.mark.parametrize("password", ["with" + chr(code) + "control" for code in (*range(32), 127)])
def test_password_cannot_inject_terminal_input(clients, tmp_path, password):
    module, fake = clients
    with pytest.raises(ValueError):
        module.run_operation(payload("transfer", connection={**CONNECTION, "password": password}, path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction":"out"}))
    assert fake.calls == []


@pytest.mark.parametrize("user", ["with\tcontrol", "with\x7fcontrol", "with\x03control"])
def test_user_cannot_inject_terminal_control_characters(clients, tmp_path, user):
    module, fake = clients
    with pytest.raises(ValueError):
        module.run_operation(payload("transfer", connection={**CONNECTION, "user": user}, path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction":"out"}))
    assert fake.calls == []


@pytest.mark.parametrize("changes", [{"name": "renamed"}, {"owner": "other"}, {"id": 43}, {"kind": "V"}])
def test_transfer_rechecks_exact_object_before_calling_bcp(clients, tmp_path, changes):
    module, fake = clients
    fake.observed_object.update(changes)
    with pytest.raises(ValueError, match="(?i)object"):
        module.run_operation(payload("transfer", path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction": "out"}))
    assert len(fake.metadata_calls) == 1
    assert fake.calls == []


def test_transfer_rechecks_full_schema_before_calling_bcp(clients, tmp_path):
    module, fake = clients
    fake.observed_columns[0]["length"] = 8
    with pytest.raises(ValueError, match="(?i)schema"):
        module.run_operation(payload("transfer", path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction": "out"}))
    assert len(fake.metadata_calls) == 1
    assert fake.calls == []


@pytest.mark.parametrize("encoding,sap_name", [("utf-8", "utf8"), ("cp949", "cp949"), ("euc-kr", "eucksc")])
def test_out_calls_sap_bcp_character_mode_and_reports_copy_time(clients, tmp_path, encoding, sap_name):
    module, fake = clients
    result = module.run_operation(payload("transfer", path=str(tmp_path / "한글 data.bcp"), columns=COLUMNS,
                                          options={"direction": "out", "encoding": encoding}))
    assert result["rows"] == 3 and result["direction"] == "out"
    assert result["bcp_seconds"] == 0.5 and result["bcp_rows_per_second"] == 6
    argv = fake.calls[0]["argv"]
    assert argv[:3] == ["C:/SAP/bcp.exe", "연구db.dbo.orders", "out"]
    assert argv[3] == str(tmp_path / "한글 data.bcp")
    assert argv[argv.index("-J") + 1] == sap_name
    assert "-c" in argv and "-P" not in argv and "-E" not in argv
    assert "-b" not in argv
    assert argv[argv.index("-T") + 1] == "2147483647"
    assert fake.calls[0]["timeout"] is None


def test_in_uses_append_batch_and_identity_flags_and_reports_progress(clients, tmp_path):
    module, fake = clients
    path = tmp_path / "data.bcp"
    path.write_text("1\n2\n3\n", encoding="utf-8")
    fake.progress = ["1000 rows sent to the server. 1000 rows total sent.\n", "2000 rows sent to the server. 3000 rows total sent.\n"]
    events = []
    result = module.run_operation(payload("transfer", path=str(path), columns=COLUMNS,
                                          options={"direction": "in", "batch_size": 500, "keep_identity": True}), events.append)
    assert result["rows"] == 3
    argv = fake.calls[0]["argv"]
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


def test_command_diagnostic_includes_all_arguments_and_endpoint_without_password(clients, tmp_path):
    module, fake = clients
    events = []
    path = str(tmp_path / '한글 data.bcp')
    module.run_operation(payload('transfer', path=path, columns=COLUMNS,
                                options={'direction': 'out'}), events.append)
    command = next(event['message'] for event in events if event.get('type') == 'bcp_command')
    assert 'C:/SAP/bcp.exe' in command and '연구db.dbo.orders' in command
    assert path in command and 'ase.internal:5000' in command
    for flag in ('-I', '-S', '-U', '-J', '-z', '-c', '-t', '-r', '-m', '-T'):
        assert f'"{flag}"' in command
    assert '\\t' in command and '\\n' in command
    assert CONNECTION['password'] not in command and '"-P"' not in command
    assert events.index(next(e for e in events if e.get('type') == 'bcp_command')) < events.index(next(e for e in events if e.get('type') == 'bcp_output'))


@pytest.mark.parametrize('direction', ['in', 'out'])
def test_failure_reports_sanitized_server_message_and_direction(clients, tmp_path, direction):
    module, fake = clients
    path = tmp_path / 'input.bcp'
    path.write_text('1\n')
    fake.copy_output = "Server Message: Msg 10330, Level 14\nSELECT permission denied on object orders. private-password\n"
    fake.copy_returncode = 1
    with pytest.raises(ValueError) as error:
        module.run_operation(payload('transfer', path=str(path), columns=COLUMNS,
                                    options={'direction': direction}))
    detail = str(error.value)
    assert 'Server Message:' in detail and 'SELECT permission denied' in detail
    assert '연구db.dbo.orders' in detail and 'exit code: 1' in detail
    assert ('Completed import batches may remain' in detail) == (direction == 'in')
    assert CONNECTION['password'] not in detail


def test_failure_log_is_bounded_and_preserves_beginning_and_end(clients, tmp_path):
    module, fake = clients
    fake.progress = ['Server Message: SELECT permission denied\n', 'x' * 40000]
    fake.copy_output = '\nBCP failed: final diagnostic\n'
    fake.copy_returncode = 1
    with pytest.raises(ValueError) as error:
        module.run_operation(payload('transfer', path=str(tmp_path / 'data.bcp'), columns=COLUMNS,
                                    options={'direction': 'out'}))
    detail = str(error.value)
    assert 'SELECT permission denied' in detail and 'final diagnostic' in detail
    assert len(detail) < 17000 and 'truncated' in detail


@pytest.mark.parametrize('secret', ['quote"secret', 'back\\slash', '비밀번호', '숨김'])
def test_command_redacts_credentials_before_windows_quoting(secret):
    module = cli_module()
    message = module._command_diagnostic(['bcp.exe', 'db.dbo.t', 'out', f'C:\\{secret}\\file.txt'],
                                         {**CONNECTION, 'password': secret})
    assert secret not in message
    assert 'bcp.exe' in message and 'db.dbo.t' in message and 'file.txt' in message


@pytest.mark.parametrize('secret', ['orders', 'failed', '숨김'])
def test_failure_redacts_credentials_in_context_and_fixed_labels(clients, tmp_path, secret):
    module, fake = clients
    fake.copy_output = 'Server Message: SELECT permission denied\n'
    fake.copy_returncode = 1
    with pytest.raises(ValueError) as error:
        module.run_operation(payload('transfer', connection={**CONNECTION, 'password': secret},
                                    path=str(tmp_path / 'data.bcp'), columns=COLUMNS, options={'direction': 'out'}))
    assert secret not in str(error.value)


@pytest.mark.parametrize("failed", [False, True])
def test_bcp_messages_stream_through_the_worker_callback_even_on_failure(clients, tmp_path, failed):
    module, fake = clients
    fake.progress = ["Starting copy...\n", "1000 rows total sent.\n"]
    if failed:
        fake.copy_output = "CT-LIBRARY error: private-password login rejected\n"
        fake.copy_returncode = 1
    events = []
    request = payload("transfer", path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction": "out"})
    if failed:
        with pytest.raises(ValueError):
            module.run_operation(request, events.append)
    else:
        assert module.run_operation(request, events.append)["rows"] == 3
    messages = [event["message"] for event in events if event.get("type") == "bcp_output"]
    expected = "".join(fake.progress) + fake.copy_output.replace(CONNECTION["password"], "[hidden]")
    assert "".join(messages) == expected
    assert CONNECTION["password"] not in "".join(messages)


def test_error_in_stream_is_fatal_even_if_returned_output_tail_reports_success(clients, tmp_path):
    module, fake = clients
    fake.progress = ["CT-LIBRARY error: private-password\n", "3 rows copied.\n"]
    with pytest.raises(ValueError):
        module.run_operation(payload("transfer", path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction": "out"}))


def test_literal_control_terminators_are_passed_as_one_argument(clients, tmp_path):
    module, fake = clients
    module.run_operation(payload("transfer", path=str(tmp_path / "data.bcp"), columns=COLUMNS,
                                 options={"direction": "out", "field_hex": "09", "row_hex": "0d0a"}))
    argv = fake.calls[0]["argv"]
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
        result = ase_process.run_client([sys.executable, "-u", str(script), *argv[1:]], actual_password,
                                        **{**kwargs, "timeout": 10})
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


def test_transfer_schema_uses_fresh_odbc_metadata_without_an_isql_path(clients, tmp_path):
    module, fake = clients
    result = module.run_operation(payload("transfer", path=str(tmp_path / "data.bcp"), columns=COLUMNS, options={"direction":"out"}))
    assert result["rows"] == 3
    assert len(fake.metadata_calls) == 1
    assert fake.metadata_calls[0]["object"] == OBJECT
    assert fake.metadata_calls[0]["connection"] == {**CONNECTION, "login_timeout":15, "query_timeout":60}
    assert len(fake.calls) == 1
    assert fake.calls[0]["argv"][0] == "C:/SAP/bcp.exe"
    assert not fake.calls[0]["ini_path"].exists()


def test_automatic_out_fetches_fresh_schema_once(clients, tmp_path):
    module, fake = clients
    result = module.run_operation(payload('transfer', path=str(tmp_path / 'data.bcp'), auto_validate=True, options={'direction': 'out'}))
    assert result['rows'] == 3
    assert len(fake.metadata_calls) == 1
    assert len(fake.calls) == 1


def test_automatic_out_still_rejects_empty_or_wrong_object_schema(clients, tmp_path):
    module, fake = clients
    fake.observed_columns = []
    with pytest.raises(ValueError):
        module.run_operation(payload('transfer', path=str(tmp_path / 'data.bcp'), auto_validate=True, options={'direction': 'out'}))
    assert not fake.calls
