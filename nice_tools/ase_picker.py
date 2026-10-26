"""Windows Common Item Dialogs for files that remain on the user's computer.

The worker calls these synchronously on its own STA thread. Only filesystem
paths cross this boundary; shell namespace items and multiple selection do not.
"""

from __future__ import annotations

import ctypes
import ntpath
import sys
import uuid


HRESULT = ctypes.c_int32  # Windows LONG is 32 bits, including on x64.
UINT = ctypes.c_uint32
PVOID = ctypes.c_void_p
PPVOID = ctypes.POINTER(PVOID)
WINFUNCTYPE = getattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE)
MAX_PATH_CHARS = 32768
FOS_OVERWRITEPROMPT = 0x00000002
FOS_NOCHANGEDIR = 0x00000008
FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040
FOS_PATHMUSTEXIST = 0x00000800
FOS_FILEMUSTEXIST = 0x00001000
FOS_DONTADDTORECENT = 0x02000000
ERROR_CANCELLED = 0x800704C7
SIGDN_FILESYSPATH = 0x80058000


class GUID(ctypes.Structure):
    _fields_ = [('Data1', UINT), ('Data2', ctypes.c_uint16),
                ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


CLSID_FILE_OPEN_DIALOG = GUID.parse('DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7')
CLSID_FILE_SAVE_DIALOG = GUID.parse('C0B4E2F3-BA21-4773-8DBA-335EC946EB8B')
IID_FILE_OPEN_DIALOG = GUID.parse('D57C7288-D4AD-4768-BE02-9D969532D960')
IID_FILE_SAVE_DIALOG = GUID.parse('84BCCD23-5FDE-4CDB-AEA4-AF64B83D78AB')
IID_SHELL_ITEM = GUID.parse('43826D1E-E718-42EE-BC55-A1E261C37BFE')


class COMDLG_FILTERSPEC(ctypes.Structure):
    _fields_ = [('pszName', ctypes.c_wchar_p), ('pszSpec', ctypes.c_wchar_p)]


def _load_libraries():
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    ole = ctypes.WinDLL('ole32', use_last_error=True)
    ole.CoInitializeEx.argtypes = [PVOID, UINT]
    ole.CoInitializeEx.restype = HRESULT
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), PVOID, UINT, ctypes.POINTER(GUID), PPVOID]
    ole.CoCreateInstance.restype = HRESULT
    ole.CoTaskMemFree.argtypes = [PVOID]
    ole.CoTaskMemFree.restype = None
    shell.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, PVOID, ctypes.POINTER(GUID), PPVOID]
    shell.SHCreateItemFromParsingName.restype = HRESULT
    return shell, ole


def _invoke(pointer, index, argtypes, *args):
    """Invoke an HRESULT-returning COM vtable slot using Windows ABI widths."""
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(PVOID))).contents
    function = WINFUNCTYPE(HRESULT, PVOID, *argtypes)(table[index])
    return function(pointer, *args)


def _release(pointer):
    if pointer:
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(PVOID))).contents
        WINFUNCTYPE(UINT, PVOID)(table[2])(pointer)  # IUnknown.Release


def _check(result, action):
    if ctypes.c_int32(result).value < 0:
        raise ValueError(f'Windows selection {action} failed (0x{result & 0xFFFFFFFF:08x}).')


def _configure_dialog(dialog, *, folder=False, direction='in', suggested_name=None, text_only=False):
    """Configure one real IFileDialog; kept separate for native configuration checks."""
    options = UINT()
    _check(_invoke(dialog, 10, (ctypes.POINTER(UINT),), ctypes.byref(options)), 'options')
    flags = options.value | FOS_FORCEFILESYSTEM | FOS_NOCHANGEDIR | FOS_PATHMUSTEXIST | FOS_DONTADDTORECENT
    flags |= FOS_PICKFOLDERS if folder else (FOS_FILEMUSTEXIST if direction == 'in' else FOS_OVERWRITEPROMPT)
    _check(_invoke(dialog, 9, (UINT,), flags), 'options')
    title = 'Select a folder' if folder else ('Select a BCP input file' if direction == 'in' else 'Save a BCP output file')
    if text_only:
        title = 'Select a text file'
    _check(_invoke(dialog, 17, (ctypes.c_wchar_p,), title), 'title')
    if not folder:
        name, pattern = ('Text files (*.txt;*.csv;*.log;*.sql;*.dat)', '*.txt;*.csv;*.log;*.sql;*.dat') if text_only else (
            'BCP and text files (*.bcp;*.txt)', '*.bcp;*.txt')
        filters = (COMDLG_FILTERSPEC * 2)((name, pattern), ('All files (*.*)', '*.*'))
        _check(_invoke(dialog, 4, (UINT, ctypes.POINTER(COMDLG_FILTERSPEC)), 2, filters), 'file filters')
        _check(_invoke(dialog, 5, (UINT,), 1), 'file filter index')
        _check(_invoke(dialog, 22, (ctypes.c_wchar_p,), 'txt' if text_only else 'bcp'), 'default extension')
        if suggested_name is not None:
            _check(_invoke(dialog, 15, (ctypes.c_wchar_p,), suggested_name), 'suggested filename')


