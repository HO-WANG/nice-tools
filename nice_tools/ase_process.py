"""SAP console clients with password prompts, bounded logs and process ownership.

The database password travels through an owned console, never command arguments,
environment variables or a temporary file. Windows uses ConPTY because SAP's
console password reader need not accept redirected stdin. POSIX pipes exist for
source development and disposable client tests; Windows releases require ConPTY.
"""

from __future__ import annotations

import codecs
import ctypes
import math
import os
import queue
import re
import signal
import subprocess
import threading
import time


MAX_CLIENT_OUTPUT = 10 * 1024 * 1024
_PASSWORD_PROMPT = re.compile(r"(?i)password\s*:")
_HIDDEN = "[비밀번호 숨김]"


def windows_command_line(argv):
    """Quote every CRT argument, including literal BCP CR/LF terminators.

    list2cmdline quotes only spaces/tabs. Always quoting also makes newline
    terminators unambiguous while retaining quotes and trailing backslashes.
    No command shell is involved.
    """
    arguments = []
    for value in argv:
        result = '"'
        slashes = 0
        for char in value:
            if char == "\\":
                slashes += 1
            elif char == '"':
                result += "\\" * (slashes * 2 + 1) + char
                slashes = 0
            else:
                result += "\\" * slashes + char
                slashes = 0
        result += "\\" * (slashes * 2) + '"'
        arguments.append(result)
    return " ".join(arguments)


