"""The ``/mcppin`` slash command."""

from __future__ import annotations

import time
from typing import Any

from .pins import render_diff, server_of
from .runtime import Runtime

USAGE = (
    "Usage:\n"
    "  /mcppin                     pinned servers and tools held for review\n"
    "  /mcppin diff <tool>         what changed in a held tool\n"
    "  /mcppin approve <tool|server|all>   accept the new definition (owner)\n"
    "  /mcppin forget <tool|server>        drop a pin; it is checked again on next use (owner)"
)


def _ago(ts: Any) -> str:
    try:
        s = max(0, int(time.time()) - int(ts))
    except Exception:
        return "?"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit} ago"
    return f"{s}s ago"


class Commands:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime

    def handle(self, raw_args: str = "", *, platform: str = "", source: Any = None, **_: Any) -> str:
        parts = (raw_args or "").split()
        sub = parts[0].lower() if parts else ""
        try:
            if sub in ("", "status", "list"):
                return self._status()
            if sub == "diff" and len(parts) == 2:
                return self._diff(parts[1])
            if sub in ("approve", "forget") and len(parts) == 2:
                who = self.rt.caller(source, platform)
                if not self.rt.may_approve(who):
                    return ("Only the owner can do that: run it from a local session, or be listed in Hermes's "
                            "allow_admin_from (group_allow_admin_from in group chats).")
                if sub == "approve":
                    names = self.rt.store.approve(parts[1], by=who.label())
                    if not names:
                        return f"Nothing held matches '{parts[1]}'. /mcppin lists what is held."
                    self.rt.degraded = ""
                    return "Approved and pinned:\n" + "\n".join(f"  {n}" for n in names)
                names = self.rt.store.forget(parts[1])
                return ("Forgot:\n" + "\n".join(f"  {n}" for n in names)) if names else f"No pin matches '{parts[1]}'."
        except Exception as exc:
            return f"mcp-pin: {exc}"
        return USAGE

    def _status(self) -> str:
        self.rt.sync()
        data = self.rt.store.snapshot()
        tools, pending = data.get("tools", {}), data.get("pending", {})
        lines: list[str] = []
        if self.rt.degraded:
            lines.append(f"⚠ MCP tools are blocked: {self.rt.degraded}")
        if pending:
            lines.append(f"Held for review ({len(pending)}):")
            for name in sorted(pending):
                p = pending[name]
                lines.append(f"  {name}  [{p.get('kind', '?')}, {_ago(p.get('detected_at'))}]")
            lines.append("Inspect with /mcppin diff <tool>, accept with /mcppin approve <tool|server|all>.")
        servers: dict[str, int] = {}
        for name in tools:
            servers[server_of(name)] = servers.get(server_of(name), 0) + 1
        if servers:
            lines.append("Pinned: " + ", ".join(f"{s} ({n} tool{'s' if n != 1 else ''})" for s, n in sorted(servers.items())))
        if not lines:
            lines.append("No MCP tools seen yet. Tools are pinned the first time the model is shown them.")
        return "\n".join(lines)

    def _diff(self, name: str) -> str:
        self.rt.sync()
        data = self.rt.store.snapshot()
        p = data.get("pending", {}).get(name)
        if p is None:
            return f"'{name}' is not held. /mcppin lists what is."
        old = data.get("tools", {}).get(name)
        head = f"{name}: {'changed since approval' if old else 'new tool on a pinned server'}"
        return f"{head}\n{render_diff(old, p)}"