def _show_dialog(*, folder=False, direction='in', initial_directory=None, suggested_name=None, text_only=False):
    shell, ole = _load_libraries()
    _check(ole.CoInitializeEx(None, 2), 'initialization')  # COINIT_APARTMENTTHREADED
    dialog, item, initial, path = PVOID(), PVOID(), PVOID(), PVOID()
    try:
        clsid, iid = (CLSID_FILE_SAVE_DIALOG, IID_FILE_SAVE_DIALOG) if direction == 'out' else (CLSID_FILE_OPEN_DIALOG, IID_FILE_OPEN_DIALOG)
        _check(ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(dialog)), 'dialog creation')
        if not dialog:
            raise ValueError('Windows selection returned no dialog.')
        _configure_dialog(dialog, folder=folder, direction=direction, suggested_name=suggested_name, text_only=text_only)
        if initial_directory:
            try:
                # The remembered location is only a hint: moved/deleted folders
                # and Shell navigation failures must still allow reselection.
                resolved = shell.SHCreateItemFromParsingName(
                    initial_directory, None, ctypes.byref(IID_SHELL_ITEM), ctypes.byref(initial))
                if ctypes.c_int32(resolved).value >= 0 and initial:
                    _invoke(dialog, 12, (PVOID,), initial)  # best-effort SetFolder
            finally:
                _release(initial)
                initial = PVOID()
        shown = _invoke(dialog, 3, (PVOID,), None)  # IModalWindow.Show
        if shown & 0xFFFFFFFF == ERROR_CANCELLED:
            return {'cancelled': True}
        _check(shown, 'dialog')
        _check(_invoke(dialog, 20, (PPVOID,), ctypes.byref(item)), 'result')
        if not item:
            raise ValueError('Windows selection returned no filesystem item.')
        _check(_invoke(item, 5, (UINT, PPVOID), SIGDN_FILESYSPATH, ctypes.byref(path)), 'filesystem path')
        chosen = ctypes.wstring_at(path) if path else ''
        if not chosen:
            raise ValueError('The selected item does not have a filesystem path.')
        return {'cancelled': False, 'path': chosen}
    finally:
        if path:
            ole.CoTaskMemFree(path)
        _release(item)
        _release(dialog)
        ole.CoUninitialize()  # Both S_OK and S_FALSE require one balance.


def pick_directory(initial_directory: str | None = None) -> dict:
    """Choose a filesystem folder with IFileOpenDialog + FOS_PICKFOLDERS."""
    if sys.platform != 'win32':
        raise ValueError('Native folder selection requires the Windows desktop application.')
    if initial_directory is not None and (
        not isinstance(initial_directory, str) or '\0' in initial_directory or len(initial_directory) >= MAX_PATH_CHARS
    ):
        raise ValueError('The initial folder path is invalid.')
    return _show_dialog(folder=True, initial_directory=initial_directory)


def pick_file(direction: str, suggested_name: str | None = None, *, text_only: bool = False) -> dict:
    """Open an existing file, or choose a save path with IFileSaveDialog."""
    if not isinstance(direction, str) or direction.lower() not in {'in', 'out'}:
        raise ValueError("File direction must be 'in' or 'out'.")
    direction = direction.lower()
    if sys.platform != 'win32':
        raise ValueError('Native file selection requires the Windows desktop application.')
    if text_only and direction != 'in':
        raise ValueError('Text inspection only opens existing files.')
    if suggested_name is not None:
        if not isinstance(suggested_name, str) or '\0' in suggested_name:
            raise ValueError('Suggested filename is invalid.')
        suggested_name = ntpath.basename(suggested_name)
        if len(suggested_name) >= MAX_PATH_CHARS:
            raise ValueError('Suggested filename is too long.')
    return _show_dialog(direction=direction, suggested_name=suggested_name, text_only=text_only)
