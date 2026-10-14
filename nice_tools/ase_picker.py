"""Windows file dialogs for files that remain on the user's own computer."""

from __future__ import annotations

import ctypes
import ntpath
import sys


OFN_OVERWRITEPROMPT = 0x00000002
OFN_HIDEREADONLY = 0x00000004
OFN_NOCHANGEDIR = 0x00000008
OFN_PATHMUSTEXIST = 0x00000800
OFN_FILEMUSTEXIST = 0x00001000
OFN_EXPLORER = 0x00080000
OFN_DONTADDTORECENT = 0x02000000
MAX_PATH_CHARS = 32768


class OPENFILENAMEW(ctypes.Structure):
    # Explicit Windows integer widths also make this layout inspectable on Linux.
    _fields_ = [
        ("lStructSize", ctypes.c_uint32),
        ("hwndOwner", ctypes.c_void_p),
        ("hInstance", ctypes.c_void_p),
        ("lpstrFilter", ctypes.c_wchar_p),
        ("lpstrCustomFilter", ctypes.POINTER(ctypes.c_wchar)),
        ("nMaxCustFilter", ctypes.c_uint32),
        ("nFilterIndex", ctypes.c_uint32),
        ("lpstrFile", ctypes.POINTER(ctypes.c_wchar)),
        ("nMaxFile", ctypes.c_uint32),
        ("lpstrFileTitle", ctypes.POINTER(ctypes.c_wchar)),
        ("nMaxFileTitle", ctypes.c_uint32),
        ("lpstrInitialDir", ctypes.c_wchar_p),
        ("lpstrTitle", ctypes.c_wchar_p),
        ("Flags", ctypes.c_uint32),
        ("nFileOffset", ctypes.c_uint16),
        ("nFileExtension", ctypes.c_uint16),
        ("lpstrDefExt", ctypes.c_wchar_p),
        ("lCustData", ctypes.c_ssize_t),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", ctypes.c_wchar_p),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", ctypes.c_uint32),
        ("FlagsEx", ctypes.c_uint32),
    ]


def _load_dialog_library():
    return ctypes.WinDLL("comdlg32", use_last_error=True)


def _pick_file(direction: str, suggested_name: str | None = None, *, sql_ini=False) -> dict:
    """Return a real Windows path, or an explicit cancellation result."""
    if not isinstance(direction, str) or direction.lower() not in {"in", "out"}:
        raise ValueError("File direction must be 'in' or 'out'.")
    direction = direction.lower()
    if sys.platform != "win32":
        raise ValueError("Native file selection requires the Windows desktop application.")

    library = _load_dialog_library()
    for name in ("GetOpenFileNameW", "GetSaveFileNameW"):
        function = getattr(library, name)
        function.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
        function.restype = ctypes.c_int32
    library.CommDlgExtendedError.argtypes = []
    library.CommDlgExtendedError.restype = ctypes.c_uint32

    buffer = ctypes.create_unicode_buffer(MAX_PATH_CHARS)
    if suggested_name is not None:
        if not isinstance(suggested_name, str) or "\0" in suggested_name:
            raise ValueError("Suggested filename is invalid.")
        # A suggested table filename should not change the dialog's directory.
        filename = ntpath.basename(suggested_name)
        if len(filename) >= MAX_PATH_CHARS:
            raise ValueError("Suggested filename is too long.")
        buffer.value = filename

    request = OPENFILENAMEW()
    request.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    request.lpstrFilter = ("SAP server files (*.ini)\0*.ini\0All files (*.*)\0*.*\0\0" if sql_ini else
                           "BCP and text files (*.bcp;*.txt)\0*.bcp;*.txt\0All files (*.*)\0*.*\0\0")
    request.nFilterIndex = 1
    request.lpstrFile = buffer
    request.nMaxFile = MAX_PATH_CHARS
    request.lpstrTitle = ("Select SAP sql.ini file" if sql_ini else
                          "Select a BCP input file" if direction == "in" else "Save a BCP output file")
    request.lpstrDefExt = "ini" if sql_ini else "bcp"
    request.Flags = OFN_EXPLORER | OFN_HIDEREADONLY | OFN_NOCHANGEDIR | OFN_PATHMUSTEXIST | OFN_DONTADDTORECENT
    request.Flags |= OFN_FILEMUSTEXIST if direction == "in" else OFN_OVERWRITEPROMPT
    function = library.GetOpenFileNameW if direction == "in" else library.GetSaveFileNameW
    if function(ctypes.byref(request)):
        if not buffer.value:
            raise ValueError("The file dialog returned an empty path.")
        return {"cancelled": False, "path": buffer.value}
    error = library.CommDlgExtendedError()
    if error:
        raise ValueError(f"Windows file selection failed (0x{error:04x}).")
    return {"cancelled": True}


def pick_file(direction: str, suggested_name: str | None = None) -> dict:
    return _pick_file(direction, suggested_name)


def pick_sql_ini() -> dict:
    """Choose the local SAP server configuration without a database session."""
    return _pick_file("in", "sql.ini", sql_ini=True)