def resume_process(pid):
    """Resume the initial thread of a CREATE_SUSPENDED Windows subprocess.

    subprocess.Popen closes its primary-thread handle. Find that suspended thread
    after the worker has joined a job and before its bootloader can start children.
    Refuse unexpected thread states rather than execute an unowned process tree.
    POSIX workers already use a separately owned process group.
    """
    if os.name != "nt":
        return
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32First.restype = wintypes.BOOL
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32Next.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise OSError("Windows 작업자 스레드를 확인하지 못했습니다.")
    threads = []
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Thread32First(snapshot, ctypes.byref(entry)):
            raise OSError("Windows 작업자 스레드를 확인하지 못했습니다.")
        while True:
            if entry.th32OwnerProcessID == int(pid):
                threads.append(int(entry.th32ThreadID))
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel.Thread32Next(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise OSError("Windows 작업자 스레드 목록을 읽지 못했습니다.")
                break
    finally:
        kernel.CloseHandle(snapshot)
    if len(threads) != 1:
        raise OSError("Windows 작업자의 일시 중지 상태를 확인하지 못했습니다.")
    thread = kernel.OpenThread(0x0002, False, threads[0])  # THREAD_SUSPEND_RESUME
    if not thread:
        raise OSError("Windows 작업자 스레드를 열지 못했습니다.")
    try:
        if kernel.ResumeThread(thread) != 1:
            raise OSError("Windows 작업자 스레드를 안전하게 재개하지 못했습니다.")
    finally:
        kernel.CloseHandle(thread)


class WindowsJob:
    """Own a Windows process tree; closing the handle terminates its members.

    Assign a CREATE_SUSPENDED outer worker before resume_process so its bootloader
    and SAP descendants all inherit ownership. Assigning an already-running client
    cannot adopt children created before assignment. On POSIX this helper is
    deliberately inert; ClientProcess uses an owned process group instead.
    """

    def __init__(self, pid=None):
        self._handle = None
        self._lock = threading.RLock()
        if os.name != "nt":
            return
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("Windows 작업 프로세스 그룹을 만들지 못했습니다.")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(handle)
            raise OSError("Windows 작업 프로세스 그룹을 설정하지 못했습니다.")
        self._handle = handle
        try:
            if pid is not None:
                self.assign(pid)
        except BaseException:
            self.close()
            raise

    def assign(self, pid):
        if os.name != "nt":
            return
        with self._lock:
            if self._handle is None:
                raise OSError("Windows 작업 프로세스 그룹이 닫혔습니다.")
            process = self._kernel.OpenProcess(0x0100 | 0x0001, False, int(pid))
            if not process:
                raise OSError("Windows 작업 프로세스를 확인하지 못했습니다.")
            try:
                if not self._kernel.AssignProcessToJobObject(self._handle, process):
                    raise OSError("Windows 작업 프로세스 그룹에 등록하지 못했습니다.")
            finally:
                self._kernel.CloseHandle(process)

    def terminate(self):
        with self._lock:
            if self._handle is not None:
                if not self._kernel.TerminateJobObject(self._handle, 1):
                    # Kill-on-close is also the fallback if explicit termination
                    # fails; never keep an apparently terminated job alive.
                    self._kernel.CloseHandle(self._handle)
                    self._handle = None

    def close(self):
        with self._lock:
            if self._handle is not None:
                self._kernel.CloseHandle(self._handle)
                self._handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class _WindowsConsole:
    """Built-in ConPTY with job assignment before the suspended client runs."""

    def __init__(self, argv):
        from ctypes import wintypes

        class Coord(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class StartupInfo(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class StartupInfoEx(ctypes.Structure):
            _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

        class ProcessInfo(ctypes.Structure):
            _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD]
        kernel.CreatePipe.restype = wintypes.BOOL
        kernel.CreatePseudoConsole.argtypes = [Coord, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        kernel.CreatePseudoConsole.restype = ctypes.c_long
        kernel.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
        kernel.ClosePseudoConsole.restype = None
        kernel.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
        kernel.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        kernel.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        kernel.DeleteProcThreadAttributeList.restype = None
        kernel.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ProcessInfo)]
        kernel.CreateProcessW.restype = wintypes.BOOL
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.PeekNamedPipe.restype = wintypes.BOOL
        kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.ReadFile.restype = wintypes.BOOL
        kernel.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.WriteFile.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.job = None
        self.hpc = ctypes.c_void_p()
        self.process = None
        self.input = None
        self.output = None
        self.returncode = None
        self._console_close_thread = None
        self._lock = threading.RLock()
        child_input = wintypes.HANDLE()
        input_writer = wintypes.HANDLE()
        output_reader = wintypes.HANDLE()
        child_output = wintypes.HANDLE()
        info = ProcessInfo()
        attributes = None
        initialized = False
        try:
            if not kernel.CreatePipe(ctypes.byref(child_input), ctypes.byref(input_writer), None, 65536):
                raise OSError("SAP 콘솔 입력 파이프를 만들지 못했습니다.")
            self.input = input_writer.value
            if not kernel.CreatePipe(ctypes.byref(output_reader), ctypes.byref(child_output), None, 65536):
                raise OSError("SAP 콘솔 출력 파이프를 만들지 못했습니다.")
            self.output = output_reader.value
            # The password console remains invisible. No inherited-cursor flag:
            # that flag would require answering terminal position queries.
            if kernel.CreatePseudoConsole(Coord(32767, 25), child_input, child_output, 0, ctypes.byref(self.hpc)) != 0:
                raise OSError("Windows ConPTY 콘솔을 만들지 못했습니다.")
            size = ctypes.c_size_t()
            kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            if not kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
                raise OSError("SAP 콘솔 프로세스 속성을 만들지 못했습니다.")
            initialized = True
            if not kernel.UpdateProcThreadAttribute(attributes, 0, 0x00020016, self.hpc, ctypes.sizeof(ctypes.c_void_p), None, None):
                raise OSError("SAP 콘솔 프로세스 속성을 설정하지 못했습니다.")
            startup = StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            command = ctypes.create_unicode_buffer(windows_command_line(argv))
            # CREATE_SUSPENDED + EXTENDED_STARTUPINFO_PRESENT: no client code or
            # descendant can execute until it belongs to the kill-on-close job.
            if not kernel.CreateProcessW(argv[0], command, None, None, False, 0x00080004, None, None, ctypes.byref(startup), ctypes.byref(info)):
                raise OSError("SAP 클라이언트를 시작하지 못했습니다.")
            self.process = info.hProcess
            self.pid = int(info.dwProcessId)
            self.job = WindowsJob(self.pid)
            if kernel.ResumeThread(info.hThread) != 1:
                raise OSError("SAP 클라이언트를 실행하지 못했습니다.")
        except BaseException:
            self.close()
            raise
        finally:
            if info.hThread:
                kernel.CloseHandle(info.hThread)
            if initialized:
                kernel.DeleteProcThreadAttributeList(attributes)
            if child_input:
                kernel.CloseHandle(child_input)
            if child_output:
                kernel.CloseHandle(child_output)

    def poll(self):
        from ctypes import wintypes

        with self._lock:
            if self.process is None:
                return self.returncode
            status = self.kernel.WaitForSingleObject(self.process, 0)
            if status == 258:  # WAIT_TIMEOUT
                return None
            if status != 0:
                raise OSError("SAP 클라이언트 종료 상태를 확인하지 못했습니다.")
            code = wintypes.DWORD()
            if not self.kernel.GetExitCodeProcess(self.process, ctypes.byref(code)):
                raise OSError("SAP 클라이언트 종료 코드를 확인하지 못했습니다.")
            self.returncode = int(code.value)
            return self.returncode

    def wait(self, timeout=None):
        if self.process is None:
            return self.returncode
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, min(0xFFFFFFFE, int(timeout * 1000)))
        status = self.kernel.WaitForSingleObject(self.process, milliseconds)
        if status == 258:
            raise subprocess.TimeoutExpired("SAP client", timeout)
        if status != 0:
            raise OSError("SAP 클라이언트 종료를 기다리지 못했습니다.")
        return self.poll()

    def kill(self):
        with self._lock:
            if self.job is not None:
                self.job.terminate()
            elif self.process is not None:
                self.kernel.TerminateProcess(self.process, 1)

    def read(self):
        from ctypes import wintypes

        available = wintypes.DWORD()
        if not self.kernel.PeekNamedPipe(self.output, None, 0, None, ctypes.byref(available), None):
            if ctypes.get_last_error() in (109, 232):
                return None
            raise OSError("SAP 콘솔 출력을 확인하지 못했습니다.")
        if not available.value:
            if self.poll() is not None:
                self._start_console_close()
                closing = self._console_close_thread
                if closing is not None and not closing.is_alive():
                    # The final flush may have arrived between the first empty
                    # Peek and completed ClosePseudoConsole. Drain it before EOF.
                    # A valid but empty pipe need not report BROKEN_PIPE here.
                    if not self.kernel.PeekNamedPipe(self.output, None, 0, None, ctypes.byref(available), None):
                        if ctypes.get_last_error() in (109, 232):
                            return None
                        raise OSError("SAP 콘솔 출력을 확인하지 못했습니다.")
                    if not available.value:
                        return None
            if not available.value:
                return b""
        buffer = ctypes.create_string_buffer(min(available.value, 8192))
        received = wintypes.DWORD()
        if not self.kernel.ReadFile(self.output, buffer, len(buffer), ctypes.byref(received), None):
            raise OSError("SAP 콘솔 출력을 읽지 못했습니다.")
        return buffer.raw[:received.value]

    def write(self, text):
        from ctypes import wintypes

        encoded = text.encode("utf-8")
        with self._lock:
            offset = 0
            while offset < len(encoded):
                chunk = encoded[offset:]
                written = wintypes.DWORD()
                if not self.kernel.WriteFile(self.input, chunk, len(chunk), ctypes.byref(written), None) or not written.value:
                    raise OSError("SAP 콘솔 입력을 전달하지 못했습니다.")
                offset += written.value

    def _start_console_close(self):
        with self._lock:
            if self._console_close_thread is not None or not self.hpc.value:
                return
            hpc = self.hpc
            self.hpc = ctypes.c_void_p()
            # ConPTY may flush the final rows/error after the client exits. Keep
            # its output reader alive until ClosePseudoConsole finishes instead
            # of treating process exit as terminal-output EOF.
            self._console_close_thread = threading.Thread(
                target=self.kernel.ClosePseudoConsole, args=(hpc,),
                daemon=True, name="sap-console-close",
            )
            self._console_close_thread.start()

    def close(self):
        self.kill()
        if self.process is not None:
            try:
                self.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.job is not None:
            self.job.close()
        if self.input is not None:
            self.kernel.CloseHandle(self.input)
            self.input = None
        if self.hpc.value:
            # A full output pipe can block ClosePseudoConsole. The reader has
            # stopped after process termination, so drain its remaining bytes.
            if self.output is not None:
                try:
                    while self.read():
                        pass
                except OSError:
                    pass
            self._start_console_close()
        if self._console_close_thread is not None:
            self._console_close_thread.join(timeout=5)
        for name in ("output", "process"):
            handle = getattr(self, name)
            if handle is not None:
                self.kernel.CloseHandle(handle)
                setattr(self, name, None)


