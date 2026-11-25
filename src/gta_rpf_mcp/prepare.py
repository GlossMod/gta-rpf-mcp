from __future__ import annotations

from pathlib import Path
import json
import os
import struct
import tempfile

from .backend import WindowsBridge
from .gta_xml import (DLCLIST, GAMECONFIG, PED_META, XML_MAX, add_peds, dlclist, document, inventory,
                      parse_xml, setup_pack, textual_xml, update_menyoo, vehicle_models)
from .protocol import IDENTIFIER, MAX_FILE_BYTES, OUTPUT_NAMES, Rejected, invalid, parse_job, token
from .workspace import (copy_file, held_dir, held_tree, load_input, metadata, open_file, path_absent,
                        promote, read_limited, snapshot_file, split_relative)


def conflict(text):
    return Rejected("conflict", text)


def check_rpf(path):
    # RPF version identifiers are little-endian (0x52504637 = on-disk 7FPR).
    with open_file(path) as src:
        header = src.read(16)
        size = os.fstat(src.fileno()).st_size
    if len(header) == 16:
        magic, count, names, encryption = struct.unpack("<IIII", header)
        if (magic == 0x52504637 and count > 0 and 16 + count * 16 + names <= size
                and encryption in {0, 0x4E45504F, 0x0FFFFFF9, 0x0FEFFFFF}):
            return
    raise Rejected("unsupported_rpf", "Expected a GTA V Legacy RPF7 archive with a complete supported directory table.")


def _identity(ref):
    return (ref["sha256"], ref["size"])


def _model_clusters(entry):
    models = {}
    for f in entry["files"]:
        stem, ext = f["name"].rsplit(".", 1)
        model = stem.removesuffix("_hi")
        token(model, IDENTIFIER, "vehicle model name")
        models.setdefault(model, {})
        if f["name"] in models[model] and _identity(f["source"]) != _identity(models[model][f["name"]]["source"]):
            raise conflict("Two files replace the same vehicle asset with different bytes.")
        models[model][f["name"]] = f
    if not any(k.endswith(".yft") and not k.endswith("_hi.yft") for v in models.values() for k in v):
        raise invalid("A vehicle contribution needs a non-hi YFT model.")
    return models


def _ped_files(entry):
    name = entry["modelName"].lower()
    top = set()
    accepted = {}
    for f in entry["files"]:
        parent, _, leaf = f["name"].rpartition("/")
        if parent and (not entry["streamed"] or parent != name):
            raise invalid("A streamed Ped component must be inside its model directory.")
        if not parent:
            if leaf.rsplit(".", 1)[0] != name:
                raise invalid("A Ped root asset must match modelName.")
            top.add(leaf.rsplit(".", 1)[1])
        key = f["name"]
        if key in accepted and _identity(accepted[key]["source"]) != _identity(f["source"]):
            raise conflict("Two files replace the same Ped asset with different bytes.")
        accepted[key] = f
    minimum = {"ymt", "yft"} if entry["streamed"] else {"ymt", "yft", "ydd"}
    if not (minimum & top) or (entry["streamed"] and not any("/" in k for k in accepted)):
        raise invalid("The Ped contribution is missing its root model or streamed components.")
    return accepted


def models_for(entries):
    vehicles = {}
    owners = {}
    peds = {}
    assets = {}
    for entry in entries:
        if entry["kind"] == "vehicle":
            for name, variant in _model_clusters(entry).items():
                signature = ("vehicle", {k: _identity(v["source"]) for k, v in variant.items()})
                previous = owners.setdefault(name, signature)
                if previous != signature:
                    raise conflict("Multiple contributions claim the same model with different assets or types.")
                for k, v in variant.items():
                    vehicles.setdefault(k, v)
        elif entry["kind"] == "ped":
            name = entry["modelName"].lower()
            variant = _ped_files(entry)
            signature = ("ped", entry["sex"], entry["streamed"], {k: _identity(v["source"]) for k, v in variant.items()})
            previous = owners.setdefault(name, signature)
            if previous != signature:
                raise conflict("Multiple contributions claim the same model with different assets or types.")
            peds.setdefault(name, entry)
            for k, v in variant.items():
                assets.setdefault(k, v)
    return vehicles, peds, assets


