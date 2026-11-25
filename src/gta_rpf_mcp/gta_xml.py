from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from .protocol import IDENTIFIER, Rejected
from .workspace import read_limited

XML_MAX = 8_000_000
DLCLIST = "common/data/dlclist.xml"
GAMECONFIG = "common/data/gameconfig.xml"
PED_META = "data/pedmodelinfo.meta"
_SELECT_PACK = re.compile(r"dlcpacks:[/\\]+([A-Za-z0-9_-]+)[/\\]*\Z", re.I | re.ASCII)


def parse_xml(data, label, tag=None):
    try:
        raw = data.decode("utf-8-sig") if isinstance(data, bytes) else data.removeprefix("\ufeff")
        if (len(raw.encode("utf-8")) > XML_MAX or "\x00" in raw
                or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I)):
            raise ValueError("forbidden XML")
        declaration = re.match(r"\A<\?xml\s+[^?]*\bencoding\s*=\s*['\"]([^'\"]+)", raw, re.I)
        if declaration and declaration[1].lower() != "utf-8":
            raise ValueError("unexpected XML encoding")
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        parsed = ET.fromstring(raw, parser=parser)
        if tag is not None and parsed.tag != tag:
            raise ValueError("unexpected XML root")
        # The serializer must not rewrite namespace prefixes used by game tools.
        if any(isinstance(n.tag, str) and ("{" in n.tag or any("{" in k for k in n.attrib)) for n in parsed.iter()):
            raise ValueError("unexpected XML namespace")
        pending = [(parsed, 1)]
        count = 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if depth > 96 or count > 100_000:
                raise ValueError("XML structure exceeds its budget")
            pending.extend((child, depth + 1) for child in node)
        return parsed
    except (ValueError, TypeError, UnicodeError, ET.ParseError) as exc:
        raise Rejected("invalid_xml", f"Invalid or unsupported XML in {label}.") from exc


def single(node, name, label):
    matched = node.findall(name)
    if len(matched) != 1:
        raise Rejected("invalid_xml", f"Expected one {name} in {label}.")
    return matched[0]


def xml_bytes(tree):
    raw = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
    if len(raw) > XML_MAX:
        raise Rejected("invalid_xml", "Edited XML exceeds its size limit.")
    return raw


def document(path: Path, label, tag=None):
    return parse_xml(read_limited(path, XML_MAX), label, tag)


def textual_xml(data):
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    return data.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def setup_pack(data):
    root = parse_xml(data, "setup2.xml")
    node = single(root, ".//nameHash", "setup2.xml")
    value = (node.text or "").strip()
    if len(node) or not IDENTIFIER.fullmatch(value):
        raise Rejected("invalid_dlc", "The DLC archive has an invalid setup2.xml nameHash.")
    return value.lower()


def dlclist(base, add_names):
    # Some older Legacy update builds use a differently named outer element.
    root = parse_xml(base, "dlclist.xml")
    paths = single(root, "Paths", "dlclist.xml")
    present = {}
    for item in paths.findall("Item"):
        normalized = _SELECT_PACK.fullmatch((item.text or "").strip())
        if normalized:
            key = normalized[1].lower()
            present[key] = present.get(key, 0) + 1
    added = False
    for name in add_names:
        if present.get(name, 0) > 1:
            raise Rejected("conflict", "A requested DLC pack has duplicate mounts in the original dlclist.xml.")
        if name not in present:
            ET.SubElement(paths, "Item").text = f"dlcpacks:/{name}/"
            present[name] = 1
            added = True
    return xml_bytes(root) if added else None


def inventory(paths):
    normalized = {}
    for path in paths:
        key = path.replace("\\", "/").lower()
        parts = key.split("/")
        if not key or len(parts) > 32 or any(not p or p in {".", ".."} or ":" in p or any(ord(c) < 32 for c in p) for p in parts):
            raise Rejected("verification_failed", "An RPF inventory contains an unsafe entry name.")
        if key in normalized:
            raise Rejected("conflict", "An RPF inventory contains case-insensitive duplicate entries.")
        normalized[key] = path
    return normalized


def vehicle_models(paths):
    matches = OrderedDict()
    for path in paths:
        parent, _, name = path.rpartition("/")
        stem, sep, ext = name.rpartition(".")
        if sep and ext in {"yft", "ytd"} and not stem.endswith("_hi") and IDENTIFIER.fullmatch(stem):
            matches.setdefault((parent, stem), set()).add(ext)
    return list(dict.fromkeys(stem for (_, stem), exts in matches.items() if exts == {"yft", "ytd"}))


def update_menyoo(baseline, models):
    old = baseline.read_bytes() if baseline is not None else None
    root = parse_xml(old, "AddedVehicleModels.xml", "AddedVehicleModels") if old is not None else ET.Element("AddedVehicleModels")
    seen = set()
    for entry in root.findall("VehModel"):
        model = (entry.get("modelName") or "").strip().lower()
        if not model:
            raise Rejected("invalid_xml", "An existing Menyoo entry has no modelName.")
        seen.add(model)
    added = False
    for model in models:
        if model not in seen:
            ET.SubElement(root, "VehModel", {"modelName": model, "customName": model})
            seen.add(model)
            added = True
    return xml_bytes(root) if added or old is None else old


