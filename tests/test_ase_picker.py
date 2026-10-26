import ctypes
from types import SimpleNamespace

import pytest


@pytest.fixture
def picker():
    try:
        from nice_tools import ase_picker
    except ImportError:
        pytest.fail("Native ASE file picker is not implemented")
    return ase_picker


class Call:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


def dialog_library(picker, invoke, extended_error=0):
    def receive(pointer):
        return invoke(ctypes.cast(pointer, ctypes.POINTER(picker.OPENFILENAMEW)).contents)
    return SimpleNamespace(
        GetOpenFileNameW=Call(receive), GetSaveFileNameW=Call(receive),
        CommDlgExtendedError=Call(lambda: extended_error),
    )


@pytest.mark.parametrize("direction", ["in", "out"])
def test_windows_dialog_uses_unicode_buffer_and_required_flags(picker, monkeypatch, direction):
    chosen = "C:\\资料 目录\\文件.bcp"
    def invoke(request):
        assert request.nMaxFile == 32768
        assert request.Flags & picker.OFN_NOCHANGEDIR
        assert request.Flags & picker.OFN_PATHMUSTEXIST
        if direction == "in":
            assert request.Flags & picker.OFN_FILEMUSTEXIST
        else:
            assert request.Flags & picker.OFN_OVERWRITEPROMPT
        buffer = ctypes.cast(request.lpstrFile, ctypes.POINTER(ctypes.c_wchar * request.nMaxFile)).contents
        buffer.value = chosen
        return 1
    monkeypatch.setattr(picker.sys, "platform", "win32")
    monkeypatch.setattr(picker, "_load_dialog_library", lambda: dialog_library(picker, invoke))
    assert picker.pick_file(direction, "预览.bcp") == {"cancelled": False, "path": chosen}


def test_windows_dialog_reports_user_cancellation(picker, monkeypatch):
    monkeypatch.setattr(picker.sys, "platform", "win32")
    monkeypatch.setattr(picker, "_load_dialog_library", lambda: dialog_library(picker, lambda request: 0))
    assert picker.pick_file("out", "output.bcp") == {"cancelled": True}


def test_text_inspection_dialog_opens_existing_text_files(picker,monkeypatch):
    def invoke(request):
        assert request.Flags & picker.OFN_FILEMUSTEXIST
        assert request.lpstrTitle=='Select a text file'
        assert request.lpstrDefExt=='txt'
        assert request.lpstrFilter.startswith('Text files')
        return 0
    monkeypatch.setattr(picker.sys,'platform','win32')
    monkeypatch.setattr(picker,'_load_dialog_library',lambda:dialog_library(picker,invoke))
    assert picker.pick_file('in',text_only=True)=={'cancelled':True}


def test_windows_dialog_error_is_distinct_from_cancellation(picker, monkeypatch):
    monkeypatch.setattr(picker.sys, "platform", "win32")
    monkeypatch.setattr(picker, "_load_dialog_library", lambda: dialog_library(picker, lambda request: 0, extended_error=0x3002))
    with pytest.raises(ValueError, match="0x3002"):
        picker.pick_file("in")


def test_linux_picker_has_descriptive_platform_error(picker, monkeypatch):
    monkeypatch.setattr(picker.sys, "platform", "linux")
    with pytest.raises(ValueError, match="Windows"):
        picker.pick_file("in")


def test_picker_rejects_invalid_direction(picker):
    with pytest.raises(ValueError, match="direction"):
        picker.pick_file("invalid")


def directory_libraries(picker, chosen, *, path_success=True, initialized=0):
    calls = []
    def browse(pointer):
        request = ctypes.cast(pointer, ctypes.POINTER(picker.BROWSEINFOW)).contents
        assert request.ulFlags & 0x01 and request.ulFlags & 0x40
        request.lpfn(123, 1, 0, request.lParam)
        calls.append("browse")
        return 456 if chosen is not None else None
    def get_path(pidl, buffer, length, flags):
        assert pidl == 456 and length == 32768
        buffer.value = chosen
        return int(path_success)
    def select(hwnd, message, unicode_path, value):
        assert hwnd == 123 and message == 0x467 and unicode_path == 1
        calls.append(("initial", ctypes.wstring_at(value)))
        return 1
    shell = SimpleNamespace(SHBrowseForFolderW=Call(browse), SHGetPathFromIDListEx=Call(get_path))
    ole = SimpleNamespace(CoInitializeEx=Call(lambda *_: initialized),
                          CoTaskMemFree=Call(lambda pidl: calls.append(("free", pidl))),
                          CoUninitialize=Call(lambda: calls.append("uninitialize")))
    user = SimpleNamespace(SendMessageW=Call(select))
    return (shell, ole, user), calls


@pytest.mark.parametrize("chosen", ["C:\\资料 目录\\导出", None])
@pytest.mark.parametrize("initialized", [0, 1])
def test_folder_picker_initial_unicode_directory_and_com_cleanup(picker, monkeypatch, chosen, initialized):
    libraries, calls = directory_libraries(picker, chosen, initialized=initialized)
    monkeypatch.setattr(picker.sys, "platform", "win32")
    monkeypatch.setattr(picker, "_load_directory_libraries", lambda: libraries, raising=False)
    result = picker.pick_directory("C:\\程序 目录")
    assert result == ({"cancelled": True} if chosen is None else {"cancelled": False, "path": chosen})
    assert ("initial", "C:\\程序 目录") in calls
    assert (("free", 456) in calls) == (chosen is not None)
    assert calls[-1] == "uninitialize"


def test_folder_picker_frees_pidl_if_path_conversion_fails(picker, monkeypatch):
    libraries, calls = directory_libraries(picker, "C:\\not-a-filesystem-folder", path_success=False)
    monkeypatch.setattr(picker.sys, "platform", "win32")
    monkeypatch.setattr(picker, "_load_directory_libraries", lambda: libraries, raising=False)
    with pytest.raises(ValueError, match="folder"):
        picker.pick_directory("C:\\default")
    assert calls[-2:] == [("free", 456), "uninitialize"]


def test_folder_picker_requires_windows(picker, monkeypatch):
    monkeypatch.setattr(picker.sys, "platform", "linux")
    with pytest.raises(ValueError, match="Windows"):
        picker.pick_directory()


def test_folder_picker_does_not_uninitialize_failed_com_start(picker, monkeypatch):
    libraries, calls = directory_libraries(picker, None, initialized=-2147417850)
    monkeypatch.setattr(picker.sys, "platform", "win32")
    monkeypatch.setattr(picker, "_load_directory_libraries", lambda: libraries)
    with pytest.raises(ValueError, match="initialization"):
        picker.pick_directory()
    assert not calls
