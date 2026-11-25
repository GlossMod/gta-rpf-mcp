from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gta_rpf_mcp import rpf7, rpf_crypto
from gta_rpf_mcp.archive_tools import ARCHIVE_TOOLS, call_archive_tool
from gta_rpf_mcp.protocol import Rejected
from test_python_backend import open_rpf_fixture, resource_fixture


class ArchiveToolsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gta-rpf-archive-tools-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.job_id = "a" * 32
        self.job = self.root / "GlossMod" / "AMM" / "gta-rpf-jobs" / self.job_id
        (self.job / "inputs").mkdir(parents=True)
        self.old = b"<old>Original update config</old>"
        self.new = b"<new>Replacement update config</new>"
        self.entry = "common/data/gameconfig.xml"
        self.manifest = self.stage_manifest("legacy", rpf_crypto.OPEN)

    def stage_input(self, data):
        sha256 = hashlib.sha256(data).hexdigest()
        path = f"inputs/{sha256}"
        (self.job / path).write_bytes(data)
        return {"path": path, "sha256": sha256, "size": len(data)}

    def save_manifest(self, manifest):
        (self.job / "input.json").write_text(json.dumps(manifest), encoding="utf-8")

    def stage_manifest(self, edition, mode):
        raw = open_rpf_fixture({self.entry: self.old, "keep.xml": b"keep"},
                               archive_name="update.rpf", mode=mode, encrypt_files=mode != rpf_crypto.OPEN)
        baseline = self.stage_input(raw)
        self.baseline = {"path": baseline["path"], "name": "update.rpf"}
        self.source = self.stage_input(self.new)
        manifest = {"schemaVersion": 1, "jobId": self.job_id, "kind": edition,
                    "targets": [{"id": "update", "baseline": baseline,
                                 "contributions": [{"kind": "gameconfig", "source": self.source}]}]}
        self.save_manifest(manifest)
        return manifest

    def call(self, tool, **kwargs):
        result = call_archive_tool(f"gta_rpf_{tool}", {"jobId": self.job_id, **kwargs})
        self.assertEqual((result["jobId"], result["schemaVersion"], result["status"]), (self.job_id, 1, "ok"))
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        return result

    def reject(self, tool, code, **kwargs):
        with self.assertRaises(Rejected) as context:
            self.call(tool, **kwargs)
        self.assertEqual(context.exception.code, code)
        self.assertNotIn(str(self.root), str(context.exception))

    def entry_bytes(self, archive, entry):
        return base64.b64decode(self.call("read_entry", archive=archive, entry=entry)["data"], validate=True)

    def archive_file(self, archive):
        path = self.job / archive["path"]
        data = path.read_bytes()
        self.assertEqual(archive["name"], path.name)
        self.assertEqual(archive["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(archive["size"], len(data))
        return path

    def test_tool_definitions_are_independent(self):
        self.assertEqual([tool["name"] for tool in ARCHIVE_TOOLS], [
            "gta_rpf_list_entries", "gta_rpf_read_entry", "gta_rpf_write_entry", "gta_rpf_create_archive",
        ])
        self.assertEqual([tool["annotations"]["readOnlyHint"] for tool in ARCHIVE_TOOLS], [True, True, False, False])
        self.assertTrue(all(not tool["inputSchema"]["additionalProperties"] for tool in ARCHIVE_TOOLS))

    def test_ng_manifest_roundtrip_in_each_edition_without_touching_job_outputs(self):
        (self.job / "output").mkdir()
        official = self.job / "output" / "update.rpf"
        result_file = self.job / "result.json"
        official.write_bytes(b"official AMM output")
        result_file.write_bytes(b"official AMM result")
        for edition in ("legacy", "enhanced"):
            with self.subTest(edition=edition):
                self.stage_manifest(edition, rpf_crypto.NG)
                input_file = self.job / self.baseline["path"]
                input_manifest = (self.job / "input.json").read_bytes()
                original = input_file.read_bytes()
                page = self.call("list_entries", archive=self.baseline, limit=1)
                self.assertEqual((page["entries"], page["total"], page["nextOffset"]), ([self.entry], 2, 1))
                tail = self.call("list_entries", archive=self.baseline, offset=page["nextOffset"])
                self.assertEqual((tail["entries"], tail["nextOffset"]), (["keep.xml"], None))
                first = self.call("read_entry", archive=self.baseline, entry=self.entry, limit=5)
                rest = self.call("read_entry", archive=self.baseline, entry=self.entry, offset=first["nextOffset"])
                self.assertEqual((first["encoding"], first["size"], first["sha256"], rest["nextOffset"]),
                                 ("base64", len(self.old), hashlib.sha256(self.old).hexdigest(), None))
                self.assertEqual(base64.b64decode(first["data"]) + base64.b64decode(rest["data"]), self.old)

                written = self.call("write_entry", archive=self.baseline, entry=self.entry, sourcePath=self.source["path"])
                self.assertEqual(written["entry"], self.entry)
                archive = written["archive"]
                output = self.archive_file(archive)
                self.assertEqual(rpf7.read_entry(output, self.entry), self.new)
                self.assertEqual(self.entry_bytes(archive, "keep.xml"), b"keep")
                self.assertEqual(self.entry_bytes(archive, self.entry), self.new)
                second = self.call("write_entry", archive=archive, entry="keep.xml", sourcePath=self.source["path"])["archive"]
                self.assertNotEqual(archive["path"], second["path"])
                self.assertEqual(self.entry_bytes(second, "keep.xml"), self.new)
                self.assertEqual(self.entry_bytes(archive, "keep.xml"), b"keep")
                self.assertEqual(input_file.read_bytes(), original)
                self.assertEqual((self.job / "input.json").read_bytes(), input_manifest)
                self.assertEqual(official.read_bytes(), b"official AMM output")
                self.assertEqual(result_file.read_bytes(), b"official AMM result")

    def test_create_nested_archive_reopens_with_binary_and_resources(self):
        vehicle = resource_fixture()
        source = self.stage_input(vehicle)
        # Create a valid manifest with mixed sources and hash-named stored files.
        self.manifest["targets"].append({"id": "gmm", "baseline": None, "contributions": [{
            "kind": "vehicle", "files": [{"name": "car.yft", "source": source}],
        }]})
        self.save_manifest(self.manifest)
        binary_entry = "x64/models.rpf/data.xml"
        resource_entry = "x64/models.rpf/car.yft"
        result = self.call("create_archive", archiveName="dlc.rpf", entries=[
            {"entry": "x64\\models.rpf\\data.xml", "sourcePath": self.source["path"]},
            {"entry": resource_entry, "sourcePath": source["path"]},
        ])
        archive = result["archive"]
        output = self.archive_file(archive)
        self.assertEqual(rpf7.list_entries(output), ["x64/models.rpf", resource_entry, binary_entry])
        self.assertEqual(self.call("list_entries", archive=archive)["entries"], rpf7.list_entries(output))
        self.assertEqual(self.entry_bytes(archive, resource_entry), vehicle)
        self.assertEqual(self.entry_bytes(archive, binary_entry), self.new)
        edit = self.call("write_entry", archive=archive, entry=binary_entry, sourcePath=self.source["path"])["archive"]
        self.assertEqual(self.entry_bytes(edit, binary_entry), self.new)
        self.reject("write_entry", "unsupported_rpf", archive=archive, entry=resource_entry,
                    sourcePath=self.source["path"])

    def test_utf8_byte_pages_preserve_text_and_binary_is_still_available(self):
        chinese = chr(0x6E2C)
        emoji = chr(0x1F600)
        xml = f"<a>{chinese}{emoji}</a>"
        xml_bytes = xml.encode("utf-8")
        raw = open_rpf_fixture({self.entry: xml_bytes, "bytes.bin": b"abc\xff"})
        self.manifest["targets"][0]["baseline"] = self.stage_input(raw)
        self.save_manifest(self.manifest)
        archive = {"path": self.manifest["targets"][0]["baseline"]["path"], "name": "fixture.rpf"}
        args = {"archive": archive, "entry": self.entry, "encoding": "utf8", "limit": 4}
        first = self.call("read_entry", **args)
        second = self.call("read_entry", **args, offset=first["nextOffset"])
        third = self.call("read_entry", **args, offset=second["nextOffset"])
        fourth = self.call("read_entry", **args, offset=third["nextOffset"])
        self.assertEqual([page["nextOffset"] for page in (first, second, third, fourth)], [3, 6, 10, None])
        self.assertEqual("".join(page["data"] for page in (first, second, third, fourth)), xml)
        self.assertEqual((first["encoding"], first["size"], first["sha256"]),
                         ("utf8", len(xml_bytes), hashlib.sha256(xml_bytes).hexdigest()))
        self.reject("read_entry", "invalid_entry_encoding", **args, offset=4)
        self.reject("read_entry", "invalid_entry_encoding", archive=archive, entry="bytes.bin", encoding="utf8")
        self.reject("read_entry", "invalid_job", archive=archive, entry=self.entry, encoding="utf8", limit=3)
        self.reject("read_entry", "invalid_job", archive=archive, entry=self.entry, encoding="hex")
        self.assertEqual(self.entry_bytes(archive, "bytes.bin"), b"abc\xff")

    def test_paths_must_be_manifest_inputs_or_complete_staged_references(self):
        undeclared = "inputs/unlisted"
        (self.job / undeclared).write_bytes(b"private data")
        for path in (undeclared, "../private.rpf", "output/update.rpf", "result.json",
                     str(self.root / "private.rpf"), "inputs/unlisted:stream"):
            with self.subTest(path=path):
                self.reject("list_entries", "invalid_job", archive={"path": path, "name": "update.rpf"})
                self.reject("write_entry", "invalid_job", archive=self.baseline, entry=self.entry, sourcePath=path)
        self.reject("list_entries", "invalid_job", archive={**self.baseline, "name": "CON.rpf"})
        self.reject("read_entry", "invalid_job", archive=self.baseline, entry="../private")
        self.reject("read_entry", "entry_not_found", archive=self.baseline, entry="missing.xml")
        archive = self.call("write_entry", archive=self.baseline, entry=self.entry, sourcePath=self.source["path"])["archive"]
        self.reject("list_entries", "invalid_job", archive={"path": archive["path"], "name": archive["name"]})
        self.reject("list_entries", "invalid_job", archive={**archive, "name": "other.rpf"})
        self.reject("list_entries", "input_changed", archive={**archive, "sha256": "0" * 64})
        self.reject("write_entry", "invalid_job", archive=archive, entry=self.entry, sourcePath=archive["path"])

    def test_archive_input_metadata_and_file_safety(self):
        baseline_metadata = self.manifest["targets"][0]["baseline"]
        baseline = {**baseline_metadata, "name": "update.rpf"}
        self.assertEqual(self.entry_bytes(baseline, self.entry), self.old)
        self.reject("list_entries", "invalid_job", archive={**baseline, "size": baseline["size"] + 1})

        path = self.job / baseline["path"]
        external = self.root / "outside.rpf"
        path.rename(external)
        self.reject("list_entries", "workspace_io", archive=baseline)
        os.link(external, path)
        self.reject("list_entries", "unsafe_workspace", archive=baseline)
        path.unlink()
        external.rename(path)

        source = self.job / self.source["path"]
        source.write_bytes(bytes(len(self.new)))
        self.reject("write_entry", "input_changed", archive=baseline, entry=self.entry, sourcePath=self.source["path"])
        self.assertFalse(any(self.job.glob(".work-rpf-*")))

    def test_workspaces_and_manifest_are_required(self):
        with self.assertRaises(Rejected) as context:
            call_archive_tool("gta_rpf_list_entries", {"jobId": "f" * 32, "archive": self.baseline})
        self.assertEqual(context.exception.code, "missing_workspace")
        (self.job / "input.json").write_bytes(b'{"jobId":null,"jobId":false}')
        self.reject("list_entries", "invalid_job", archive=self.baseline)

    def test_unsafe_creation_mappings_and_tool_limits(self):
        for source_paths in (("../outside",), ("RPF/AUX.xml",), ("./file",), ("C:payload",),
                             ("a.xml", "A.XML"), ("A.xml", "a.xml/file"), ("a.xml/file", "A.xml"),
                             ("Data/first.xml", "data/second.xml"), ("x" * 181,)):
            with self.subTest(source_paths=source_paths):
                self.reject("create_archive", "invalid_job", archiveName="new.rpf", entries=[
                    {"entry": entry, "sourcePath": self.source["path"]} for entry in source_paths
                ])
        self.reject("create_archive", "invalid_job", archiveName="new.rpf", entries=[])
        self.reject("create_archive", "invalid_job", archiveName="../new.rpf", entries=[])
        self.reject("list_entries", "invalid_job", archive=self.baseline, limit=501)
        self.reject("list_entries", "invalid_job", archive=self.baseline, offset=True)
        self.reject("read_entry", "invalid_job", archive=self.baseline, entry=self.entry, limit=131073)
        with patch("gta_rpf_mcp.archive_tools._MAX_BUILD_BYTES", len(self.new)):
            self.reject("create_archive", "invalid_job", archiveName="new.rpf", entries=[
                {"entry": entry, "sourcePath": self.source["path"]} for entry in ("one.xml", "two.xml")
            ])
        self.source["size"] = 128 * 1024**2 + 1
        self.save_manifest(self.manifest)
        self.reject("write_entry", "invalid_job", archive=self.baseline, entry=self.entry, sourcePath=self.source["path"])
        self.assertFalse(any(self.job.glob(".work-rpf-*")))

    def test_failed_readback_does_not_publish_and_concurrent_writes_are_unique(self):
        args = {"archive": self.baseline, "entry": self.entry, "sourcePath": self.source["path"]}
        with patch.object(rpf7, "verify_assets", side_effect=rpf7.RpfError(str(self.root))):
            self.reject("write_entry", "verification_failed", **args)
            self.reject("create_archive", "verification_failed", archiveName="new.rpf",
                        entries=[{"entry": "data.xml", "sourcePath": self.source["path"]}])
        self.assertFalse(any(self.job.glob(".work-rpf-*")))
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = [workers.submit(self.call, "write_entry", **args) for _ in range(2)]
            first, second = [future.result()["archive"] for future in results]
        self.assertNotEqual(first["path"], second["path"])
        for archive in (first, second):
            self.assertEqual(self.entry_bytes(archive, self.entry), self.new)
        self.assertEqual(self.entry_bytes(self.baseline, self.entry), self.old)


if __name__ == "__main__":
    unittest.main()
