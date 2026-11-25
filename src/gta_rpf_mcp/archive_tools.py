"""Independent RPF7 operations on inputs and staged copies in an AMM job."""
from __future__ import annotations

import base64
import codecs
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from . import rpf7
from .protocol import JOB_ID, SHA256, Rejected, invalid, parse_job, record, standalone_inputs, token
from .rpf_crypto import RpfCryptoError
from .workspace import (
    held_dir, held_tree, load_input, metadata, promote, read_limited,
    snapshot_file, split_relative, workspace_parent,
)

_ARTIFACTS = ".work-rpf-artifacts"  # Reserved by split_relative for service-owned paths.
_MAX_ARCHIVE_BYTES = 0xFFFFFFFF
_MAX_ENTRY_BYTES = 128 * 1024**2
_MAX_BUILD_BYTES = 512 * 1024**2
_MAX_BUILD_ENTRIES = 256
_MAX_LIST_PAGE = 500
_MAX_LIST_PAGE_JSON_BYTES = 128 * 1024
_MAX_READ_PAGE_BYTES = 128 * 1024
_MAX_ENTRY_PATH = 256 * 64  # RPF7 name and path depth bounds.

_JOB = {"type": "string", "pattern": "^[0-9a-f]{32}$", "minLength": 32, "maxLength": 32,
        "description": "AMM job ID identifying the workspace and its input.json."}
_NAME = {"type": "string", "minLength": 5, "maxLength": 180, "pattern": r"^[^/\\]+\.[Rr][Pp][Ff]$",
         "description": "Original Windows RPF basename, including .rpf. The NG cipher uses this name."}
_INPUT = {"type": "string", "minLength": 1, "maxLength": 512,
          "description": "Exact path from gta_rpf_describe_job inputs[].path."}
_ENTRY = {"type": "string", "minLength": 1, "maxLength": _MAX_ENTRY_PATH,
          "description": "RPF entry path, for example common/data/gameconfig.xml or x64/models.rpf/car.yft."}
_OFFSET = {"type": "integer", "minimum": 0, "maximum": _MAX_ARCHIVE_BYTES, "default": 0}


def _object(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_ARCHIVE = {
    **_object({
        "path": {**_INPUT, "description": "Exact path from the job's inputs or a returned archive reference."},
        "name": _NAME,
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$", "minLength": 64, "maxLength": 64},
        "size": {"type": "integer", "minimum": 16, "maximum": _MAX_ARCHIVE_BYTES},
    }, ["path", "name"]),
    "dependentRequired": {"sha256": ["size"], "size": ["sha256"]},
    "description": "Pass gta_rpf_describe_job inputs[].archive or result.archive from an archive mutation as returned.",
    "examples": [{"path": "inputs/4a8e1f0c", "name": "update.rpf"}],
}


def _tool(name: str, description: str, properties: dict, required: list[str], *, reads: bool) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": _object({"jobId": _JOB, **properties}, ["jobId", *required]),
        "annotations": {"readOnlyHint": reads, "destructiveHint": False, "idempotentHint": reads, "openWorldHint": False},
    }