def build_ped_item(name, sex, streamed):
    male = sex == "Male"
    movement = "move_m@generic" if male else "move_f@generic"
    pedtype = "CIVMALE" if male else "CIVFEMALE"
    gender = "male" if male else "female"
    prefix = "M" if male else "F"
    item = ET.Element("Item")
    values = {
        "Name": name, "PropsName": "null", "ClipDictionaryName": movement,
        "BlendShapeFileName": "null", "ExpressionSetName": f"expr_set_ambient_{gender}",
        "ExpressionDictionaryName": "null", "ExpressionName": "null", "Pedtype": pedtype,
        "MovementClipSet": movement, "StrafeClipSet": "move_ped_strafing",
        "MovementToStrafeClipSet": "move_ped_to_strafe", "InjuredStrafeClipSet": "move_strafe_injured",
        "FullBodyDamageClipSet": "dam_ko", "AdditiveDamageClipSet": "dam_ad",
        "DefaultGestureClipSet": f"ANIM_GROUP_GESTURE_{prefix}_GENERIC",
        "FacialClipsetGroupName": f"facial_clipset_group_gen_{gender}", "DefaultVisemeClipSet": f"ANIM_GROUP_VISEMES_{prefix}_LO",
        "SidestepClipSet": "CLIP_SET_ID_INVALID", "PoseMatcherName": sex, "PoseMatcherProneName": f"{sex}_prone",
        "GetupSetHash": "NMBS_SLOW_GETUPS", "CreatureMetadataName": "null", "DecisionMakerName": "DEFAULT",
        "MotionTaskDataSetName": "STANDARD_PED", "DefaultTaskDataSetName": "STANDARD_PED", "PedCapsuleName": f"STANDARD_{gender.upper()}",
        "PedLayoutName": "", "PedComponentSetName": "", "PedComponentClothName": "", "PedIKSettingsName": "", "TaskDataName": "",
        "IsStreamedGfx": (str(streamed).lower(),), "AmbulanceShouldRespondTo": ("true",), "CanRideBikeWithNoHelmet": ("false",),
        "CanSpawnInCar": ("true",), "IsHeadBlendPed": ("false",), "bOnlyBulkyItemVariations": ("false",),
        "RelationshipGroup": pedtype, "NavCapabilitiesName": "STANDARD_PED", "PerceptionInfo": "DEFAULT_PERCEPTION",
        "DefaultBrawlingStyle": "BS_AI", "DefaultUnarmedWeapon": "WEAPON_UNARMED",
        "Personality": "MERRYWEATHER" if male else "Streamed_Female", "CombatInfo": "ARMY" if male else "DEFAULT",
        "VfxInfoName": "VFXPEDINFO_HUMAN_GENERIC", "AmbientClipsForFlee": "FLEE", "Radio1": "RADIO_GENRE_PUNK", "Radio2": "RADIO_GENRE_JAZZ",
        "FUpOffset": ("0.000000",), "RUpOffset": ("0.000000",), "FFrontOffset": ("0.000000",), "RFrontOffset": ("0.147000",),
        "MinActivationImpulse": ("20.000000",), "Stubble": ("0.000000",), "HDDist": ("3.000000",), "TargetingThreatModifier": ("1.000000",),
        "KilledPerceptionRangeModifer": ("-1.000000",), "Sexiness": "", "Age": ("0",), "MaxPassengersInCar": ("0",), "ExternallyDrivenDOFs": "",
        "PedVoiceGroup": "BAR_PERSON_PVG", "AnimalAudioObject": "", "AbilityType": "SAT_NONE", "ThermalBehaviour": "TB_WARM",
        "SuperlodType": "SLOD_HUMAN", "ScenarioPopStreamingSlot": "SCENARIO_POP_STREAMING_NORMAL", "DefaultSpawningPreference": "DSP_NORMAL",
        "DefaultRemoveRangeMultiplier": ("1.000000",), "AllowCloseSpawning": ("false",),
    }
    for key, value in values.items():
        if isinstance(value, tuple):
            ET.SubElement(item, key, {"value": value[0]})
        else:
            ET.SubElement(item, key).text = value
    return item


def add_peds(root, peds):
    bucket = single(root, "InitDatas", "pedmodelinfo.meta")
    names = set()
    for item in bucket.findall("Item"):
        node = single(item, "Name", "pedmodelinfo.meta")
        name = (node.text or "").strip().lower()
        if not name or name in names:
            raise Rejected("conflict", "The GMM template contains duplicate or missing Ped names.")
        names.add(name)
    for name, entry in peds.items():
        if name in names:
            raise Rejected("conflict", "A contributed Ped would replace an entry in the immutable template.")
        names.add(name)
        bucket.append(build_ped_item(name, entry["sex"], entry["streamed"]))
    return xml_bytes(root)
