from __future__ import annotations

import hashlib
from pathlib import Path
import random
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from gta_rpf_mcp import rpf7, rpf_crypto


HEADER = struct.Struct("<4I")
RPF7 = 0x52504637
OPEN = 0x4E45504F
NG = 0x0FEFFFFF
RSC7 = 0x37435352
LEGACY_RSC7 = 0x07435352
SECTOR = 512


def fixture(name: str, files: dict[str, tuple[bytes, bool, bool]], mode: int = OPEN) -> bytes:
    """Independent flat RPF fixture with known file sectors and metadata."""
    records = bytearray(HEADER.pack(0, 0x7FFFFF00, 1, len(files)))
    names = bytearray(b"\0")
    payload = bytearray()
    for entry, (data, compressed, encrypted) in files.items():
        name_offset = len(names)
        names.extend(entry.encode() + b"\0")
        stored = zlib.compress(data, wbits=-15) if compressed else data
        if encrypted:
            stored, _ = rpf_crypto.encrypt_toc(mode, entry, len(data), stored, b"")
        sector = 1 + len(payload) // SECTOR
        encoded_size = len(stored) if compressed else 0
        records.extend(struct.pack("<QII", name_offset | encoded_size << 16 | sector << 40,
                                   len(data), int(encrypted)))
        payload.extend(stored)
        payload.extend(b"\0" * (-len(payload) % SECTOR))
    names.extend(b"\0" * (-len(names) % 16))
    assert 16 + len(records) + len(names) <= SECTOR
    records, names = rpf_crypto.encrypt_toc(mode, name, SECTOR + len(payload), bytes(records), bytes(names))
    return (HEADER.pack(RPF7, len(files) + 1, len(names), mode) + records + names).ljust(SECTOR, b"\0") + payload


def resource(data: bytes, version: int = 162, magic: int = RSC7, level: int = 3) -> bytes:
    system = (version & 0xF0) << 24 | 0x01000000
    graphics = (version & 0x0F) << 28
    return HEADER.pack(magic, version, system, graphics) + zlib.compress(data, level=level, wbits=-15)


def expected(data: bytes) -> tuple[str, int]:
    return hashlib.sha256(data).hexdigest(), len(data)


