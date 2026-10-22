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

BFFCALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_ssize_t, ctypes.c_ssize_t,
)


class BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", ctypes.c_void_p), ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", ctypes.POINTER(ctypes.c_wchar)), ("lpszTitle", ctypes.c_wchar_p),
        ("ulFlags", ctypes.c_uint32), ("lpfn", BFFCALLBACK),
        ("lParam", ctypes.c_ssize_t), ("iImage", ctypes.c_int32),
    ]


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


def _load_directory_libraries():
    return (ctypes.WinDLL("shell32", use_last_error=True),
            ctypes.WinDLL("ole32", use_last_error=True),
            ctypes.WinDLL("user32", use_last_error=True))


def pick_directory(initial_directory: str | None = None) -> dict:
    """Choose a filesystem folder in an owned worker with a COM STA dialog."""
    if sys.platform != "win32":
        raise ValueError("Native folder selection requires the Windows desktop application.")
    if initial_directory is not None and (
        not isinstance(initial_directory, str) or "\0" in initial_directory or len(initial_directory) >= MAX_PATH_CHARS
    ):
        raise ValueError("The initial folder path is invalid.")
    shell, ole, user = _load_directory_libraries()
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole.CoInitializeEx.restype = ctypes.c_int32
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    shell.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell.SHGetPathFromIDListEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar), ctypes.c_uint32, ctypes.c_uint32]
    shell.SHGetPathFromIDListEx.restype = ctypes.c_int32
    user.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t]
    user.SendMessageW.restype = ctypes.c_ssize_t
    if ole.CoInitializeEx(None, 2) < 0:  # COINIT_APARTMENTTHREADED
        raise ValueError("Windows folder selection initialization failed.")
    pidl = None
    try:
        initial = ctypes.create_unicode_buffer(initial_directory or "")
        display = ctypes.create_unicode_buffer(MAX_PATH_CHARS)
        @BFFCALLBACK
        def callback(hwnd, message, _parameter, data):
            if message == 1 and initial_directory:  # BFFM_INITIALIZED
                user.SendMessageW(hwnd, 0x467, 1, data)  # BFFM_SETSELECTIONW
            return 0
        request = BROWSEINFOW()
        request.pszDisplayName = display
        request.lpszTitle = "Select a folder"
        request.ulFlags = 0x01 | 0x10 | 0x40  # filesystem folders, editable path, new dialog
        request.lpfn = callback
        request.lParam = ctypes.cast(initial, ctypes.c_void_p).value
        pidl = shell.SHBrowseForFolderW(ctypes.byref(request))
        if not pidl:
            return {"cancelled": True}
        chosen = ctypes.create_unicode_buffer(MAX_PATH_CHARS)
        if not shell.SHGetPathFromIDListEx(pidl, chosen, MAX_PATH_CHARS, 0) or not chosen.value:
            raise ValueError("The selected folder does not have a filesystem path.")
        return {"cancelled": False, "path": chosen.value}
    finally:
        if pidl:
            ole.CoTaskMemFree(pidl)
        ole.CoUninitialize()


def pick_file(direction: str, suggested_name: str | None = None) -> dict:
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
    request.lpstrFilter = "BCP and text files (*.bcp;*.txt)\0*.bcp;*.txt\0All files (*.*)\0*.*\0\0"
    request.nFilterIndex = 1
    request.lpstrFile = buffer
    request.nMaxFile = MAX_PATH_CHARS
    request.lpstrTitle = "Select a BCP input file" if direction == "in" else "Save a BCP output file"
    request.lpstrDefExt = "bcp"
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
