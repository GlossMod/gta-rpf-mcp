import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gta_rpf_mcp.server import Server
from test_python_backend import open_rpf_fixture


class StandaloneArchiveTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"LOCALAPPDATA": str(root)}))
        self.job_id = "1" * 32
        self.job = root / "GlossMod/AMM/gta-rpf-jobs" / self.job_id
        (self.job / "inputs").mkdir(parents=True)
        self.xml = b"<SMandatoryPacksData><Paths/></SMandatoryPacksData>"
        self.entry = "common/data/dlclist.xml"
        raw = open_rpf_fixture({self.entry: self.xml}, archive_name="update.rpf")
        self.source = self.stage(raw)
        self.payload = self.stage(b"")
        self.manifest = {"schemaVersion": 1, "kind": "enhanced", "jobId": self.job_id,
                         "archiveInputs": [{"name": "update.rpf", "source": self.source}],
                         "payloadInputs": [self.payload]}
        self.save(self.manifest)
        self.server = Server()
        self.server.request({"params": {"protocolVersion": "2025-06-18"}}, "initialize")

    def stage(self, data):
        digest = hashlib.sha256(data).hexdigest()
        path = "inputs/" + digest
        (self.job / path).write_bytes(data)
        return {"sha256": digest, "path": path, "size": len(data)}

    def save(self, manifest):
        (self.job / "input.json").write_text(json.dumps(manifest), encoding="utf8")

    def call(self, tool, **args):
        return self.server.tool("gta_rpf_" + tool, {"jobId": self.job_id, **args})["structuredContent"]

    def test_discover_selected_archive_read_dlclist_and_reject_installation(self):
        first = self.call("describe_job", limit=1)
        self.assertEqual(first["preparationTools"], [])
        self.assertEqual(first["targets"], [])
        self.assertEqual(first["total"], 2)
        archive = first["inputs"][0]["archive"]
        second = self.call("describe_job", offset=first["nextOffset"])
        self.assertEqual(second["inputs"][0]["path"], self.payload["path"])
        self.assertIsNone(second["nextOffset"])
        xml = self.call("read_entry", archive=archive, entry=self.entry, encoding="utf8")
        self.assertEqual(xml["data"], self.xml.decode())
        self.assertEqual(self.call("prepare")["code"], "tool_job_mismatch")
        self.assertEqual(self.call("inspect_dlc")["code"], "tool_job_mismatch")
        for name in (".prepare.lock", "result.json", "output"):
            self.assertFalse((self.job / name).exists())

    def test_rejects_cross_role_and_mixed_manifests(self):
        archive = self.call("describe_job")["inputs"][0]["archive"]
        for wrong in ({**archive, "name": "dlc.rpf"}, {**archive, "path": self.payload["path"]}):
            self.assertEqual(self.call("read_entry", archive=wrong, entry=self.entry)["code"], "invalid_job")
        self.assertEqual(self.call("write_entry", archive=archive, entry=self.entry,
                                   sourcePath=archive["path"])["code"], "invalid_job")
        for changes in ({"targets": []}, {"inspectDlc": self.source}, {"payloadInputs": [self.source]},
                        {"archiveInputs": [{"name": "../update.rpf", "source": self.source}]}):
            self.save({**self.manifest, **changes})
            self.assertEqual(self.call("describe_job")["code"], "invalid_job")
            self.assertEqual(self.call("list_entries", archive=archive)["code"], "invalid_job")
