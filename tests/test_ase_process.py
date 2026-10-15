"""Run real disposable clients; Windows exercises their console password input."""

import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest


def client(tmp_path, body):
    script = tmp_path / "가짜 SAP client.py"
    script.write_text(
        "import json, os, sys, time\n"
        "def password():\n"
        "    print('Password:', end='', flush=True)\n"
        "    if os.name == 'nt':\n"
        "        import msvcrt\n"
        "        chars = []\n"
        "        while True:\n"
        "            char = msvcrt.getwch()\n"
        "            if char in ('\\r', '\\n'): break\n"
        "            chars.append(char)\n"
        "        return ''.join(chars)\n"
        "    return sys.stdin.readline().rstrip('\\r\\n')\n"
        + body,
        encoding="utf-8",
    )
    return [sys.executable, "-u", str(script)]


def run(*args, **kwargs):
    from nice_tools.ase_process import run_client

    return run_client(*args, **kwargs)


def test_prompts_without_secret_arguments_and_then_accepts_sql(tmp_path):
    secret = "A-test-secret$012"
    argv = client(tmp_path, "pw = password()\nprint(json.dumps({'password_ok': pw == 'A-test-secret$012', 'argv': sys.argv[1:]}), flush=True)\nprint('SQL:' + sys.stdin.readline().strip(), flush=True)\n")
    output = run(argv, secret, input_text="select 1\n", timeout=10)
    assert output["returncode"] == 0
    assert '"password_ok": true' in output["stdout"]
    assert "SQL:select 1" in output["stdout"]
    assert secret not in output["stdout"]
    assert secret not in argv
    assert output["elapsed_seconds"] >= 0


@pytest.mark.parametrize("secret", ["", "비밀키-secret"])
def test_empty_and_unicode_passwords_use_the_prompt(tmp_path, secret):
    argv = client(tmp_path, "pw = password()\nprint('MATCH:' + str(pw == " + repr(secret) + "), flush=True)\n")
    assert "MATCH:True" in run(argv, secret, timeout=10)["stdout"]


@pytest.mark.parametrize("secret", ["one\ntwo", "one\rtwo", "one\x00two", "one\ttwo", "one\x1btwo", "one\x7ftwo"])
def test_multiline_password_rejected_before_starting_client(tmp_path, secret):
    marker = tmp_path / "started"
    argv = client(tmp_path, "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('started')\n")
    with pytest.raises(ValueError):
        run(argv, secret, timeout=2)
    assert not marker.exists()


def test_output_callbacks_and_returned_text_redact_split_password(tmp_path):
    secret = "dont-leak-secret"
    argv = client(tmp_path, "pw = password()\nprint('logged:' + pw[:5], end='', flush=True)\ntime.sleep(.05)\nprint(pw[5:], flush=True)\n")
    chunks = []
    output = run(argv, secret, timeout=10, on_output=chunks.append)
    assert secret not in output["stdout"]
    assert secret not in "".join(chunks)
    assert "[비밀번호 숨김]" in output["stdout"]


def test_real_client_output_crosses_bcp_worker_protocol_and_parent_console(tmp_path, monkeypatch, capsys):
    """Exercise prompt -> CLI -> JSON records -> parent console without ASE."""
    import io
    from nice_tools import ase_cli, ase_jobs, ase_worker
    from nice_tools.ase_process import _RedactedOutput, run_client

    secret = "native-stream-secret"
    argv = client(tmp_path, "pw = password()\nprint('Starting copy...', flush=True)\nprint(pw[:7], end='', flush=True)\ntime.sleep(.05)\nprint(pw[7:], flush=True)\nprint('3 rows copied.', flush=True)\n")
    monkeypatch.setattr(ase_cli, "_run_client", lambda _argv, password, **kwargs: run_client(argv, password, **kwargs))
    obj = {"database": "db", "owner": "dbo", "name": "t"}
    options = {"direction": "out", "encoding": "utf-8", "field_hex": "09", "row_hex": "0a"}
    def execute(payload, emit):
        return ase_cli._copy(payload["connection"], obj, [], str(tmp_path / "data.bcp"), options,
                             {"bcp_path": "unused-fixture-bcp.exe"}, emit)
    monkeypatch.setattr(ase_worker, "execute", execute)
    stream = io.StringIO()
    assert ase_worker.main(io.StringIO(json.dumps({"connection": {"host": "localhost", "port": 5000, "user": "u", "password": secret}})), stream) == 0
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert records[-1]["event"] == "result" and records[-1]["result"]["rows"] == 3
    assert secret not in stream.getvalue()
    # This is the same streaming redactor/sink used by the owning job; the
    # process-manager tests separately verify dispatch before worker exit.
    console = _RedactedOutput(secret, ase_jobs._write_bcp_output)
    for record in records:
        if record.get("type") == "bcp_output":
            console.feed(record["message"])
    console.feed("", final=True)
    output = capsys.readouterr().out
    assert "Starting copy..." in output and "3 rows copied." in output
    assert secret not in output and "native-" not in output