class ClientProcess:
    """Registered cancellation handle for a single owned SAP client invocation."""

    def __init__(self, argv):
        self._lock = threading.RLock()
        self._job = None
        self._console = None
        self._process = None
        if os.name == "nt":
            self._console = _WindowsConsole(argv)
            self.pid = self._console.pid
        else:
            self._process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
            self.pid = self._process.pid

    def poll(self):
        if self._process is not None:
            return self._process.poll()
        return self._console.poll()

    def wait(self, timeout=None):
        if self._process is not None:
            return self._process.wait(timeout=timeout)
        return self._console.wait(timeout)

    def kill(self):
        with self._lock:
            if self._console is not None:
                self._console.kill()
            elif self._process is not None:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def write(self, text):
        with self._lock:
            if self._console is not None:
                self._console.write(text)
            else:
                self._process.stdin.write(text.encode("utf-8"))
                self._process.stdin.flush()

    def read(self):
        if self._console is not None:
            return self._console.read()
        return os.read(self._process.stdout.fileno(), 8192)

    def close(self):
        self.kill()
        if self._console is not None:
            self._console.close()
        if self._process is not None:
            try:
                self._process.wait(timeout=5)
            finally:
                self._process.stdin.close()
                self._process.stdout.close()


class _ConsoleText:
    """Drop fragmented terminal escape sequences without touching visible text."""

    def __init__(self):
        self.state = "text"

    def feed(self, text):
        result = []
        for char in text:
            if self.state == "escape":
                self.state = "csi" if char == "[" else "osc" if char == "]" else "text"
            elif self.state == "csi":
                if "@" <= char <= "~":
                    self.state = "text"
            elif self.state == "osc":
                if char == "\x07":
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "osc_escape"
            elif self.state == "osc_escape":
                self.state = "text" if char == "\\" else "osc"
            elif char == "\x1b":
                self.state = "escape"
            elif char not in "\r\x00\x07":
                result.append(char)
        return "".join(result)


