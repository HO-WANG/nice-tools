"""Persistent ASE ODBC sessions and isolated, cancellable BCP workers."""

from __future__ import annotations

import atexit
import contextlib
import copy
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time


def connection_data(value):
    if not isinstance(value, dict):
        raise ValueError("접속 정보를 입력하세요.")
    host = value.get("host", "")
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ValueError("서버 주소를 입력하세요.")
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", host):
            raise ValueError("서버 주소에는 IP 또는 호스트 이름을 입력하세요.") from None
    port = value.get("port", 5000)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("포트는 1~65535 사이의 정수여야 합니다.")
    result = {"host": host, "port": port, "login_timeout": 15, "query_timeout": 60}
    for key, label in (("user", "사용자"), ("password", "비밀번호")):
        text = value.get(key)
        try:
            valid = isinstance(text, str) and "\x00" not in text and len(text.encode("utf-8")) <= 255 and (key == "password" or bool(text))
        except UnicodeError:
            valid = False
        if not valid:
            minimum = 0 if key == "password" else 1
            raise ValueError(f"{label}는 UTF-8 기준 {minimum}~255바이트로 입력하세요.")
        result[key] = text
    if "dsn" in value:
        dsn = value["dsn"]
        if (not isinstance(dsn, str) or len(dsn) > 255
                or any(ord(char) < 32 or ord(char) == 127 for char in dsn)):
            raise ValueError("ODBC DSN은 제어 문자 없이 255자 이내로 입력하세요.")
        if dsn.strip():
            result["dsn"] = dsn.strip()
    return result


def object_data(value):
    if not isinstance(value, dict):
        raise ValueError("오브젝트를 선택하세요.")
    result = {}
    for key in ("database", "owner", "name"):
        text = value.get(key)
        if not isinstance(text, str) or not text or "\x00" in text or len(text) > 255:
            raise ValueError("오브젝트 이름이 올바르지 않습니다.")
        result[key] = text
    for key in ("owner_id", "id"):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, int) or not 0 < number <= 2147483647:
            raise ValueError("오브젝트 식별자가 올바르지 않습니다.")
        result[key] = number
    if value.get("kind") not in ("U", "V", "P"):
        raise ValueError("오브젝트 종류가 올바르지 않습니다.")
    result["kind"] = value["kind"]
    return result


