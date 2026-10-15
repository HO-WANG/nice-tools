"""Own OUT reservations across native copying, publication and cleanup."""

from __future__ import annotations

import contextlib
import ctypes
import os
from pathlib import Path


def file_identity(stat):
    return (stat.st_dev, stat.st_ino)


def cleanup_owned_path(path, identity):
    """Never remove a same-named replacement or a symlink to another file."""
    with contextlib.suppress(OSError):
        if file_identity(Path(path).lstat()) == tuple(identity):
            Path(path).unlink()


class _Directory:
    def __init__(self, path):
        self.path = Path(path)
        self.fd = None
        self.handle = None
        if os.name == 'nt':
            library = ctypes.WinDLL('kernel32', use_last_error=True)
            library.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                           ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            library.CreateFileW.restype = ctypes.c_void_p
            library.CloseHandle.argtypes = [ctypes.c_void_p]
            library.CloseHandle.restype = ctypes.c_int32
            # FILE_LIST_DIRECTORY establishes read access for sharing checks;
            # zero requests metadata only and does not prevent rename. Share
            # reads/writes for SAP, but omit DELETE until the export settles.
            handle = library.CreateFileW(str(self.path), 0x1, 0x3, None, 3, 0x02000000, None)
            if handle is None or handle == ctypes.c_void_p(-1).value:
                raise OSError(ctypes.get_last_error(), 'The OUT directory could not be reserved safely.')
            self.library, self.handle = library, handle
            try:
                self.identity = file_identity(self.path.stat())
            except Exception:
                self.close()
                raise
        else:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
            self.identity = file_identity(os.fstat(self.fd))

    def cleanup(self, stage, identity):
        if self.fd is not None:
            # The directory descriptor still owns the original directory even
            # if it was moved while SAP held the stage open.
            with contextlib.suppress(OSError):
                stat = os.stat(Path(stage).name, dir_fd=self.fd, follow_symlinks=False)
                if file_identity(stat) == tuple(identity):
                    os.unlink(Path(stage).name, dir_fd=self.fd)
        else:
            cleanup_owned_path(stage, identity)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.handle is not None:
            self.library.CloseHandle(self.handle)
            self.handle = None


class ExportReservation:
    def __init__(self, root, stage, stage_identity):
        self.root, self.stage = Path(root), Path(stage)
        self.stage_identity = tuple(stage_identity)
        self.directory = None
        try:
            self.directory = _Directory(self.root)
            self.boundary = {'root': str(self.root),
                             'root_identity': self.directory.identity,
                             'stage_identity': self.stage_identity}
            self.validate()
        except Exception:
            self.close()
            raise

    def validate(self):
        if (self.root.resolve() != self.root or self.stage.parent != self.root
                or file_identity(self.root.stat()) != self.boundary['root_identity']
                or file_identity(self.stage.lstat()) != self.stage_identity):
            raise ValueError('OUT 폴더 또는 임시 파일이 변경되어 결과를 저장하지 않았습니다.')

    def cleanup(self):
        if self.directory is not None:
            self.directory.cleanup(self.stage, self.stage_identity)

    def close(self):
        if self.directory is not None:
            self.directory.close()
            self.directory = None
