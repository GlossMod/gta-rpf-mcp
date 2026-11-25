from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from gta_rpf_mcp import __version__, backend
from gta_rpf_mcp.gta_xml import DLCLIST, GAMECONFIG, PED_META, dlclist, parse_xml, vehicle_models
from gta_rpf_mcp.prepare import prepare
from gta_rpf_mcp.protocol import Rejected, strict_json
from gta_rpf_mcp.server import run
from gta_rpf_mcp.workspace import split_relative

HERE = Path(__file__).resolve().parents[1]
WORK = HERE / ".verify"
WORK.mkdir(exist_ok=True)
MAGIC = struct.pack("<IIII", 0x52504637, 1, 1, 0x4E45504F)
OLD_LIST = "<SMandatoryPacksData><!-- keep --><Paths><Item>dlcpacks:/stock/</Item></Paths></SMandatoryPacksData>"
OLD_CONFIG = "<fwAllConfigs><stock/></fwAllConfigs>"
NEW_CONFIG = "<fwAllConfigs><modded/></fwAllConfigs>"
SETUP = "<SSetupData><nameHash>{}</nameHash></SSetupData>"
CONTENT = "<Content><filename>dlc_gmm:/data/pedmodelinfo.meta</filename></Content>"


def fixture_pack(entries):
    return MAGIC + json.dumps(entries, separators=(",", ":")).encode("utf-8")


def fixture_entries(path):
    return json.loads(path.read_bytes()[len(MAGIC):])


class FakeBridge:
    def __init__(self, work, _jobs, template_required):
        self.work = work
        self.template = work / "template" / "gmm"
        if template_required:
            (self.template / "x64/peds.rpf").mkdir(parents=True)
            (self.template / "x64/vehicles.rpf").mkdir()
            (self.template / "data").mkdir()
            (self.template / "setup2.xml").write_text(SETUP.format("gmm"), encoding="utf-8")
            (self.template / "content.xml").write_text(CONTENT, encoding="utf-8")

    def read(self, path, entry):
        if entry not in fixture_entries(path):
            raise Rejected("verification_failed", "Missing test entry.")
        if path.name not in {"dlc.rpf", "update.rpf"}:
            raise AssertionError("Archive was not opened under its canonical name.")
        return fixture_entries(path)[entry]

    def files(self, path):
        return list(fixture_entries(path))

    def write(self, path, key, source):
        entries = fixture_entries(path)
        entries[key] = source.read_text(encoding="utf-8")
        path.write_bytes(fixture_pack(entries))

    def verify_assets(self, archive, expected):
        entries = fixture_entries(archive)
        for name, (digest, size) in expected.items():
            raw = entries[name].encode("utf-8")
            if (hashlib.sha256(raw).hexdigest(), len(raw)) != (digest, size):
                raise Rejected("verification_failed", "Fixture model did not survive readback.")

    def create_gmm(self):
        entries = {}
        for p in self.template.rglob("*"):
            name = p.relative_to(self.template).as_posix()
            if p.is_dir():
                if p.suffix == ".rpf":
                    entries[name] = ""
            else:
                entries[name] = p.read_text(encoding="utf-8")
        (self.work / "archives/gmm/dlc.rpf").write_bytes(fixture_pack(entries))


class BrokenWriter(FakeBridge):
    def write(self, path, key, source):
        super().write(path, key, source)
        if key == GAMECONFIG:
            entries = fixture_entries(path)
            entries[DLCLIST] = OLD_LIST
            path.write_bytes(fixture_pack(entries))


