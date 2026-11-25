from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from gta_rpf_mcp import rpf7, rpf_crypto
from gta_rpf_mcp.gta_xml import DLCLIST, GAMECONFIG
from gta_rpf_mcp.prepare import prepare


OLD_LIST = "<SMandatoryPacksData><Paths><Item>dlcpacks:/stock/</Item></Paths></SMandatoryPacksData>"
OLD_CONFIG = "<fwAllConfigs><stock/></fwAllConfigs>"
NEW_CONFIG = "<fwAllConfigs><pythonBackend/></fwAllConfigs>"
VEHICLE_BYTES = b"independent vehicle resource fixture\x00\x01\x02"


def open_rpf_fixture(
    files: dict[str, bytes], *, archive_name: str = "fixture.rpf",
    mode: int = rpf_crypto.OPEN, encrypt_files: bool = False,
) -> bytes:
    """Build raw RPF7 bytes without using the production archive writer."""
    tree: dict = {}
    for path, payload in files.items():
        parts = path.split("/")
        folder = tree
        for part in parts[:-1]:
            folder = folder.setdefault(part, {})
        folder[parts[-1]] = payload

    entries: list[tuple[str, bytes | dict]] = [("", tree)]
    names = bytearray()
    table = bytearray()
    file_records: list[tuple[int, bytes]] = []
    index = 0
    while index < len(entries):
        name, value = entries[index]
        name_offset = len(names)
        names.extend(name.encode("ascii") + b"\0")
        if isinstance(value, dict):
            first_child = len(entries)
            children = sorted(value.items())
            table.extend(struct.pack("<IIII", name_offset, 0x7FFFFF00, first_child, len(children)))
            entries.extend(children)
        else:
            record_offset = len(table)
            encrypted = encrypt_files
            stored = (
                rpf_crypto.encrypt_toc(mode, name, len(value), value, b"")[0]
                if encrypted else value
            )
            table.extend(struct.pack("<QII", name_offset, len(value), int(encrypted)))
            file_records.append((record_offset, stored))
        index += 1

    names.extend(b"\0" * (-len(names) % 16))
    data_start = 16 + len(table) + len(names)
    data = bytearray(b"\0" * (-data_start % 512))
    for record_offset, payload in file_records:
        sector = (data_start + len(data)) // 512
        name_offset, = struct.unpack_from("<Q", table, record_offset)
        struct.pack_into("<Q", table, record_offset, name_offset | (sector << 40))
        data.extend(payload)
        data.extend(b"\0" * (-len(payload) % 512))

    archive_size = 16 + len(table) + len(names) + len(data)
    encrypted_table, encrypted_names = rpf_crypto.encrypt_toc(
        mode, archive_name, archive_size, bytes(table), bytes(names),
    )
    header = struct.pack("<IIII", 0x52504637, len(entries), len(names), mode)
    return header + encrypted_table + encrypted_names + data


def resource_fixture() -> bytes:
    """An opaque RSC7-shaped asset; the archive test only asserts byte preservation."""
    return struct.pack("<IIII", 0x37435352, 0, 0, 0) + zlib.compress(VEHICLE_BYTES, wbits=-15)


class DefaultPythonBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gta-rpf-python-backend-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "GlossMod" / "AMM" / "gta-rpf-jobs"
        self.jobs.mkdir(parents=True)
        self.select_job("a" * 32)

    def select_job(self, job_id: str):
        self.job_id = job_id
        self.job = self.jobs / job_id
        (self.job / "inputs").mkdir(parents=True, exist_ok=True)

    def write_input(self, name: str, value: bytes) -> dict:
        path = self.job / "inputs" / name
        path.write_bytes(value)
        return {
            "path": f"inputs/{name}",
            "size": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }

    def write_request(self, request: dict):
        (self.job / "input.json").write_text(json.dumps(request), encoding="utf-8")

    def baseline_ref(
        self, *, mode: int = rpf_crypto.OPEN, encrypt_files: bool = False,
    ) -> tuple[bytes, dict]:
        raw = open_rpf_fixture({
            DLCLIST: OLD_LIST.encode("utf-8"),
            GAMECONFIG: OLD_CONFIG.encode("utf-8"),
            "common/data/keep.xml": b"<keep unchanged=\"true\" />",
        }, archive_name="update.rpf", mode=mode, encrypt_files=encrypt_files)
        return raw, self.write_input("update.rpf", raw)

    def vehicle_contribution(self, prefix: str = "car") -> dict:
        yft = self.write_input(f"{prefix}.yft", resource_fixture())
        ytd = self.write_input(f"{prefix}.ytd", resource_fixture())
        return {
            "kind": "vehicle",
            "files": [
                {"name": f"{prefix}.yft", "source": yft},
                {"name": f"{prefix}.ytd", "source": ytd},
            ],
        }

    def test_default_prepare_updates_and_builds_reopenable_nested_gmm(self):
        baseline_bytes, baseline = self.baseline_ref()
        vehicle_contribution = self.vehicle_contribution()
        config = self.write_input("gameconfig.xml", NEW_CONFIG.encode("utf-8"))
        request = {
            "schemaVersion": 1,
            "jobId": self.job_id,
            "kind": "legacy",
            "targets": [
                {
                    "id": "update",
                    "baseline": baseline,
                    "contributions": [
                        vehicle_contribution,
                        {"kind": "gameconfig", "source": config},
                    ],
                },
                {
                    "id": "gmm",
                    "baseline": None,
                    "contributions": [vehicle_contribution],
                },
            ],
        }
        self.write_request(request)

        # No backend factory is supplied: exercise the actual default backend and
        # the same job/schema route the MCP server uses.
        result = prepare(self.jobs, self.job_id)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(
            [item["id"] for item in result["outputs"]], ["update", "gmm"]
        )
        self.assertEqual((self.job / "inputs/update.rpf").read_bytes(), baseline_bytes)
        self.assertFalse(any(self.job.glob(".work-*")))

        update_archive = self.job / "output/update.rpf"
        self.assertIn(DLCLIST, rpf7.list_entries(update_archive))
        self.assertIn(GAMECONFIG, rpf7.list_entries(update_archive))
        dlclist = rpf7.read_entry(update_archive, DLCLIST).decode("utf-8")
        self.assertIn("dlcpacks:/gmm/", dlclist)
        self.assertEqual(rpf7.read_entry(update_archive, GAMECONFIG).decode("utf-8"), NEW_CONFIG)
        self.assertEqual(
            rpf7.read_entry(update_archive, "common/data/keep.xml"),
            b"<keep unchanged=\"true\" />",
        )

        # Read the finished outer archive from disk, extract its nested vehicles
        # archive, then reopen that archive and verify the original resource bytes.
        gmm_archive = self.job / "output/gmm.rpf"
        gmm_entries = rpf7.list_entries(gmm_archive)
        self.assertIn("x64/vehicles.rpf", gmm_entries)
        nested_bytes = rpf7.read_entry(gmm_archive, "x64/vehicles.rpf")
        nested_archive = self.root / "reopened-vehicles.rpf"
        nested_archive.write_bytes(nested_bytes)
        self.assertIn("car.yft", rpf7.list_entries(nested_archive))
        self.assertEqual(rpf7.read_entry(nested_archive, "car.yft"), resource_fixture())

    def test_enhanced_update_and_inspect_use_the_default_backend(self):
        # The raw table and records are assembled here, not with the production
        # archive writer. NG cryptography is also checked against upstream C# vectors.
        baseline_bytes, baseline = self.baseline_ref(
            mode=rpf_crypto.NG, encrypt_files=True,
        )
        dlc_bytes = open_rpf_fixture({
            "setup2.xml": b"<SSetupData><nameHash>testaddon</nameHash></SSetupData>",
        }, archive_name="dlc.rpf", mode=rpf_crypto.NG, encrypt_files=True)
        dlc = self.write_input("dlc.rpf", dlc_bytes)
        config = self.write_input("enhanced-gameconfig.xml", NEW_CONFIG.encode("utf-8"))
        self.write_request({
            "schemaVersion": 1,
            "jobId": self.job_id,
            "kind": "enhanced",
            "targets": [{
                "id": "update",
                "baseline": baseline,
                "contributions": [
                    {"kind": "dlc", "packName": "testaddon", "source": dlc},
                    {"kind": "gameconfig", "source": config},
                ],
            }],
        })

        result = prepare(self.jobs, self.job_id)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual([item["id"] for item in result["outputs"]], ["update"])
        self.assertEqual((self.job / "inputs/update.rpf").read_bytes(), baseline_bytes)
        output = self.job / "output/update.rpf"
        self.assertIn("dlcpacks:/testaddon/", rpf7.read_entry(output, DLCLIST).decode("utf-8"))
        self.assertEqual(rpf7.read_entry(output, GAMECONFIG).decode("utf-8"), NEW_CONFIG)

        self.select_job("b" * 32)
        inspected_dlc = self.write_input("inspect.rpf", dlc_bytes)
        self.write_request({
            "schemaVersion": 1,
            "jobId": self.job_id,
            "kind": "enhanced",
            "targets": [],
            "inspectDlc": inspected_dlc,
        })
        inspected = prepare(self.jobs, self.job_id)
        self.assertEqual(inspected, {
            "schemaVersion": 1,
            "jobId": self.job_id,
            "status": "inspected",
            "packName": "testaddon",
        })
        self.assertFalse((self.job / "output").exists())

    def test_enhanced_gmm_loose_assets_and_menyoo_are_rejected(self):
        cases = (
            ("gmm", "gmm", "vehicle"),
            ("loose-vehicle", "update", "vehicle"),
            ("loose-ped", "update", "ped"),
            ("menyoo", "menyoo", "vehicle"),
        )
        for index, (label, target_id, contribution_kind) in enumerate(cases, start=1):
            with self.subTest(feature=label):
                job_id = f"{index:032x}"
                self.select_job(job_id)
                if contribution_kind == "vehicle":
                    contribution = self.vehicle_contribution(label.replace("-", ""))
                else:
                    ped = self.write_input("plain.ydd", b"opaque ped resource")
                    contribution = {
                        "kind": "ped",
                        "modelName": "plain",
                        "sex": "Male",
                        "streamed": False,
                        "files": [{"name": "plain.ydd", "source": ped}],
                    }
                if target_id == "update":
                    _, baseline = self.baseline_ref()
                else:
                    baseline = None
                self.write_request({
                    "schemaVersion": 1,
                    "jobId": job_id,
                    "kind": "enhanced",
                    "targets": [{
                        "id": target_id,
                        "baseline": baseline,
                        "contributions": [contribution],
                    }],
                })

                result = prepare(self.jobs, job_id)

                self.assertEqual(result["status"], "error", result)
                self.assertFalse((self.job / "output").exists())


if __name__ == "__main__":
    unittest.main()