ARCHIVE_TOOLS = [
    _tool("gta_rpf_list_entries", "List file paths and nested archives in an RPF7 archive staged in the AMM job.", {
        "archive": _ARCHIVE, "offset": _OFFSET,
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIST_PAGE, "default": 100},
    }, ["archive"], reads=True),
    _tool("gta_rpf_read_entry", "Read an RPF7 entry as base64 or UTF-8 and return its SHA-256. Entry limit is 128 MiB.", {
        "archive": _ARCHIVE, "entry": _ENTRY, "offset": _OFFSET,
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_READ_PAGE_BYTES, "default": 65536,
                  "description": "Max bytes per page. UTF-8 requires a limit of at least 4 bytes."},
        "encoding": {"type": "string", "enum": ["base64", "utf8"], "default": "base64",
                     "description": "UTF-8 pages end on character boundaries. Offsets and sizes are always bytes."},
    }, ["archive", "entry"], reads=True),
    _tool("gta_rpf_write_entry", "Replace an existing binary or supported RPF7 resource file with a job input and return a verified staged copy.", {
        "archive": _ARCHIVE, "entry": _ENTRY, "sourcePath": _INPUT,
    }, ["archive", "entry", "sourcePath"], reads=False),
    _tool("gta_rpf_add_entry", "Add a new file from a job input to the root or an existing nested RPF7 archive. Create ordinary parent directories if needed and return a verified staged copy.", {
        "archive": _ARCHIVE, "entry": _ENTRY, "sourcePath": _INPUT,
    }, ["archive", "entry", "sourcePath"], reads=False),
    _tool("gta_rpf_delete_entry", "Remove an existing file from the root or a nested RPF7 archive and return a verified staged copy. Does not delete directories or nested archive containers.", {
        "archive": _ARCHIVE, "entry": _ENTRY,
    }, ["archive", "entry"], reads=False),
    _tool("gta_rpf_create_archive", "Build an OPEN RPF7 archive from verified job inputs. Directories ending in .rpf become nested archives.", {
        "archiveName": _NAME,
        "entries": {"type": "array", "minItems": 1, "maxItems": _MAX_BUILD_ENTRIES,
                    "items": _object({"entry": {**_ENTRY, "maxLength": 512}, "sourcePath": _INPUT}, ["entry", "sourcePath"]),
                    "examples": [[{"entry": "x64/models.rpf/example.xml", "sourcePath": "inputs/8f0a2bd1"}]]},
    }, ["archiveName", "entries"], reads=False),
]
_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in ARCHIVE_TOOLS}


