"""Pinned release smoke tests, run only with an explicitly supplied verified ZIP."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import struct
import unittest
import zlib

from gta_rpf_mcp import backend
from gta_rpf_mcp.gta_xml import DLCLIST, GAMECONFIG, PED_META
import test_service as fixtures
from test_service import NEW_CONFIG, OLD_CONFIG, OLD_LIST, SETUP, target


def open_rpf(files):
    """Tiny fixture writer for synthetic OPEN RPF7 binary entries only."""
    tree = {}
    for path, data in files.items():
        parts = path.split("/")
        folder = tree
        for part in parts[:-1]:
            folder = folder.setdefault(part, {})
        folder[parts[-1]] = data if isinstance(data, bytes) else data.encode("utf-8")
    entries = [["", tree]]
    names = bytearray()
    table = bytearray()
    contents = []
    for name, value in entries:
        offset = len(names)
        names += name.encode("ascii") + b"\0"
        if isinstance(value, dict):
            table += struct.pack("<IIII", offset, 0x7FFFFF00, len(entries), len(value))
            entries.extend(value.items())
        else:
            contents.append((len(table), value))
            table += struct.pack("<QII", offset, len(value), 0)
    names += b"\0" * (-len(names) % 16)
    end = 16 + len(table) + len(names)
    data = bytearray(b"\0" * (-end % 512))
    for at, blob in contents:
        sector = (end + len(data)) // 512
        offset, = struct.unpack_from("<Q", table, at)
        struct.pack_into("<Q", table, at, offset | (sector << 40))
        data += blob + b"\0" * (-len(blob) % 512)
    return struct.pack("<IIII", 0x52504637, len(entries), len(names), 0x4E45504F) + table + names + data


def resource_fixture():
    return struct.pack("<IIII", 0x37435352, 0, 0, 0) + zlib.compress(b"test resource", wbits=-15)


@unittest.skipUnless(os.name == "nt" and "GTA_RPF_TEST_RELEASE_ZIP" in os.environ, "requires explicitly provided pinned 3DM release ZIP on Windows")
class PinnedReleaseTests(unittest.TestCase):
    setUp = fixtures.JobTests.setUp
    new_job = fixtures.JobTests.new_job
    work = fixtures.JobTests.work
    input = fixtures.JobTests.input
    file = fixtures.JobTests.file
    asset = fixtures.JobTests.asset

    def bundle(self):
        supplied = Path(os.environ["GTA_RPF_TEST_RELEASE_ZIP"])
        approved = backend._get_pins()
        self.assertIn(hashlib.sha256(supplied.read_bytes()).hexdigest(), [pin["sha256"] for pin in approved])
        loc = backend.package_directory(self.jobs)
        loc.mkdir()
        local = loc / backend.PACKAGE_NAME
        shutil.copyfile(supplied, local)
        self.assertTrue(backend.status(self.jobs)["ready"])
        return local

    def test_pinned_release_update_the_selected_copy_and_probe(self):
        self.bundle()
        baseraw = open_rpf({DLCLIST: OLD_LIST, GAMECONFIG: OLD_CONFIG, "safe.xml": "reserved"})
        base = self.file("update", baseraw)
        addon = open_rpf({"setup2.xml": SETUP.format("TestAddon"), "x64/vehicles.rpf": open_rpf({"addon.yft": "xx", "addon.ytd": "yy"})})
        d = {"kind": "dlc", "packName": "testaddon", "source": self.file("ad", addon)}
        c = {"kind": "gameconfig", "source": self.file("new", NEW_CONFIG)}
        result = self.work(target("update", base, d, c), target("menyoo", None, d), factory=backend.WindowsBridge)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual((self.job / base["path"]).read_bytes(), baseraw)
        self.assertIn('modelName="addon"', (self.job / "output/menyoo.xml").read_text(encoding="utf-8"))
        work = self.job / "verify"
        work.mkdir()
        bridge = backend.WindowsBridge(work, self.jobs, False)
        try:
            output = work / "archives/update/update.rpf"
            output.parent.mkdir(parents=True)
            shutil.copyfile(self.job / "output/update.rpf", output)
            self.assertIn("dlcpacks:/testaddon/", bridge.read(output, DLCLIST))
            self.assertEqual(bridge.read(output, GAMECONFIG), NEW_CONFIG)
        finally:
            bridge.close()
        self.new_job("b" * 32)
        self.assertEqual(self.work(inspectDlc=self.file("probe", addon), factory=backend.WindowsBridge)["packName"], "testaddon")

    def test_pinned_release_builds_native_gmm_from_fresh_template(self):
        zipped = self.bundle()
        d0 = hashlib.sha256(zipped.read_bytes()).hexdigest()
        simple = {"kind": "ped", "modelName": "plain", "sex": "Male", "streamed": False,
                  "files": [self.asset("plain.ydd", resource_fixture())]}
        streamed = {"kind": "ped", "modelName": "streamed", "sex": "Female", "streamed": True,
                    "files": [self.asset("streamed.yft", resource_fixture()), self.asset("streamed/uppr_000_u.ydd", resource_fixture())]}
        car = {"kind": "vehicle", "files": [self.asset("car.yft", resource_fixture()), self.asset("car.ytd", resource_fixture())]}
        result = self.work(target("gmm", None, simple, streamed, car), factory=backend.WindowsBridge)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(hashlib.sha256(zipped.read_bytes()).hexdigest(), d0)
        w = self.job / "verify"
        w.mkdir()
        b = backend.WindowsBridge(w, self.jobs, True)
        try:
            self.assertFalse((b.template / PED_META).exists())
            self.assertFalse((b.template / "x64/peds.rpf/streamed").exists())
            dst = w / "archives/gmm/dlc.rpf"
            dst.parent.mkdir(parents=True)
            shutil.copyfile(self.job / "output/gmm.rpf", dst)
            files = b.files(dst)
            self.assertIn("x64/peds.rpf/streamed/uppr_000_u.ydd", files)
            self.assertIn("x64/peds.rpf/plain.ydd", files)
            self.assertIn("x64/vehicles.rpf/car.ytd", files)
            self.assertIn('<IsStreamedGfx value="true"', b.read(dst, PED_META))
            # The internal fixed PowerShell surface does not permit other reads/writes.
            with self.assertRaisesRegex(Exception, "rejected"):
                b._run("read", archive="archives/gmm/dlc.rpf", entry="../../secret")
        finally:
            b.close()
