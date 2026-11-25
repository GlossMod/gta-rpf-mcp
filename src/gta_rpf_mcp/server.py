from __future__ import annotations

import json
import sys

from . import __version__
from .backend import status as backend_status
from .prepare import prepare
from .protocol import JOB_ID, Rejected, strict_json, token
from .workspace import workspace_parent

_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_LINE = 1024 * 1024
_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_TOOLS = [
    {"name": "gta_rpf_prepare", "description": "Prepare one manager-created GTA V Legacy archive job; never apply it to a game.",
     "inputSchema": {**_EMPTY, "properties": {"jobId": {"type": "string", "pattern": "^[0-9a-f]{32}$", "minLength": 32, "maxLength": 32}},
                     "required": ["jobId"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
    {"name": "gta_rpf_status", "description": "Check whether the prescribed local Legacy release and template are verified and available.",
     "inputSchema": _EMPTY,
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
]


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
                value = backend_status(workspace_parent())
            elif name == "gta_rpf_prepare" and args.keys() == {"jobId"}:
                job_id = token(args["jobId"], JOB_ID, "job ID")
                value = prepare(workspace_parent(), job_id)
            else:
                raise RpcError(-32602, "Unknown tool or invalid tool arguments")
        except Rejected as exc:
            value = {"status": "error", "code": exc.code, "message": str(exc)}
        except (OSError, ValueError, RecursionError):
            value = {"status": "error", "code": "workspace_io", "message": "The workspace or verified tool could not be used."}
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
