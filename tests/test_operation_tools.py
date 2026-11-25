"""Regression coverage for the dedicated MCP operation tools."""
from __future__ import annotations

from io import BytesIO
import json
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from gta_rpf_mcp import rpf7, rpf_crypto
from gta_rpf_mcp.gta_xml import DLCLIST, GAMECONFIG, PED_META
from gta_rpf_mcp.server import run
from test_python_backend import open_rpf_fixture, resource_fixture
import test_service as fixtures


JOB_TOOLS = (
    "gta_rpf_inspect_dlc",
    "gta_rpf_register_dlc",
    "gta_rpf_replace_gameconfig",
    "gta_rpf_build_vehicle_pack",
    "gta_rpf_build_ped_pack",
    "gta_rpf_update_menyoo",
)


class OperationToolTests(unittest.TestCase):
    setUp = fixtures.JobTests.setUp
    new_job = fixtures.JobTests.new_job
    file = fixtures.JobTests.file
    asset = fixtures.JobTests.asset
    input = fixtures.JobTests.input

    def _rpc(self, *requests):
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
        for index, (method, params) in enumerate(requests, start=2):
            messages.append({
                "jsonrpc": "2.0",
                "id": index,
                "method": method,
                "params": params,
            })
        source = BytesIO(b"\n".join(json.dumps(message).encode() for message in messages) + b"\n")
        destination = BytesIO()
        with patch("gta_rpf_mcp.server.workspace_parent", return_value=self.jobs):
            run(source, destination)
        responses = [json.loads(line) for line in destination.getvalue().splitlines()]
        return [response for response in responses if response.get("id") != 1]

    def _call_sequence(self, *calls):
        responses = self._rpc(*(
            ("tools/call", {"name": name, "arguments": arguments})
            for name, arguments in calls
        ))
        results = []
        for response in responses:
            self.assertNotIn("error", response, response)
            result = response["result"]
            self.assertTrue(result["content"], result)
            results.append(json.loads(result["content"][0]["text"]))
        return results

    def _call(self, name, arguments=None):
        if arguments is None:
            arguments = {"jobId": self.id}
        return self._call_sequence((name, arguments))[0]

    def _operation_calls(self, *names):
        return self._call_sequence(*(
            (name, {"jobId": self.id}) for name in names
        ))

    def _tools(self):
        response, = self._rpc(("tools/list", {}))
        self.assertNotIn("error", response, response)
        return {tool["name"]: tool for tool in response["result"]["tools"]}

    def _select_job(self, number):
        self.new_job(f"{number:032x}")

    @staticmethod
    def _archive_options(edition):
        return {
            "mode": rpf_crypto.NG if edition == "enhanced" else rpf_crypto.OPEN,
            "encrypt_files": edition == "enhanced",
        }

    def _baseline(self, edition):
        options = self._archive_options(edition)
        raw = open_rpf_fixture({
            DLCLIST: fixtures.OLD_LIST.encode("utf-8"),
            GAMECONFIG: fixtures.OLD_CONFIG.encode("utf-8"),
            "common/data/keep.xml": b"<keep unchanged=\"true\" />",
        }, archive_name="update.rpf", **options)
        return self.file("update.rpf", raw)

    def _dlc(self, edition, *, with_vehicle=False):
        options = self._archive_options(edition)
        entries = {
            "setup2.xml": fixtures.SETUP.format("testaddon").encode("utf-8"),
        }
        if with_vehicle:
            nested = open_rpf_fixture({
                "addon.yft": resource_fixture(),
                "addon.ytd": resource_fixture(),
            }, archive_name="vehicles.rpf", **options)
            entries["x64/vehicles.rpf"] = nested
        raw = open_rpf_fixture(entries, archive_name="dlc.rpf", **options)
        return {
            "kind": "dlc",
            "packName": "testaddon",
            "source": self.file("testaddon.dlc.rpf", raw),
        }

    def _vehicle(self, name):
        return {
            "kind": "vehicle",
            "files": [
                self.asset(f"{name}.yft", resource_fixture()),
                self.asset(f"{name}.ytd", resource_fixture()),
            ],
        }

    def _ped(self, name):
        return {
            "kind": "ped",
            "modelName": name,
            "sex": "Female",
            "streamed": False,
            "files": [self.asset(f"{name}.ydd", resource_fixture())],
        }

    def test_operation_tools_use_job_id_and_status_lists_edition_support(self):
        tools = self._tools()
        required = set(JOB_TOOLS) | {"gta_rpf_prepare", "gta_rpf_status"}
        self.assertTrue(required <= tools.keys(), sorted(required - tools.keys()))

        for name in (*JOB_TOOLS, "gta_rpf_prepare"):
            with self.subTest(tool=name):
                schema = tools[name]["inputSchema"]
                self.assertEqual(set(schema["properties"]), {"jobId"})
                self.assertEqual(schema["required"], ["jobId"])
                self.assertFalse(schema["additionalProperties"])

        status_schema = tools["gta_rpf_status"]["inputSchema"]
        self.assertEqual(status_schema["properties"], {})
        self.assertFalse(status_schema["additionalProperties"])
        status = self._call("gta_rpf_status", {})
        self.assertTrue(status["ready"], status)
        self.assertEqual(
            status["legacyJobs"],
            ["inspectDlc", "dlc", "gameconfig", "vehicle", "ped", "menyoo"],
        )
        self.assertEqual(
            status["enhancedJobs"],
            ["inspectDlc", "dlc", "gameconfig"],
        )

    def test_inspect_register_and_replace_reopen_legacy_and_enhanced_archives(self):
        job_number = 1
        for edition in ("legacy", "enhanced"):
            with self.subTest(edition=edition):
                self._select_job(job_number)
                job_number += 1
                dlc = self._dlc(edition)
                self.input(edition=edition, inspectDlc=dlc["source"])
                inspected = self._call("gta_rpf_inspect_dlc")
                self.assertEqual((inspected["status"], inspected["packName"]), ("inspected", "testaddon"))

                self._select_job(job_number)
                job_number += 1
                baseline = self._baseline(edition)
                dlc = self._dlc(edition)
                self.input(fixtures.target("update", baseline, dlc), edition=edition)
                registered = self._call("gta_rpf_register_dlc")
                self.assertEqual(registered["status"], "ok", registered)
                registered_archive = self.job / "output/update.rpf"
                registered_list = rpf7.read_entry(registered_archive, DLCLIST).decode("utf-8")
                self.assertIn("dlcpacks:/testaddon/", registered_list)
                self.assertEqual(
                    rpf7.read_entry(registered_archive, "common/data/keep.xml"),
                    b"<keep unchanged=\"true\" />",
                )

                self._select_job(job_number)
                job_number += 1
                baseline = self._baseline(edition)
                config = {
                    "kind": "gameconfig",
                    "source": self.file("gameconfig.xml", fixtures.NEW_CONFIG.encode("utf-8")),
                }
                self.input(fixtures.target("update", baseline, config), edition=edition)
                replaced = self._call("gta_rpf_replace_gameconfig")
                self.assertEqual(replaced["status"], "ok", replaced)
                self.assertEqual(
                    rpf7.read_entry(self.job / "output/update.rpf", GAMECONFIG),
                    fixtures.NEW_CONFIG.encode("utf-8"),
                )

    def test_vehicle_pack_processes_every_mixed_target_and_reopens_outputs(self):
        baseline = self._baseline("legacy")
        dlc = self._dlc("legacy", with_vehicle=True)
        vehicle = self._vehicle("mixed")
        self.input(
            fixtures.target("update", baseline, dlc),
            fixtures.target("gmm", None, vehicle),
            fixtures.target("menyoo", None, dlc),
        )

        result = self._call("gta_rpf_build_vehicle_pack")

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual([item["id"] for item in result["outputs"]], ["update", "gmm", "menyoo"])
        update_archive = self.job / "output/update.rpf"
        self.assertIn(
            "dlcpacks:/testaddon/",
            rpf7.read_entry(update_archive, DLCLIST).decode("utf-8"),
        )
        gmm_archive = self.job / "output/gmm.rpf"
        self.assertEqual(
            rpf7.read_entry(gmm_archive, "x64/vehicles.rpf/mixed.yft"),
            resource_fixture(),
        )
        menyoo = ET.parse(self.job / "output/menyoo.xml")
        self.assertIn(
            "addon",
            {node.get("modelName") for node in menyoo.findall("VehModel")},
        )

    def test_mismatch_does_not_consume_job_and_model_tools_match_gmm_kind(self):
        self.input(fixtures.target("gmm", None, self._ped("walker")))

        wrong, correct = self._operation_calls(
            "gta_rpf_build_vehicle_pack",
            "gta_rpf_build_ped_pack",
        )

        self.assertEqual((wrong["status"], wrong["code"]), ("error", "tool_job_mismatch"))
        self.assertEqual(correct["status"], "ok", correct)
        gmm_archive = self.job / "output/gmm.rpf"
        self.assertEqual(
            rpf7.read_entry(gmm_archive, "x64/peds.rpf/walker.ydd"),
            resource_fixture(),
        )
        peds = ET.fromstring(rpf7.read_entry(gmm_archive, PED_META))
        self.assertEqual(
            peds.find("InitDatas/Item/Name").text,
            "walker",
        )

    def test_gmm_and_menyoo_tools_require_their_target_even_for_matching_models(self):
        baseline = self._baseline("legacy")
        self.input(fixtures.target("update", baseline, self._vehicle("outside")))

        gmm_mismatch, menyoo_mismatch, generic = self._operation_calls(
            "gta_rpf_build_vehicle_pack",
            "gta_rpf_update_menyoo",
            "gta_rpf_prepare",
        )

        self.assertEqual((gmm_mismatch["status"], gmm_mismatch["code"]), ("error", "tool_job_mismatch"))
        self.assertEqual((menyoo_mismatch["status"], menyoo_mismatch["code"]), ("error", "tool_job_mismatch"))
        self.assertEqual(generic["status"], "ok", generic)
        self.assertEqual([item["id"] for item in generic["outputs"]], ["update"])

        self._select_job(100)
        self.input(fixtures.target("menyoo", None, self._vehicle("listed")))
        menyoo = self._call("gta_rpf_update_menyoo")
        self.assertEqual(menyoo["status"], "ok", menyoo)
        self.assertIn(
            "listed",
            {node.get("modelName") for node in ET.parse(self.job / "output/menyoo.xml").findall("VehModel")},
        )

    def test_empty_targets_use_generic_prepare(self):
        self.input()
        result = self._call("gta_rpf_prepare")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["outputs"], [])
        self.assertEqual(list((self.job / "output").iterdir()), [])

    def test_enhanced_rejects_legacy_model_pack_and_menyoo_tools(self):
        self.input(
            fixtures.target("gmm", None, self._vehicle("enhancedcar")),
            edition="enhanced",
        )
        vehicle = self._call("gta_rpf_build_vehicle_pack")
        self.assertEqual((vehicle["status"], vehicle["code"]), ("error", "enhanced_unverified"))

        self._select_job(101)
        self.input(
            fixtures.target("menyoo", None, self._vehicle("enhancedlisted")),
            edition="enhanced",
        )
        menyoo = self._call("gta_rpf_update_menyoo")
        self.assertEqual((menyoo["status"], menyoo["code"]), ("error", "enhanced_unverified"))


if __name__ == "__main__":
    unittest.main()