def target(tid, baseline, *entries):
    return {"id": tid, "baseline": baseline, "contributions": list(entries)}


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "GlossMod/AMM/gta-rpf-jobs"
        self.jobs.mkdir(parents=True)
        self.new_job("a" * 32)

    def new_job(self, id_):
        self.id = id_
        self.job = self.jobs / self.id
        (self.job / "inputs").mkdir(parents=True)

    def file(self, name, data):
        path = self.job / "inputs" / name
        if isinstance(data, str):
            data = data.encode("utf-8")
        path.write_bytes(data)
        return {"path": f"inputs/{name}", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def asset(self, name, payload=None):
        return {"name": name, "source": self.file(f"blob{len(list((self.job/'inputs').iterdir()))}",
                                                   payload if payload is not None else f"fixture:{name}")}

    def baseline(self):
        return self.file("baseline", fixture_pack({DLCLIST: OLD_LIST, GAMECONFIG: OLD_CONFIG, "unrelated.xml": "keep"}))

    def dlc(self, pack="TestPack", **entries):
        return {"kind": "dlc", "packName": pack.lower(), "source": self.file(f"dlc{len(list((self.job/'inputs').iterdir()))}",
                fixture_pack({"setup2.xml": SETUP.format(pack), **entries}))}

    def input(self, *targets, edition="legacy", **extra):
        request = {"schemaVersion": 1, "jobId": self.id, "kind": edition, "targets": list(targets), **extra}
        (self.job / "input.json").write_bytes(json.dumps(request).encode("utf-8"))

    def work(self, *targets, factory=FakeBridge, **kwargs):
        self.input(*targets, **kwargs)
        result = prepare(self.jobs, self.id, factory)
        self.assertEqual(result, strict_json((self.job / "result.json").read_bytes()))
        self.assertFalse(any(self.job.glob(".work-*")))
        if result["status"] != "ok":
            self.assertFalse((self.job / "output").exists())
        return result

    def test_composed_work_and_immutable_baselines(self):
        base = self.baseline()
        cfg = {"kind": "gameconfig", "source": self.file("config", NEW_CONFIG)}
        d = self.dlc("Addon", **{"x64/vehicles.rpf/dlccar.ytd": "b", "x64/vehicles.rpf/dlccar.yft": "a"})
        vehicle = {"kind": "vehicle", "files": [self.asset("Custom.YFT"), self.asset("custom.ytd")]}
        ped = {"kind": "ped", "modelName": "Player", "sex": "Female", "streamed": True,
               "files": [self.asset("Player.ymt"), self.asset("PLAYER\\head_000_u.ydd")]}
        meny = self.file("menyoo", '<AddedVehicleModels><VehModel modelName="Custom" customName="Keep me"/></AddedVehicleModels>')
        result = self.work(target("update", base, d, vehicle, ped, cfg), target("gmm", None, vehicle, ped),
                           target("menyoo", meny, vehicle, d))
        self.assertEqual(result["status"], "ok", result)
        out = self.job / "output"
        self.assertEqual([r["id"] for r in result["outputs"]], ["update", "gmm", "menyoo"])
        for r, filename in zip(result["outputs"], ("update.rpf", "gmm.rpf", "menyoo.xml")):
            data = (out / filename).read_bytes()
            self.assertEqual((r["size"], r["sha256"]), (len(data), hashlib.sha256(data).hexdigest()))
        updated = fixture_entries(out / "update.rpf")
        self.assertEqual(updated[GAMECONFIG], NEW_CONFIG)
        self.assertEqual(updated["unrelated.xml"], "keep")
        self.assertIn("<!-- keep -->", updated[DLCLIST])
        self.assertEqual([i.text for i in ET.fromstring(updated[DLCLIST]).findall("Paths/Item")],
                         ["dlcpacks:/stock/", "dlcpacks:/addon/", "dlcpacks:/gmm/"])
        gmm = fixture_entries(out / "gmm.rpf")
        self.assertIn("x64/peds.rpf/player/head_000_u.ydd", gmm)
        item = ET.fromstring(gmm[PED_META]).find("InitDatas/Item")
        self.assertEqual((item.findtext("Name"), item.findtext("Pedtype"), item.find("IsStreamedGfx").get("value")), ("player", "CIVFEMALE", "true"))
        names = [i.attrib for i in ET.parse(out / "menyoo.xml").findall("VehModel")]
        self.assertEqual(names, [{"modelName": "Custom", "customName": "Keep me"}, {"modelName": "dlccar", "customName": "dlccar"}])
        self.assertEqual(fixture_entries(self.job / base["path"])[DLCLIST], OLD_LIST)

    def test_inspect_without_output_and_reserved_name_rejection(self):
        r = self.work(inspectDlc=self.dlc("PackUP")["source"])
        self.assertEqual(r, {"schemaVersion": 1, "jobId": self.id, "status": "inspected", "packName": "packup"})
        self.new_job("b" * 32)
        self.assertEqual(self.work(inspectDlc=self.dlc("GMM")["source"])["code"], "conflict")

    def test_empty_and_replay(self):
        self.assertEqual(self.work()["outputs"], [])
        with self.assertRaisesRegex(Rejected, "already"):
            prepare(self.jobs, self.id, FakeBridge)

    def test_enhanced_and_unknown_schema_fields_fail_closed(self):
        v = {"kind": "vehicle", "files": [self.asset("car.yft")]}
        self.assertEqual(self.work(target("update", self.baseline(), v), edition="enhanced")["code"], "enhanced_unverified")
        self.new_job("b" * 32)
        self.assertEqual(self.work(toolPath="other.dll")["code"], "invalid_job")

    def test_conflicting_dlc_and_authority(self):
        base = self.baseline()
        a, b = self.dlc("Same"), self.dlc("same", **{"second.xml": "changed"})
        self.assertEqual(self.work(target("update", base, a, b))["code"], "conflict")
        self.new_job("b" * 32)
        bad = self.dlc("Other")
        bad["packName"] = "claim"
        self.assertEqual(self.work(target("update", self.baseline(), bad))["code"], "dlc_name_mismatch")

    def test_dedup_idempotent_baseline_and_model_conflict(self):
        base = self.baseline()
        stock = self.dlc("stock")
        result = self.work(target("update", base, stock, stock))
        self.assertEqual(result["outputs"][0]["sha256"], base["sha256"])
        self.new_job("b" * 32)
        a = {"kind": "vehicle", "files": [self.asset("same.yft", "aaa")]}
        b = {"kind": "ped", "modelName": "same", "sex": "Male", "streamed": False,
             "files": [self.asset("SAME.ydd")]}
        self.assertEqual(self.work(target("gmm", None, a, b))["code"], "conflict")

    def test_gameconfig_conflict_and_bad_xml(self):
        a = {"kind": "gameconfig", "source": self.file("a", OLD_CONFIG)}
        b = {"kind": "gameconfig", "source": self.file("b", NEW_CONFIG)}
        self.assertEqual(self.work(target("update", self.baseline(), a, b))["code"], "conflict")
        self.new_job("b" * 32)
        bad = {"kind": "gameconfig", "source": self.file("x", "<!DOCTYPE a><fwAllConfigs />")}
        self.assertEqual(self.work(target("update", self.baseline(), bad))["code"], "invalid_xml")

    def test_failed_final_archive_readback_never_publishes_earlier_output(self):
        c = {"kind": "gameconfig", "source": self.file("config", NEW_CONFIG)}
        d = self.dlc()
        v = {"kind": "vehicle", "files": [self.asset("foo.yft")]}
        result = self.work(target("menyoo", None, v), target("update", self.baseline(), d, c), factory=BrokenWriter)
        self.assertEqual(result["code"], "verification_failed")

    def test_bad_input_digest_and_hardlink(self):
        mod = self.dlc()
        mod["source"]["sha256"] = "0" * 64
        self.assertEqual(self.work(inspectDlc=mod["source"])["code"], "input_changed")
        self.new_job("b" * 32)
        mod = self.dlc()
        os.link(self.job / mod["source"]["path"], self.job / "inputs/link")
        self.assertEqual(self.work(inspectDlc=mod["source"])["code"], "unsafe_workspace")

    def test_reject_symlink_or_junction_ancestor(self):
        victim = Path(self.temp.name) / "outside"
        victim.mkdir()
        source = self.dlc()["source"]
        shutil.copyfile(self.job / source["path"], victim / "file")
        link = self.job / "jump"
        if os.name == "nt":
            command = [str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"), "-NoProfile", "-Command",
                       f"New-Item -ItemType Junction -Path '{link}' -Target '{victim}' | Out-Null"]
            subprocess.run(command, check=True, capture_output=True)
        else:
            link.symlink_to(victim, target_is_directory=True)
        self.assertEqual(self.work(inspectDlc={**source, "path": "jump/file"})["code"], "unsafe_workspace")

    def test_work_rebuilt_removes_previous_assets(self):
        car = {"kind": "vehicle", "files": [self.asset("removed.yft")]}
        self.assertEqual(self.work(target("gmm", None, car))["status"], "ok")
        self.assertIn("x64/vehicles.rpf/removed.yft", fixture_entries(self.job / "output/gmm.rpf"))
        self.new_job("b" * 32)
        new = {"kind": "vehicle", "files": [self.asset("keep.yft")]}
        self.assertEqual(self.work(target("gmm", None, new))["status"], "ok")
        entries = fixture_entries(self.job / "output/gmm.rpf")
        self.assertIn("x64/vehicles.rpf/keep.yft", entries)
        self.assertNotIn("x64/vehicles.rpf/removed.yft", entries)
        self.assertIsNotNone(ET.fromstring(entries[PED_META]).find("InitDatas"))

    def test_bad_rpf_table_is_rejected(self):
        invalid = struct.pack("<IIII", 0x52504637, 900, 20, 0x4E45504F)
        result = self.work(inspectDlc=self.file("bad", invalid))
        self.assertEqual(result["code"], "unsupported_rpf")

    def test_backend_runs_without_a_tool_directory(self):
        self.assertEqual(backend.status()["backend"], "python-rpf7")
        self.assertTrue(backend.status()["ready"])
        self.assertFalse((self.jobs.parent / "gta-rpf-tools").exists())


class ValidationTests(unittest.TestCase):
    def test_windows_paths(self):
        for value in (r"C:\file", "C:/file", "//server/share", "x/../y", "x//y", "x/Z:stream",
                      "x/AUX.txt", "x/COM1.yft", "x/CoM².txt", "x/foo.", "x/foo ", "x/Foo~1", "output/foo", ".WORK-bad/abc", "input.json"):
            with self.subTest(value=value), self.assertRaises(Rejected):
                split_relative(value)
        self.assertEqual(split_relative(r"inputs\模型.yft"), ["inputs", "模型.yft"])

    def test_nested_xml_entity_and_duplicate_json(self):
        for payload in (b"<!doctype a><a/>", b"<a>\x00</a>", (b"<a>" * 120) + (b"</a>" * 120)):
            with self.assertRaises(Rejected):
                parse_xml(payload, "test")
        with self.assertRaises(ValueError):
            strict_json(b'{"jobId":"a","jobId":"b"}')

    def test_case_alias_and_duplicate_baseline(self):
        good = r"<X><Paths><Item>DLCPACKS:\Foo\</Item></Paths></X>"
        self.assertIsNone(dlclist(good, ["foo"]))
        with self.assertRaisesRegex(Rejected, "duplicate"):
            dlclist(good.replace("</Paths>", "<Item>dlcpacks:/foo/</Item></Paths>"), ["foo"])
        self.assertEqual(vehicle_models(["a/f.yft", "b/f.ytd", "a/g.ytd", "a/g_hi.yft", "a/h.ytd", "a/h.yft"]), ["h"])

    def test_stdio_version_tools_error_and_notifications(self):
        def rpc(method, id_=None, **extra):
            return {"jsonrpc": "2.0", "method": method, **({"id": id_} if id_ else {}), **extra}
        msgs = [rpc("tools/list", 99), rpc("initialize", 1, params={"protocolVersion": "2025-03-26"}),
                rpc("notifications/initialized"), rpc("tools/list", 2), rpc("tools/call", 3, params={"name": "gta_rpf_prepare", "arguments": {"jobId": "../bad"}}),
                rpc("tools/call", 4, params={"name": "gta_rpf_prepare", "arguments": {"jobId": "a" * 32, "path": "other"}})]
        out = io.BytesIO()
        run(io.BytesIO(b"\n".join(json.dumps(m).encode("utf-8") for m in msgs) + b"\n"), out)
        responses = [strict_json(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in responses], [99, 1, 2, 3, 4])
        self.assertEqual(responses[0]["error"]["code"], -32002)
        self.assertEqual(responses[1]["result"]["serverInfo"]["version"], __version__)
        self.assertEqual([t["name"] for t in responses[2]["result"]["tools"]], ["gta_rpf_prepare", "gta_rpf_status"])
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertIn("error", responses[4])


if __name__ == "__main__":
    unittest.main()
