"""Developer-only real Common Item Dialog open/cancel checks for the frozen EXE.

No test hooks enter the application. WM_CLOSE is restricted to modern dialog
windows belonging to the worker's kill-on-close Windows Job. A noninteractive
Windows desktop fails explicitly instead of silently skipping the UI check.
"""

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time


class _OwnedDialogWindows:
    def __init__(self, job):
        self.job = job
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_ssize_t)
        self.kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]
        self.kernel.IsProcessInJob.restype = ctypes.c_int32
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int32
        self.user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.user.GetWindowThreadProcessId.restype = ctypes.c_uint32
        self.user.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar), ctypes.c_int32]
        self.user.GetClassNameW.restype = ctypes.c_int32
        self.user.EnumWindows.argtypes = [self.callback_type, ctypes.c_ssize_t]
        self.user.EnumWindows.restype = ctypes.c_int32
        self.user.EnumChildWindows.argtypes = [ctypes.c_void_p, self.callback_type, ctypes.c_ssize_t]
        self.user.EnumChildWindows.restype = ctypes.c_int32
        self.user.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t]
        self.user.PostMessageW.restype = ctypes.c_int32

    def _owned(self, hwnd):
        pid = ctypes.c_uint32()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = self.kernel.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not process:
            return False
        try:
            belongs = ctypes.c_int32()
            return bool(self.kernel.IsProcessInJob(process, self.job._handle, ctypes.byref(belongs)) and belongs.value)
        finally:
            self.kernel.CloseHandle(process)

    def _class(self, hwnd):
        name = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, name, len(name))
        return name.value

    def close_modern_dialogs(self):
        observed = []
        @self.callback_type
        def receive(hwnd, _):
            if not self._owned(hwnd) or self._class(hwnd) != '#32770':
                return 1
            modern = []
            @self.callback_type
            def child(child_hwnd, _):
                if self._class(child_hwnd) == 'DirectUIHWND':
                    modern.append(child_hwnd)
                return 1
            self.user.EnumChildWindows(hwnd, child, 0)
            # Recheck process/job membership immediately before sending WM_CLOSE.
            if modern and self._owned(hwnd) and self.user.PostMessageW(hwnd, 0x0010, 0, 0):
                observed.append(hwnd)
            return 1
        if not self.user.EnumWindows(receive, 0):
            raise OSError('Could not enumerate the Windows desktop for the owned picker.')
        return observed


def _check_dialog(command, directory, payload):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from nice_tools.ase_process import WindowsJob, resume_process
    process = subprocess.Popen(command, cwd=directory, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW | 0x00000004)
    guard = None
    try:
        guard = WindowsJob(process.pid)
        resume_process(process.pid)
        windows = _OwnedDialogWindows(guard)
        process.stdin.write(json.dumps(payload).encode('utf-8'))
        process.stdin.close()
        process.stdin = None
        observed = False
        deadline = time.monotonic() + 35
        while process.poll() is None and time.monotonic() < deadline:
            observed = bool(windows.close_modern_dialogs()) or observed
            time.sleep(0.05)
        if process.poll() is None:
            raise RuntimeError('No cancellable owned Common Item Dialog completed; this Windows desktop may be noninteractive.')
        stdout, _stderr = process.communicate(timeout=5)
        if not observed:
            raise AssertionError('No real modern Common Item Dialog was observed in the owned worker.')
        if process.returncode:
            raise AssertionError('The frozen native picker worker failed.')
        events = [json.loads(line) for line in stdout.splitlines()]
        results = [event['result'] for event in events if event.get('event') == 'result']
        if results != [{'cancelled': True}]:
            raise AssertionError('The real frozen dialog did not return cancellation.')
    finally:
        if guard is not None:
            guard.close()
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def check_dialogs(executable, directory):
    """Open and cancel real frozen folder/open/save dialogs, or raise on failure."""
    if os.name != 'nt':
        raise ValueError('Native dialog verification requires Windows.')
    command = [str(Path(executable).resolve()), '--ase-worker']
    requests = [
        {'operation': 'pick-directory', 'initial_directory': str(Path(directory).resolve())},
        {'operation': 'pick', 'direction': 'in'},
        {'operation': 'pick', 'direction': 'out', 'suggested_name': '검증-cancelled.bcp'},
    ]
    for payload in requests:
        _check_dialog(command, directory, payload)
    print('Real frozen Common Item Dialog folder/open/save cancellation passed (owned windows; no visual review).')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('executable', type=Path)
    parser.add_argument('--directory', type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    check_dialogs(arguments.executable, arguments.directory)