def test_failed_client_exit_preserves_its_nonsecret_error(tmp_path):
    argv = client(tmp_path, "print('SAP executable cannot connect', flush=True)\nsys.exit(7)\n")
    output = run(argv, "private", timeout=10)
    assert output["returncode"] == 7
    assert "cannot connect" in output["stdout"]


def test_success_without_password_prompt_is_rejected(tmp_path):
    argv = client(tmp_path, "print('Unexpected client behavior', flush=True)\n")
    with pytest.raises(ValueError, match="프롬프트"):
        run(argv, "private", timeout=10)


def test_timeout_kills_registered_client(tmp_path):
    argv = client(tmp_path, "pw = password()\ntime.sleep(120)\n")
    handles = []
    started = time.monotonic()
    with pytest.raises(ValueError, match="시간"):
        run(argv, "private", timeout=.3, register=handles.append)
    assert time.monotonic() - started < 5
    assert len(handles) == 1
    assert handles[0].poll() is not None


def test_registered_handle_cancels_client_from_another_thread(tmp_path):
    argv = client(tmp_path, "pw = password()\ntime.sleep(120)\n")
    handles = []
    ready = threading.Event()
    result = []

    def register(handle):
        handles.append(handle)
        ready.set()

    def execute():
        try:
            result.append(run(argv, "private", timeout=10, register=register))
        except ValueError as error:
            result.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert ready.wait(5)
    handles[0].kill()
    thread.join(5)
    assert not thread.is_alive()
    assert handles[0].poll() is not None


def test_password_prompt_split_across_output_chunks(tmp_path):
    argv = client(tmp_path, "print('Pass', end='', flush=True)\ntime.sleep(.05)\nprint('word:', end='', flush=True)\nif os.name == 'nt':\n    import msvcrt\n    chars=[]\n    while True:\n        ch=msvcrt.getwch()\n        if ch == '\\r': break\n        chars.append(ch)\n    pw=''.join(chars)\nelse:\n    pw=sys.stdin.readline().strip()\nprint('MATCH:' + str(pw == 'private'), flush=True)\n")
    assert "MATCH:True" in run(argv, "private", timeout=10)["stdout"]


def test_output_tail_is_bounded_while_callback_sees_complete_stream(tmp_path, monkeypatch):
    from nice_tools import ase_process

    monkeypatch.setattr(ase_process, "MAX_CLIENT_OUTPUT", 1024)
    argv = client(tmp_path, "pw = password()\nprint('x' * 2000, flush=True)\nprint('Msg 50000 fatal error', flush=True)\n")
    chunks = []
    output = run(argv, "private", timeout=10, on_output=chunks.append)
    assert output["stdout_truncated"]
    assert len(output["stdout"]) <= 1024
    assert "Msg 50000 fatal error" in output["stdout"]
    assert len("".join(chunks)) > 2000


def test_ansi_sequences_do_not_hide_prompt_or_leak_echoed_secret(tmp_path):
    argv = client(tmp_path, "print('\\x1b[32m', end='', flush=True)\npw=password()\nprint('\\x1b[0m' + pw, flush=True)\n")
    output = run(argv, "private", timeout=10)
    assert "private" not in output["stdout"]
    assert "\x1b" not in output["stdout"]


def test_timeout_cleans_descendant_processes(tmp_path):
    marker = tmp_path / "escaped-child"
    child_code = "import time; from pathlib import Path; time.sleep(1); Path(" + repr(str(marker)) + ").write_text('escaped')"
    argv = client(tmp_path, "pw=password()\nimport subprocess\nsubprocess.Popen([sys.executable, '-c', " + repr(child_code) + "])\ntime.sleep(120)\n")
    with pytest.raises(ValueError, match="시간"):
        run(argv, "private", timeout=.3)
    time.sleep(1.1)
    assert not marker.exists()


