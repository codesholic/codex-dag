"""Small OS boundary for journal locks, app storage and parent observation.

All Windows APIs are loaded lazily so the same source keeps its POSIX behavior.
Locks never change journal bytes, and process handles only observe a parent.
"""
from contextlib import contextmanager
import ctypes
from functools import lru_cache
import os
from pathlib import Path


def default_data_directory():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        return base / "Codex DAG"
    return Path.home() / "Library/Application Support/Codex DAG"


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32),
                ("hEvent", ctypes.c_void_p)]


@lru_cache(maxsize=1)
def _windows_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_Overlapped)]
    kernel.LockFileEx.restype = ctypes.c_int
    kernel.UnlockFileEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_uint32, ctypes.POINTER(_Overlapped)]
    kernel.UnlockFileEx.restype = ctypes.c_int
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    return kernel


@contextmanager
def locked_file(handle, exclusive=False, nonblocking=False):
    """Coordinate readers/writers with one whole-ledger advisory lock.

Windows locks the same first byte for every cooperating reader/writer, including
empty files. LockFileEx supports shared readers without writing a sentinel byte.
"""
    if os.name != "nt":
        import fcntl
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if nonblocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(handle, flags)
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
        return
    import msvcrt
    api = _windows_api()
    native = msvcrt.get_osfhandle(handle.fileno())
    overlapped = _Overlapped()
    flags = (2 if exclusive else 0) | (1 if nonblocking else 0)
    if not api.LockFileEx(native, flags, 0, 1, 0, ctypes.byref(overlapped)):
        code = ctypes.get_last_error()
        if code == 33 and nonblocking:  # ERROR_LOCK_VIOLATION
            raise BlockingIOError("File is locked by another Codex DAG process")
        raise ctypes.WinError(code)
    try:
        yield handle
    finally:
        if not api.UnlockFileEx(native, 0, 1, 0, ctypes.byref(overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())


class WindowsParentProcess:
    """Observe the original parent handle, never a possibly recycled PID."""
    def __init__(self, pid):
        self.api = _windows_api()
        self.handle = self.api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not self.handle:
            code = ctypes.get_last_error()
            if code != 87:  # ERROR_INVALID_PARAMETER: the parent already exited
                raise ctypes.WinError(code)

    def alive(self):
        if not self.handle:
            return False
        result = self.api.WaitForSingleObject(self.handle, 0)
        if result == 258:  # WAIT_TIMEOUT: still running
            return True
        if result == 0:  # WAIT_OBJECT_0: process exited
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class PosixParentProcess:
    def __init__(self, pid):
        self.pid = pid

    def alive(self):
        try:
            os.kill(self.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def close(self):
        pass


def observe_parent(pid):
    return WindowsParentProcess(pid) if os.name == "nt" else PosixParentProcess(pid)
