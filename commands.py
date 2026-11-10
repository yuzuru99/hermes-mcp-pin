"""The ``/mcppin`` slash command."""

from __future__ import annotations

import time
from typing import Any

from .pins import ReviewMismatch, paginate, render_review, review_code, server_of
from .runtime import Runtime

USAGE = (
    "Usage:\n"
    "  /mcppin                              pinned servers and tools held for review\n"
    "  /mcppin diff <tool|server|all> [page]   what changed, with the approval code\n"
    "  /mcppin approve <tool|server|all> <code>  accept exactly what diff showed (owner)\n"
    "  /mcppin forget <tool|server|all>     drop pins; those tools are held again on next use (owner)\n"
    "  /mcppin reset confirm                start over as on a first run (owner)"
)
OWNER_ONLY = ("Only the owner can do that: run it from a local session (CLI, TUI, Desktop), or be listed in "
              "Hermes's allow_admin_from (group_allow_admin_from in group chats).")


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
            if sub == "diff" and len(parts) in (2, 3):
                page = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 1
                return self._diff(parts[1], page)
            if sub in ("approve", "forget", "reset"):
                if not self.rt.may_approve(self.rt.caller(source, platform)):
                    return OWNER_ONLY
                who = self.rt.caller(source, platform).label()
                if sub == "approve":
                    if len(parts) != 3:
                        return ("Approving needs the code from /mcppin diff, so only what you reviewed is pinned:\n"
                                "  /mcppin approve <tool|server|all> <code>")
                    return self._approve(parts[1], parts[2], who)
                if sub == "forget" and len(parts) == 2:
                    names = self.rt.store.forget(parts[1])
                    if not names:
                        return f"No pin matches '{parts[1]}'."
                    return ("Forgot (each is held for review the next time it is seen):\n"
                            + "\n".join(f"  {n}" for n in names))
                if sub == "reset" and len(parts) == 2 and parts[1] == "confirm":
                    self.rt.store.reset()
                    self.rt.sync()
                    return "Pins cleared. Every MCP tool is now evaluated as on a first run."
        except ReviewMismatch as exc:
            return str(exc)
        except Exception as exc:
            return f"mcp-pin: {exc}"
        return USAGE

    def _approve(self, selector: str, code: str, who: str) -> str:
        names = self.rt.store.approve(selector, code, by=who)
        if not names:
            return f"Nothing held matches '{selector}'. /mcppin lists what is held."
        self.rt.sync()
        return "Approved and pinned:\n" + "\n".join(f"  {n}" for n in names)

    def _status(self) -> str:
        _, degraded = self.rt.sync()
        data = self.rt.store.snapshot()
        tools, pending = data.get("tools", {}), data.get("pending", {})
        lines: list[str] = []
        if degraded:
            lines.append(f"⚠ MCP tools are blocked: {degraded}")
        if pending:
            lines.append(f"Held for review ({len(pending)}):")
            servers = set()
            for name in sorted(pending):
                p = pending[name]
                servers.add(p.get("server") or server_of(name))
                lines.append(f"  {name}  [{p.get('kind', '?')}, {_ago(p.get('detected_at'))}]")
            hint = next(iter(servers)) if len(servers) == 1 else "<tool|server|all>"
            lines.append(f"Review with /mcppin diff {hint}; it shows the code to approve with.")
        counts: dict[str, int] = {}
        for name, pin in tools.items():
            server = pin.get("server") or server_of(name)
            counts[server] = counts.get(server, 0) + 1
        if counts:
            lines.append("Pinned: " + ", ".join(f"{s} ({n} tool{'s' if n != 1 else ''})" for s, n in sorted(counts.items())))
        if not lines:
            lines.append("No MCP tools seen yet. Tools are pinned the first time Hermes registers them.")
        return "\n".join(lines)

    def _diff(self, selector: str, page: int) -> str:
        self.rt.sync()
        pending = self.rt.store.pending_matching(selector)
        if not pending:
            return f"Nothing held matches '{selector}'. /mcppin lists what is held."
        pins = self.rt.store.snapshot().get("tools", {})
        text, page, pages = paginate(render_review(pending, pins), page)
        code = review_code(pending)
        head = (f"{len(pending)} held definition(s) for '{selector}' — page {page}/{pages}"
                + (f" (next: /mcppin diff {selector} {page + 1})" if page < pages else ""))
        foot = (f"Read every page, then approve exactly this with: /mcppin approve {selector} {code}"
                if page == pages else f"Approval code is on the last page ({pages}).")
        return f"{head}\n{text}\n{foot}"