class _RedactedOutput:
    def __init__(self, password, callback):
        self.password = password
        self.hidden = "" if password and password in _HIDDEN else _HIDDEN
        self.callback = callback
        self.pending = ""
        self.tail = ""
        self.truncated = False

    def feed(self, text, final=False):
        self.pending += text
        if self.password:
            # Retain an incomplete secret across reads; replace complete matches
            # before deciding the safe prefix, including overlapping boundaries.
            self.pending = self.pending.replace(self.password, self.hidden)
            retain = 0 if final else len(self.password) - 1
        else:
            retain = 0
        safe_length = max(0, len(self.pending) - retain)
        emitted = self.pending[:safe_length]
        self.pending = self.pending[safe_length:]
        if not emitted:
            return
        self.tail += emitted
        if len(self.tail) > MAX_CLIENT_OUTPUT:
            self.tail = self.tail[-MAX_CLIENT_OUTPUT:]
            self.truncated = True
        if self.callback is not None:
            self.callback(emitted)


def run_client(argv, password, input_text="", timeout=90, on_output=None, register=None, *, _parse_output=None):
    """Authenticate at Password:, stream sanitized output and return an exit code.

    ``stdout`` retains at most MAX_CLIENT_OUTPUT characters; ``stdout_truncated``
    reports tail retention. ``on_output`` receives the complete sanitized stream
    so long BCP progress never caps the transfer or hides an earlier failure.
    Timeout=None allows long transfers; caller cancellation uses register(handle).
    The SQL script, if supplied, must explicitly terminate the client.
    _parse_output is a private, trusted semantic parser called before redaction.
    It must retain only numeric counts/flags and never expose or log raw strings:
    a password such as 'rows' or 'Error' must not change BCP result semantics.
    """
    if not isinstance(password, str) or any(ord(char) < 32 or ord(char) == 127 for char in password):
        raise ValueError("비밀번호에는 줄바꿈이나 제어 문자를 사용할 수 없습니다.")
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
        raise ValueError("SAP 클라이언트 실행 인자가 올바르지 않습니다.")
    if any(arg.startswith("-P") for arg in argv[1:]):
        raise ValueError("비밀번호를 SAP 클라이언트 인자로 전달할 수 없습니다.")
    if input_text is None:
        input_text = ""
    if not isinstance(input_text, str):
        raise ValueError("SAP 클라이언트 입력이 올바르지 않습니다.")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("SAP 클라이언트 제한 시간이 올바르지 않습니다.")
    started = time.monotonic()
    client = None
    stopped = threading.Event()
    events = queue.Queue(maxsize=64)
    reader = None
    unregister = None
    output = _RedactedOutput(password, on_output)
    console = _ConsoleText()
    prompted = False
    prompt_tail = ""

    def enqueue(value):
        while not stopped.is_set():
            try:
                events.put(value, timeout=.05)
                return
            except queue.Full:
                pass

    def read_output():
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while not stopped.is_set():
                part = client.read()
                if part is None or (part == b"" and os.name != "nt"):
                    text = decoder.decode(b"", final=True)
                    if text:
                        enqueue(text)
                    break
                if part:
                    enqueue(decoder.decode(part) if isinstance(part, bytes) else part)
                else:
                    stopped.wait(.01)
        except Exception:
            enqueue(False)
        finally:
            enqueue(None)

    try:
        client = ClientProcess(list(argv))
        if register is not None:
            unregister = register(client)
        reader = threading.Thread(target=read_output, daemon=True, name="sap-client-output")
        reader.start()
        while True:
            if timeout is not None and time.monotonic() - started >= timeout:
                raise ValueError("SAP 클라이언트 응답 시간이 초과되었습니다.")
            try:
                part = events.get(timeout=.025)
            except queue.Empty:
                continue
            if part is None:
                break
            if part is False:
                if client.poll() is None:
                    raise ValueError("SAP 클라이언트 출력을 읽지 못했습니다.")
                break
            text = console.feed(part)
            if not prompted:
                prompt_tail = (prompt_tail + text)[-256:]
                if _PASSWORD_PROMPT.search(prompt_tail):
                    prompted = True
                    client.write(password + ("\r" if os.name == "nt" else "\n"))
                    if input_text:
                        client.write(input_text.replace("\r\n", "\n").replace("\n", "\r") if os.name == "nt" else input_text)
            if _parse_output is not None:
                _parse_output(text)
            output.feed(text)
        output.feed("", final=True)
        status = client.poll()
        if status is None:
            # Pipe EOF may precede OS exit bookkeeping by a few milliseconds.
            try:
                status = client.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                raise ValueError("SAP 클라이언트가 출력을 닫은 뒤 종료하지 않았습니다.") from None
        if not prompted and status == 0:
            raise ValueError("SAP 클라이언트의 비밀번호 입력 프롬프트를 확인하지 못했습니다.")
        return {
            "stdout": output.tail, "stdout_truncated": output.truncated,
            "returncode": status, "elapsed_seconds": max(0.0, time.monotonic() - started),
        }
    except (OSError, EOFError, RuntimeError):
        raise ValueError("SAP 클라이언트를 실행하거나 입력을 전달하지 못했습니다.") from None
    except ValueError as error:
        detail = str(error)
        if password:
            detail = detail.replace(password, _HIDDEN if password not in _HIDDEN else "")
        raise ValueError(detail) from None
    except Exception:
        raise ValueError("SAP 클라이언트 실행 중 오류가 발생했습니다.") from None
    finally:
        if client is not None:
            client.kill()
        stopped.set()
        if reader is not None:
            reader.join(timeout=2)
        if client is not None:
            client.close()
        if callable(unregister):
            try:
                unregister()
            except Exception:
                # The process is already reaped; registry cleanup must not expose
                # callback internals or override a transfer's original exception.
                pass
