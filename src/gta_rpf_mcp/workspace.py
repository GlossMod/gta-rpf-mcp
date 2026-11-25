from __future__ import annotations

import ctypes
import hashlib
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
import re
import stat

from .protocol import MAX_JOB_BYTES, Rejected, invalid, strict_json

_REPARSE = 0x400
_DEVICES = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])\Z", re.I)
_FORBIDDEN = frozenset('<>:"|?*')
_INTERNAL = frozenset({"input.json", "output", "result.json", ".prepare.lock"})
_CHUNK = 1024 * 1024


def split_relative(value, *, job_input=True):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise invalid("Invalid task-relative path.")
    parts = value.replace("\\", "/").split("/")
    if len(parts) > 24:
        raise invalid("Task-relative path is too deep.")
    for part in parts:
        if (not part or len(part) > 180 or part in {".", ".."} or part.endswith((".", " "))
                or part.startswith(" ") or part.split(".", 1)[0].endswith(" ")
                or any(c in _FORBIDDEN or ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in part)
                or _DEVICES.fullmatch(part.split(".", 1)[0])
                or ("~" in part and re.search(r"~[0-9]", part))):
            raise invalid("Unsafe task-relative path component.")
    first = parts[0].casefold()
    if job_input and (first in _INTERNAL or first.startswith(".work-")):
        raise invalid("Input path points to a reserved task namespace.")
    return parts


def workspace_parent() -> Path:
    raw = os.environ.get("LOCALAPPDATA", "")
    base = Path(raw)
    if not base.is_absolute() or not base.anchor or any(p == ".." for p in base.parts):
        raise Rejected("workspace_unavailable", "LOCALAPPDATA must identify the local application data directory.")
    if os.name == "nt":
        if not re.fullmatch(r"[A-Za-z]:\\", base.anchor):
            raise Rejected("workspace_unavailable", "LOCALAPPDATA must use a local Windows drive.")
        kernel, _, _ = _windows()
        kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel.GetDriveTypeW.restype = ctypes.c_uint
        if kernel.GetDriveTypeW(base.anchor) != 3:  # DRIVE_FIXED
            raise Rejected("workspace_unavailable", "LOCALAPPDATA must use a fixed local Windows drive.")
    return base / "GlossMod" / "AMM" / "gta-rpf-jobs"


def _check(info, *, directory=False):
    if getattr(info, "st_file_attributes", 0) & _REPARSE:
        raise Rejected("unsafe_workspace", "Reparse points are not allowed in the task workspace.")
    if directory and stat.S_ISDIR(info.st_mode):
        return
    if not directory and stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
        return
    raise Rejected("unsafe_workspace", "Task paths must use ordinary directories and single-link regular files.")


def _windows():
    from ctypes import wintypes

    class FileInfo(ctypes.Structure):
        _fields_ = [("attrs", wintypes.DWORD), ("created", wintypes.FILETIME), ("accessed", wintypes.FILETIME),
                    ("written", wintypes.FILETIME), ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD), ("index_high", wintypes.DWORD),
                    ("index_low", wintypes.DWORD)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.CreateFileW.restype = wintypes.HANDLE
    k.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInfo)]
    k.GetFileInformationByHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k, FileInfo, ctypes.c_void_p(-1).value


def _open_win(path: Path, directory: bool):
    k, ft, invalid_handle = _windows()
    # OPEN_REPARSE_POINT probes the object rather than following it; do not
    # share DELETE, so trusted parents cannot be renamed while leased.
    mode = 0 if directory else 0x80000000
    sharing = 3 if directory else 1
    flags = 0x00200000 | (0x02000000 if directory else 0)
    h = k.CreateFileW(str(path), mode, sharing, None, 3, flags, None)
    if h == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = ft()
        if not k.GetFileInformationByHandle(h, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (info.attrs & _REPARSE or bool(info.attrs & 0x10) != directory
                or (not directory and (info.links != 1 or info.attrs & 0x40))):
            raise Rejected("unsafe_workspace", "Task paths must not be links, reparse points or devices.")
    except BaseException:
        k.CloseHandle(h)
        raise
    return k, h


@contextmanager
def held_dir(path: Path):
    _check(path.lstat(), directory=True)
    if os.name == "nt":
        k, h = _open_win(path, True)
        try:
            yield
        finally:
            k.CloseHandle(h)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield
        finally:
            os.close(fd)


@contextmanager
def held_tree(path: Path):
    if not path.is_absolute():
        raise invalid("Task root must be absolute.")
    with ExitStack() as stack:
        for parent in (*reversed(path.parents), path):
            stack.enter_context(held_dir(parent))
        yield path


@contextmanager
def open_file(path: Path):
    _check(path.lstat())
    if os.name == "nt":
        import msvcrt
        k, h = _open_win(path, False)
        try:
            fd = msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            k.CloseHandle(h)
            raise
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        _check(os.fstat(fd))
        yield stream


def read_limited(path: Path, bound: int) -> bytes:
    with open_file(path) as src:
        if not 0 < os.fstat(src.fileno()).st_size <= bound:
            raise invalid("Required file is empty or exceeds its size limit.")
        data = src.read(bound + 1)
    if len(data) > bound:
        raise invalid("Required file exceeds its size limit.")
    return data


def load_input(job: Path):
    try:
        return strict_json(read_limited(job / "input.json", MAX_JOB_BYTES))
    except (ValueError, UnicodeError, RecursionError, FileNotFoundError) as exc:
        raise invalid("Cannot read a valid UTF-8 input.json.") from exc


def digest_stream(stream, target=None, expected=None):
    total = 0
    h = hashlib.sha256()
    while chunk := stream.read(_CHUNK):
        total += len(chunk)
        if expected is not None and total > expected:
            raise Rejected("input_changed", "A task input no longer matches its declared size.")
        h.update(chunk)
        if target is not None:
            target.write(chunk)
    return h.hexdigest(), total


def snapshot_file(job: Path, file_ref: dict, dest: Path):
    names = split_relative(file_ref["path"])
    current = job
    with ExitStack() as stack:
        for part in names[:-1]:
            current /= part
            stack.enter_context(held_dir(current))
        with open_file(current / names[-1]) as src, dest.open("xb") as out:
            if os.fstat(src.fileno()).st_size != file_ref["size"]:
                raise Rejected("input_changed", "A task input no longer matches its declared size.")
            actual = digest_stream(src, out, file_ref["size"])
            if actual != (file_ref["sha256"], file_ref["size"]):
                raise Rejected("input_changed", "A task input no longer matches its declared SHA-256 or size.")


def metadata(path: Path):
    with open_file(path) as src:
        return digest_stream(src)


def copy_file(src: Path, dst: Path):
    with open_file(src) as a, dst.open("xb") as b:
        return digest_stream(a, b)


def path_absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def promote(src: Path, dst: Path):
    if not path_absent(dst):
        raise Rejected("job_already_used", "The task output already exists.")
    # On Windows rename fails if dst is raced into existence; the service only
    # runs on Windows. The workspace lease and job lock cover normal concurrency.
    src.rename(dst)
