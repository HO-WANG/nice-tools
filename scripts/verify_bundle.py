"""배포 파일 하나만 복사해 실행하고 실제 화면·업로드 요청을 검사한다."""

import argparse
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import json
from pathlib import Path

from smoke import main as smoke_main


def validate_windows_x64(path: Path):
    if path.name != "nice-tools.exe":
        raise ValueError("Distribution must be named nice-tools.exe")
    with path.open("rb") as file:
        header = file.read(64)
        if header[:2] != b"MZ" or len(header) < 64:
            raise ValueError("Distribution is not a Windows executable")
        file.seek(struct.unpack_from("<I", header, 60)[0])
        pe = file.read(6)
        if pe[:4] != b"PE\0\0" or pe[4:] != b"\x64\x86":
            raise ValueError("Distribution must be Windows x64")


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            process.kill()
        process.wait(timeout=10)


def spawn_worker(executable, directory, worker="--ase-worker"):
    guard = None
    process = subprocess.Popen(
        [str(executable), worker], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=directory,
        creationflags=(subprocess.CREATE_NO_WINDOW | 0x00000004) if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    try:
        if os.name == "nt":
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from nice_tools.ase_process import WindowsJob, resume_process
            guard = WindowsJob(process.pid)
            resume_process(process.pid)
        return process, guard
    except BaseException:
        if guard is not None:
            guard.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
        raise


def worker_request(executable, payload, directory, expect_error=False, worker="--ase-worker"):
    process, guard = spawn_worker(executable, directory, worker)
    try:
        stdout, stderr = process.communicate(json.dumps(payload).encode(), timeout=60)
        returncode = process.returncode
    finally:
        if guard is not None:
            guard.close()
        elif process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
    password = payload.get("connection", {}).get("password", "").encode()
    assert not password or (password not in stdout and password not in stderr), "Worker emitted private credentials"
    events = [json.loads(line) for line in stdout.splitlines()]
    if expect_error:
        assert returncode != 0 and any(event["event"] == "error" for event in events), "Expected a failed transfer"
        return events
    assert returncode == 0, "Frozen worker failed"
    results = [event["result"] for event in events if event["event"] == "result"]
    assert len(results) == 1, "Frozen worker did not return one result"
    return results[0]


def local_comparison_worker_checks(executable, directory):
    """Read real local files through the actual frozen worker, without a dialog double."""
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from nice_tools.local_compare_worker import folder_descriptor
    left,right=Path(directory)/'로컬 before',Path(directory)/'로컬 after'
    left.mkdir();right.mkdir()
    (left/'nested').mkdir();(right/'nested').mkdir()
    (left/'nested'/'encoding.txt').write_bytes('한국어 설정\n'.encode('cp949'))
    (right/'nested'/'encoding.txt').write_text('한국어 설정\n',encoding='utf-8')
    for root,prefix in ((left,'old'),(right,'new')):
        (root/'changed.txt').write_text('\n'.join(f'{prefix} {i}' for i in range(225)),encoding='utf-8')
    # Complete comparison beyond the former size cap, including the final byte.
    content=b'a'*(11*1024*1024)
    (left/'large.txt').write_bytes(content+b'x')
    (right/'large.txt').write_bytes(content+b'y')
    (left/'invalid.txt').write_bytes(b'binary\0invalid')
    (right/'invalid.txt').write_bytes(b'binary\0invalid')
    (right/'limit.txt').write_text(''.join(f'new {i}\n' for i in range(1000)),encoding='utf-8')
    shared='202609|1011156045|CT9701|101'
    for root,prefix,middle in ((left,'old',['removed',shared]),(right,'new',[shared,'inserted'])):
        records=([f'{prefix} {i}' for i in range(300)] + ['before'] + middle
                 + ['after'] + [f'{prefix} tail {i}' for i in range(300)])
        (root/'alignment.txt').write_text('\n'.join(records)+'\n',encoding='utf-8')
    cache=Path(directory)/'native-comparison-cache'
    payload={'operation':'compare','left':folder_descriptor(left),'right':folder_descriptor(right),
             'options':{},'cache_dir':str(cache)}
    result=worker_request(executable,payload,directory,worker='--comparison-worker')
    assert result['summary']=={'total':6,'same':1,'different':3,'left_only':0,'right_only':1,'error':1}, 'Frozen local full-file/encoding comparison failed'
    reports={item['path']:item.get('reports',{}) for item in result['results']}
    assert reports['changed.txt']['compared']['state']=='ready', 'Frozen small report was not prepared'
    assert reports['limit.txt']['compared']['state']=='deferred', 'Frozen exact1000 threshold was not deferred'
    query={'operation':'diff','cache_dir':str(cache),'path':'changed.txt','mode':'compared','page':0}
    page=worker_request(executable,query,directory,worker='--comparison-worker')
    assert page['additions']==225 and page['deletions']==225 and len(page['lines'])==200 and page['pages']==3
    last=worker_request(executable,{**query,'page':2},directory,worker='--comparison-worker')
    assert last['lines'][-1]['text']=='new 224' and last['total_lines']==451, 'Frozen cached diff lost its final line'
    deferred=worker_request(executable,{**query,'path':'alignment.txt'},directory,worker='--comparison-worker')
    assert deferred['state']=='deferred' and deferred['lines']==[], 'Frozen lookup started deferred analysis'
    alignment=worker_request(executable,{**query,'path':'alignment.txt','page':3,'analyze':True},directory,worker='--comparison-worker')
    assert (alignment['additions'],alignment['deletions'])==(601,601), 'Frozen diff inflated change counts'
    assert [row for row in alignment['lines'] if row['text']==shared]==[
        {'kind':'context','text':shared,'old_line':303,'new_line':302}], 'Frozen diff changed an identical shifted record'
    assert alignment['truncated'] and alignment['total_lines']==1000 and alignment['pages']==5
    limit=worker_request(executable,{**query,'path':'limit.txt','analyze':True},directory,worker='--comparison-worker')
    assert limit['additions']==1000 and limit['deletions']==0 and limit['truncated'] and limit['full_total_lines']==1001
    tail=worker_request(executable,{**query,'path':'limit.txt','page':4},directory,worker='--comparison-worker')
    assert len(tail['lines'])==200 and tail['lines'][-1]['text']=='new 998', 'Frozen report exceeded/lost display cap'
    (right/'changed.txt').write_bytes(b'edited after comparison\n')
    failed=worker_request(executable,query,directory,expect_error=True,worker='--comparison-worker')
    assert any(event.get('code')=='changed' for event in failed), 'Frozen diff reused stale source metadata'
    print('Local comparison actual worker: direct files, mixed encodings, >10MiB final-byte difference, invalid text, cached pages, shifted equal records and change detection passed.')


def text_inspection_worker_checks(executable,directory):
    """Exercise real frozen character inspection, not a runtime simulator."""
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from nice_tools.text_inspection_worker import file_descriptor
    folder=Path(directory)/'숨은 문자 검사'
    folder.mkdir()
    path=folder/'문자.txt'
    original='A\u00a0B\u200bC\x00\r\n한글中😀|1\t'.encode('utf-8-sig')
    path.write_bytes(original)
    def scan(source,cache,options):
        payload={'operation':'scan','source':file_descriptor(source),'cache_dir':str(cache),'options':options}
        return worker_request(executable,payload,directory,worker='--text-inspection-worker')['summary']
    def page(cache,index=0,error=False):
        return worker_request(executable,{'operation':'page','cache_dir':str(cache),'page':index},directory,
                              expect_error=error,worker='--text-inspection-worker')
    regular=folder/'regular-cache'
    summary=scan(path,regular,{})
    assert summary['findings']==3 and summary['bom'] and summary['common_whitespace']['crlf']==1
    assert [row['codepoint'] for row in page(regular)['results']]==['U+00A0','U+200B','U+0000']
    strict=folder/'strict-cache'
    summary=scan(path,strict,{'encoding':'UTF8','strict_mode':True})
    assert summary['findings']==5 and summary['kinds']['strict']==2
    assert path.read_bytes()==original,'Inspection changed its source'
    legacy=folder/'legacy.txt';legacy.write_bytes('text Привет'.encode('cp1251'))
    summary=scan(legacy,folder/'legacy-cache',{'encoding':'windows-1251','strict_mode':True})
    assert summary['findings']==6 and summary['encoding']=='cp1251'
    wide=folder/'wide.txt';wide.write_bytes('a\0b\u200b'.encode('utf-16'))
    assert scan(wide,folder/'wide-cache',{})['findings']==2
    variation=folder/'variation.txt';variation.write_text('A\u180b\u180fB',encoding='utf-8')
    assert scan(variation,folder/'variation-cache',{})['kinds']['variation']==2
    invalid=folder/'invalid-utf7.txt';invalid.write_bytes(b'+2AA-\0')
    invalid_cache=folder/'invalid-cache'
    failed=worker_request(executable,{'operation':'scan','source':file_descriptor(invalid),
                          'cache_dir':str(invalid_cache),'options':{'encoding':'utf-7'}},directory,
                          expect_error=True,worker='--text-inspection-worker')
    assert any(event.get('code')=='encoding' for event in failed) and not (invalid_cache/'metadata.json').exists()
    large=folder/'large.txt';large.write_bytes(b'a'*(11*1024*1024)+'中'.encode())
    large_cache=folder/'large-cache'
    summary=scan(large,large_cache,{'encoding':'utf-8','strict_mode':True})
    assert summary['findings']==1 and page(large_cache)['results'][0]['column']==11*1024*1024+1
    many=folder/'many.txt';many.write_text('\u200b\n'*405,encoding='utf-8')
    many_cache=folder/'many-cache'
    assert scan(many,many_cache,{})['findings']==405
    last=page(many_cache,2)
    assert last['pages']==3 and len(last['results'])==5 and last['results'][-1]['line']==405
    path.write_bytes(b'changed\n')
    assert any(event.get('code')=='changed' for event in page(regular,error=True))
    print('Text inspection owned worker: invisible/NUL characters, BOM, strict mode, manual codec aliases, UTF16, >10MiB late finding, complete pages, unchanged source and stale-source rejection passed.')


def odbc_worker_checks(executable, directory, password, require_odbc):
    """Load real frozen pyodbc; never simulate catalog success without ASE."""
    connection = {"host": "fixture.internal", "port": 5000, "user": "smoke", "password": password}
    status = worker_request(executable, {"operation": "status", "connection": connection}, directory)
    assert "sql_ini" not in status and "data_sources" not in status["odbc"], "Frozen worker retains removed connection discovery"
    if require_odbc:
        assert status["odbc"]["module_available"] is True, "Frozen worker could not import native pyodbc"
        assert status["odbc"]["version"] == "5.3.0", "Frozen worker did not load pinned native pyodbc"
    events = worker_request(executable, {"operation": "databases", "connection": connection}, directory, expect_error=True)
    assert any("ODBC" in event.get("detail", "") for event in events), "Worker did not report invalid ODBC driver"
    target = Path(directory) / "한글 preserved.bcp"
    target.write_bytes(b"existing destination\n")
    stage = Path(directory) / ".nice-tools-smoke-out.bcp"
    stage.touch()
    obj = {"database": "fixture_db", "owner": "dbo", "owner_id": 7, "id": 42, "name": "orders", "kind": "U"}
    columns = [{"position": 1, "name": "id", "type_id": 56, "type_name": "int", "length": 4,
                "precision": 0, "scale": 0, "nullable": False, "identity": False}]
    worker_request(executable, {"operation": "transfer", "connection": connection, "object": obj,
                   "path": str(target), "stage_path": str(stage), "defer_publish": True,
                   "columns": columns, "options": {"direction": "out", "encoding": "utf-8"},
                   "out_target_fingerprint": {"exists": True, "size": target.stat().st_size,
                                              "mtime_ns": target.stat().st_mtime_ns}}, directory, expect_error=True)
    assert target.read_bytes() == b"existing destination\n", "ODBC schema failure replaced an existing export"
    assert not stage.exists(), "ODBC schema failure retained reserved staging"
    target.unlink()
    process_kind = "actual Windows EXE" if require_odbc else "development process"
    print(f"ODBC {process_kind} worker smoke: native extension/discovery, sanitized missing-driver errors and export preservation passed; no live ASE transfer tested.")


def verify(path: Path, allow_non_windows: bool = False, sap_test_clients: Path | None = None):
    if not allow_non_windows:
        validate_windows_x64(path)
    with tempfile.TemporaryDirectory(prefix="nice-tools 실행 검사 ") as directory:
        executable = Path(directory) / path.name
        shutil.copy2(path, executable)
        assert len(list(Path(directory).iterdir())) == 1
        process = subprocess.Popen(
            [str(executable), "--no-browser"], cwd=directory,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        output = queue.Queue()

        def read_output():
            for line in process.stdout:
                output.put(line)
            output.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        lines = []
        try:
            while True:
                line = output.get(timeout=60)
                if line is None:
                    raise RuntimeError("Executable exited before readiness: " + "".join(lines))
                lines.append(line)
                match = re.search(r"http://127\.0\.0\.1:\d+", line)
                if match:
                    smoke_args = ["--url", match.group(), "--large-files", "--config-folder", directory]
                    if not allow_non_windows:
                        smoke_args += ["--require-odbc"]
                    if sap_test_clients is not None:
                        smoke_args += ["--sap-test-clients", str(sap_test_clients), "--executable", str(executable)]
                    smoke_main(smoke_args)
                    break
            # HTTP failures must also reach the existing frozen launcher's
            # console, not just a child worker or a web response.
            deadline = time.monotonic() + 10
            while '[IQ connect failed]' not in ''.join(lines) or 'driver-resolution' not in ''.join(lines):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError('Frozen launcher did not print the IQ failure phase')
                try:
                    line = output.get(timeout=remaining)
                except queue.Empty:
                    raise AssertionError('Frozen launcher did not print the IQ failure phase') from None
                if line is None:
                    raise AssertionError('Frozen launcher exited before printing the IQ failure')
                lines.append(line)
            while True:
                try:
                    line = output.get_nowait()
                except queue.Empty:
                    break
                if line is not None:
                    lines.append(line)
            console = ''.join(lines)
            assert all(secret not in console for secret in (
                'iq-exe-verification-secret-9!', 'iq-profile-verification-secret-9!',
            )), 'Frozen launcher emitted IQ credentials'
            print('IQ frozen launcher console: sanitized connection failure and phase passed; no live IQ export tested.')
            local_comparison_worker_checks(executable,directory)
            text_inspection_worker_checks(executable,directory)
            if process.poll() is not None:
                raise RuntimeError("Executable exited unexpectedly")
        finally:
            stop_process(process)
    print("Single-file bundle: isolated execution, bundled assets and real comparison passed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--allow-non-windows", action="store_true", help="Development check only; never a Windows release validation")
    parser.add_argument("--sap-test-clients", type=Path, help="External developer-only compiled console fixture folder")
    args = parser.parse_args()
    verify(args.executable.resolve(), args.allow_non_windows,
           args.sap_test_clients.resolve(strict=True) if args.sap_test_clients else None)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}".replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::error::Single EXE verification failed: " + detail, flush=True)
        raise
