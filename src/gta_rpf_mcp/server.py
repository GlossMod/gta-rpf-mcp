from __future__ import annotations

import json
import sys

from . import __version__
from .archive_tools import ARCHIVE_TOOLS, call_archive_tool
from .backend import status as backend_status
from .job_info import describe_job
from .prepare import prepare
from .protocol import JOB_ID, Rejected, strict_json, token
from .workspace import workspace_parent

_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_LINE = 1024 * 1024
_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_JOB_ID_SCHEMA = {
    "type": "string", "pattern": "^[0-9a-f]{32}$", "minLength": 32, "maxLength": 32,
    "description": "The jobId returned by AMM's native prepare_gta_rpf_job tool for this game.",
}
_JOB_SCHEMA = {**_EMPTY, "properties": {"jobId": _JOB_ID_SCHEMA}, "required": ["jobId"]}
_READ_HINTS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_ARCHIVE_TOOL_NAMES = {tool["name"] for tool in ARCHIVE_TOOLS}
# The service is shared by both adapters. The staged manifest determines the edition.
_JOB_TOOLS = {
    "gta_rpf_prepare": (None, "Prepare any AMM archive job, including uninstall rebuilds and empty jobs."),
    "gta_rpf_inspect_dlc": ("inspectDlc", "Legacy and Enhanced: read the staged DLC's setup2.xml and return its pack name."),
    "gta_rpf_register_dlc": ("dlc", "Legacy and Enhanced: register a DLC in the staged update.rpf dlclist.xml."),
    "gta_rpf_replace_gameconfig": ("gameconfig", "Legacy and Enhanced: replace gameconfig.xml in the staged update.rpf."),
    "gta_rpf_build_vehicle_pack": ("vehicle", "Legacy only: build the staged GMM DLC with the job's vehicle models."),
    "gta_rpf_build_ped_pack": ("ped", "Legacy only: build the staged GMM DLC with the job's Ped models and metadata."),
    "gta_rpf_update_menyoo": ("menyoo", "Legacy only: rebuild the staged Menyoo vehicle list from the job's models."),
}
_TOOLS = [
    {"name": name, "description": description + " Call once per AMM jobId. All targets in that job are staged together.",
     "inputSchema": _JOB_SCHEMA,
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}}
    for name, (_, description) in _JOB_TOOLS.items()
] + [
    {"name": "gta_rpf_status", "description": "Report the bundled Python RPF7 backend and supported jobs for each GTA V edition.",
     "inputSchema": _EMPTY,
     "annotations": _READ_HINTS},
    {"name": "gta_rpf_describe_job",
     "description": "Discover a staged job's edition, available preparation tools and manifest input paths for archive tools. Pages start at offset 0.",
     "inputSchema": {**_JOB_SCHEMA, "properties": {"jobId": _JOB_ID_SCHEMA,
                     "offset": {"type": "integer", "minimum": 0, "default": 0},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 32}}},
     "annotations": _READ_HINTS},
] + ARCHIVE_TOOLS


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _json(record):
    return json.dumps(record, ensure_ascii=True, separators=(",", ":"))


def _params(msg, *, default=None):
    value = msg.get("params", default)
    if not isinstance(value, dict):
        raise RpcError(-32602, "Invalid params")
    return value


class Server:
    def __init__(self):
        self.version = None
        self.initialized = False

    def tool(self, name, args):
        try:
            if name == "gta_rpf_status" and not args:
                value = backend_status()
            elif name in _ARCHIVE_TOOL_NAMES:
                value = call_archive_tool(name, args)
            elif name == "gta_rpf_describe_job" and "jobId" in args and not args.keys() - {"jobId", "offset", "limit"}:
                job_id = token(args["jobId"], JOB_ID, "job ID")
                offset, limit = args.get("offset", 0), args.get("limit", 32)
                if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
                    raise RpcError(-32602, "Invalid page bounds")
                value, operations = describe_job(workspace_parent(), job_id, offset, limit)
                value["preparationTools"] = [tool for tool, (op, _) in _JOB_TOOLS.items()
                                             if op is None or op in operations]
            elif name in _JOB_TOOLS and args.keys() == {"jobId"}:
                job_id = token(args["jobId"], JOB_ID, "job ID")
                value = prepare(workspace_parent(), job_id, operation=_JOB_TOOLS[name][0])
            else:
                raise RpcError(-32602, "Unknown tool or invalid tool arguments")
        except Rejected as exc:
            value = {"status": "error", "code": exc.code, "message": str(exc)}
        except (OSError, ValueError, RecursionError):
            value = {"status": "error", "code": "workspace_io", "message": "The workspace or archive could not be used."}
        result = {"content": [{"type": "text", "text": _json(value)}], "isError": value.get("status") == "error"}
        if self.version in _PROTOCOLS[:2]:
            result["structuredContent"] = value
        return result

    def request(self, msg, method):
        if method == "ping":
            return {}
        if method == "initialize":
            p = _params(msg)
            proposed = p.get("protocolVersion")
            if self.version is not None or not isinstance(proposed, str):
                raise RpcError(-32602, "Invalid initialize request")
            self.version = proposed if proposed in _PROTOCOLS else _PROTOCOLS[0]
            return {"protocolVersion": self.version, "serverInfo": {"name": "gta-rpf-mcp", "version": __version__},
                    "capabilities": {"tools": {}}}
        if not self.initialized:
            raise RpcError(-32002, "Server not initialized")
        if method == "tools/list":
            if set(_params(msg, default={})) - {"_meta"}:
                raise RpcError(-32602, "Invalid list parameters")
            return {"tools": _TOOLS}
        if method == "tools/call":
            p = _params(msg)
            if set(p) - {"name", "arguments", "_meta"} or not isinstance(p.get("name"), str) or not isinstance(p.get("arguments", {}), dict):
                raise RpcError(-32602, "Invalid tool parameters")
            return self.tool(p["name"], p.get("arguments", {}))
        raise RpcError(-32601, "Method not found")

    def message(self, msg):
        valid_id = isinstance(msg, dict) and ("id" not in msg or type(msg["id"]) in {str, int, type(None)})
        if not valid_id or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str) or set(msg) - {"jsonrpc", "id", "method", "params"}:
            return _error(None, -32600, "Invalid Request")
        method = msg["method"]
        if "id" not in msg:
            if method == "notifications/initialized" and self.version is not None:
                self.initialized = True
            return None
        try:
            return {"jsonrpc": "2.0", "id": msg["id"], "result": self.request(msg, method)}
        except RpcError as exc:
            return _error(msg["id"], exc.code, str(exc))


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def run(source, dest):
    server = Server()
    while line := source.readline(_LINE + 1):
        if len(line) > _LINE:
            while not line.endswith(b"\n"):
                line = source.readline(_LINE + 1)
                if not line:
                    break
            result = _error(None, -32700, "Oversized JSON message")
        else:
            try:
                msg = strict_json(line)
            except (ValueError, UnicodeError, RecursionError):
                result = _error(None, -32700, "Parse error")
            else:
                result = server.message(msg)
        if result is not None:
            dest.write(_json(result).encode("ascii") + b"\n")
            dest.flush()
    return 0


def main():
    try:
        return run(sys.stdin.buffer, sys.stdout.buffer)
    except BrokenPipeError:
        return 0
