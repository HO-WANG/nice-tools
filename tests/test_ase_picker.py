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


def test_sql_ini_dialog_is_an_existing_file_picker_before_connection(picker, monkeypatch):
    chosen = "C:\\SAP 설치\\ini\\sql.ini"
    def invoke(request):
        assert "*.ini" in request.lpstrFilter
        assert "sql.ini" in request.lpstrTitle
        assert request.lpstrDefExt == "ini"
        assert request.Flags & picker.OFN_FILEMUSTEXIST
        assert request.Flags & picker.OFN_NOCHANGEDIR
        assert request.Flags & picker.OFN_DONTADDTORECENT
        assert not request.Flags & picker.OFN_OVERWRITEPROMPT
        buffer = ctypes.cast(request.lpstrFile, ctypes.POINTER(ctypes.c_wchar * request.nMaxFile)).contents
        assert buffer.value == "sql.ini"
        buffer.value = chosen
        return 1
    monkeypatch.setattr(picker.sys, "platform", "win32")
    monkeypatch.setattr(picker, "_load_dialog_library", lambda: dialog_library(picker, invoke))
    assert picker.pick_sql_ini() == {"cancelled": False, "path": chosen}


def test_sql_ini_picker_cancellation_does_not_return_a_path(picker, monkeypatch):
    monkeypatch.setattr(picker.sys, "platform", "win32")
    monkeypatch.setattr(picker, "_load_dialog_library", lambda: dialog_library(picker, lambda request: 0))
    assert picker.pick_sql_ini() == {"cancelled": True}
