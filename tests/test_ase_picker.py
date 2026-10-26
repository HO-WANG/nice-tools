import ctypes
from types import SimpleNamespace

import pytest

from nice_tools import ase_picker as picker


class Call:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class DialogCOM:
    """The native boundary only: exercise the production configuration/lifetime."""
    def __init__(self, monkeypatch, *, initialized=0, fail=None, chosen='C:\\자료 폴더\\文件.txt'):
        self.calls = []
        self.fail = fail
        self.chosen = ctypes.create_unicode_buffer(chosen)
        self.shell = SimpleNamespace(SHCreateItemFromParsingName=Call(self.initial))
        self.ole = SimpleNamespace(
            CoInitializeEx=Call(lambda *_: initialized),
            CoUninitialize=Call(lambda: self.calls.append('uninitialize')),
            CoCreateInstance=Call(self.create),
            CoTaskMemFree=Call(lambda pointer: self.calls.append(('free', pointer.value))),
        )
        monkeypatch.setattr(picker.sys, 'platform', 'win32')
        monkeypatch.setattr(picker, '_load_libraries', lambda: (self.shell, self.ole), raising=False)
        monkeypatch.setattr(picker, '_invoke', self.invoke, raising=False)
        monkeypatch.setattr(picker, '_release', lambda pointer: self.calls.append(('release', pointer.value)) if pointer else None, raising=False)

    def create(self, clsid, outer, context, iid, output):
        self.calls.append(('create', bytes(ctypes.cast(clsid, ctypes.POINTER(picker.GUID)).contents),
                           bytes(ctypes.cast(iid, ctypes.POINTER(picker.GUID)).contents)))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 101
        return self.fail[1] if self.fail and self.fail[0] == 'create' else 0

    def initial(self, path, bind, iid, output):
        self.calls.append(('initial', path))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 102
        return self.fail[1] if self.fail and self.fail[0] == 'initial' else 0

    def invoke(self, pointer, index, argtypes, *args):
        self.calls.append((index, args))
        if index == 10:  # IFileDialog.GetOptions
            ctypes.cast(args[0], ctypes.POINTER(ctypes.c_uint32))[0] = 0x40
        elif index == 20:  # IFileDialog.GetResult
            ctypes.cast(args[0], ctypes.POINTER(ctypes.c_void_p))[0] = 103
        elif pointer.value == 103 and index == 5:  # IShellItem.GetDisplayName
            assert args[0] == 0x80058000  # SIGDN_FILESYSPATH
            ctypes.cast(args[1], ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(self.chosen)
        if self.fail and self.fail[0] == index:
            return self.fail[1]
        return 0

    def args(self, index):
        return [call[1] for call in self.calls if isinstance(call, tuple) and call[0] == index]


@pytest.mark.parametrize('direction', ['in', 'out'])
def test_common_item_dialog_file_contract(monkeypatch, direction):
    native = DialogCOM(monkeypatch)
    assert picker.pick_file(direction, 'C:\\other\\预览.bcp') == {'cancelled': False, 'path': native.chosen.value}
    assert native.calls[0][1] == bytes(picker.CLSID_FILE_OPEN_DIALOG if direction == 'in' else picker.CLSID_FILE_SAVE_DIALOG)
    flags = native.args(9)[0][0]
    assert flags & picker.FOS_FORCEFILESYSTEM
    assert flags & picker.FOS_NOCHANGEDIR
    assert flags & picker.FOS_PATHMUSTEXIST
    assert flags & picker.FOS_DONTADDTORECENT
    assert flags & (picker.FOS_FILEMUSTEXIST if direction == 'in' else picker.FOS_OVERWRITEPROMPT)
    assert not flags & picker.FOS_PICKFOLDERS
    assert native.args(15) == [('预览.bcp',)]
    assert native.args(22) == [('bcp',)]
    assert native.calls[-4:] == [('free', ctypes.addressof(native.chosen)), ('release', 103), ('release', 101), 'uninitialize']


@pytest.mark.parametrize('initialized', [0, 1])
def test_common_item_folder_initial_path_and_cleanup(monkeypatch, initialized):
    native = DialogCOM(monkeypatch, initialized=initialized)
    result = picker.pick_directory('C:\\프로그램 目录')
    assert result['path'] == native.chosen.value
    assert ('initial', 'C:\\프로그램 目录') in native.calls
    assert native.args(12)[0][0].value == 102
    assert ('release', 102) in native.calls
    assert native.args(9)[0][0] & picker.FOS_PICKFOLDERS
    assert not native.args(4)  # Folder dialog does not set file filters.
    assert native.calls[-1] == 'uninitialize'


def test_text_inspection_sets_text_filter(monkeypatch):
    native = DialogCOM(monkeypatch, fail=(3, 0x800704C7))
    assert picker.pick_file('in', text_only=True) == {'cancelled': True}
    filters = native.args(4)[0]
    assert filters[0] == 2
    assert filters[1][0].pszName == 'Text files (*.txt;*.csv;*.log;*.sql;*.dat)'
    assert filters[1][0].pszSpec == '*.txt;*.csv;*.log;*.sql;*.dat'
    assert native.args(17) == [('Select a text file',)]
    assert native.args(22) == [('txt',)]
    assert not native.args(20)
    assert native.calls[-2:] == [('release', 101), 'uninitialize']


@pytest.mark.parametrize('operation', ['folder', 'in', 'out'])
def test_cancel_is_only_show_cancel_hresult(monkeypatch, operation):
    native = DialogCOM(monkeypatch, fail=(3, -2147023673))
    result = picker.pick_directory() if operation == 'folder' else picker.pick_file(operation)
    assert result == {'cancelled': True}
    assert native.calls[-2:] == [('release', 101), 'uninitialize']


@pytest.mark.parametrize('stage', ['create', 9, 3, 20, 5])
def test_failure_releases_every_acquired_resource(monkeypatch, stage):
    native = DialogCOM(monkeypatch, fail=(stage, 0x80004005))
    with pytest.raises(ValueError, match='0x80004005'):
        picker.pick_directory('C:\\initial')
    assert ('release', 101) in native.calls
    if stage not in ('create', 9):
        assert ('release', 102) in native.calls
    if stage in (20, 5):
        assert ('release', 103) in native.calls
    if stage == 5:
        assert ('free', ctypes.addressof(native.chosen)) in native.calls
    assert native.calls[-1] == 'uninitialize'


def test_failed_sta_does_not_uninitialize(monkeypatch):
    native = DialogCOM(monkeypatch, initialized=0x80010106)
    with pytest.raises(ValueError, match='initialization'):
        picker.pick_directory()
    assert not native.calls


def test_empty_filesystem_path_is_rejected_and_freed(monkeypatch):
    native = DialogCOM(monkeypatch, chosen='')
    with pytest.raises(ValueError, match='filesystem path'):
        picker.pick_file('in')
    assert native.calls[-4:] == [('free', ctypes.addressof(native.chosen)), ('release', 103), ('release', 101), 'uninitialize']


@pytest.mark.parametrize('method,args,kwargs', [
    ('pick_file', ('invalid',), {}), ('pick_file', ('in', 12), {}),
    ('pick_file', ('in', 'bad\0.txt'), {}), ('pick_file', ('out',), {'text_only': True}),
    ('pick_file', ('in', 'x' * 32768), {}),
    ('pick_directory', (12,), {}), ('pick_directory', ('bad\0path',), {}),
    ('pick_directory', ('x' * 32768,), {}),
])
def test_validation_precedes_com(monkeypatch, method, args, kwargs):
    native = DialogCOM(monkeypatch)
    with pytest.raises(ValueError):
        getattr(picker, method)(*args, **kwargs)
    assert not native.calls


@pytest.mark.parametrize('method,args', [('pick_directory', ()), ('pick_file', ('in',))])
def test_non_windows_has_descriptive_error(monkeypatch, method, args):
    monkeypatch.setattr(picker.sys, 'platform', 'linux')
    with pytest.raises(ValueError, match='Windows'):
        getattr(picker, method)(*args)


def test_guid_hresult_and_com_pointer_native_widths():
    assert ctypes.sizeof(picker.GUID) == 16
    assert ctypes.sizeof(picker.HRESULT) == 4
    assert bytes(picker.CLSID_FILE_OPEN_DIALOG).hex() == '9c5a1cdc8ae8de4da5a160f82a20aef7'
    # An actual ctypes vtable catches pointer truncation and calling convention mistakes.
    callback_type = getattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE)(
        ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    expected = 0x123456789 if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x12345
    @callback_type
    def callback(this, output):
        output[0] = expected
        return -2147467259
    table = (ctypes.c_void_p * 1)(ctypes.cast(callback, ctypes.c_void_p).value)
    instance = (ctypes.c_void_p * 1)(ctypes.addressof(table))
    output = ctypes.c_void_p()
    assert picker._invoke(ctypes.cast(instance, ctypes.c_void_p), 0,
                          (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(output)) == -2147467259
    assert output.value == expected


@pytest.mark.parametrize('stage,error', [
    ('initial', 0x80070002),  # previously selected folder was removed
    ('initial', 0x80070003),  # previously selected folder was renamed/moved
    (12, 0x80004005),  # Shell item resolves but SetFolder cannot use it
])
@pytest.mark.parametrize('cancelled', [False, True])
def test_stale_initial_folder_still_allows_reselection(monkeypatch, stage, error, cancelled):
    native = DialogCOM(monkeypatch, fail=(stage, error))
    invoke = picker._invoke
    def receive(pointer, index, argtypes, *args):
        result = invoke(pointer, index, argtypes, *args)
        return 0x800704C7 if index == 3 and cancelled else result
    monkeypatch.setattr(picker, '_invoke', receive)
    result = picker.pick_directory('C:\\previously selected')
    assert native.args(3) == [(None,)]
    assert result == ({'cancelled': True} if cancelled else {'cancelled': False, 'path': native.chosen.value})
    assert bool(native.args(12)) == (stage == 12)
    assert native.calls.count(('release', 102)) == 1
    assert native.calls.count(('release', 101)) == 1
    assert native.calls.count('uninitialize') == 1
    if cancelled:
        assert not native.args(20)
    else:
        assert ('release', 103) in native.calls
    assert native.calls[-1] == 'uninitialize'
