"""Real Windows COM configuration; no modal UI or mocks are needed here."""
import ctypes
import os
from pathlib import Path
import sys

import pytest

from nice_tools import ase_picker as picker


@pytest.mark.skipif(sys.platform != 'win32', reason='Requires the real Windows shell COM implementation')
@pytest.mark.parametrize('folder,direction,text_only', [
    (True, 'in', False), (False, 'in', False), (False, 'out', False), (False, 'in', True),
])
def test_real_common_item_dialog_configuration(tmp_path, folder, direction, text_only):
    shell, ole = picker._load_libraries()
    picker._check(ole.CoInitializeEx(None, 2), 'initialization')
    dialog, initial, selected, path = (ctypes.c_void_p() for _ in range(4))
    try:
        clsid, iid = (picker.CLSID_FILE_SAVE_DIALOG, picker.IID_FILE_SAVE_DIALOG) if direction == 'out' else (
            picker.CLSID_FILE_OPEN_DIALOG, picker.IID_FILE_OPEN_DIALOG)
        picker._check(ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(dialog)), 'creation')
        assert dialog
        picker._configure_dialog(dialog, folder=folder, direction=direction, suggested_name='검증.bcp', text_only=text_only)
        flags = ctypes.c_uint32()
        picker._check(picker._invoke(dialog, 10, (ctypes.POINTER(ctypes.c_uint32),), ctypes.byref(flags)), 'options')
        assert flags.value & picker.FOS_FORCEFILESYSTEM
        assert flags.value & picker.FOS_DONTADDTORECENT
        assert flags.value & picker.FOS_NOCHANGEDIR
        assert bool(flags.value & picker.FOS_PICKFOLDERS) == folder
        expected = picker.FOS_PATHMUSTEXIST if folder else (
            picker.FOS_FILEMUSTEXIST if direction == 'in' else picker.FOS_OVERWRITEPROMPT)
        assert flags.value & expected
        if not folder:
            index = ctypes.c_uint32()
            picker._check(picker._invoke(dialog, 6, (ctypes.POINTER(ctypes.c_uint32),), ctypes.byref(index)), 'filter index')
            assert index.value == 1
        # A real Unicode initial folder must survive Shell item creation/SetFolder/GetFolder.
        initial_folder = tmp_path / '초기 目录'
        initial_folder.mkdir()
        picker._check(shell.SHCreateItemFromParsingName(str(initial_folder), None,
                      ctypes.byref(picker.IID_SHELL_ITEM), ctypes.byref(initial)), 'initial folder')
        picker._check(picker._invoke(dialog, 12, (ctypes.c_void_p,), initial), 'initial folder')
        picker._check(picker._invoke(dialog, 13, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(selected)), 'get folder')
        picker._check(picker._invoke(selected, 5, (ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)),
                      picker.SIGDN_FILESYSPATH, ctypes.byref(path)), 'filesystem path')
        assert Path(ctypes.wstring_at(path)) == initial_folder
    finally:
        if path:
            ole.CoTaskMemFree(path)
        picker._release(selected)
        picker._release(initial)
        picker._release(dialog)
        ole.CoUninitialize()


def test_developer_dialog_verifier_rejects_non_windows(tmp_path):
    if os.name == 'nt':
        pytest.skip('The verifier is available on Windows')
    from scripts.verify_modern_dialogs import check_dialogs
    with pytest.raises(ValueError, match='Windows'):
        check_dialogs(tmp_path / 'nice-tools.exe', tmp_path)


@pytest.mark.parametrize('ownership_lost', [False, True])
def test_dialog_verifier_closes_only_modern_job_owned_windows(ownership_lost):
    from types import SimpleNamespace
    from scripts.verify_modern_dialogs import _OwnedDialogWindows
    verifier = _OwnedDialogWindows.__new__(_OwnedDialogWindows)
    verifier.callback_type = lambda callback: callback
    verifier.job = SimpleNamespace(_handle=999)
    inspected, closed, queries = [], [], []
    pids = {10: 100, 20: 200, 30: 100}
    classes = {10: '#32770', 11: 'DirectUIHWND', 20: '#32770', 21: 'DirectUIHWND', 30: '#32770'}
    def enum_windows(callback, _):
        for hwnd in (10, 20, 30):
            callback(hwnd, 0)
        return 1
    def enum_children(hwnd, callback, _):
        inspected.append(hwnd)
        for child in {10: [11], 20: [21], 30: []}[hwnd]:
            callback(child, 0)
        return 1
    def get_pid(hwnd, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32))[0] = pids[hwnd]
        return 1
    def get_class(hwnd, buffer, _):
        buffer.value = classes[hwnd]
        return len(buffer.value)
    def in_job(process, job, output):
        assert job == 999
        queries.append(process)
        belongs = process == 100 and not (ownership_lost and len(queries) > 1)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_int32))[0] = int(belongs)
        return 1
    verifier.kernel = SimpleNamespace(OpenProcess=lambda _access, _inherit, pid: pid,
                                     IsProcessInJob=in_job, CloseHandle=lambda _: 1)
    verifier.user = SimpleNamespace(EnumWindows=enum_windows, EnumChildWindows=enum_children,
                                   GetWindowThreadProcessId=get_pid, GetClassNameW=get_class,
                                   PostMessageW=lambda *args: closed.append(args) or 1)
    observed = verifier.close_modern_dialogs()
    assert 20 not in inspected  # Another app's modern dialog is never traversed/closed.
    assert closed == ([] if ownership_lost else [(10, 0x0010, 0, 0)])
    assert observed == ([] if ownership_lost else [10])


@pytest.mark.skipif(sys.platform != 'win32', reason='Requires the real Windows shell COM implementation')
@pytest.mark.parametrize('mode', ['missing', 'renamed'])
def test_real_stale_initial_folder_reaches_show(tmp_path, monkeypatch, mode):
    """Use real COM/Shell resolution; intercept only modal Show to avoid a desktop."""
    previous = tmp_path / '이전 目录'
    if mode == 'renamed':
        previous.mkdir()
        previous.rename(tmp_path / 'moved')
    invoked = []
    invoke = picker._invoke
    def without_modal_ui(pointer, index, argtypes, *args):
        if index == 3:
            invoked.append(index)
            return picker.ERROR_CANCELLED
        return invoke(pointer, index, argtypes, *args)
    monkeypatch.setattr(picker, '_invoke', without_modal_ui)
    assert picker.pick_directory(str(previous)) == {'cancelled': True}
    assert invoked == [3]