def target_fingerprint(path):
    try:
        stat = Path(path).stat()
    except FileNotFoundError:
        return {"exists": False}
    return {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


class AseManager:
    def __init__(self, worker_command=None):
        self.worker_command = worker_command or (
            [sys.executable, "--ase-worker"] if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "nice_tools.ase_worker"]
        )
        self.lock = threading.RLock()
        self.sessions = {}
        self.processes = {}
        self.worker_jobs = {}
        self.closed = False
        self.pending_adapters = set()

    @staticmethod
    def _redact(text, connection):
        text = str(text)
        password = connection.get("password", "")
        if password:
            text = text.replace(password, "[비밀번호 숨김]")
        return text[:2000]

    def _session(self, token):
        if not isinstance(token, str):
            raise ValueError("다시 접속하세요.")
        with self.lock:
            session = self.sessions.get(token)
            if not session:
                raise ValueError("접속 정보가 만료되었습니다. 다시 접속하세요.")
            session["touched"] = time.monotonic()
            return session

    def _terminate(self, process):
        """Kill the owned process tree, including children of a finished worker."""
        with self.lock:
            worker = process in self.worker_jobs
            guard = self.worker_jobs.get(process)
        if worker and guard is not None:
            guard.terminate()
        elif worker and os.name != "nt":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()

    def _release_worker(self, process):
        try:
            self._terminate(process)
            process.wait()
        finally:
            with self.lock:
                self.processes.pop(process, None)
                guard = self.worker_jobs.pop(process, None)
            if guard is not None:
                guard.close()
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def _spawn(self, payload, session=None):
        process = subprocess.Popen(
            self.worker_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        guard = None
        try:
            if os.name == "nt":
                from .ase_process import WindowsJob, resume_process
                guard = WindowsJob()
                # Suspend before the one-file bootstrapper can create Python's
                # child process. Assignment covers every later worker/CLI child.
                guard.assign(process.pid)
                resume_process(process.pid)
            with self.lock:
                self.worker_jobs[process] = guard
                if self.closed or (session is not None and session not in self.sessions):
                    self._terminate(process)
                    raise ValueError("접속 요청이 취소되었습니다.")
                self.processes[process] = session
            return process
        except Exception:
            if guard is not None:
                guard.close()
            if process.poll() is None:
                process.kill()
            process.wait()
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
            with self.lock:
                self.worker_jobs.pop(process, None)
                self.processes.pop(process, None)
            raise

    @staticmethod
    def _close_adapter(adapter):
        if adapter is None:
            return
        # Cancel outside the adapter's query lock, then close after the active
        # cursor releases it. Continue cleanup if the driver reports an error.
        with contextlib.suppress(Exception):
            adapter.cancel()
        with contextlib.suppress(Exception):
            adapter.close()

    def _invoke_direct(self, payload, session=None, adapter=None):
        """Reuse the owning session's ODBC connection for catalogs/preview."""
        from .ase_worker import execute
        started_at = time.monotonic()
        with self.lock:
            if self.closed:
                raise ValueError("접속 요청이 취소되었습니다.")
        if adapter is None:
            adapter = self._session(session)["adapter"]
        try:
            if payload["operation"] == "preview":
                result = execute(payload, catalog=adapter.run_operation)
            else:
                result = adapter.run_operation(payload)
            if not isinstance(result, dict):
                raise ValueError("ASE ODBC 응답을 읽지 못했습니다.")
            if isinstance(result.get("timings"), dict):
                result["timings"] = {**result["timings"], "total_seconds": max(0.0, time.monotonic() - started_at)}
            return result
        except (ValueError, OSError) as error:
            raise ValueError(self._redact(error, payload.get("connection", {}))) from None

    def _invoke(self, payload, session=None, timeout=90):
        process = self._spawn(payload, session)
        try:
            output, _ = process.communicate(json.dumps(payload, ensure_ascii=False), timeout=timeout)
            result = None
            error = None
            for line in output.splitlines():
                event = json.loads(line)
                if event.get("event") == "result":
                    result = event.get("result")
                elif event.get("event") == "error":
                    error = event.get("detail", "ASE 작업을 완료하지 못했습니다.")
            if process.returncode != 0 or error or not isinstance(result, dict):
                raise ValueError(self._redact(error or "ASE 작업을 완료하지 못했습니다.", payload.get("connection", {})))
            return result
        except subprocess.TimeoutExpired:
            self._terminate(process)
            process.communicate()
            raise ValueError("ASE 응답 시간이 초과되었습니다.") from None
        except (OSError, json.JSONDecodeError):
            raise ValueError("ASE 작업 프로세스의 응답을 읽지 못했습니다.") from None
        finally:
            self._release_worker(process)

    def status(self):
        from .ase_cli import client_status
        return {**client_status(), "platform": sys.platform}

    def connect(self, payload):
        from . import ase_odbc
        connection = connection_data(payload)
        started_at = time.monotonic()
        adapter = None
        expired_adapters = []
        try:
            with self.lock:
                if self.closed:
                    raise ValueError("접속 요청이 취소되었습니다.")
            adapter = ase_odbc.OdbcSession(connection)
            with self.lock:
                if self.closed:
                    raise ValueError("접속 요청이 취소되었습니다.")
                # Own the adapter before it logs in or starts setup queries so
                # shutdown can cancel an initial query and close late handles.
                self.pending_adapters.add(adapter)
            adapter.open()
            result = self._invoke_direct({"operation": "databases", "connection": connection}, adapter=adapter)
            with self.lock:
                if self.closed:
                    raise ValueError("접속 요청이 취소되었습니다.")
                # Expire idle sessions; active transfers keep their owner.
                now = time.monotonic()
                expired = [token for token, item in self.sessions.items()
                           if now - item["touched"] > 3600 and not self._running(item)]
                for token in expired:
                    item = self.sessions.pop(token)
                    expired_adapters.append(item.get("adapter"))
                if len(self.sessions) >= 16:
                    raise ValueError("접속이 너무 많습니다. 사용하지 않는 접속을 해제하세요.")
                token = secrets.token_urlsafe(32)
                self.sessions[token] = {"connection": connection, "adapter": adapter, "files": {}, "reviews": {}, "jobs": {}, "touched": now}
                self.pending_adapters.discard(adapter)
            public = {"session": token, "databases": result.get("databases", [])}
            if isinstance(result.get("timings"), dict):
                public["timings"] = {**result["timings"], "total_seconds": max(0.0, time.monotonic() - started_at)}
            adapter = None  # Ownership moved to the registered session.
            return public
        except (ValueError, OSError) as error:
            raise ValueError(self._redact(error, connection)) from None
        finally:
            if adapter is not None:
                with self.lock:
                    self.pending_adapters.discard(adapter)
                self._close_adapter(adapter)
            for expired in expired_adapters:
                self._close_adapter(expired)

    def catalog(self, token, operation, **parameters):
        session = self._session(token)
        result = self._invoke_direct({"operation": operation, "connection": session["connection"], **parameters}, token)
        self._session(token)
        return result

    def pick_file(self, token, direction, suggested_name=None):
        session = self._session(token)
        if direction not in ("in", "out"):
            raise ValueError("IN 또는 OUT을 선택하세요.")
        if suggested_name is not None and (not isinstance(suggested_name, str) or "\x00" in suggested_name or len(suggested_name) > 32700):
            raise ValueError("파일 이름이 올바르지 않습니다.")
        result = self._invoke({"operation": "pick", "direction": direction, "suggested_name": suggested_name}, token, timeout=None)
        if result.get("cancelled"):
            return {"cancelled": True}
        path = Path(result["path"]).resolve()
        if not path.parent.is_dir() or path.is_dir() or (direction == "in" and not path.is_file()):
            raise ValueError("선택한 파일 경로를 사용할 수 없습니다.")
        file_token = secrets.token_urlsafe(32)
        with self.lock:
            self._session(token)
            # Selections and reviews are bounded metadata, independent of file size.
            session["files"].clear()
            session["reviews"].clear()
            session["files"][file_token] = {"path": str(path), "direction": direction,
                                         "target_fingerprint": target_fingerprint(path)}
        return {"cancelled": False, "file": file_token, "path": str(path)}

    def preview(self, token, object_value, file_token, options):
        from .ase_files import parse_options

        session = self._session(token)
        selected = session["files"].get(file_token) if isinstance(file_token, str) else None
        if not selected:
            raise ValueError("파일을 다시 선택하세요.")
        object_value = object_data(object_value)
        options = parse_options(options)
        if selected["direction"] != options["direction"]:
            raise ValueError("IN/OUT 변경 후 파일을 다시 선택하세요.")
        if object_value["kind"] == "P" or (options["direction"] == "in" and object_value["kind"] != "U"):
            raise ValueError("테이블은 IN/OUT, 뷰는 OUT만 가능합니다.")
        if options["direction"] == "out" and target_fingerprint(selected["path"]) != selected["target_fingerprint"]:
            raise ValueError("대상 파일이 변경되었습니다. 파일을 다시 선택하세요.")
        with self.lock:
            self._session(token)
            session["preview_version"] = session.get("preview_version", 0) + 1
            preview_version = session["preview_version"]
            session["reviews"].clear()
        payload = {"operation": "preview", "connection": session["connection"], "object": object_value,
                   "path": selected["path"], "options": options}
        result = self._invoke_direct(payload, token)
        payload.update({"operation": "transfer", "columns": result["columns"],
                        "fingerprint": result.get("fingerprint"),
                        "out_target_fingerprint": selected["target_fingerprint"]})
        review = secrets.token_urlsafe(32)
        with self.lock:
            self._session(token)
            if session["files"].get(file_token) != selected:
                raise ValueError("파일 선택이 변경되었습니다. 다시 확인하세요.")
            if session["preview_version"] != preview_version:
                raise ValueError("새로운 확인 요청이 있습니다. 이전 확인 결과를 사용하지 않습니다.")
            session["reviews"].clear()
            session["reviews"][review] = payload
        public = {key: value for key, value in result.items() if key not in ("fingerprint", "out_target_fingerprint")}
        return {**public, "review": review}

    @staticmethod
    def _running(session):
        return any(job["state"] == "running" for job in session["jobs"].values())

    def start(self, token, review):
        session = self._session(token)
        with self.lock:
            if self._running(session):
                raise ValueError("진행 중인 전송이 있습니다.")
            payload = session["reviews"].pop(review, None) if isinstance(review, str) else None
            if not payload:
                raise ValueError("파일과 대상을 다시 확인하세요. 확인 결과는 한 번만 사용할 수 있습니다.")
            payload = copy.deepcopy(payload)
            started_at = None
            if payload["options"]["direction"] == "out":
                started_at = time.monotonic()
                fd, stage = tempfile.mkstemp(prefix=".nice-tools-", suffix=".bcp", dir=Path(payload["path"]).parent)
                os.close(fd)
                payload.update({"stage_path": stage, "defer_publish": True})
            job_token = secrets.token_urlsafe(32)
            # Retain only a few completed jobs; no row data/passwords in public state.
            for old in list(session["jobs"]):
                if len(session["jobs"]) < 8:
                    break
                if session["jobs"][old]["state"] != "running":
                    session["jobs"].pop(old)
            job = {"job": job_token, "state": "running", "phase": "starting", "message": "전송 준비 중", "events": [],
                   "cancelled": False, "process": None}
            if started_at is not None:
                job["started_at"] = started_at
            session["jobs"][job_token] = job
            thread = threading.Thread(target=self._run_job, args=(token, session, job, payload), daemon=True)
            job["thread"] = thread
            thread.start()
        return {"job": job_token}

    def _run_job(self, token, session, job, payload):
        process = None
        result = None
        error = None
        try:
            process = self._spawn(payload, token)
            with self.lock:
                job["process"] = process
                if job["cancelled"]:
                    self._terminate(process)
            process.stdin.write(json.dumps(payload, ensure_ascii=False))
            process.stdin.close()
            for line in process.stdout:
                event = json.loads(line)
                if event.get("event") == "result":
                    result = event.get("result")
                elif event.get("event") == "error":
                    error = self._redact(event.get("detail", "전송 실패"), payload["connection"])
                elif event.get("event") == "progress":
                    with self.lock:
                        message = self._redact(event.get("message", "처리 중"), payload["connection"])
                        phase = str(event.get("phase", "transfer"))[:64]
                        job.update({"message": message, "phase": phase})
                        job["events"].append({"message": message, "phase": phase})
                        job["events"] = job["events"][-50:]
                        if isinstance(event.get("rows"), int):
                            job["rows"] = event["rows"]
            process.wait()
            if process.returncode != 0 or error or not isinstance(result, dict):
                raise ValueError(error or "전송 프로세스가 정상적으로 완료되지 않았습니다.")
            with self.lock:
                if not job["cancelled"]:
                    public_result = {"rows": result.get("rows"), "path": payload["path"], "direction": payload["options"]["direction"]}
                    if payload["options"]["direction"] == "out":
                        rows = result.get("rows")
                        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
                            raise ValueError("내보낸 행 수를 확인하지 못해 결과를 저장하지 않았습니다.")
                        try:
                            rate_rows = float(rows)
                        except OverflowError:
                            raise ValueError("내보낸 행 수를 확인하지 못해 결과를 저장하지 않았습니다.") from None
                        if target_fingerprint(payload["path"]) != payload["out_target_fingerprint"]:
                            raise ValueError("대상 파일이 변경되어 결과를 저장하지 않았습니다.")
                        # Capture the completed stage size before publishing so
                        # later user edits cannot alter the export statistics.
                        bytes_written = Path(payload["stage_path"]).stat().st_size
                        os.replace(payload["stage_path"], payload["path"])
                        elapsed = max(0.0, time.monotonic() - job["started_at"])
                        job["elapsed_seconds"] = elapsed
                        rate = rate_rows / elapsed if elapsed > 0 else None
                        public_result["statistics"] = {
                            "elapsed_seconds": elapsed,
                            "rows_per_second": rate if rate is None or math.isfinite(rate) else None,
                            "bytes_written": bytes_written,
                        }
                        bcp_seconds = result.get("bcp_seconds")
                        bcp_rate = result.get("bcp_rows_per_second")
                        if (
                            isinstance(bcp_seconds, (int, float)) and not isinstance(bcp_seconds, bool)
                            and math.isfinite(bcp_seconds) and bcp_seconds >= 0
                            and (bcp_rate is None or (
                                isinstance(bcp_rate, (int, float)) and not isinstance(bcp_rate, bool)
                                and math.isfinite(bcp_rate) and bcp_rate >= 0
                            ))
                        ):
                            public_result["statistics"].update({"bcp_seconds": bcp_seconds, "bcp_rows_per_second": bcp_rate})
                    job.update({"state": "succeeded", "phase": "done", "message": "전송 완료", "rows": result.get("rows"),
                                "result": public_result})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            with self.lock:
                if not job["cancelled"]:
                    detail = self._redact(str(exc), payload["connection"]) if isinstance(exc, ValueError) else "전송을 완료하지 못했습니다. 대상과 파일 상태를 확인하세요."
                    job.update({"state": "failed", "phase": "done", "message": error or detail})
                    if "started_at" in job:
                        job["elapsed_seconds"] = max(0.0, time.monotonic() - job["started_at"])
        finally:
            if process:
                self._release_worker(process)
            stage = payload.get("stage_path")
            if stage:
                try:
                    Path(stage).unlink(missing_ok=True)
                except OSError:
                    with self.lock:
                        job["events"].append({"message": "임시 OUT 파일을 지우지 못했습니다: " + stage})
            with self.lock:
                if process:
                    self.processes.pop(process, None)
                job["process"] = None
                if job["cancelled"]:
                    job.update({"state": "cancelled", "phase": "done", "message": "전송 취소됨"})
                    if "started_at" in job:
                        job["elapsed_seconds"] = max(0.0, time.monotonic() - job["started_at"])
                # Transfer payload no longer needs its password reference.
                payload.clear()

    def job(self, token, job_token):
        session = self._session(token)
        with self.lock:
            job = session["jobs"].get(job_token) if isinstance(job_token, str) else None
            if not job:
                raise ValueError("전송 작업을 찾을 수 없습니다.")
            return copy.deepcopy({key: value for key, value in job.items() if key not in ("cancelled", "process", "thread", "started_at")})

    def cancel(self, token, job_token):
        session = self._session(token)
        with self.lock:
            job = session["jobs"].get(job_token) if isinstance(job_token, str) else None
            if not job:
                raise ValueError("전송 작업을 찾을 수 없습니다.")
            if job["state"] == "running":
                job["cancelled"] = True
                job["message"] = "취소 중"
                process = job["process"]
                if process:
                    self._terminate(process)
        return self.job(token, job_token)

    def disconnect(self, token):
        session = self._session(token)
        with self.lock:
            for job in session["jobs"].values():
                if job["state"] == "running":
                    job["cancelled"] = True
            for process, owner in list(self.processes.items()):
                if owner == token:
                    self._terminate(process)
            self.sessions.pop(token, None)
        self._close_adapter(session.get("adapter"))
        return {"ok": True}

    def close(self):
        with self.lock:
            self.closed = True
            adapters = [session.get("adapter") for session in self.sessions.values()]
            adapters.extend(self.pending_adapters)
            self.pending_adapters.clear()
            threads = []
            for session in self.sessions.values():
                for job in session["jobs"].values():
                    if job.get("thread"):
                        threads.append(job["thread"])
                    if job["state"] == "running":
                        job["cancelled"] = True
            processes = list(self.processes)
            for process in processes:
                self._terminate(process)
            self.sessions.clear()
        for adapter in adapters:
            self._close_adapter(adapter)
        for process in processes:
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=5)


manager = AseManager()
atexit.register(manager.close)