def test_registration_cleanup_runs_after_process_is_reaped(tmp_path):
    argv = client(tmp_path, "pw=password()\nprint('done', flush=True)\n")
    handles = []
    cleaned = []

    def register(handle):
        handles.append(handle)
        return lambda: cleaned.append(handle.poll())

    output = run(argv, "private", register=register, timeout=10)
    assert output["returncode"] == 0
    assert cleaned == [0]
    assert handles[0].wait(timeout=.1) == 0


def test_password_argument_is_rejected_before_starting_client(tmp_path):
    marker = tmp_path / "started-password-argument"
    argv = client(tmp_path, "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('started')\n")
    with pytest.raises(ValueError, match="인자"):
        run([*argv, "-Pprivate"], "private", timeout=2)
    assert not marker.exists()


def test_callback_error_cannot_expose_database_password(tmp_path):
    argv = client(tmp_path, "pw=password()\nprint('done', flush=True)\n")

    def reject(text):
        raise ValueError("private")

    with pytest.raises(ValueError) as error:
        run(argv, "private", on_output=reject, timeout=10)
    assert "private" not in str(error.value)


def test_command_arguments_preserve_literal_newlines_quotes_and_backslashes(tmp_path):
    values = ["line one\nline two", "\r\n", "\t", 'a"quoted"name', "trailing\\", ""]
    argv = client(tmp_path, "pw=password()\nprint('ARGS:' + json.dumps(sys.argv[1:]), flush=True)\n")
    output = run([*argv, *values], "private", timeout=10)
    line = next(line for line in output["stdout"].splitlines() if "ARGS:" in line)
    assert json.loads(line.split("ARGS:", 1)[1]) == values


def test_windows_command_line_quotes_newline_terminators():
    from nice_tools.ase_process import windows_command_line

    text = windows_command_line(["bcp.exe", "-r", "\n"])
    assert text.endswith('"\n"')


def test_secret_contained_in_redaction_marker_still_does_not_leak(tmp_path):
    argv = client(tmp_path, "pw=password()\nprint(pw, flush=True)\n")
    assert "비밀번호" not in run(argv, "비밀번호", timeout=10)["stdout"]


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows suspended ConPTY process creation")
def test_windows_owns_descendants_spawned_before_password_prompt(tmp_path):
    marker = tmp_path / "escaped-child-before-auth"
    child_code = "import time; from pathlib import Path; time.sleep(1); Path(" + repr(str(marker)) + ").write_text('escaped')"
    argv = client(tmp_path, "import subprocess\nsubprocess.Popen([sys.executable, '-c', " + repr(child_code) + "])\ntime.sleep(120)\n")
    with pytest.raises(ValueError, match="시간"):
        run(argv, "private", timeout=.3)
    time.sleep(1.1)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt" or not os.environ.get("NICE_TOOLS_SAP_TEST_CLIENTS"), reason="Requires compiled Windows console fixtures")
def test_windows_native_console_password_and_complete_exit_output(tmp_path):
    folder = Path(os.environ["NICE_TOOLS_SAP_TEST_CLIENTS"])
    executable = folder / "bcp.exe"
    assert executable.is_file(), "The CI console fixture must exist."
    fixture_password = "fixture-db-password-never-log!42"
    handles = []
    output_file = tmp_path / "한글 output.txt"
    argv = [str(executable), "fixture_db.dbo.orders", "out", str(output_file), "-c", "-J", "utf8", "-t", "\t", "-r", "\n", "-T", "2147483647"]
    chunks = []
    result = run(argv, fixture_password, on_output=chunks.append, register=handles.append, timeout=10)
    assert result["returncode"] == 0
    assert fixture_password not in result["stdout"]
    assert fixture_password not in "".join(chunks)
    assert handles[0].poll() == 0
    assert "3 rows copied." in result["stdout"]
    assert output_file.read_bytes() == b"1\n2\n3\n"