class Preparation:
    def __init__(self, work, archive_ops, snapshots):
        self.work = work
        self.bridge = archive_ops
        self.snapshot = snapshots
        self.dlcs = {}
        self.edit = work / "edit"
        self.edit.mkdir()

    def source(self, ref):
        return self.snapshot[ref["path"]]

    def read_dlc(self, ref):
        key = _identity(ref)
        if key not in self.dlcs:
            canonical = self.work / "archives" / f"dlc{len(self.dlcs)}" / "dlc.rpf"
            canonical.parent.mkdir(parents=True)
            copy_file(self.source(ref), canonical)
            check_rpf(canonical)
            name = setup_pack(self.bridge.read(canonical, "setup2.xml"))
            token(name, IDENTIFIER, "DLC pack name")
            if name == "gmm":
                raise conflict("The DLC pack name gmm is reserved.")
            self.dlcs[key] = (name, canonical)
        return self.dlcs[key]

    def dlc_entry(self, entry):
        name, path = self.read_dlc(entry["source"])
        if name != entry["packName"].lower():
            raise Rejected("dlc_name_mismatch", "The contributed packName does not match setup2.xml nameHash.")
        return name, path

    def validate_targets(self, targets):
        packs = {}
        all_models = []
        for target in targets:
            for entry in target["contributions"]:
                kind = entry["kind"]
                if kind == "dlc":
                    name, _ = self.dlc_entry(entry)
                    value = _identity(entry["source"])
                    if packs.setdefault(name, value) != value:
                        raise conflict("Multiple different archives declare the same DLC pack.")
                elif kind in {"vehicle", "ped"}:
                    all_models.append(entry)
        models_for(all_models)

    def update(self, baseline, entries, out):
        canonical = self.work / "archives" / "update" / "update.rpf"
        canonical.parent.mkdir(parents=True)
        copy_file(self.source(baseline), canonical)
        check_rpf(canonical)
        packs = []
        configs = []
        for entry in entries:
            kind = entry["kind"]
            if kind == "dlc":
                packs.append(self.dlc_entry(entry)[0])
            elif kind in {"vehicle", "ped"}:
                packs.append("gmm")
            elif kind == "gameconfig":
                configs.append(entry["source"])
        writes = {}
        if packs:
            old = self.bridge.read(canonical, DLCLIST)
            changed = dlclist(old, list(dict.fromkeys(packs)))
            if changed is not None:
                writes[DLCLIST] = changed
        if configs:
            if len({_identity(c) for c in configs}) != 1:
                raise conflict("Multiple different gameconfig.xml files are active.")
            new = read_limited(self.source(configs[0]), XML_MAX)
            parse_xml(new, "replacement gameconfig.xml", "fwAllConfigs")
            # Require the destination to exist/read back in the selected baseline.
            orig = self.bridge.read(canonical, GAMECONFIG)
            parse_xml(orig, "baseline gameconfig.xml", "fwAllConfigs")
            if textual_xml(orig) != textual_xml(new):
                writes[GAMECONFIG] = new
        for internal, data in writes.items():
            source = self.edit / internal.rsplit("/", 1)[1]
            _write(source, data)
            self.bridge.write(canonical, internal, source)
        # Reopen the *finished* archive, after all writes. In particular, check
        # that the latter write did not corrupt/replace the earlier one.
        check_rpf(canonical)
        for internal, data in writes.items():
            if textual_xml(self.bridge.read(canonical, internal)) != textual_xml(data):
                raise Rejected("verification_failed", "The final update.rpf XML readback did not match the intended bytes.")
        copy_file(canonical, out)

    def gmm(self, entries, out):
        vehicles, peds, ped_assets = models_for(entries)
        template = self.bridge.template  # private, fresh copy extracted from a pinned package
        canonical = self.work / "archives" / "gmm" / "dlc.rpf"
        canonical.parent.mkdir(parents=True)
        setup = read_limited(template / "setup2.xml", XML_MAX)
        if setup_pack(setup) != "gmm":
            raise Rejected("missing_template", "Unexpected setup2.xml pack identity in the pinned GMM template.")
        content = document(template / "content.xml", "GMM content.xml")
        for folder, assets in (("vehicles.rpf", vehicles), ("peds.rpf", ped_assets)):
            root = template / "x64" / folder
            with held_dir(root):
                for name, f in assets.items():
                    parts = name.split("/")
                    dest = root.joinpath(*parts)
                    if not path_absent(dest):
                        raise conflict("A model asset would replace an existing template entry.")
                    if len(parts) == 2:
                        dest.parent.mkdir(exist_ok=True)
                        with held_dir(dest.parent):
                            copy_file(self.source(f["source"]), dest)
                    else:
                        copy_file(self.source(f["source"]), dest)
        # The stock template mounts Ped metadata even for vehicle-only DLCs.
        # Materialize an empty valid document when the template has no copy.
        if not any((node.text or "").strip().lower().replace("\\", "/").endswith("/data/pedmodelinfo.meta") for node in content.iter()):
            raise Rejected("missing_template", "The pinned GMM template does not register its Ped metadata.")
        meta_path = template.joinpath(*PED_META.split("/"))
        if path_absent(meta_path):
            prior = parse_xml(b"<CPedModelInfo__InitDataList><InitDatas /></CPedModelInfo__InitDataList>", "Ped defaults")
            meta_data = add_peds(prior, peds)
        else:
            meta_data = read_limited(meta_path, XML_MAX)
            prior = parse_xml(meta_data, "template pedmodelinfo.meta", "CPedModelInfo__InitDataList")
            if peds:
                meta_data = add_peds(prior, peds)
        if peds or path_absent(meta_path):
            stage = self.edit / "pedmodelinfo.meta"
            _write(stage, meta_data)
            stage.replace(meta_path)
        expected = set()
        extra = set()
        for folder, dirs, files in os.walk(template):
            rel = Path(folder).relative_to(template)
            expected.update((rel / f).as_posix().lower() for f in files)
            if rel.name.endswith(".rpf"):
                expected.add(rel.as_posix().lower())
            if not dirs and not files:
                # Upstream fills empty directories with a .empty placeholder.
                # It is not a deployed feature and may be omitted by the writer.
                extra.add((rel / ".empty").as_posix().lower())
        self.bridge.create_gmm()
        check_rpf(canonical)
        actual = set(inventory(self.bridge.files(canonical)))
        if not expected <= actual <= expected | extra:
            raise Rejected("verification_failed", "Created GMM DLC does not match the planned archive inventory.")
        if textual_xml(self.bridge.read(canonical, "setup2.xml")) != textual_xml(setup):
            raise Rejected("verification_failed", "Created GMM DLC setup2.xml did not survive readback.")
        if textual_xml(self.bridge.read(canonical, PED_META)) != textual_xml(meta_data):
            raise Rejected("verification_failed", "Created GMM Ped metadata did not match readback.")
        # NG uses the canonical archive name; gmm.rpf is a wire name only.
        copy_file(canonical, out)

    def menyoo(self, baseline, entries, out):
        models = []
        for entry in entries:
            if entry["kind"] == "vehicle":
                models.extend(vehicle_models(f["name"] for f in entry["files"]))
            else:
                _, rpf = self.dlc_entry(entry)
                models.extend(vehicle_models(inventory(self.bridge.files(rpf))))
        if baseline:
            # Reject oversized XML before the helper loads the snapshot.
            read_limited(self.source(baseline), XML_MAX)
        data = update_menyoo(self.source(baseline) if baseline else None, list(dict.fromkeys(models)))
        _write(out, data)
        result = document(out, "prepared Menyoo list", "AddedVehicleModels")
        if not set(models) <= {(e.get("modelName") or "").lower().strip() for e in result.findall("VehModel")}:
            raise Rejected("verification_failed", "Prepared Menyoo list failed readback.")


