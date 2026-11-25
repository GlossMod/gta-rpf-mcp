from __future__ import annotations

from contextlib import ExitStack
import ctypes
import json
import os
from functools import lru_cache
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import zipfile

from .protocol import Rejected, SHA256, strict_json
from .windows_job import bounded_job
from .workspace import _windows, digest_stream, held_dir, held_tree, open_file, split_relative

PROJECT = 205014
RESOURCE = 356110
RELEASE = "1.4.0"
PACKAGE_NAME = f"{PROJECT}-{RESOURCE}.zip"
_FILES = 512
_RELEASE_SIZE = 64 * 1024**2
_EXPANDED_SIZE = 96 * 1024**2
_RESPONSE_SIZE = 16 * 1024**2
_TOOL_DIR = Path(__file__).parent
_KEYS = frozenset({"gtav_aes_key.dat", "gtav_hash_lut.dat", "gtav_ng_decrypt_tables.dat",
                   "gtav_ng_encrypt_luts.dat", "gtav_ng_encrypt_tables.dat", "gtav_ng_key.dat"})
_DLLS = frozenset({"rpf.dll", "ragelib.dll", "ragelib.gta5.dll"})
_ENTRY_NAMES = frozenset({"common/data/dlclist.xml", "common/data/gameconfig.xml", "setup2.xml", "data/pedmodelinfo.meta"})


def issue(code, text):
    return Rejected(code, text)


@lru_cache(maxsize=1)
def release_pins():
    pins = strict_json((_TOOL_DIR / "release-pins.json").read_bytes())
    if (pins["schemaVersion"], pins["projectId"], pins["resourceId"], pins["version"]) != (1, PROJECT, RESOURCE, RELEASE):
        raise RuntimeError("Mismatched embedded release provenance")
    return pins["pins"]


def package_directory(jobs: Path) -> Path:
    return jobs.parent / "gta-rpf-tools"


def _check_pin(pins, digest):
    for pin in pins:
        if (isinstance(pin, dict) and pin.keys() == {"sha256", "root", "evidence"}
                and isinstance(pin["sha256"], str) and SHA256.fullmatch(pin["sha256"])
                and isinstance(pin["root"], str) and isinstance(pin["evidence"], str) and pin["evidence"].strip()
                and digest == pin["sha256"]):
            return pin
    raise issue("unverified_tool", "The local 3DM 205014 ZIP does not match the release allowlist.")


def _get_pins():
    pins = release_pins()
    if not pins:
        raise issue("unverified_tool", "No independently verified SHA-256 for 3DM 205014 resource 356110 is embedded in this build.")
    return pins


