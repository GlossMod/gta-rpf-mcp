"""Project manifest-backed input references for agents working with a staged job."""
from pathlib import Path

from .protocol import job_operations, parse_job
from .workspace import held_tree, load_input, split_relative


def describe_job(jobs: Path, job_id: str, offset: int, limit: int):
    job = jobs / job_id
    with held_tree(job):
        manifest = load_input(job)
        targets, _, inspection = parse_job(manifest, job_id, split_relative)

    # A digest can be reused in several targets and even under different model names.
    inputs = {}

    def add(ref, role, name, archive_name=None):
        row = {**ref, "role": role, "name": name}
        if archive_name:
            row["archive"] = {"path": ref["path"], "name": archive_name}
        inputs[(ref["path"], role, name)] = row

    if inspection is not None:
        add(inspection, "inspectDlc", "dlc.rpf", "dlc.rpf")
    for target in targets:
        target_id = target["id"]
        if target["baseline"] is not None:
            if target_id == "update":
                add(target["baseline"], "baseline", "update.rpf", "update.rpf")
            else:
                add(target["baseline"], "baseline", "menyoo.xml")
        for item in target["contributions"]:
            kind = item["kind"]
            if kind == "dlc":
                add(item["source"], kind, item["packName"] + "/dlc.rpf", "dlc.rpf")
            elif kind == "gameconfig":
                add(item["source"], kind, "gameconfig.xml")
            else:
                for asset in item["files"]:
                    add(asset["source"], kind, asset["name"])
    rows = list(inputs.values())
    end = offset + limit
    return ({"jobId": job_id, "edition": manifest["kind"], "targets": [t["id"] for t in targets],
             "inputs": rows[offset:end], "total": len(rows), "nextOffset": end if end < len(rows) else None},
            job_operations(targets, inspection))