def test_worker_resume_helper_is_inert_off_windows():
    if os.name == "nt":
        pytest.skip("The native Windows resume test covers this platform.")
    from nice_tools.ase_process import resume_process

    assert resume_process(os.getpid()) is None


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows suspended subprocess")
def test_resume_worker_only_after_windows_job_assignment(tmp_path):
    import subprocess
    from nice_tools.ase_process import WindowsJob, resume_process

    marker = tmp_path / "worker-resumed"
    code = "from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('resumed')"
    process = subprocess.Popen([sys.executable, "-c", code], creationflags=0x4 | subprocess.CREATE_NO_WINDOW)
    job = None
    try:
        time.sleep(.05)
        assert not marker.exists()
        job = WindowsJob(process.pid)
        resume_process(process.pid)
        assert process.wait(timeout=5) == 0
        assert marker.read_text() == "resumed"
    finally:
        if job is not None:
            job.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("secret", ["rows", "Error"])
def test_private_protocol_parser_receives_semantics_before_public_redaction(tmp_path, secret):
    argv = client(tmp_path, "pw=password()\nprint('echoed:' + pw, flush=True)\nprint('Error: conversion rejected', flush=True)\nprint('3 rows copied.', flush=True)\n")
    raw = []
    public = []
    result = run(argv, secret, on_output=public.append, _parse_output=raw.append, timeout=10)
    protocol = "".join(raw)
    assert "Error: conversion rejected" in protocol
    assert "3 rows copied." in protocol
    assert secret not in "".join(public)
    assert secret not in result["stdout"]


def test_private_protocol_parser_exception_is_sanitized_and_reaps_client(tmp_path):
    argv = client(tmp_path, "pw=password()\nprint('private-secret', flush=True)\ntime.sleep(120)\n")
    handles = []

    def parse(text):
        if "private-secret" in text:
            raise ValueError(text)

    with pytest.raises(ValueError) as error:
        run(argv, "private-secret", _parse_output=parse, register=handles.append, timeout=10)
    assert "private-secret" not in str(error.value)
    assert handles[0].poll() is not None


def finished_console(peeks, final_bytes=b""):
    """Model a closed native console whose reader pipe still remains valid."""
    import ctypes
    from nice_tools.ase_process import _WindowsConsole

    class Kernel:
        def __init__(self):
            self.peeks = list(peeks)
            self.peek_calls = 0

        def PeekNamedPipe(self, handle, buffer, length, read, available, left):
            self.peek_calls += 1
            count = self.peeks.pop(0)
            if count is None:
                return False
            available._obj.value = count
            return True

        def ReadFile(self, handle, buffer, length, received, overlapped):
            ctypes.memmove(buffer, final_bytes, len(final_bytes))
            received._obj.value = len(final_bytes)
            return True

    class ClosedConsoleThread:
        def is_alive(self):
            return False

    console = _WindowsConsole.__new__(_WindowsConsole)
    console.kernel = Kernel()
    console.output = 123
    console.poll = lambda: 0
    console._console_close_thread = ClosedConsoleThread()
    console._start_console_close = lambda: None
    return console


def test_finished_console_keeps_waiting_while_empty_reader_pipe_stays_open():
    console = finished_console([0, 0])
    assert console.read() == b""
    assert console.read() == b""
    assert console.kernel.peek_calls == 2


def test_finished_console_drains_late_flush_before_broken_pipe_eof(monkeypatch):
    from nice_tools import ase_process

    monkeypatch.setattr(ase_process.ctypes, "get_last_error", lambda: 109, raising=False)
    final = b"3 rows copied.\n"
    console = finished_console([0, len(final), None], final)
    assert console.read() == b""
    assert console.read() == final
    assert console.read() is None
    assert console.kernel.peek_calls == 3


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows parent with redirected standard handles")
def test_windows_owned_console_does_not_inherit_redirected_parent_stdio(tmp_path):
    import subprocess

    argv = client(tmp_path, "pw=password()\nprint('MATCH:' + str(pw == 'private'), flush=True)\n")
    parent_code = (
        "import json; from nice_tools.ase_process import run_client; "
        "result=run_client(" + repr(argv) + ", 'private', timeout=8); "
        "print(json.dumps(result))"
    )
    parent = subprocess.run(
        [sys.executable, "-c", parent_code], input="", stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert parent.returncode == 0, parent.stderr
    result = json.loads(parent.stdout)
    assert result["returncode"] == 0
    assert "Password:" in result["stdout"]
    assert "MATCH:True" in result["stdout"]
