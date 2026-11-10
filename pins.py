"""Pin store and the pure logic of MCP Pin: fingerprint every MCP tool definition the model is shown,
remember the approved one, and hold any tool whose definition changed (or that appeared later on a
server already pinned) until the owner approves it.

Only what reaches the model is fingerprinted: the tool's description and its parameter schema, as they
appear in the provider request. That is exactly the text a poisoned or rug-pulled server would change.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

MCP_PREFIX = "mcp__"
HELD_NOTE = "[mcp-pin] Held: this tool changed since the owner approved it, so its new definition is hidden and calls are blocked until the owner reviews it."
NEW_NOTE = "[mcp-pin] Held: new tool awaiting the owner's approval; calls are blocked until then."
EMPTY_PARAMS: dict = {"type": "object", "properties": {}}
_STORE_VERSION = 1


def is_mcp_tool(name: str) -> bool:
    return isinstance(name, str) and name.startswith(MCP_PREFIX) and "__" in name[len(MCP_PREFIX):]


def server_of(name: str) -> str:
    """``mcp__<server>__<tool>`` -> ``<server>`` (the sanitized name Hermes put on the wire)."""
    return name[len(MCP_PREFIX):].split("__", 1)[0]


def fingerprint(description: str, parameters: Any) -> str:
    blob = json.dumps({"description": description or "", "parameters": parameters or {}},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class McpDef:
    """One MCP tool as Hermes registered it: the canonical definition every surface derives from
    (eager tool list, the tool_search catalog, tool_describe), independent of provider wire format."""

    name: str
    server: str
    description: str
    parameters: Any


@dataclass
class ToolDef:
    """One tool definition as found in a provider request, with a way to write it back in place."""

    name: str
    description: str
    parameters: Any
    _set: Callable[[str, Any], None] = field(repr=False, default=lambda d, p: None)

    @property
    def hash(self) -> str:
        return fingerprint(self.description, self.parameters)

    def replace(self, description: str, parameters: Any) -> None:
        self._set(description, parameters)
        self.description, self.parameters = description, copy.deepcopy(parameters)


def iter_tool_defs(request: Any) -> Iterator[ToolDef]:
    """Every tool definition in a provider request, whatever the wire shape:

    - chat completions: ``tools[] = {"type": "function", "function": {name, description, parameters}}``
    - Responses / Codex: ``tools[] = {"type": "function", name, description, parameters}``
    - Anthropic messages: ``tools[] = {name, description, input_schema}``
    - Bedrock Converse: ``toolConfig.tools[] = {"toolSpec": {name, description, inputSchema: {json}}}``
    """
    if not isinstance(request, dict):
        return
    for item in request.get("tools") or []:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if isinstance(fn, dict) and "name" in fn:
            yield ToolDef(str(fn.get("name")), fn.get("description") or "", fn.get("parameters") or {},
                          _setter(fn, "description", "parameters"))
        elif "input_schema" in item and "name" in item:
            yield ToolDef(str(item["name"]), item.get("description") or "", item.get("input_schema") or {},
                          _setter(item, "description", "input_schema"))
        elif "name" in item and "parameters" in item:
            yield ToolDef(str(item["name"]), item.get("description") or "", item.get("parameters") or {},
                          _setter(item, "description", "parameters"))
    tool_config = request.get("toolConfig")
    if isinstance(tool_config, dict):
        for item in tool_config.get("tools") or []:
            spec = item.get("toolSpec") if isinstance(item, dict) else None
            if isinstance(spec, dict) and "name" in spec:
                schema = spec.get("inputSchema") if isinstance(spec.get("inputSchema"), dict) else {}
                yield ToolDef(str(spec["name"]), spec.get("description") or "", schema.get("json") or {},
                              _bedrock_setter(spec))


def _setter(obj: dict, desc_key: str, params_key: str) -> Callable[[str, Any], None]:
    def put(description: str, parameters: Any) -> None:
        obj[desc_key] = description
        obj[params_key] = copy.deepcopy(parameters)
    return put


def _bedrock_setter(spec: dict) -> Callable[[str, Any], None]:
    def put(description: str, parameters: Any) -> None:
        spec["description"] = description
        spec["inputSchema"] = {"json": copy.deepcopy(parameters)}
    return put


class StoreError(Exception):
    """The pin file exists but cannot be read: MCP tools are blocked rather than re-pinned."""


class PinStore:
    """``pins.json`` in the plugin's data directory. Reloaded when another process changed it (the CLI
    approving while the gateway runs); written atomically."""

    def __init__(self, path: Callable[[], Path], clock: Callable[[], float] = time.time) -> None:
        self._path_fn = path
        self._clock = clock
        self._lock = threading.RLock()
        self._mtime: float | None = None
        self._data: dict[str, Any] = {"version": _STORE_VERSION, "tools": {}, "pending": {}}
        self.error: str = ""

    # -- persistence ---------------------------------------------------------------------------

    def _path(self) -> Path:
        return Path(self._path_fn())

    def _load(self) -> None:
        path = self._path()
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self._mtime, self.error = None, ""
            self._data = {"version": _STORE_VERSION, "tools": {}, "pending": {}}
            return
        if mtime == self._mtime and not self.error:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
                raise ValueError("not a pin file")
            data.setdefault("pending", {})
            self._data, self._mtime, self.error = data, mtime, ""
        except Exception as exc:
            self.error = f"{path.name} is unreadable ({type(exc).__name__}); MCP tools are blocked until it is fixed or removed"
            raise StoreError(self.error) from exc

    def _save(self) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        self._mtime = path.stat().st_mtime

    # -- queries -------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._load()
            return copy.deepcopy(self._data)

    def state_of(self, name: str) -> str:
        """``pinned`` | ``held`` | ``unknown`` (never seen in a request)."""
        with self._lock:
            self._load()
            if name in self._data["pending"]:
                return "held"
            return "pinned" if name in self._data["tools"] else "unknown"

    # -- the core: observe what the model is about to be shown ---------------------------------

    def observe(self, defs: list["McpDef"], *, new_servers: str = "pin", new_tools: str = "hold") -> list[dict]:
        """Compare the current MCP tool definitions (from Hermes's tool registry) with their pins: pin
        unseen tools per policy and record changed ones as pending. Returns the events."""
        events: list[dict] = []
        if not defs:
            return events
        with self._lock:
            self._load()
            tools, pending = self._data["tools"], self._data["pending"]
            known_servers = {p.get("server") or server_of(n) for n, p in tools.items()}
            now = int(self._clock())
            dirty = False
            for d in defs:
                h = fingerprint(d.description, d.parameters)
                pin = tools.get(d.name)
                if pin is not None:
                    if pin["hash"] == h:
                        if d.name in pending:  # the server went back to the approved definition
                            del pending[d.name]
                            events.append({"event": "restored", "tool": d.name})
                            dirty = True
                        continue
                    if pending.get(d.name, {}).get("hash") != h:
                        pending[d.name] = {"kind": "changed", "hash": h, "server": d.server,
                                           "description": d.description, "parameters": d.parameters,
                                           "detected_at": now}
                        events.append({"event": "changed", "tool": d.name})
                        dirty = True
                    continue
                policy = new_tools if d.server in known_servers else new_servers
                if policy == "pin":
                    tools[d.name] = {"hash": h, "server": d.server, "description": d.description,
                                     "parameters": d.parameters, "pinned_at": now, "approved_by": "first use"}
                    pending.pop(d.name, None)
                    events.append({"event": "pinned", "tool": d.name})
                    dirty = True
                elif pending.get(d.name, {}).get("hash") != h:
                    pending[d.name] = {"kind": "new", "hash": h, "server": d.server,
                                       "description": d.description, "parameters": d.parameters,
                                       "detected_at": now}
                    events.append({"event": "new", "tool": d.name})
                    dirty = True
            if dirty:
                self._save()
        return events

    def held(self) -> dict[str, dict]:
        """Pending (held) tools: name -> {kind, approved description or None}."""
        with self._lock:
            self._load()
            return {n: {"kind": p.get("kind"), "approved": (self._data["tools"].get(n) or {}).get("description")}
                    for n, p in self._data["pending"].items()}

    # -- owner actions -------------------------------------------------------------------------

    def approve(self, selector: str, by: str) -> list[str]:
        """Approve pending tools matching *selector*: a full tool name, ``<server>`` or ``all``."""
        with self._lock:
            self._load()
            pending = self._data["pending"]
            names = [n for n, p in pending.items() if _matches(n, selector, p.get("server", ""))]
            now = int(self._clock())
            for n in names:
                p = pending.pop(n)
                self._data["tools"][n] = {"hash": p["hash"], "server": p.get("server") or server_of(n),
                                          "description": p["description"],
                                          "parameters": p["parameters"], "pinned_at": now, "approved_by": by}
            if names:
                self._save()
            return sorted(names)

    def forget(self, selector: str) -> list[str]:
        """Drop pins (and pending entries) matching *selector*; they are re-evaluated on next sight."""
        with self._lock:
            self._load()
            entries = {**self._data["tools"], **self._data["pending"]}
            names = sorted(n for n, p in entries.items() if _matches(n, selector, p.get("server", "")))
            for n in names:
                self._data["tools"].pop(n, None)
                self._data["pending"].pop(n, None)
            if names:
                self._save()
            return names


def _matches(name: str, selector: str, server: str = "") -> bool:
    selector = selector.strip()
    if selector in ("all", name):
        return True
    server = server or server_of(name)
    return selector in (server, f"mcp-{server}", f"{MCP_PREFIX}{server}")


def render_diff(old: dict | None, new: dict, width: int = 3500) -> str:
    """Line diff of description and parameter schema between the pinned and the pending definition."""
    def lines(d: dict | None) -> list[str]:
        if d is None:
            return []
        return [*(d.get("description") or "").splitlines(), "--- parameters ---",
                *json.dumps(d.get("parameters") or {}, indent=2, sort_keys=True, ensure_ascii=False).splitlines()]
    diff = list(difflib.unified_diff(lines(old), lines(new), "approved", "now", lineterm="", n=2))
    text = "\n".join(diff[2:] if len(diff) > 2 else diff) or "(no textual difference)"
    return text if len(text) <= width else text[:width] + "\n… (truncated)"