def _integer(value, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise invalid(f"Invalid {label}.")
    return value


def _archive_name(value) -> str:
    parts = split_relative(value)
    if len(parts) != 1 or len(value) < 5 or not value.lower().endswith(".rpf"):
        raise invalid("Archive name must be a Windows RPF basename ending in .rpf.")
    return value


def _input_reference(value, refs: dict) -> dict:
    path = "/".join(split_relative(value))
    if path not in refs:
        raise invalid("Path does not refer to a declared job input.")
    return refs[path]


def _archive_reference(value, refs: dict) -> tuple[str, dict, list[str]]:
    if not isinstance(value, dict) or value.keys() not in ({"path", "name"}, {"path", "name", "size", "sha256"}):
        raise invalid("Invalid archive reference fields.")
    name = _archive_name(value["name"])
    parts = split_relative(value["path"], job_input=False)
    expected = None
    if "size" in value:
        expected = {"size": _integer(value["size"], 16, _MAX_ARCHIVE_BYTES, "archive byte size"),
                    "sha256": token(value["sha256"], SHA256, "archive SHA-256")}
    if parts[0] == _ARTIFACTS:
        if len(parts) != 3 or parts[-1] != name or expected is None:
            raise invalid("Pass the complete staged archive reference returned by write or create.")
        token(parts[1], JOB_ID, "staged archive ID")
        return name, {"path": name, **expected}, parts[:-1]
    ref = _input_reference(value["path"], refs)
    if "role" in ref and (ref["role"] != "archive" or ref["name"] != name):
        raise invalid("Use the archive reference issued for this job.")
    _integer(ref["size"], 16, _MAX_ARCHIVE_BYTES, "archive byte size")
    if expected is not None and any(ref[key] != val for key, val in expected.items()):
        raise invalid("Archive metadata does not match its job input reference.")
    return name, ref, []


def _entry_path(value) -> str:
    try:
        rpf7._path_keys(value)  # Validate with exactly the syntax the archive reader supports.
    except rpf7.RpfError as exc:
        raise invalid("Invalid RPF7 entry path.") from exc
    return value.replace("\\", "/")


def _snapshot_archive(job: Path, archive: tuple[str, dict, list[str]], staging: Path) -> Path:
    name, ref, parents = archive
    dest = staging / name
    with ExitStack() as stack:
        base = job
        for part in parents:
            base /= part
            stack.enter_context(held_dir(base))
        snapshot_file(base, ref, dest)
    return dest


def _entry_source(value, refs: dict) -> dict:
    ref = _input_reference(value, refs)
    if "role" in ref and ref["role"] != "payload":
        raise invalid("Use a declared payload as the entry source.")
    if ref["size"] > _MAX_ENTRY_BYTES:
        raise invalid("Entry source exceeds 128 MiB.")
    return ref


def _creation_inputs(entries, refs: dict) -> tuple[dict[str, list[str]], dict[str, dict]]:
    if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_BUILD_ENTRIES:
        raise invalid("Invalid archive creation entry count.")
    directories = {}
    files = {}
    file_keys = set()
    for item in entries:
        item = record(item, {"entry", "sourcePath"}, "archive creation entry")
        parts = split_relative(_entry_path(item["entry"]), job_input=False)
        entry = "/".join(parts)
        key = entry.casefold()
        if key in file_keys or key in directories:
            raise invalid("Duplicate archive entry or conflicting file and directory paths.")
        for i in range(1, len(parts)):
            parents = parts[:i]
            parent = "/".join(parents).casefold()
            if parent in file_keys or (parent in directories and directories[parent] != parents):
                raise invalid("Conflicting archive directory paths or casing.")
            directories[parent] = parents
        file_keys.add(key)
        files[entry] = _entry_source(item["sourcePath"], refs)
    if sum(ref["size"] for ref in files.values()) > _MAX_BUILD_BYTES:
        raise invalid("Archive creation inputs exceed 512 MiB.")
    return directories, files


def _build_archive(job: Path, work: Path, archive: Path, plan: tuple[dict[str, list[str]], dict[str, dict]]) -> None:
    directories, files = plan
    source = work / "source"
    source.mkdir()
    with ExitStack() as stack:
        stack.enter_context(held_dir(source))
        for parts in directories.values():
            directory = source.joinpath(*parts)
            directory.mkdir()
            stack.enter_context(held_dir(directory))
        for entry, ref in files.items():
            snapshot_file(job, ref, source.joinpath(*entry.split("/")))
        rpf7.create_archive(source, archive)
    _verify(archive, {entry: (ref["sha256"], ref["size"]) for entry, ref in files.items()})


def _verify(archive: Path, entries: dict[str, tuple[str, int]]) -> None:
    try:
        rpf7.verify_assets(archive, entries)
    except (rpf7.RpfError, RpfCryptoError) as exc:
        raise Rejected("verification_failed", "Reopened archive did not match the staged input data.") from exc


def _publish(job: Path, folder: Path, archive: Path) -> dict:
    sha256, size = metadata(archive)
    if not 16 <= size <= _MAX_ARCHIVE_BYTES:
        raise Rejected("verification_failed", "Verified archive has an invalid byte size.")
    artifacts = job / _ARTIFACTS
    artifacts.mkdir(exist_ok=True)
    with held_dir(artifacts):
        identifier = uuid4().hex
        promote(folder, artifacts / identifier)
    return {"path": f"{_ARTIFACTS}/{identifier}/{archive.name}", "name": archive.name,
            "sha256": sha256, "size": size}


def _list_page(archive: Path, offset: int, limit: int) -> dict:
    entries = rpf7.list_entries(archive)
    page = []
    size = 0
    for entry in entries[offset:offset + limit]:
        size += len(json.dumps(entry, ensure_ascii=True)) + 1
        if size > _MAX_LIST_PAGE_JSON_BYTES:
            if not page:
                raise Rejected("entry_too_large", "An archive path exceeds the list response size limit.")
            break
        page.append(entry)
    end = offset + len(page)
    return {"entries": page, "total": len(entries), "offset": offset,
            "nextOffset": end if end < len(entries) else None}


def _read_page(archive: Path, entry: str, offset: int, limit: int, encoding: str) -> dict:
    data = rpf7.read_entry(archive, entry, max_bytes=_MAX_ENTRY_BYTES)
    page = data[offset:offset + limit]
    end = offset + len(page)
    if encoding == "utf8":
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        try:
            content = decoder.decode(page, final=end >= len(data))
        except UnicodeDecodeError as exc:
            raise Rejected("invalid_entry_encoding", "The entry page is not valid UTF-8 at the given byte offset.") from exc
        end -= len(decoder.getstate()[0])
    else:
        content = base64.b64encode(page).decode("ascii")
    return {"entry": entry, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "offset": offset, "nextOffset": end if end < len(data) else None,
            "encoding": encoding, "data": content}


def _execute(job: Path, name: str, args: dict, refs: dict) -> dict:
    if name == "gta_rpf_create_archive":
        archive_name = _archive_name(args["archiveName"])
        plan = _creation_inputs(args["entries"], refs)
    else:
        archive_ref = _archive_reference(args["archive"], refs)
        if name != "gta_rpf_list_entries":
            entry = _entry_path(args["entry"])
        if name in {"gta_rpf_write_entry", "gta_rpf_add_entry"}:
            source = _entry_source(args["sourcePath"], refs)
        elif name in {"gta_rpf_list_entries", "gta_rpf_read_entry"}:
            offset = _integer(args.get("offset", 0), 0, _MAX_ARCHIVE_BYTES, "page offset")
            limit_schema = _SCHEMAS[name]["properties"]["limit"]
            limit = _integer(args.get("limit", limit_schema["default"]), 1, limit_schema["maximum"], "page limit")
            encoding = args.get("encoding", "base64")
            if encoding not in ("base64", "utf8") or (encoding == "utf8" and limit < 4):
                raise invalid("Invalid read encoding or UTF-8 page limit.")

    with tempfile.TemporaryDirectory(prefix=".work-rpf-", dir=job) as temp:
        work = Path(temp)
        with held_dir(work):
            output = work / "archive"
            output.mkdir()
            with held_dir(output):
                if name == "gta_rpf_create_archive":
                    archive = output / archive_name
                    _build_archive(job, work, archive, plan)
                else:
                    archive = _snapshot_archive(job, archive_ref, output)
                    if name == "gta_rpf_list_entries":
                        return _list_page(archive, offset, limit)
                    if name == "gta_rpf_read_entry":
                        return _read_page(archive, entry, offset, limit, encoding)
                    if name == "gta_rpf_delete_entry":
                        rpf7.delete_entry(archive, entry)
                        try:
                            rpf7.verify_entry_absent(archive, entry)
                        except (rpf7.RpfError, RpfCryptoError) as exc:
                            raise Rejected("verification_failed", "Reopened archive did not confirm the entry deletion.") from exc
                    else:
                        payload = work / "payload"
                        snapshot_file(job, source, payload)
                        edit = rpf7.add_entry if name == "gta_rpf_add_entry" else rpf7.replace_entry
                        edit(archive, entry, read_limited(payload, _MAX_ENTRY_BYTES, allow_empty=True))
                        _verify(archive, {entry: (source["sha256"], source["size"])})
            result = {"archive": _publish(job, output, archive)}
            if name != "gta_rpf_create_archive":
                result["entry"] = entry
            return result


def call_archive_tool(name: str, args: dict) -> dict:
    """Run one tool for an existing AMM job, returning a JSON object or raising Rejected."""
    if not isinstance(name, str) or name not in _SCHEMAS or not isinstance(args, dict):
        raise invalid("Unknown archive tool or invalid arguments.")
    schema = _SCHEMAS[name]
    if not set(schema["required"]) <= args.keys() <= schema["properties"].keys():
        raise invalid("Invalid archive tool argument fields.")
    job_id = token(args["jobId"], JOB_ID, "job ID")
    try:
        job = workspace_parent() / job_id
        with ExitStack() as stack:
            try:
                stack.enter_context(held_tree(job))
            except FileNotFoundError as exc:
                raise Rejected("missing_workspace", "The fixed task workspace does not exist.") from exc
            manifest = load_input(job)
            refs = standalone_inputs(manifest, job_id, split_relative)
            if refs is None:
                _, refs, _ = parse_job(manifest, job_id, split_relative)
            result = _execute(job, name, args, {ref["path"]: ref for ref in refs})
            return {"schemaVersion": 1, "jobId": job_id, "status": "ok", **result}
    except rpf7.EntryExistsError as exc:
        raise Rejected("entry_exists", "An RPF7 entry already occupies the requested path or its parent.") from exc
    except rpf7.EntryNotFoundError as exc:
        raise Rejected("entry_not_found", "Requested RPF7 entry was not found.") from exc
    except (rpf7.RpfError, RpfCryptoError) as exc:
        raise Rejected("unsupported_rpf", "The archive format, entry type, or size is unsupported for this operation.") from exc
    except (OSError, UnicodeError, RecursionError) as exc:
        raise Rejected("workspace_io", "Could not securely read or stage an archive job file.") from exc
