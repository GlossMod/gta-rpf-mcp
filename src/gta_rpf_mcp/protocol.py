from __future__ import annotations

import json
import re

MAX_JOB_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024**3
MAX_JOB_FILES = 2048
MAX_CONTRIBUTIONS = 768
MAX_JOB_INPUT_BYTES = 32 * 1024**3
JOB_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
MODEL_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}\.[A-Za-z0-9]{3}\Z", re.ASCII)
OUTPUT_NAMES = {"update": "update.rpf", "gmm": "gmm.rpf", "menyoo": "menyoo.xml"}
_ALLOWED = {"update": {"dlc", "gameconfig", "vehicle", "ped"}, "gmm": {"vehicle", "ped"}, "menyoo": {"dlc", "vehicle"}}
VEHICLE_EXT = {"yft", "ytd"}
PED_EXT = {"ydd", "yft", "yld", "ymt", "ytd"}


class Rejected(Exception):
    """An expected failure. Messages must not disclose private or absolute paths."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def strict_json(data: bytes):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate object key")
            out[key] = value
        return out

    def constant(_):
        raise ValueError("non-finite number")

    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def invalid(message: str) -> Rejected:
    return Rejected("invalid_job", message)


def record(value, keys: set[str], label: str):
    if not isinstance(value, dict) or value.keys() != keys:
        raise invalid(f"Invalid {label} object fields.")
    return value


def token(value, pattern: re.Pattern, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise invalid(f"Invalid {label}.")
    if pattern is IDENTIFIER and re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])", value):
        raise invalid(f"Invalid {label}.")
    return value


def model_path(value, kind: str, streamed: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 185:
        raise invalid("Invalid model file name.")
    parts = value.replace("\\", "/").split("/")
    if len(parts) > (2 if streamed and kind == "ped" else 1):
        raise invalid("Invalid model file nesting.")
    if len(parts) == 2:
        token(parts[0], IDENTIFIER, "streamed model directory")
    token(parts[-1], MODEL_FILE, "model filename")
    token(parts[-1].rsplit(".", 1)[0], IDENTIFIER, "model stem")
    if parts[-1].rsplit(".", 1)[-1].lower() not in (VEHICLE_EXT if kind == "vehicle" else PED_EXT):
        raise invalid("Unsupported model file extension.")
    return "/".join(parts).lower()


def parse_job(value, job_id: str, parse_path):
    inspection = isinstance(value, dict) and "inspectDlc" in value
    keys = {"schemaVersion", "jobId", "kind", "targets"}
    job = record(value, keys | ({"inspectDlc"} if inspection else set()), "job")
    if type(job["schemaVersion"]) is not int or job["schemaVersion"] != 1 or job["jobId"] != job_id:
        raise invalid("Unsupported schema version or mismatched job ID.")
    if job["kind"] == "enhanced":
        raise Rejected("enhanced_unverified", "GTA V Enhanced RPF preparation has not been verified.")
    if job["kind"] != "legacy":
        raise invalid("Unsupported game edition.")
    if not isinstance(job["targets"], list) or len(job["targets"]) > len(_ALLOWED):
        raise invalid("Invalid target list.")
    refs = {}

    def ref(value):
        file_ref = record(value, {"path", "sha256", "size"}, "file reference")
        relative = parse_path(file_ref["path"])
        token(file_ref["sha256"], SHA256, "SHA-256")
        if type(file_ref["size"]) is not int or not 0 < file_ref["size"] <= MAX_FILE_BYTES:
            raise invalid("Invalid file size.")
        key = "/".join(relative)
        canonical = key.casefold()
        parsed = {**file_ref, "path": key}
        if canonical in refs and refs[canonical] != parsed:
            raise invalid("Conflicting references to the same task file.")
        refs[canonical] = parsed
        return parsed

    if inspection:
        if job["targets"]:
            raise invalid("A DLC inspection must not have deployment targets.")
        dlc = ref(job["inspectDlc"])
        return [], [dlc], dlc

    targets = []
    seen = set()
    count = 0
    for target in job["targets"]:
        target = record(target, {"id", "baseline", "contributions"}, "target")
        name = target["id"]
        if not isinstance(name, str) or name not in _ALLOWED or name in seen:
            raise invalid("Unsupported or duplicate target ID.")
        seen.add(name)
        baseline = None if target["baseline"] is None else ref(target["baseline"])
        if (name == "update" and baseline is None) or (name == "gmm" and baseline is not None):
            raise invalid("Unexpected or missing target baseline.")
        if not isinstance(target["contributions"], list) or not target["contributions"]:
            raise invalid("Each supplied target needs active contributions.")
        count += len(target["contributions"])
        if count > MAX_CONTRIBUTIONS:
            raise invalid("Too many contributions.")
        entries = []
        for entry in target["contributions"]:
            kind = entry.get("kind") if isinstance(entry, dict) else None
            if not isinstance(kind, str) or kind not in _ALLOWED[name]:
                raise invalid(f"Unsupported contribution for {name}.")
            if kind in {"dlc", "gameconfig"}:
                keys = {"kind", "source"} | ({"packName"} if kind == "dlc" else set())
                entry = {**record(entry, keys, "contribution"), "source": ref(entry["source"])}
                if kind == "dlc":
                    token(entry["packName"], IDENTIFIER, "DLC pack name")
                    if entry["packName"].lower() == "gmm":
                        raise Rejected("conflict", "The DLC pack name gmm is reserved.")
            else:
                keys = {"kind", "files"} | ({"modelName", "sex", "streamed"} if kind == "ped" else set())
                entry = {**record(entry, keys, "contribution")}
                if kind == "ped":
                    token(entry["modelName"], IDENTIFIER, "Ped model name")
                    if entry["sex"] not in ("Male", "Female") or type(entry["streamed"]) is not bool:
                        raise invalid("Invalid Ped options.")
                if not isinstance(entry["files"], list) or not 0 < len(entry["files"]) <= MAX_JOB_FILES:
                    raise invalid("Invalid contribution file list.")
                parsed_files = []
                for model in entry["files"]:
                    model = record(model, {"name", "source"}, "model file")
                    parsed_files.append({"name": model_path(model["name"], kind, entry.get("streamed", False)),
                                         "source": ref(model["source"])})
                entry["files"] = parsed_files
            entries.append(entry)
        targets.append({"id": name, "baseline": baseline, "contributions": entries})
    if len(refs) > MAX_JOB_FILES or sum(v["size"] for v in refs.values()) > MAX_JOB_INPUT_BYTES:
        raise invalid("The job exceeds the input budget.")
    return targets, list(refs.values()), None
