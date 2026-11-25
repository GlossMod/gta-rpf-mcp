"""Fixed operations used by AMM's staged jobs. All input paths are private copies."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from . import __version__, rpf7
from .gta_xml import DLCLIST, GAMECONFIG, PED_META, XML_MAX
from .protocol import Rejected
from .workspace import read_limited


def status() -> dict:
    package = files("gta_rpf_mcp")
    resources = ("template/setup2.xml", "template/content.xml", "data/rpf7-aes.dat",
                 "data/rpf7-ng-decrypt.xz", "data/rpf7-ng-encrypt.xz")
    if not all(package.joinpath(*name.split("/")).is_file() for name in resources):
        return {"ready": False, "backend": "python-rpf7", "version": __version__,
                "code": "incomplete_package", "message": "The installed wheel is missing RPF7 resources.",
                "legacy": False, "enhanced": False, "legacyJobs": [], "enhancedJobs": []}
    return {"ready": True, "backend": "python-rpf7", "version": __version__,
            "legacy": True, "enhanced": True,
            "legacyJobs": ["inspectDlc", "dlc", "gameconfig", "vehicle", "ped", "menyoo"],
            "enhancedJobs": ["inspectDlc", "dlc", "gameconfig"]}


class ArchiveBackend:
    def __init__(self, work: Path, _jobs: Path, template_required: bool):
        self.work = work
        self.template = work / "template" / "gmm"
        if template_required:
            (self.template / "data").mkdir(parents=True)
            for name in ("peds.rpf", "vehicles.rpf", "gmm_mods.rpf"):
                (self.template / "x64" / name).mkdir(parents=True)
            packaged = files("gta_rpf_mcp").joinpath("template")
            for name in ("setup2.xml", "content.xml"):
                (self.template / name).write_bytes(packaged.joinpath(name).read_bytes())

    def read(self, archive: Path, entry: str) -> bytes:
        if entry not in {DLCLIST, GAMECONFIG, "setup2.xml", PED_META}:
            raise ValueError("Unexpected archive read")
        raw = rpf7.read_entry(archive, entry, max_bytes=XML_MAX)
        if not raw:
            raise Rejected("invalid_xml", "The required archive XML is empty.")
        return raw

    def files(self, archive: Path) -> list[str]:
        return rpf7.list_entries(archive)

    def write(self, archive: Path, entry: str, source: Path):
        if (entry not in {DLCLIST, GAMECONFIG} or archive != self.work / "archives/update/update.rpf"
                or source != self.work / "edit" / entry.rsplit("/", 1)[1]):
            raise ValueError("Unexpected archive write")
        rpf7.replace_entry(archive, entry, read_limited(source, XML_MAX))

    def verify_assets(self, archive: Path, expected: dict[str, tuple[str, int]]):
        rpf7.verify_assets(archive, expected)

    def create_gmm(self):
        rpf7.create_archive(self.template, self.work / "archives/gmm/dlc.rpf")