def _write(path, data):
    with path.open("xb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def prepare(jobs: Path, job_id: str, backend_factory=WindowsBridge):
    from .protocol import JOB_ID

    token(job_id, JOB_ID, "job ID")
    job = jobs / job_id
    try:
        with held_tree(job):
            try:
                with (job / ".prepare.lock").open("xb"):
                    return _prepare_locked(job, jobs, job_id, backend_factory)
            except FileExistsError as exc:
                raise Rejected("job_already_used", "The task is already running or has been used.") from exc
    except FileNotFoundError as exc:
        raise Rejected("missing_workspace", "The fixed task workspace does not exist.") from exc
    except PermissionError as exc:
        raise Rejected("unsafe_workspace", "The task workspace or file could not be securely opened.") from exc


def _prepare_locked(job, jobs, job_id, backend_factory):
    base = {"schemaVersion": 1, "jobId": job_id}
    if not path_absent(job / "output") or not path_absent(job / "result.json"):
        raise Rejected("job_already_used", "The task output already exists.")
    with tempfile.TemporaryDirectory(prefix=".work-", dir=job) as temp:
        work = Path(temp)
        with held_dir(work):
            try:
                result = _prepare_work(job, jobs, job_id, work, base, backend_factory)
            except Rejected as exc:
                result = {**base, "status": "error", "code": exc.code, "message": str(exc)}
            except OSError:
                result = {**base, "status": "error", "code": "workspace_io", "message": "Could not use a job file or stage a result."}
            result_file = work / "result.json"
            _write(result_file, json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
            if result["status"] == "ok":
                promote(work / "output", job / "output")
            promote(result_file, job / "result.json")
            return result


def _prepare_work(job, jobs, job_id, work, base, backend_factory):
    targets, refs, inspect_dlc = parse_job(load_input(job), job_id, split_relative)
    snapshots = {}
    if refs:
        folder = work / "inputs"
        folder.mkdir()
        with held_dir(folder):
            for i, ref in enumerate(refs):
                p = folder / str(i)
                snapshot_file(job, ref, p)
                snapshots[ref["path"]] = p
    backend = None
    try:
        prep = None
        if targets or inspect_dlc is not None:
            # Menyoo contributions inferred from loose vehicle files need no native code.
            native = inspect_dlc is not None or any(t["id"] != "menyoo" or any(e["kind"] == "dlc" for e in t["contributions"]) for t in targets)
            if native:
                backend = backend_factory(work, jobs, any(t["id"] == "gmm" for t in targets))
            prep = Preparation(work, backend, snapshots)
            prep.validate_targets(targets)
        if inspect_dlc is not None:
            return {**base, "status": "inspected", "packName": prep.read_dlc(inspect_dlc)[0]}
        output = work / "output"
        output.mkdir()
        results = []
        with held_dir(output):
            for target in targets:
                tid = target["id"]
                contributions = target["contributions"]
                dest = output / OUTPUT_NAMES[tid]
                if tid == "update":
                    prep.update(target["baseline"], contributions, dest)
                elif tid == "gmm":
                    prep.gmm(contributions, dest)
                else:
                    prep.menyoo(target["baseline"], contributions, dest)
                digest, size = metadata(dest)
                if not 0 < size <= MAX_FILE_BYTES:
                    raise Rejected("verification_failed", "The prepared output was empty or exceeded its size limit.")
                results.append({"id": tid, "sha256": digest, "size": size})
            if {p.name for p in output.iterdir()} != {OUTPUT_NAMES[r["id"]] for r in results}:
                raise Rejected("verification_failed", "The prepared output directory contains an unexpected file.")
        return {**base, "status": "ok", "outputs": results}
    finally:
        if backend is not None and hasattr(backend, "close"):
            backend.close()
