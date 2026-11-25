"""Bounded RPF7 archive I/O, using the PC RageLib entry and resource layouts."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from io import BytesIO
import os
from pathlib import Path
import stat
from struct import Struct
import tempfile
from typing import BinaryIO
import zlib

from . import rpf_crypto


_HEADER = Struct("<4I")
_MAGIC = 0x52504637
_DIRECTORY = 0x7FFFFF00
_BLOCK = 512
_RESOURCE = 0x800000
_MAX_SECTOR = _RESOURCE - 1
_SIZE24 = 0xFFFFFF
_MAX_ARCHIVE = 0xFFFFFFFF
_RSC7 = 0x37435352
_RSC7_RAGELIB = 0x07435352
_REQUIRED_RSC7 = {"ydd", "ydr", "yft", "yld", "ymt", "ytd"}
_ENHANCED_VERSIONS = {"ydd": {159}, "ydr": {159}, "yft": {171}, "ytd": {5}}
_CHUNK = 1024 * 1024
_MAX_ENTRY_BYTES = 128 * 1024 * 1024
_MAX_CIPHER_BYTES = 16 * 1024 * 1024
_MAX_TOC_ENTRIES = 250_000
_MAX_NAME_TABLE = 16 * 1024 * 1024
_MAX_PATH_STORAGE = 32 * 1024 * 1024
_MAX_PACKED_NESTED = 512 * 1024 * 1024
_MAX_RESOURCE_EXPANSION = 1024 * 1024 * 1024
_MAX_NAME = 255
_MAX_DEPTH = 64
_MAX_NESTED = 16
_INVALID_NAME = frozenset('/\\<>:"|?*')


class RpfError(ValueError):
    """Malformed or unsupported RPF data, or an unsupported archive operation."""


class EntryNotFoundError(RpfError):
    """The requested file does not exist in the archive."""


class EntryExistsError(RpfError):
    """The requested file path is already occupied."""


def _align(value: int, unit: int = _BLOCK) -> int:
    return (value + unit - 1) // unit * unit


def _name_key(name: str) -> str:
    try:
        size = len(name.encode("utf-8"))
    except UnicodeError as exc:
        raise RpfError("Invalid UTF-8 RPF7 entry name.") from exc
    if (not name or name in {".", ".."} or name[-1] in ". " or size > _MAX_NAME
            or any(ch in _INVALID_NAME or ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        raise RpfError("Invalid RPF7 entry name.")
    return name.casefold()


def _path_parts(path: str) -> list[str]:
    if not isinstance(path, str) or len(path) > (_MAX_NAME + 1) * _MAX_DEPTH:
        raise RpfError("RPF7 entry path exceeds its size limit.")
    parts = path.replace("\\", "/").split("/")
    if len(parts) > _MAX_DEPTH:
        raise RpfError("RPF7 entry path exceeds its depth limit.")
    for part in parts:
        _name_key(part)
    return parts


def _path_keys(path: str) -> list[str]:
    return [part.casefold() for part in _path_parts(path)]


@dataclass(frozen=True, slots=True)
class _View:
    stream: BinaryIO
    offset: int
    size: int
    name: str

    def part(self, offset: int, size: int, name: str = "") -> _View:
        if offset < 0 or size < 0 or offset + size > self.size:
            raise RpfError("RPF7 file data extends past its archive.")
        return _View(self.stream, self.offset + offset, size, name)

    def read(self, offset: int, size: int) -> bytes:
        part = self.part(offset, size)
        self.stream.seek(part.offset)
        data = self.stream.read(size)
        if len(data) != size:
            raise RpfError("Truncated RPF7 data.")
        return data

    def chunks(self) -> Iterator[bytes]:
        for offset in range(0, self.size, _CHUNK):
            yield self.read(offset, min(self.size - offset, _CHUNK))


@dataclass(frozen=True, slots=True)
class _File:
    name: str
    index: int
    offset: int
    size: int
    unpacked: int
    compressed: bool
    encrypted: bool
    resource: bool
    flags: tuple[int, int]


@dataclass(slots=True)
class _Archive:
    view: _View
    mode: int
    table: bytes
    names: bytes
    files: dict[str, tuple[str, _File]]
    directories: dict[str, int]

    @property
    def toc_end(self) -> int:
        return _HEADER.size + len(self.table) + len(self.names)


@dataclass(slots=True)
class _Budget:
    count: int = 0
    path_storage: int = 0
    packed_nested: int = 0

    def add(self, count: int) -> None:
        self.count += count
        if self.count > _MAX_TOC_ENTRIES:
            raise RpfError("RPF7 entry count exceeds its limit.")

    def add_path(self, text: str) -> None:
        self.path_storage += len(text.encode("utf-8"))
        if self.path_storage > _MAX_PATH_STORAGE:
            raise RpfError("RPF7 paths exceed their storage limit.")

    def add_packed_archive(self, entry: _File) -> None:
        if entry.compressed:
            self.packed_nested += entry.unpacked
            if self.packed_nested > _MAX_PACKED_NESTED:
                raise RpfError("Expanded nested RPF7 archives exceed their size limit.")


def _entry_name(names: bytes, offset: int) -> str:
    end = names.find(b"\0", offset, offset + _MAX_NAME + 1)
    if end < 0:
        raise RpfError("Invalid RPF7 name table reference.")
    try:
        return names[offset:end].decode("utf-8")
    except UnicodeError as exc:
        raise RpfError("Invalid UTF-8 RPF7 entry name.") from exc


def _large_resource_size(header: bytes) -> int:
    return int.from_bytes(bytes(header[index] for index in (7, 14, 5, 2)), "little")


def _parse(view: _View, budget: _Budget) -> _Archive:
    if view.size > _MAX_ARCHIVE:
        raise RpfError("RPF7 archive exceeds its size limit.")
    magic, count, name_size, mode = _HEADER.unpack(view.read(0, _HEADER.size))
    if magic != _MAGIC:
        raise RpfError("Unsupported archive format. Expected PC RPF7.")
    if not count or not 0 < name_size <= _MAX_NAME_TABLE:
        raise RpfError("Invalid RPF7 table dimensions.")
    budget.add(count)
    names_start = _HEADER.size * (count + 1)
    toc_end = names_start + name_size
    view.part(0, toc_end)
    table, names = rpf_crypto.decrypt_toc(
        mode, view.name, view.size, view.read(_HEADER.size, count * _HEADER.size),
        view.read(names_start, name_size),
    )

    name_offset, kind, first, length = _HEADER.unpack_from(table)
    if kind != _DIRECTORY or _entry_name(names, name_offset):
        raise RpfError("Invalid RPF7 root directory.")
    pending = [("", "", first, length, 0)]
    seen = {0}
    files = {}
    directories = {"": 0}
    extents = []
    while pending:
        prefix, key_prefix, first, length, depth = pending.pop()
        if first > count or length > count - first or (length and depth >= _MAX_DEPTH):
            raise RpfError("Invalid RPF7 directory range or depth.")
        siblings = set()
        for index in range(first, first + length):
            if index in seen:
                raise RpfError("RPF7 directory entries have overlapping ranges or a cycle.")
            seen.add(index)
            a, b, c, d = _HEADER.unpack_from(table, index * _HEADER.size)
            directory = b == _DIRECTORY
            name = _entry_name(names, a if directory else a & 0xFFFF)
            key = _name_key(name)
            if key in siblings:
                raise RpfError("RPF7 directory contains duplicate names.")
            siblings.add(key)
            path = prefix + name
            path_key = key_prefix + key
            budget.add_path(path)
            budget.add_path(path_key)
            if directory:
                directories[path_key] = index
                pending.append((path + "/", path_key + "/", c, d, depth + 1))
                continue

            resource = bool(b >> 31)
            if not resource and d not in {0, 1}:
                raise RpfError("Invalid RPF7 file encryption flag.")
            size_field = (a >> 16) | ((b & 0xFF) << 16)
            offset = ((b >> 8) & _MAX_SECTOR) * _BLOCK
            size = size_field if resource or size_field else c
            if size and offset < toc_end:
                raise RpfError("RPF7 file data overlaps its directory tables.")
            if resource and size_field == _SIZE24:
                size = _large_resource_size(view.read(offset, _HEADER.size))
                if size < _SIZE24:
                    raise RpfError("Invalid RPF7 large resource size.")
            if resource and size < _HEADER.size:
                raise RpfError("Truncated RPF7 resource header.")
            view.part(offset, size)
            if size:
                extents.append((offset, offset + size))
            entry = _File(name, index, offset, size, size if resource else c,
                          not resource and bool(size_field), not resource and bool(d),
                          resource, (c, d) if resource else (0, 0))
            files[path_key] = (path, entry)

    if len(seen) != count:
        raise RpfError("RPF7 directory contains unreachable records.")
    end = toc_end
    for offset, next_end in sorted(extents):
        if offset < end:
            raise RpfError("RPF7 files have overlapping data ranges.")
        end = next_end
    return _Archive(view, mode, table, names, files, directories)


def _payload_chunks(archive: _Archive, entry: _File) -> Iterator[bytes]:
    view = archive.view.part(entry.offset, entry.size, entry.name)
    if entry.encrypted:
        if view.size > _MAX_CIPHER_BYTES:
            raise RpfError("Encrypted RPF7 file exceeds its size limit.")
        yield rpf_crypto.decrypt_entry(archive.mode, entry.name, entry.unpacked, view.read(0, view.size))
    else:
        yield from view.chunks()


def _inflate(chunks: Iterable[bytes], capacity: int, write: Callable[[bytes], object],
             *, exact: bool = True) -> None:
    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    remaining = capacity
    try:
        for chunk in chunks:
            while chunk:
                output = inflater.decompress(chunk, min(remaining + 1, _CHUNK))
                remaining -= len(output)
                if remaining < 0 or inflater.unused_data:
                    raise RpfError("RPF7 compressed entry does not match its declared length.")
                write(output)
                chunk = inflater.unconsumed_tail
    except zlib.error as exc:
        raise RpfError("Invalid RPF7 raw DEFLATE data.") from exc
    if not inflater.eof or (exact and remaining):
        raise RpfError("RPF7 compressed entry does not match its declared length.")


def _extract_binary(archive: _Archive, entry: _File, write: Callable[[bytes], object]) -> None:
    chunks = _payload_chunks(archive, entry)
    if entry.compressed:
        _inflate(chunks, entry.unpacked, write)
    else:
        for chunk in chunks:
            write(chunk)


def _resource_version(system: int, graphics: int) -> int:
    return ((system >> 28) << 4) | (graphics >> 28)


def _check_resource_version(name: str, version: int) -> None:
    suffix = name.casefold().rpartition(".")[2]
    if version in _ENHANCED_VERSIONS.get(suffix, ()):
        raise RpfError("Unsupported GTA V Enhanced resource version.")


def _resource_chunks(archive: _Archive, entry: _File) -> Iterator[bytes]:
    resource = archive.view.part(entry.offset, entry.size)
    raw_header = resource.read(0, _HEADER.size)
    magic, _, _, _ = _HEADER.unpack(raw_header)
    system, graphics = entry.flags
    version = _resource_version(system, graphics)
    _check_resource_version(entry.name, version)
    if entry.size >= _SIZE24:
        # The extended size overwrites byte 2 of the magic. Byte 3 survives.
        magic = _RSC7_RAGELIB if raw_header[:2] == b"RS" and raw_header[3] == 7 else _RSC7
    elif magic not in {_RSC7, _RSC7_RAGELIB}:
        if raw_header.startswith(b"RSC"):
            raise RpfError("Unsupported RPF7 resource signature.")
        magic = _RSC7
    yield _HEADER.pack(magic, version, system, graphics)
    yield from resource.part(_HEADER.size, entry.size - _HEADER.size).chunks()


def _nested(path: str, entry: _File) -> bool:
    return path.casefold().endswith(".rpf") and not entry.resource


@contextmanager
def _open_nested(archive: _Archive, entry: _File, budget: _Budget) -> Iterator[_View]:
    if not entry.compressed and not entry.encrypted:
        yield archive.view.part(entry.offset, entry.size, entry.name)
        return
    budget.add_packed_archive(entry)
    with tempfile.TemporaryFile() as stream:
        _extract_binary(archive, entry, stream.write)
        yield _View(stream, 0, entry.unpacked, entry.name)


def _nested_prefix(archive: _Archive, parts: list[str]) -> tuple[_File, int] | None:
    for index in range(1, len(parts)):
        key = "/".join(parts[:index])
        match = archive.files.get(key)
        if match is not None and _nested(*match):
            return match[1], index
    return None


def _locate(archive: _Archive, parts: list[str]) -> tuple[_File, list[str]]:
    nested = _nested_prefix(archive, parts)
    if nested is not None:
        entry, stop = nested
        return entry, parts[stop:]
    match = archive.files.get("/".join(parts))
    if match is None:
        raise EntryNotFoundError("Requested RPF7 entry was not found.")
    return match[1], []


def _list(view: _View, prefix: str, budget: _Budget, depth: int, result: list[str]) -> None:
    if depth > _MAX_NESTED:
        raise RpfError("Nested RPF7 depth exceeds its limit.")
    archive = _parse(view, budget)
    for path, entry in sorted(archive.files.values(), key=lambda item: item[0].casefold()):
        result.append(prefix + path)
        budget.add_path(result[-1])
        if _nested(path, entry):
            with _open_nested(archive, entry, budget) as nested:
                _list(nested, prefix + path + "/", budget, depth + 1, result)


def list_entries(archive: Path) -> list[str]:
    """Return paths to files and nested archives, using forward slashes."""
    with archive.open("rb") as stream:
        result: list[str] = []
        _list(_View(stream, 0, os.fstat(stream.fileno()).st_size, archive.name), "", _Budget(), 0, result)
        return result


def _read(view: _View, parts: list[str], limit: int, budget: _Budget, depth: int) -> bytes:
    if depth > _MAX_NESTED:
        raise RpfError("Nested RPF7 depth exceeds its limit.")
    archive = _parse(view, budget)
    entry, rest = _locate(archive, parts)
    if rest:
        with _open_nested(archive, entry, budget) as nested:
            return _read(nested, rest, limit, budget, depth + 1)
    if entry.unpacked > limit:
        raise RpfError("Requested RPF7 entry exceeds its read size limit.")
    if entry.resource:
        return b"".join(_resource_chunks(archive, entry))
    if not (entry.encrypted or entry.compressed):
        return view.read(entry.offset, entry.size)
    with BytesIO() as data:
        _extract_binary(archive, entry, data.write)
        return data.getvalue()


def read_entry(archive: Path, entry: str, *, max_bytes: int = _MAX_ENTRY_BYTES) -> bytes:
    """Read a binary file or a resource with its RSC7 header rebuilt from the TOC."""
    if type(max_bytes) is not int or not 0 <= max_bytes <= _MAX_ENTRY_BYTES:
        raise RpfError("Invalid RPF7 read size limit.")
    parts = _path_keys(entry)
    with archive.open("rb") as stream:
        return _read(_View(stream, 0, os.fstat(stream.fileno()).st_size, archive.name),
                     parts, max_bytes, _Budget(), 0)


@dataclass(frozen=True, slots=True)
class _Check:
    parts: list[str]
    sha256: str
    size: int


def _verify(view: _View, checks: list[_Check], budget: _Budget, depth: int) -> None:
    if depth > _MAX_NESTED:
        raise RpfError("Nested RPF7 depth exceeds its limit.")
    archive = _parse(view, budget)
    nested_checks: dict[_File, list[_Check]] = {}
    for check in checks:
        entry, rest = _locate(archive, check.parts)
        if rest:
            nested_checks.setdefault(entry, []).append(_Check(rest, check.sha256, check.size))
            continue
        if entry.unpacked != check.size:
            raise RpfError("RPF7 asset readback has an unexpected size.")
        digest = hashlib.sha256()
        if entry.resource:
            for chunk in _resource_chunks(archive, entry):
                digest.update(chunk)
        else:
            _extract_binary(archive, entry, digest.update)
        if digest.hexdigest() != check.sha256:
            raise RpfError("RPF7 asset readback has an unexpected SHA-256 digest.")

    for entry, children in nested_checks.items():
        with _open_nested(archive, entry, budget) as nested:
            _verify(nested, children, budget, depth + 1)


def verify_assets(archive: Path, expected: dict[str, tuple[str, int]]) -> None:
    """Verify exported SHA-256 and byte size of entries, streaming each payload."""
    if len(expected) > _MAX_TOC_ENTRIES:
        raise RpfError("RPF7 asset count exceeds its limit.")
    checks = []
    paths = set()
    budget = _Budget()
    for name, value in expected.items():
        if not isinstance(value, tuple) or len(value) != 2:
            raise RpfError("Invalid RPF7 asset verification request.")
        digest, size = value
        parts = _path_keys(name)
        key = "/".join(parts)
        budget.add_path(key)
        if (key in paths or not isinstance(digest, str) or len(digest) != 64
                or any(ch not in "0123456789abcdefABCDEF" for ch in digest)
                or type(size) is not int or not 0 <= size <= _MAX_ARCHIVE):
            raise RpfError("Invalid RPF7 asset verification request.")
        paths.add(key)
        checks.append(_Check(parts, digest.lower(), size))
    with archive.open("rb") as stream:
        _verify(_View(stream, 0, os.fstat(stream.fileno()).st_size, archive.name), checks, budget, 0)


def _choose_offset(archive: _Archive, entry: _File, size: int) -> int:
    extents = sorted((other.offset, other.offset + other.size)
                     for _, other in archive.files.values() if other != entry and other.size)
    cursor = _align(archive.toc_end)
    gap = None
    for start, end in extents + [(archive.view.size, archive.view.size)]:
        if cursor <= entry.offset < start and entry.offset + size <= start:
            return entry.offset
        if gap is None and cursor + size <= start:
            gap = cursor
        cursor = _align(end)
    if gap is not None:
        return gap
    if entry.size and all(start < entry.offset for start, _ in extents):
        return entry.offset
    return _align(archive.view.size)


def _binary_record(name_offset: int, offset: int, stored: int, size: int, encrypted: bool) -> bytes:
    return _file_record(name_offset, offset, stored, size, int(encrypted))


def _file_record(name_offset: int, offset: int, stored: int, c: int, d: int, *, resource=False) -> bytes:
    sector = offset // _BLOCK
    if not 0 <= sector <= _MAX_SECTOR or not 0 <= stored <= _SIZE24:
        raise RpfError("RPF7 file offset or size cannot be represented.")
    if resource:
        sector |= _RESOURCE
    offset_and_size = (sector << 8) | (stored >> 16)
    if offset_and_size == _DIRECTORY:
        raise RpfError("RPF7 file offset collides with a directory marker.")
    return _HEADER.pack(name_offset | ((stored & 0xFFFF) << 16), offset_and_size, c, d)


def _compress(view: _View, destination: BinaryIO) -> int:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    for chunk in view.chunks():
        destination.write(compressor.compress(chunk))
    destination.write(compressor.flush())
    return destination.tell()


def _copy(view: _View, destination: BinaryIO, offset: int = 0) -> None:
    for chunk in view.chunks():
        destination.seek(offset)
        destination.write(chunk)
        offset += len(chunk)


def _write_replacement(archive: _Archive, entry: _File, replacement: _View) -> None:
    stream = archive.view.stream
    with tempfile.TemporaryFile() as compressed:
        encoded = replacement
        stored = 0
        if entry.compressed:
            compressed_size = _compress(replacement, compressed)
            if compressed_size <= _SIZE24:
                encoded = _View(compressed, 0, compressed_size, entry.name)
                stored = compressed_size
        offset = _choose_offset(archive, entry, encoded.size)
        end = offset + encoded.size
        final_size = archive.view.size if end <= archive.view.size else _align(end)
        if final_size > _MAX_ARCHIVE:
            raise RpfError("Updated RPF7 archive exceeds its size limit.")
        record_offset = entry.index * _HEADER.size
        name_offset = int.from_bytes(archive.table[record_offset:record_offset + 2], "little")
        record = _binary_record(name_offset, offset, stored, replacement.size, entry.encrypted)
        updated_table = archive.table[:record_offset] + record + archive.table[record_offset + _HEADER.size:]
        table, names = rpf_crypto.encrypt_toc(
            archive.mode, archive.view.name, final_size, updated_table, archive.names,
        )
        if entry.encrypted:
            if encoded.size > _MAX_CIPHER_BYTES:
                raise RpfError("Encrypted RPF7 replacement exceeds its size limit.")
            mode = rpf_crypto.AES if archive.mode == rpf_crypto.AES else rpf_crypto.NG
            # Both PC ciphers use the same block transform for TOC and file data.
            ciphertext, _ = rpf_crypto.encrypt_toc(
                mode, entry.name, replacement.size, encoded.read(0, encoded.size), b"",
            )
            stream.seek(offset)
            stream.write(ciphertext)
        else:
            _copy(encoded, stream, offset)
        if final_size != archive.view.size:
            stream.truncate(final_size)
        stream.seek(_HEADER.size)
        stream.write(table)
        stream.write(names)


def _rewrite_catalog(archive: _Archive, target: BinaryIO, budget: _Budget, *,
                     parent: int = 0, addition: list[str] | None = None,
                     data: bytes = b"", removed: int | None = None, replaced: int | None = None) -> None:
    """Reindex directory ranges and copy retained file payloads in their stored form."""
    old_count = len(archive.table) // _HEADER.size
    new_names = addition or []
    new_count = old_count + len(new_names)
    old_files = {entry.index: entry for _, entry in archive.files.values()}
    order = [0]
    records = {}

    def entry_name(index: int) -> str:
        if index >= old_count:
            return new_names[index - old_count]
        if index in old_files:
            return old_files[index].name
        name_offset, _, _, _ = _HEADER.unpack_from(archive.table, index * _HEADER.size)
        return _entry_name(archive.names, name_offset)

    # A directory's children must be one contiguous range in the TOC.
    for index in order:
        children = None
        if index >= old_count:
            if index + 1 < new_count:
                children = [index + 1]
        elif index not in old_files:
            _, _, first, count = _HEADER.unpack_from(archive.table, index * _HEADER.size)
            children = [child for child in range(first, first + count) if child != removed]
            if new_names and index == parent:
                children.append(old_count)
                children.sort(key=entry_name)
        if children is not None:
            records[index] = (len(order), len(children))
            order.extend(children)

    # File name offsets are 16-bit; directory offsets are 32-bit.
    names = bytearray(b"\0")
    offsets = {"": 0}
    for directories in (False, True):
        for index in order:
            if (index in records) != directories:
                continue
            name = entry_name(index)
            if name not in offsets:
                offsets[name] = len(names)
                names.extend(name.encode("utf-8") + b"\0")
            if not directories and offsets[name] > 0xFFFF:
                raise RpfError("RPF7 file name table exceeds the 16-bit file name range.")
    if _align(len(names), 16) > _MAX_NAME_TABLE:
        raise RpfError("RPF7 name table exceeds its limit.")
    names.extend(bytes(-len(names) % 16))
    offset = _align(_HEADER.size * (len(order) + 1) + len(names))
    table = bytearray()
    for index in order:
        name = entry_name(index)
        name_offset = offsets[name]
        if index in records:
            table.extend(_HEADER.pack(name_offset, _DIRECTORY, *records[index]))
            continue
        if index >= old_count or index == replaced:
            table.extend(_source_record(target, _View(BytesIO(data), 0, len(data), name),
                                        name_offset, offset, budget))
            offset = _align(offset + len(data))
            continue
        entry = old_files[index]
        a, b, c, d = _HEADER.unpack_from(archive.table, index * _HEADER.size)
        stored = (a >> 16) | ((b & 0xFF) << 16)
        end = _align(offset + entry.size)
        if end > _MAX_ARCHIVE:
            raise RpfError("Updated RPF7 archive exceeds its size limit.")
        record = _file_record(name_offset, offset, stored, c, d, resource=entry.resource)
        _copy(archive.view.part(entry.offset, entry.size), target, offset)
        table.extend(record)
        offset = end

    # NG TOC encryption depends on the final archive size, not just the basename.
    sealed_table, sealed_names = rpf_crypto.encrypt_toc(
        archive.mode, archive.view.name, offset, bytes(table), bytes(names),
    )
    target.seek(0)
    target.write(_HEADER.pack(_MAGIC, len(order), len(names), archive.mode))
    target.write(sealed_table)
    target.write(sealed_names)
    target.truncate(offset)


def _insert(archive: _Archive, target: BinaryIO, parts: list[str], keys: list[str],
            data: bytes, budget: _Budget) -> None:
    key = "/".join(keys)
    if key in archive.files or key in archive.directories:
        raise EntryExistsError("The requested RPF7 path already exists.")
    if keys[-1].endswith(".rpf"):
        raise RpfError("RPF7 addition requires a file other than a nested archive.")
    parent = 0
    start = 0
    for index in range(1, len(parts)):
        prefix = "/".join(keys[:index])
        if prefix in archive.files:
            raise EntryExistsError("An RPF7 file occupies the requested parent path.")
        if prefix not in archive.directories:
            break
        parent = archive.directories[prefix]
        start = index
    addition = parts[start:]
    if any(part.casefold().endswith(".rpf") for part in addition[:-1]):
        raise EntryNotFoundError("The enclosing nested RPF7 archive was not found.")
    budget.add(len(addition))
    for index in range(start + 1, len(parts) + 1):
        budget.add_path("/".join(parts[:index]))
        budget.add_path("/".join(keys[:index]))
    _rewrite_catalog(archive, target, budget, parent=parent, addition=addition, data=data)


def _edit(source: _View, target: BinaryIO, parts: list[str], data: bytes,
          op: str, budget: _Budget, depth: int) -> int:
    if depth > _MAX_NESTED:
        raise RpfError("Nested RPF7 depth exceeds its limit.")
    archive = _parse(source, budget)
    keys = [part.casefold() for part in parts]
    nested = _nested_prefix(archive, keys)
    if nested is not None:
        entry, stop = nested
        with _open_nested(archive, entry, budget) as child, tempfile.TemporaryFile() as replacement:
            size = _edit(child, replacement, parts[stop:], data, op, budget, depth + 1)
            _copy(source, target)
            archive.view = _View(target, 0, source.size, source.name)
            _write_replacement(archive, entry, _View(replacement, 0, size, entry.name))
    elif op == "add":
        _insert(archive, target, parts, keys, data, budget)
    else:
        entry, _ = _locate(archive, keys)
        if _nested(entry.name, entry):
            raise RpfError("RPF7 file edits cannot replace or delete a nested archive.")
        if op == "delete":
            _rewrite_catalog(archive, target, budget, removed=entry.index)
        elif entry.resource:
            if data[:4] not in {_RSC7.to_bytes(4, "little"), _RSC7_RAGELIB.to_bytes(4, "little")}:
                raise RpfError("RPF7 resource replacement requires a supported RSC7 header.")
            _rewrite_catalog(archive, target, budget, data=data, replaced=entry.index)
        else:
            _copy(source, target)
            archive.view = _View(target, 0, source.size, source.name)
            _write_replacement(archive, entry, _View(BytesIO(data), 0, len(data), entry.name))
    return target.seek(0, os.SEEK_END)


def _verify_absent(view: _View, keys: list[str], budget: _Budget, depth: int) -> None:
    if depth > _MAX_NESTED:
        raise RpfError("Nested RPF7 depth exceeds its limit.")
    archive = _parse(view, budget)
    nested = _nested_prefix(archive, keys)
    if nested is not None:
        entry, stop = nested
        with _open_nested(archive, entry, budget) as child:
            _verify_absent(child, keys[stop:], budget, depth + 1)
        return
    path = "/".join(keys)
    if "/".join(keys[:-1]) not in archive.directories or path in archive.files or path in archive.directories:
        raise RpfError("RPF7 deletion readback requires an existing parent and an absent entry.")


def verify_entry_absent(archive: Path, entry: str) -> None:
    """Reopen an archive and verify deletion without mistaking a missing parent for success."""
    keys = _path_keys(entry)
    with archive.open("rb") as stream:
        _verify_absent(_View(stream, 0, os.fstat(stream.fileno()).st_size, archive.name), keys, _Budget(), 0)


@contextmanager
def _staged(destination: Path) -> Iterator[Path]:
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".rpf7-", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        yield temporary
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _edit_file(archive: Path, entry: str, op: str, data: bytes = b"") -> None:
    if not isinstance(data, bytes) or len(data) > _MAX_ENTRY_BYTES:
        raise RpfError("RPF7 input exceeds its size limit or is not bytes.")
    parts = _path_parts(entry)
    if _is_link(archive.lstat()):
        raise RpfError("RPF7 edit requires a regular archive file.")
    with _staged(archive) as temporary:
        with archive.open("rb") as source, temporary.open("r+b") as target:
            original = os.fstat(source.fileno())
            if not stat.S_ISREG(original.st_mode) or not _HEADER.size <= original.st_size <= _MAX_ARCHIVE:
                raise RpfError("RPF7 edit requires a valid archive size.")
            _edit(_View(source, 0, original.st_size, archive.name), target, parts, data, op, _Budget(), 0)
            target.flush()
            os.fsync(target.fileno())
        current = archive.lstat()
        # Windows can expose different ctime semantics for the file handle and path.
        if (_is_link(current) or not os.path.samestat(original, current)
                or (original.st_size, original.st_mtime_ns) != (current.st_size, current.st_mtime_ns)):
            raise RpfError("RPF7 archive changed during edit.")
        temporary.chmod(stat.S_IMODE(original.st_mode))


def replace_entry(archive: Path, entry: str, data: bytes) -> None:
    """Atomically replace an existing binary or supported resource file."""
    _edit_file(archive, entry, "replace", data)


def add_entry(archive: Path, entry: str, data: bytes) -> None:
    """Atomically add a new binary or supported resource file; create ordinary parents as needed."""
    _edit_file(archive, entry, "add", data)


def delete_entry(archive: Path, entry: str) -> None:
    """Atomically delete a file without deleting its directory or enclosing nested archive."""
    _edit_file(archive, entry, "delete")


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


@dataclass(slots=True)
class _BuildEntry:
    path: Path
    name: str
    name_offset: int
    directory: bool
    nested: bool
    level: int


def _source_entries(root: Path, budget: _Budget) -> tuple[list[_BuildEntry], bytes, dict[int, tuple[int, int]]]:
    budget.add(1)
    entries = [_BuildEntry(root, "", 0, True, False, 0)]
    names = bytearray(b"\0")
    offsets = {"": 0}
    directories = {}
    for index, parent in enumerate(entries):
        if not parent.directory:
            continue
        parent_info = parent.path.lstat()
        if _is_link(parent_info) or not stat.S_ISDIR(parent_info.st_mode):
            raise RpfError("RPF7 source must be a regular directory.")
        first = len(entries)
        with os.scandir(parent.path) as children:
            children_info = []
            for child in children:
                budget.add(1)
                children_info.append(child)
        seen = set()
        for child in sorted(children_info, key=lambda item: item.name):
            key = _name_key(child.name)
            budget.add_path(child.path)
            if key in seen:
                raise RpfError("RPF7 source directory contains duplicate names.")
            seen.add(key)
            if parent.level >= _MAX_DEPTH:
                raise RpfError("RPF7 source directory exceeds its depth limit.")
            info = child.stat(follow_symlinks=False)
            is_dir = stat.S_ISDIR(info.st_mode)
            if _is_link(info) or not (is_dir or stat.S_ISREG(info.st_mode)):
                raise RpfError("RPF7 source must contain only regular files and directories.")
            nested = is_dir and key.endswith(".rpf")
            if child.name not in offsets:
                offsets[child.name] = len(names)
                names.extend(child.name.encode("utf-8") + b"\0")
            name_offset = offsets[child.name]
            if (not is_dir or nested) and name_offset > 0xFFFF:
                raise RpfError("RPF7 file name table exceeds the 16-bit file name range.")
            if len(names) + 16 > _MAX_NAME_TABLE:
                raise RpfError("RPF7 name table exceeds its limit.")
            entries.append(_BuildEntry(Path(child.path), child.name, name_offset,
                                       is_dir and not nested, nested, parent.level + 1))
        directories[index] = (first, len(entries) - first)
    names.extend(bytes(16 - len(names) % 16))
    return entries, bytes(names), directories


def _discard(_chunk: bytes) -> None:
    pass


def _source_record(stream: BinaryIO, source: _View, name_offset: int, offset: int,
                   budget: _Budget, *, validate_nested: bool = True) -> bytes:
    size = source.size
    if size > _MAX_ARCHIVE or _align(offset + size) > _MAX_ARCHIVE:
        raise RpfError("Created RPF7 archive exceeds its size limit.")
    magic = source.read(0, min(size, 4))
    suffix = source.name.casefold().rpartition(".")[2]
    if suffix == "rpf" and validate_nested:
        _parse(source, budget)
    if magic in {_RSC7.to_bytes(4, "little"), _RSC7_RAGELIB.to_bytes(4, "little")}:
        header = source.read(0, _HEADER.size)
        _, version, system, graphics = _HEADER.unpack(header)
        _check_resource_version(source.name, version)
        if version != _resource_version(system, graphics):
            raise RpfError("RPF7 resource version does not match its flags.")
        if suffix in _REQUIRED_RSC7:
            _inflate(source.part(_HEADER.size, size - _HEADER.size).chunks(),
                     _MAX_RESOURCE_EXPANSION, _discard, exact=False)
        record = _file_record(name_offset, offset, min(size, _SIZE24), system, graphics, resource=True)
        if size >= _SIZE24:
            extended = bytearray(header)
            for index, byte in zip((7, 14, 5, 2), size.to_bytes(4, "little")):
                extended[index] = byte
            stream.seek(offset)
            stream.write(extended)
            _copy(source.part(_HEADER.size, size - _HEADER.size), stream, offset + _HEADER.size)
            return record
    elif magic.startswith(b"RSC"):
        raise RpfError("Unsupported resource signature in RPF7 source.")
    else:
        if suffix in _REQUIRED_RSC7 and (suffix != "ymt" or magic != b"PSIN"):
            raise RpfError("Model source is missing a supported RSC7 resource header.")
        record = _binary_record(name_offset, offset, 0, size, False)
    _copy(source, stream, offset)
    return record


def _build(root: Path, name: str, stream: BinaryIO, budget: _Budget, depth: int) -> int:
    if depth > _MAX_NESTED:
        raise RpfError("Nested RPF7 source depth exceeds its limit.")
    entries, names, directories = _source_entries(root, budget)
    table = bytearray()
    offset = _align(_HEADER.size * (len(entries) + 1) + len(names))
    for index, entry in enumerate(entries):
        if entry.directory:
            table.extend(_HEADER.pack(entry.name_offset, _DIRECTORY, *directories[index]))
            continue
        if entry.nested:
            with tempfile.TemporaryFile() as child:
                size = _build(entry.path, entry.name, child, budget, depth + 1)
                table.extend(_source_record(stream, _View(child, 0, size, entry.name),
                                            entry.name_offset, offset, budget, validate_nested=False))
        else:
            with entry.path.open("rb") as source:
                info = os.fstat(source.fileno())
                current = entry.path.lstat()
                if (not stat.S_ISREG(info.st_mode) or _is_link(current)
                        or not os.path.samestat(info, current)):
                    raise RpfError("RPF7 source must contain only regular files and directories.")
                size = info.st_size
                table.extend(_source_record(stream, _View(source, 0, size, entry.name),
                                            entry.name_offset, offset, budget))
        offset = _align(offset + size)
    stream.seek(0)
    stream.write(_HEADER.pack(_MAGIC, len(entries), len(names), rpf_crypto.OPEN))
    stream.write(table)
    stream.write(names)
    stream.truncate(offset)
    return offset


def create_archive(directory: Path, destination: Path) -> None:
    """Build an OPEN archive, turning each directory ending in .rpf into a nested archive."""
    if destination.resolve().is_relative_to(directory.resolve()):
        raise RpfError("RPF7 destination must be outside the source directory.")
    with _staged(destination) as temporary, temporary.open("r+b") as stream:
        _build(directory, destination.name, stream, _Budget(), 0)
        stream.flush()
        os.fsync(stream.fileno())
