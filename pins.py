"""Pin store and the pure logic of MCP Pin: fingerprint every MCP tool definition as Hermes registered
it, remember the approved one, and hold any tool whose definition changed (or that appeared later on a
server already pinned, or was forgotten) until the owner reviews and approves that exact definition.

Whether a tool is held is decided per process, from that process's own registry definition against the
shared pins; ``pending`` only records what is waiting for review (and what ``/mcppin diff`` shows).
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

MCP_PREFIX = "mcp__"
HELD_NOTE = ("[mcp-pin] Held: this tool changed since the owner approved it, so its new definition is "
             "hidden and calls are blocked until the owner reviews it.")
NEW_NOTE = "[mcp-pin] Held: new tool awaiting the owner's approval; calls are blocked until then."
EMPTY_PARAMS: dict = {"type": "object", "properties": {}}
PAGE_CHARS = 3000
_STORE_VERSION = 1


def is_mcp_tool(name: str) -> bool:
    return isinstance(name, str) and name.startswith(MCP_PREFIX) and "__" in name[len(MCP_PREFIX):]


def server_of(name: str) -> str:
    """``mcp__<server>__<tool>`` -> ``<server>`` (the sanitized name Hermes put on the wire)."""
    return name[len(MCP_PREFIX):].split("__", 1)[0]


def _canonical(value: Any) -> Any:
    """JSON-schema canonical form: ``required`` is a set, so its order must not change the hash."""
    if isinstance(value, dict):
        out = {k: _canonical(v) for k, v in value.items()}
        req = out.get("required")
        if isinstance(req, list) and all(isinstance(r, str) for r in req):
            out["required"] = sorted(req)
        return out
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def fingerprint(description: str, parameters: Any) -> str:
    blob = json.dumps({"description": description or "", "parameters": _canonical(parameters or {})},
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
    hash: str = ""

    def fp(self) -> str:
        return self.hash or fingerprint(self.description, self.parameters)


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
    """The pin file cannot be trusted (unreadable, or gone after it existed): MCP stays blocked."""


class ReviewMismatch(Exception):
    """The definitions waiting for approval are not the ones the owner reviewed."""


def review_code(entries: dict[str, dict]) -> str:
    """Short code binding an approval to exactly the pending definitions that were shown."""
    blob = "\n".join(f"{n}:{e.get('hash')}" for n, e in sorted(entries.items()))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


class PinStore:
    """``pins.json`` in the plugin's data directory, shared by every Hermes process of the profile.

    Reads are cached and reloaded when the file's (mtime_ns, size) changes. Every write happens under a
    lock file, on freshly re-read data, through a unique temp file that is fsynced and atomically
    replaced. If the file vanishes after this process has seen it, that is treated as tampering (fail
    closed) rather than as a first run; ``reset()`` is the explicit way to start over."""

    def __init__(self, path: Callable[[], Path], clock: Callable[[], float] = time.time) -> None:
        self._path_fn = path
        self._clock = clock
        self._lock = threading.RLock()
        self._key: tuple[int, int] | None = None
        self._data: dict[str, Any] = self._empty()
        self._seen_file = False
        self.error: str = ""

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _STORE_VERSION, "tools": {}, "pending": {}, "forgotten": []}

    # -- persistence ---------------------------------------------------------------------------

    def _path(self) -> Path:
        return Path(self._path_fn())

    def _load(self, force: bool = False) -> None:
        path = self._path()
        try:
            st = path.stat()
        except FileNotFoundError:
            if self._seen_file:
                self.error = (f"{path.name} disappeared; MCP tools are blocked so nothing is re-trusted silently. "
                              f"Restore it, or start over with /mcppin reset from a local session")
                raise StoreError(self.error) from None
            self._key, self.error, self._data = None, "", self._empty()
            return
        key = (st.st_mtime_ns, st.st_size)
        if key == self._key and not self.error and not force:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
                raise ValueError("not a pin file")
            data.setdefault("pending", {})
            data.setdefault("forgotten", [])
            self._data, self._key, self.error, self._seen_file = data, key, "", True
        except Exception as exc:
            self.error = f"{path.name} is unreadable ({type(exc).__name__}); MCP tools are blocked until it is fixed"
            raise StoreError(self.error) from exc

    @contextmanager
    def _writing(self):
        """Lock file + fresh read; yields the data to modify; saves on exit."""
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_name(path.name + ".lock")
        deadline = time.monotonic() + 10
        fd = None
        while fd is None:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 30:  # a crashed writer's lock
                        lock.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise StoreError(f"{lock.name} is held by another process; try again") from None
                time.sleep(0.02)
        try:
            self._load(force=True)
            yield self._data
            self._save(path)
        finally:
            os.close(fd)
            try:
                lock.unlink()
            except OSError:
                pass

    def _save(self, path: Path) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".pins-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(20):  # Windows refuses to replace a file another process has open
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        st = path.stat()
        self._key, self._seen_file, self.error = (st.st_mtime_ns, st.st_size), True, ""

    # -- queries -------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._load()
            return copy.deepcopy(self._data)

    # -- the core ------------------------------------------------------------------------------

    def _evaluate(self, data: dict, defs: list[McpDef], new_servers: str, new_tools: str):
        """Pure: (held {name: approved description or None}, mutations to apply)."""
        tools, pending = data["tools"], data["pending"]
        forgotten = set(data.get("forgotten") or [])
        known_servers = {p.get("server") or server_of(n) for n, p in tools.items()}
        held: dict[str, str | None] = {}
        pins: dict[str, dict] = {}
        records: dict[str, dict] = {}
        cleared: list[str] = []
        for d in defs:
            h = d.fp()
            pin = tools.get(d.name)
            if pin is not None:
                if pin["hash"] == h:
                    if d.name in pending and pending[d.name].get("hash") == h:
                        cleared.append(d.name)  # approved elsewhere, or the server reverted
                    continue
                held[d.name] = pin.get("description") or ""
                if pending.get(d.name, {}).get("hash") != h:
                    records[d.name] = {"kind": "changed", "hash": h, "server": d.server,
                                       "description": d.description, "parameters": d.parameters}
                continue
            policy = "hold" if d.name in forgotten else (new_tools if d.server in known_servers else new_servers)
            if policy == "pin":
                pins[d.name] = {"hash": h, "server": d.server, "description": d.description,
                                "parameters": d.parameters, "approved_by": "first use"}
                continue
            held[d.name] = None
            if pending.get(d.name, {}).get("hash") != h:
                records[d.name] = {"kind": "new", "hash": h, "server": d.server,
                                   "description": d.description, "parameters": d.parameters}
        return held, pins, records, cleared

    def observe(self, defs: list[McpDef], *, new_servers: str = "pin", new_tools: str = "hold"):
        """Check the current definitions. Returns ``(held, events)``: held maps each held tool to its
        approved description (None for a tool never approved)."""
        with self._lock:
            self._load()
            held, pins, records, cleared = self._evaluate(self._data, defs, new_servers, new_tools)
            if not (pins or records or cleared):
                return held, []
            with self._writing() as data:  # re-evaluate on fresh data under the lock
                held, pins, records, cleared = self._evaluate(data, defs, new_servers, new_tools)
                now = int(self._clock())
                for n, p in pins.items():
                    data["tools"][n] = {**p, "pinned_at": now}
                    data["pending"].pop(n, None)
                for n, r in records.items():
                    data["pending"][n] = {**r, "detected_at": now}
                for n in cleared:
                    data["pending"].pop(n, None)
            events = ([{"event": "pinned", "tool": n} for n in pins]
                      + [{"event": r["kind"], "tool": n} for n, r in records.items()]
                      + [{"event": "cleared", "tool": n} for n in cleared])
            return held, events

    # -- owner actions -------------------------------------------------------------------------

    def pending_matching(self, selector: str) -> dict[str, dict]:
        with self._lock:
            self._load()
            return {n: copy.deepcopy(p) for n, p in self._data["pending"].items()
                    if _matches(n, selector, p.get("server", ""))}

    def approve(self, selector: str, code: str, by: str) -> list[str]:
        """Approve the pending definitions matching *selector*, only if *code* is the review code of
        exactly those definitions (so nothing the owner did not see gets pinned)."""
        with self._lock:
            with self._writing() as data:
                matched = {n: p for n, p in data["pending"].items() if _matches(n, selector, p.get("server", ""))}
                if not matched:
                    return []
                if code.strip().lower() != review_code(matched):
                    raise ReviewMismatch(
                        f"The held definitions for '{selector}' are not the ones reviewed with that code "
                        f"(they may have changed since). Run /mcppin diff {selector} again.")
                now = int(self._clock())
                forgotten = set(data.get("forgotten") or [])
                for n, p in matched.items():
                    data["pending"].pop(n)
                    data["tools"][n] = {"hash": p["hash"], "server": p.get("server") or server_of(n),
                                        "description": p["description"], "parameters": p["parameters"],
                                        "pinned_at": now, "approved_by": by}
                    forgotten.discard(n)
                data["forgotten"] = sorted(forgotten)
                return sorted(matched)

    def forget(self, selector: str) -> list[str]:
        """Drop pins matching *selector*. A forgotten tool is held as new the next time it is seen,
        whatever the policy, so forgetting never re-trusts a definition by itself."""
        with self._lock:
            with self._writing() as data:
                entries = {**data["tools"], **data["pending"]}
                names = sorted(n for n, p in entries.items() if _matches(n, selector, p.get("server", "")))
                for n in names:
                    data["tools"].pop(n, None)
                    data["pending"].pop(n, None)
                data["forgotten"] = sorted(set(data.get("forgotten") or []) | set(names))
                return names

    def reset(self) -> None:
        """Start over: every tool is evaluated as on a first run (new servers per policy)."""
        with self._lock:
            self._seen_file = False  # an explicit reset may follow a vanished file
            with self._writing() as data:
                data.clear()
                data.update(self._empty())


def _matches(name: str, selector: str, server: str = "") -> bool:
    selector = selector.strip()
    if selector in ("all", name):
        return True
    servers = {server, server_of(name)} - {""}
    candidates = set()
    for s in servers:
        candidates |= {s, f"mcp-{s}", f"{MCP_PREFIX}{s}", s.replace("-", "_"), s.replace("_", "-")}
    return selector in candidates


def _escape(text: str) -> str:
    """Make invisible and control characters visible (bidi overrides, zero-width, tags, ANSI)."""
    out = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
        elif unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs") or 0xE0000 <= ord(ch) <= 0xE007F:
            out.append(f"\\u{{{ord(ch):04x}}}")
        else:
            out.append(ch)
    return "".join(out)


def render_review(pending: dict[str, dict], pins: dict[str, dict]) -> str:
    """Full line diff of every matched pending definition against its approved one (nothing cut)."""
    def lines(d: dict | None) -> list[str]:
        if d is None:
            return []
        return [*_escape(d.get("description") or "").splitlines(), "--- parameters ---",
                *_escape(json.dumps(d.get("parameters") or {}, indent=2, sort_keys=True, ensure_ascii=False)).splitlines()]
    blocks = []
    for name in sorted(pending):
        old = pins.get(name)
        diff = list(difflib.unified_diff(lines(old), lines(pending[name]), "approved", "now", lineterm="", n=2))
        body = "\n".join(diff[2:] if len(diff) > 2 else diff) or "(no textual difference)"
        head = f"== {name}: {'changed since approval' if old else 'new tool, never approved'}"
        blocks.append(f"{head}\n{body}")
    return "\n\n".join(blocks)


def paginate(text: str, page: int, size: int = PAGE_CHARS) -> tuple[str, int, int]:
    """(page text, page number, page count); pages break on line boundaries."""
    pages, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:  # one huge line: split it, never drop it
            if cur:
                pages.append(cur)
                cur = ""
            pages.append(line[:size])
            line = line[size:]
        if cur and len(cur) + 1 + len(line) > size:
            pages.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    pages.append(cur)
    page = max(1, min(page, len(pages)))
    return pages[page - 1], page, len(pages)
