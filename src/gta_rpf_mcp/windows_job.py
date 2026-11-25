from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes

from .workspace import _windows


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("per_process_time", ctypes.c_longlong), ("per_job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t), ("max_working_set", ctypes.c_size_t),
        ("process_count", wintypes.DWORD), ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(key, ctypes.c_ulonglong) for key in ("reads", "writes", "other", "read_bytes", "written_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IoCounters), ("process_bytes", ctypes.c_size_t),
                ("job_bytes", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


@contextmanager
def bounded_job():
    """Hold a non-inherited job handle; closing it kills any remaining workers."""
    k, _, _ = _windows()
    k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    h = k.CreateJobObjectW(None, None)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = _ExtendedLimits()
        # ACTIVE_PROCESS | PROCESS_MEMORY | JOB_MEMORY | KILL_ON_JOB_CLOSE
        limits.basic.flags = 0x8 | 0x100 | 0x200 | 0x2000
        limits.basic.process_count = 4
        limits.process_bytes = 1536 * 1024**2
        limits.job_bytes = 2048 * 1024**2
        if not k.SetInformationJobObject(h, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())

        def assign(pid):
            # PROCESS_SET_QUOTA | PROCESS_TERMINATE, the required rights.
            process = k.OpenProcess(0x100 | 1, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not k.AssignProcessToJobObject(h, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                k.CloseHandle(process)

        yield assign
    finally:
        k.CloseHandle(h)