class Rpf7Tests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.archive = self.root / "update.rpf"

    def test_ng_compressed_replace_rekeys_archive_only_when_size_changes(self):
        baseline = fixture(self.archive.name, {"change.xml": (b"<old/>", True, True),
                                                "keep.xml": (b"<keep/>", False, False)}, NG)
        self.archive.write_bytes(baseline)
        self.assertEqual(rpf7.read_entry(self.archive, "CHANGE.xml"), b"<old/>")
        rpf7.replace_entry(self.archive, "change.xml", b"<new/>")
        small = self.archive.read_bytes()
        self.assertEqual(len(small), len(baseline))
        self.assertEqual(small[:SECTOR], baseline[:SECTOR])
        self.assertEqual(small[2 * SECTOR:], baseline[2 * SECTOR:])

        replacement = b"<new>" + random.Random(1).randbytes(8192).hex().encode() + b"</new>"
        rpf7.replace_entry(self.archive, "change.xml", replacement)
        changed = self.archive.read_bytes()
        self.assertGreater(len(changed), len(small))
        self.assertEqual(changed[:16], baseline[:16])
        self.assertEqual(changed[2 * SECTOR:3 * SECTOR], baseline[2 * SECTOR:3 * SECTOR])
        _, count, names_size, mode = HEADER.unpack_from(changed)
        toc, _ = rpf_crypto.decrypt_toc(mode, self.archive.name, len(changed),
                                      changed[16:16 + count * 16], changed[16 + count * 16:16 + count * 16 + names_size])
        entry_fields, new_size, file_encryption = struct.unpack_from("<QII", toc, 16)
        self.assertNotEqual(entry_fields >> 40, 1)
        self.assertNotEqual((entry_fields >> 16) & 0xFFFFFF, 0)
        self.assertEqual((new_size, file_encryption), (len(replacement), 1))
        self.assertEqual(rpf7.read_entry(self.archive, "change.xml"), replacement)
        self.assertEqual(rpf7.read_entry(self.archive, "keep.xml"), b"<keep/>")

    def test_create_and_read_nested_archives_and_verify_assets_once(self):
        source = self.root / "source"
        vehicles = source / "X64" / "vehicles.rpf"
        vehicles.mkdir(parents=True)
        (source / "X64" / "empty.rpf").mkdir()
        (source / "plain").mkdir()
        (source / "plain" / "settings.xml").write_bytes(b"<original/>")
        car = resource(b"one resource")
        meta = b"PSINbinary meta"
        (vehicles / "Car.yft").write_bytes(car)
        (vehicles / "ped.ymt").write_bytes(meta)
        rpf7.create_archive(source, self.archive)
        self.assertEqual(HEADER.unpack(self.archive.read_bytes()[:16])[3], OPEN)
        self.assertEqual(rpf7.list_entries(self.archive), ["plain/settings.xml", "X64/empty.rpf",
                         "X64/vehicles.rpf", "X64/vehicles.rpf/Car.yft", "X64/vehicles.rpf/ped.ymt"])
        self.assertEqual(rpf7.read_entry(self.archive, r"x64\VEHICLES.RPF\car.yft"), car)
        self.assertEqual(rpf7.read_entry(self.archive, "x64/vehicles.rpf/ped.ymt"), meta)
        empty = self.root / "empty.rpf"
        empty.write_bytes(rpf7.read_entry(self.archive, "x64/empty.rpf"))
        self.assertEqual(rpf7.list_entries(empty), [])
        checks = {"X64/vehicles.rpf/car.yft": expected(car), "x64/vehicles.rpf/ped.ymt": expected(meta)}
        with patch.object(rpf7, "_parse", wraps=rpf7._parse) as parse:
            rpf7.verify_assets(self.archive, checks)
            self.assertEqual(parse.call_count, 2)
        checks["X64/vehicles.rpf/car.yft"] = expected(bytes(len(car)))
        with self.assertRaisesRegex(rpf7.RpfError, "SHA-256"):
            rpf7.verify_assets(self.archive, checks)

    def test_replace_inside_compressed_nested_archive(self):
        inner = fixture("dlc.rpf", {"setup2.xml": (b"<old/>", False, False)})
        self.archive.write_bytes(fixture(self.archive.name, {"dlc.rpf": (inner, True, False)}))
        rpf7.replace_entry(self.archive, "dlc.rpf/setup2.xml", b"<new>" + b"a" * 4096 + b"</new>")
        self.assertEqual(rpf7.list_entries(self.archive), ["dlc.rpf", "dlc.rpf/setup2.xml"])
        self.assertEqual(rpf7.read_entry(self.archive, "dlc.rpf/setup2.xml"), b"<new>" + b"a" * 4096 + b"</new>")

    def test_add_replace_delete_through_two_nested_encrypted_archives(self):
        leaf = fixture("Inner.rpf", {"keep.xml": (b"Encrypted sibling data", True, True)}, NG)
        outer = fixture("Outer.rpf", {"Inner.rpf": (leaf, True, True)}, NG)
        self.archive.write_bytes(fixture(self.archive.name, {
            "Outer.rpf": (outer, True, True), "keep.bin": (b"Outer sibling data", False, True),
        }, NG))
        prefix = "outer.rpf/inner.rpf/"
        entry = prefix + "Common/Data/DlcList.xml"
        rpf7.add_entry(self.archive, entry, b"<Paths/>")
        self.assertEqual(rpf7.read_entry(self.archive, entry.upper()), b"<Paths/>")
        self.assertIn("Outer.rpf/Inner.rpf/Common/Data/DlcList.xml", rpf7.list_entries(self.archive))

        payload = random.Random(42).randbytes(4096)
        rpf7.replace_entry(self.archive, entry.replace("/", "\\"), payload)
        rpf7.verify_assets(self.archive, {entry: expected(payload)})
        rpf7.delete_entry(self.archive, entry.lower())
        rpf7.verify_entry_absent(self.archive, entry)
        with self.assertRaises(rpf7.EntryNotFoundError):
            rpf7.read_entry(self.archive, entry)
        self.assertEqual(rpf7.read_entry(self.archive, prefix + "keep.xml"), b"Encrypted sibling data")
        self.assertEqual(rpf7.read_entry(self.archive, "keep.bin"), b"Outer sibling data")
        self.assertNotIn("Outer.rpf/Inner.rpf/Common/Data/DlcList.xml", rpf7.list_entries(self.archive))
        rpf7.add_entry(self.archive, entry, b"<Paths/>")
        self.assertEqual(rpf7.read_entry(self.archive, entry), b"<Paths/>")

    def test_add_and_replace_empty_standard_files(self):
        self.archive.write_bytes(fixture(self.archive.name, {}, NG))
        rpf7.add_entry(self.archive, "empty.txt", b"")
        rpf7.verify_assets(self.archive, {"empty.txt": expected(b"")})
        rpf7.replace_entry(self.archive, "empty.txt", b"content")
        self.assertEqual(rpf7.read_entry(self.archive, "empty.txt"), b"content")
        rpf7.replace_entry(self.archive, "empty.txt", b"")
        self.assertEqual(rpf7.read_entry(self.archive, "empty.txt", max_bytes=0), b"")
        rpf7.delete_entry(self.archive, "empty.txt")
        self.assertEqual(rpf7.list_entries(self.archive), [])
        rpf7.verify_entry_absent(self.archive, "empty.txt")

    def test_add_replace_delete_supported_resource_files(self):
        self.archive.write_bytes(fixture(self.archive.name, {"keep.xml": (b"keep", False, False)}))
        asset = resource(b"first vehicle")
        replacement = resource(b"second vehicle", magic=LEGACY_RSC7)
        rpf7.add_entry(self.archive, "models/car.yft", asset)
        rpf7.add_entry(self.archive, "models/keep.yft", asset)
        self.assertEqual(rpf7.read_entry(self.archive, "models/car.yft"), asset)
        rpf7.replace_entry(self.archive, "models/car.yft", replacement)
        rpf7.verify_assets(self.archive, {"models/car.yft": expected(replacement), "models/keep.yft": expected(asset)})
        original = self.archive.read_bytes()
        with self.assertRaisesRegex(rpf7.RpfError, "Enhanced resource version"):
            rpf7.replace_entry(self.archive, "models/car.yft", resource(b"unsupported", version=171))
        self.assertEqual(self.archive.read_bytes(), original)
        rpf7.delete_entry(self.archive, "models/car.yft")
        rpf7.verify_entry_absent(self.archive, "models/car.yft")
        self.assertEqual(rpf7.read_entry(self.archive, "models/keep.yft"), asset)
        self.assertEqual(rpf7.read_entry(self.archive, "keep.xml"), b"keep")

    def test_failed_add_and_delete_leave_archive_intact(self):
        leaf = fixture("nested.rpf", {"child.txt": (b"child", False, False)})
        original = fixture(self.archive.name, {"file.txt": (b"data", False, False),
                                               "nested.rpf": (leaf, False, False)})
        self.archive.write_bytes(original)
        for entry in ("FILE.txt", "file.txt/sub/name.txt", "nested.rpf/CHILD.txt"):
            with self.subTest(entry=entry), self.assertRaises(rpf7.EntryExistsError):
                rpf7.add_entry(self.archive, entry, b"new content")
        with self.assertRaises(rpf7.EntryNotFoundError):
            rpf7.add_entry(self.archive, "missing.rpf/file.txt", b"new content")
        with self.assertRaises(rpf7.EntryNotFoundError):
            rpf7.delete_entry(self.archive, "nested.rpf/missing.txt")
        with self.assertRaises(rpf7.RpfError):
            rpf7.delete_entry(self.archive, "nested.rpf")
        with self.assertRaises(rpf7.RpfError):
            rpf7.verify_entry_absent(self.archive, "missing.rpf/child.txt")
        self.assertEqual(self.archive.read_bytes(), original)

    def test_large_resource_full_header_and_digest_round_trip_both_magic_variants(self):
        source = self.root / "source"
        source.mkdir()
        large = bytes(0xFFFFFF)
        payload = zlib.compress(large, level=0, wbits=-15)
        for magic in (RSC7, LEGACY_RSC7):
            with self.subTest(magic=magic):
                asset = HEADER.pack(magic, 162, 0xA1000000, 0x20000000) + payload
                (source / "large.yft").write_bytes(asset)
                rpf7.create_archive(source, self.archive)
                with self.archive.open("rb") as stream:
                    stream.seek(SECTOR)
                    raw = stream.read(16)
                self.assertNotEqual(raw, asset[:16])
                self.assertEqual(int.from_bytes(bytes(raw[i] for i in (7, 14, 5, 2)), "little"), len(asset))
                self.assertEqual(rpf7.read_entry(self.archive, "large.yft"), asset)
                rpf7.verify_assets(self.archive, {"large.yft": expected(asset)})

    def test_reject_unsupported_resources_without_writing_destination(self):
        source = self.root / "source"
        source.mkdir()
        assets = (
            ("garbage.yft", b"garbage"), ("garbage.ydd", b"garbage"), ("garbage.ytd", b"garbage"),
            ("garbage.ymt", b"garbage"), ("garbage.yft", HEADER.pack(RSC7, 0, 0, 0) + b"garbage"),
            ("enhanced.yft", resource(b"data", 171)), ("enhanced.ydd", resource(b"data", 159)),
            ("enhanced.ytd", resource(b"data", 5)), ("enhanced.yft", b"RSC8" + bytes(12)),
        )
        for name, data in assets:
            with self.subTest(name=name, signature=data[:8]):
                asset = source / name
                asset.write_bytes(data)
                self.archive.write_bytes(b"existing output")
                with self.assertRaises(rpf7.RpfError):
                    rpf7.create_archive(source, self.archive)
                self.assertEqual(self.archive.read_bytes(), b"existing output")
                asset.unlink()

    def test_limits_precede_inflation_and_archive_failures_are_atomic(self):
        original = fixture(self.archive.name, {"huge.xml": (b"a" * (3 * 1024 * 1024), True, False)})
        self.archive.write_bytes(original)
        with patch.object(rpf7, "_inflate", side_effect=AssertionError("too early")):
            with self.assertRaisesRegex(rpf7.RpfError, "read size limit"):
                rpf7.read_entry(self.archive, "huge.xml", max_bytes=32)
        self.assertEqual(len(rpf7.read_entry(self.archive, "huge.xml")), 3 * 1024 * 1024)
        malicious = bytearray(original)
        struct.pack_into("<I", malicious, 16 + 16 + 8, 32)
        self.archive.write_bytes(malicious)
        with self.assertRaisesRegex(rpf7.RpfError, "declared length"):
            rpf7.read_entry(self.archive, "huge.xml", max_bytes=32)
        with self.assertRaisesRegex(rpf7.RpfError, "entry name"):
            rpf7.replace_entry(self.archive, "../huge.xml", b"anything")
        with self.assertRaises(rpf7.EntryNotFoundError):
            rpf7.replace_entry(self.archive, "missing.xml", b"anything")
        self.assertEqual(self.archive.read_bytes(), malicious)

    def test_windows_handle_ctime_difference_is_ignored_but_mtime_change_fails(self):
        self.archive.write_bytes(fixture(self.archive.name, {"a.xml": (b"<old/>", False, False)}))
        original_lstat = Path.lstat
        mtime_delta = 0

        def lstat(path):
            info = original_lstat(path)
            if path != self.archive:
                return info
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
                                   st_size=info.st_size, st_mtime_ns=info.st_mtime_ns + mtime_delta,
                                   st_ctime_ns=info.st_ctime_ns + 1_000_000_000)

        with patch.object(Path, "lstat", lstat):
            rpf7.replace_entry(self.archive, "a.xml", b"<new/>")
            mtime_delta = 1_000_000_000
            with self.assertRaisesRegex(rpf7.RpfError, "changed during edit"):
                rpf7.replace_entry(self.archive, "a.xml", b"<unexpected/>")
        self.assertEqual(rpf7.read_entry(self.archive, "a.xml"), b"<new/>")

    def test_reject_overlapping_extents_cycles_and_bad_archive_magic(self):
        original = bytearray(fixture(self.archive.name, {"a.xml": (b"a", False, False),
                                                        "b.xml": (b"b", False, False)}))
        overlap = original.copy()
        overlap[16 + 2 * 16 + 5] = 1
        cycle = original.copy()
        struct.pack_into("<I", cycle, 16 + 8, 0)
        magic = original.copy()
        magic[:4] = b"RPF8"
        for archive, message in ((overlap, "overlapping data"), (cycle, "cycle"), (magic, "Unsupported archive")):
            with self.subTest(message=message):
                self.archive.write_bytes(archive)
                with self.assertRaisesRegex(rpf7.RpfError, message):
                    rpf7.list_entries(self.archive)


if __name__ == "__main__":
    unittest.main()