def _status_code(jobs):
    if os.name != "nt":
        raise issue("unsupported_platform", "RPF preparation requires Windows PowerShell and .NET Framework 4.8.")
    pins = _get_pins()
    folder = package_directory(jobs)
    try:
        with held_tree(folder), open_file(folder / PACKAGE_NAME) as archive:
            if not 0 < os.fstat(archive.fileno()).st_size <= _RELEASE_SIZE:
                raise issue("unverified_tool", "Invalid 3DM release package size.")
            digest, _ = digest_stream(archive, expected=_RELEASE_SIZE)
            pin = _check_pin(pins, digest)
            archive.seek(0)
            with zipfile.ZipFile(archive) as z:
                _inspect_release(z, pin, template_required=True)
    except FileNotFoundError as exc:
        raise issue("missing_tool", "The prescribed local 3DM release ZIP is not installed.") from exc
    except (OSError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise issue("unverified_tool", "The prescribed local 3DM release ZIP could not be verified.") from exc
    _powershell()
    _framework()


def status(jobs) -> dict:
    out = {"projectId": PROJECT, "resourceId": RESOURCE, "releaseVersion": RELEASE,
           "legacy": False, "enhanced": False, "ready": False}
    try:
        _status_code(jobs)
    except Rejected as exc:
        return {**out, "code": exc.code, "message": str(exc)}
    return {**out, "legacy": True, "ready": True}


def _inspect_release(z, pin, template_required):
    """Called only after the entire ZIP matches a curated immutable digest."""
    root = tuple(split_relative(pin["root"], job_input=False)) if pin["root"] else ()
    entries = z.infolist()
    if len(entries) > _FILES or sum(i.file_size for i in entries) > _EXPANDED_SIZE:
        raise issue("unverified_tool", "The pinned release exceeds the package budget.")
    plan = []
    seen = {}
    empty_dirs = set()
    for item in entries:
        mode = item.external_attr >> 16
        if (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR} or item.flag_bits & 1
                or item.external_attr & 0x400):
            raise issue("unverified_tool", "The pinned release contains a link or unsupported ZIP entry.")
        parts = tuple(split_relative(item.filename.rstrip("/"), job_input=False))
        lowered = tuple(p.casefold() for p in parts)
        if lowered in seen:
            raise issue("unverified_tool", "The pinned release contains colliding package paths.")
        seen[lowered] = item.is_dir()
        if parts[:len(root)] != root:
            continue
        relative = parts[len(root):]
        if not relative:
            continue
        name = relative[-1].lower()
        if item.is_dir():
            if relative[0] == "gmm":
                empty_dirs.add(relative)
            continue
        if (len(relative) == 1 and name in _DLLS) or (name in _KEYS and (len(relative) == 1 or relative[:-1] == ("keys",))):
            plan.append((relative, item, "code"))
        elif relative[0] == "gmm":
            # Only known GTA data/metadata are staged from the pinned template;
            # launchers, executables, instructions, DLLs and config are not copied.
            if Path(name).suffix not in {".xml", ".meta", ".dat", ".ymt", ".yft", ".ytd", ".ydd", ".yld", ".rpf", ".rel", ".awc", ".ymap", ".ytyp", ".txt"} and name not in {".empty", ".gmmfolders", "gmmfolders"}:
                raise issue("missing_template", "The pinned GMM template contains an unsupported file type.")
            plan.append((relative, item, "template"))
    for path in seen:
        if any(seen.get(path[:i]) is False for i in range(1, len(path))):
            raise issue("unverified_tool", "The pinned release contains inconsistent directory entries.")
    code = {parts for parts, _, group in plan if group == "code"}
    if not _DLLS.issubset({parts[0].lower() for parts in code if len(parts) == 1}) or not _KEYS.issubset({parts[-1].lower() for parts in code}):
        raise issue("missing_tool", "The pinned release lacks rpf.dll or required GTA V Legacy key data.")
    if template_required:
        files = {parts for parts, _, group in plan if group == "template"}
        dirs = empty_dirs | {parts[:i] for parts in files for i in range(1, len(parts))}
        if not {("gmm", "setup2.xml"), ("gmm", "content.xml")}.issubset(files) or not {
            ("gmm", "x64", "vehicles.rpf"), ("gmm", "x64", "peds.rpf"), ("gmm", "data")
        }.issubset(dirs):
            raise issue("missing_template", "The pinned package lacks the fixed GMM DLC template structure.")
    return plan, empty_dirs


def _powershell() -> Path:
    k, _, _ = _windows()
    k.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    k.GetSystemDirectoryW.restype = ctypes.c_uint
    buf = ctypes.create_unicode_buffer(32768)
    length = k.GetSystemDirectoryW(buf, len(buf))
    if not 0 < length < len(buf):
        raise issue("missing_framework", "Windows PowerShell system directory is unavailable.")
    ps = Path(buf.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not ps.is_file():
        raise issue("missing_framework", "Windows PowerShell 5.1 is required.")
    return ps


def _framework():
    import winreg
    key = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as reg:
            release, kind = winreg.QueryValueEx(reg, "Release")
        if kind == winreg.REG_DWORD and release >= 528040:
            return
    except OSError:
        pass
    raise issue("missing_framework", ".NET Framework 4.8 is required.")


class WindowsBridge:
    def __init__(self, work: Path, jobs: Path, template_required: bool):
        if os.name != "nt":
            raise issue("unsupported_platform", "RPF preparation requires Windows PowerShell and .NET Framework 4.8.")
        pins = _get_pins()
        self.work = work
        package_dir = package_directory(jobs)
        self.exe = _powershell()
        _framework()
        self.temp = work / "temp"
        self.temp.mkdir()
        release = work / "release"
        release.mkdir()
        self.template = work / "template" / "gmm"
        opened = ExitStack()
        try:
            with held_tree(package_dir), open_file(package_dir / PACKAGE_NAME) as src:
                if not 0 < os.fstat(src.fileno()).st_size <= _RELEASE_SIZE:
                    raise issue("unverified_tool", "Invalid 3DM release package size.")
                pin = _check_pin(pins, digest_stream(src, expected=_RELEASE_SIZE)[0])
                src.seek(0)
                opened.enter_context(held_dir(release))
                with zipfile.ZipFile(src) as z:
                    plan, dirs = _inspect_release(z, pin, template_required)
                    if template_required:
                        for parts in sorted(dirs):
                            work.joinpath("template", *parts).mkdir(parents=True, exist_ok=True)
                    for parts, entry, group in plan:
                        if group == "template" and not template_required:
                            continue
                        dest = work.joinpath("release" if group == "code" else "template", *parts)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if group == "code" and len(parts) > 1:
                            opened.enter_context(held_dir(dest.parent))
                        with z.open(entry) as member, dest.open("xb") as out:
                            digest = digest_stream(member, out, entry.file_size)
                        if group == "code":
                            # Keep verified assembly bytes leased without share-write/delete.
                            # Legacy RageLib opens its key tables read/write, so verify them
                            # here but do not keep conflicting data-file handles around.
                            with ExitStack() as transient:
                                scope = opened if dest.suffix.lower() == ".dll" else transient
                                staged = scope.enter_context(open_file(dest))
                                if digest_stream(staged, expected=entry.file_size) != digest:
                                    raise issue("unverified_tool", "The staged 3DM runtime bytes have changed.")
        except FileNotFoundError as exc:
            opened.close()
            raise issue("missing_tool", "The prescribed local 3DM release ZIP is not installed.") from exc
        except (OSError, zipfile.BadZipFile, NotImplementedError) as exc:
            opened.close()
            raise issue("unverified_tool", "The pinned release could not be staged safely.") from exc
        except BaseException:
            opened.close()
            raise
        self._held = opened

    def close(self):
        self._held.close()

    def _relative(self, path):
        return path.relative_to(self.work).as_posix()

    def _run(self, op, **fields):
        system = self.exe.parents[2]
        env = {"SystemRoot": str(system.parent), "WINDIR": str(system.parent), "PATH": str(system),
               "PSModulePath": str(self.exe.parent / "Modules"), "TEMP": str(self.temp), "TMP": str(self.temp)}
        data = json.dumps({"operation": op, **fields}, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        with tempfile.TemporaryFile(dir=self.temp) as stdout:
            try:
                command = [str(self.exe), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(_TOOL_DIR / "bridge.ps1"), "-Workspace", str(self.work)]
                # The script blocks on its first input line until after the
                # process is assigned; no vendor assembly has run before then.
                with subprocess.Popen(command, cwd=self.work, env=env, stdin=subprocess.PIPE, stdout=stdout,
                                      stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW) as proc:
                    try:
                        with bounded_job() as assign:
                            assign(proc.pid)
                            proc.communicate(data, timeout=180)
                    except (OSError, subprocess.TimeoutExpired):
                        proc.kill()
                        proc.wait()
                        raise
            except subprocess.TimeoutExpired as exc:
                raise issue("bridge_timeout", "The RPF bridge exceeded its execution time limit.") from exc
            except OSError as exc:
                raise issue("bridge_failed", "The verified RPF bridge could not start.") from exc
            if proc.returncode != 0 or stdout.tell() > _RESPONSE_SIZE:
                raise issue("bridge_failed", "The verified RPF bridge rejected the requested operation.")
            try:
                stdout.seek(0)
                answer = strict_json(stdout.read(_RESPONSE_SIZE + 1))
                if not isinstance(answer, dict) or answer.keys() != {"value"}:
                    raise ValueError("invalid envelope")
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise issue("bridge_failed", "The verified RPF bridge returned malformed data.") from exc
            return answer["value"]

    def read(self, archive, entry):
        if entry not in _ENTRY_NAMES:
            raise ValueError("Unexpected RPF entry")
        value = self._run("read", archive=self._relative(archive), entry=entry)
        if not isinstance(value, str) or not value:
            raise issue("verification_failed", "The RPF archive is missing required XML or metadata.")
        return value

    def files(self, archive):
        names = self._run("files", archive=self._relative(archive))
        if not isinstance(names, list) or len(names) > 100_000 or any(not isinstance(s, str) or len(s) > 512 for s in names):
            raise issue("verification_failed", "The RPF archive returned an invalid entry list.")
        return names

    def write(self, archive, entry, source):
        if entry not in _ENTRY_NAMES:
            raise ValueError("Unexpected RPF entry")
        if self._run("write", archive=self._relative(archive), entry=entry, source=self._relative(source)) != "00":
            raise issue("bridge_failed", "The verified RPF bridge did not confirm an RPF write.")

    def create_gmm(self):
        self._run("create_gmm")
