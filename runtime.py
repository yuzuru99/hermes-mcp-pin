"""MCP Pin's hooks.

The source of truth is Hermes's tool registry: every MCP tool definition the model can learn about
(the eager tool list, the tool_search catalog, tool_describe results) is derived from it, whatever
the provider. Each hook first syncs the registry's MCP definitions against the pins, then:

- ``llm_request`` middleware hides held definitions in the outgoing request (eager tool entries and
  lines of the tool_search catalog listing);
- ``tool_execution`` middleware hides them in tool_search / tool_describe / tool_call bridge results
  (the agent loop runs those through it; Hermes does not fire ``transform_tool_result`` for them);
- ``pre_tool_call`` refuses calls to held tools.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from .pins import EMPTY_PARAMS, HELD_NOTE, NEW_NOTE, McpDef, PinStore, StoreError, is_mcp_tool, iter_tool_defs

logger = logging.getLogger("hermes.plugins.mcp_pin")

PLUGIN_NAME = "mcp-pin"
LOCAL_PLATFORMS = frozenset({"", "cli", "tui", "desktop", "local", "acp"})
DM_CHAT_TYPES = frozenset({"dm", "direct", "private"})
_POLICIES = ("pin", "hold")
# Hermes's tool_search bridge (tools/tool_search_catalog.py BRIDGE_TOOL_NAMES); xAI sees an alias.
BRIDGE_TOOLS = frozenset({"tool_search", "tool_describe", "tool_call"})


def _is_bridge(name: str) -> bool:
    return name in BRIDGE_TOOLS or name.startswith("hermes_tool_search")


def default_pins_path() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME) / "pins.json"


_FP_CACHE: dict[str, tuple[Any, tuple, str]] = {}


def registry_mcp_defs() -> list[McpDef]:
    """Every MCP tool Hermes has registered (toolset ``mcp-<server>``), from ``tools.registry``. A
    fingerprint is computed again only when the registry holds a different schema object for the tool."""
    from tools.registry import registry

    from .pins import fingerprint

    out: list[McpDef] = []
    for entry in registry.get_all_entries():
        toolset = str(getattr(entry, "toolset", "") or "")
        if not toolset.startswith("mcp-"):
            continue
        schema = getattr(entry, "schema", None) or {}
        fn = schema.get("function") if isinstance(schema.get("function"), dict) else schema
        desc, params = str(fn.get("description") or ""), fn.get("parameters") or {}
        ident = (id(schema), id(params), len(desc), len(params) if isinstance(params, dict) else 0)
        cached = _FP_CACHE.get(entry.name)
        if cached is not None and cached[0] is schema and cached[1] == ident:
            h = cached[2]
        else:
            h = fingerprint(desc, params)
            _FP_CACHE[entry.name] = (schema, ident, h)
        out.append(McpDef(str(entry.name), toolset[4:], desc, params, h))
    return out


def registry_toolset_of(name: str) -> str:
    from tools.registry import registry

    return str(registry.get_toolset_for_tool(name) or "")


def session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "")
    except Exception:
        return ""


def _id_set(raw: Any) -> frozenset[str]:
    if raw is None:
        return frozenset()
    items: Iterable[Any] = raw if isinstance(raw, (list, tuple, set, frozenset)) else (
        raw.split(",") if isinstance(raw, str) else (raw,))
    return frozenset(s for s in (str(i).strip() for i in items) if s)


def load_gateway_admins() -> dict[str, frozenset[str]]:
    """Hermes's own slash-command admins (``allow_admin_from`` / ``group_allow_admin_from``) per
    platform and scope, read through Hermes's gateway config loader."""
    from gateway.config import load_gateway_config

    out: dict[str, frozenset[str]] = {}
    for platform, pconfig in (getattr(load_gateway_config(), "platforms", None) or {}).items():
        name = str(getattr(platform, "value", platform) or "").strip().lower()
        extra = getattr(pconfig, "extra", None)
        if not name or not isinstance(extra, dict):
            continue
        for scope, key in (("dm", "allow_admin_from"), ("group", "group_allow_admin_from")):
            ids = _id_set(extra.get(key))
            if ids:
                out[f"{name}:{scope}"] = ids
    return out


class Caller:
    """Who is acting: a local session on this machine, or a gateway sender (platform, user id, chat)."""

    def __init__(self, platform: str = "", user_id: str = "", chat_type: str = "", cron: bool = False) -> None:
        self.platform = (platform or "").strip().lower()
        self.user_id = (user_id or "").strip()
        self.chat_type = (chat_type or "").strip().lower()
        self.cron = cron

    @property
    def local(self) -> bool:
        """A session on this machine. The CLI runs plugin commands through the local gateway, which
        stamps a LOCAL source carrying the socket peer as user_id; that is still the machine's owner.
        Cron jobs also get a LOCAL source, and are never treated as the owner."""
        return self.platform in LOCAL_PLATFORMS and not self.cron

    def label(self) -> str:
        return "local" if self.local else f"{self.platform or 'gateway'}:{self.user_id or '?'}"

    @classmethod
    def from_source(cls, source: Any, platform: str = "", cron: bool = False) -> "Caller":
        if source is None:
            return cls(platform or "", cron=cron)
        plat = getattr(source, "platform", None)
        plat = getattr(plat, "value", plat) or platform or ""
        chat = getattr(source, "chat_type", "") or ""
        return cls(str(plat), str(getattr(source, "user_id", "") or ""), str(getattr(chat, "value", chat)), cron)


class Runtime:
    def __init__(
        self,
        get_config: Callable[[str, Any], Any],
        set_config: Callable[[str, Any], None] | None = None,
        *,
        pins_path: Callable[[], Path] = default_pins_path,
        mcp_defs: Callable[[], list[McpDef]] = registry_mcp_defs,
        toolset_of: Callable[[str], str] = registry_toolset_of,
        gateway_admins: Callable[[], dict[str, Iterable[str]]] = load_gateway_admins,
        env: Callable[[str], str] = session_env,
    ) -> None:
        self._get = get_config
        self.store = PinStore(
            pins_path,
            marker_get=lambda: get_config("store_id", ""),
            marker_set=(lambda v: set_config("store_id", v)) if set_config is not None else None)
        self._mcp_defs = mcp_defs
        self._toolset_of = toolset_of
        self._gateway_admins = gateway_admins
        self._env = env
        self._mcp_names: frozenset[str] = frozenset()
        self.degraded = ""  # why MCP is fully withheld right now ("" = checks are working)

    def _policy(self, key: str, default: str) -> str:
        value = str(self._get(key, default) or default).strip().lower()
        return value if value in _POLICIES else default

    def sync(self) -> tuple[dict[str, str | None], str]:
        """Check the registry's MCP definitions against the pins. Returns ``(held, degraded)`` as
        local values (never read back from shared state, so a concurrent success cannot mask this
        call's failure): held maps each held tool to its approved description (None if never
        approved); degraded is non-empty when the check failed and every MCP tool must be withheld."""
        try:
            defs = self._mcp_defs()
            self._mcp_names = frozenset(d.name for d in defs) | self._mcp_names
            held, events = self.store.observe(defs, new_servers=self._policy("new_servers", "pin"),
                                              new_tools=self._policy("new_tools", "hold"))
        except Exception as exc:
            reason = str(exc) if isinstance(exc, StoreError) else f"the pin check failed ({type(exc).__name__})"
            if reason != self.degraded:
                logger.error("mcp-pin: %s; withholding every MCP tool", reason)
            self.degraded = reason  # for /mcppin status only
            return {}, reason
        self.degraded = ""
        for e in events:
            if e["event"] in ("changed", "new"):
                logger.warning("mcp-pin: %s tool %s held for review (/mcppin diff %s)", e["event"], e["tool"], e["tool"])
            else:
                logger.info("mcp-pin: %s %s", e["event"], e["tool"])
        return held, ""

    def _is_mcp(self, name: str) -> bool:
        if is_mcp_tool(name) or name in self._mcp_names:
            return True
        try:  # a clamped name registered since the last sync
            return self._toolset_of(name).startswith("mcp-")
        except Exception:
            return False

    def _hidden(self, held: dict[str, str | None], degraded: str) -> Callable[[str], tuple[str, Any] | None]:
        """name -> (description, parameters) to show instead, or None when the tool is not hidden."""
        def lookup(name: str) -> tuple[str, Any] | None:
            if degraded and self._is_mcp(name):
                return ("[mcp-pin] Unavailable: the pin check failed; calls are blocked.", EMPTY_PARAMS)
            if name not in held:
                return None
            approved = held[name]
            if approved:
                return (f"{HELD_NOTE}\nApproved description: {approved}", EMPTY_PARAMS)
            return (NEW_NOTE, EMPTY_PARAMS)
        return lookup

    # -- llm_request middleware ----------------------------------------------------------------

    def llm_request(self, request: Any = None, **_: Any) -> dict | None:
        """Hide held MCP definitions in the outgoing request: eager tool entries are replaced, and
        catalog lines (``- name: description``) inside other tool descriptions are blanked."""
        if not isinstance(request, dict):
            return None
        held, degraded = self.sync()
        if not held and not degraded:
            return None
        hide = self._hidden(held, degraded)
        names = set(held) | (set(self._mcp_names) if degraded else set())
        line_re = re.compile(r"^- (" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?::[^\n]*)?$",
                             re.M) if names else None
        changed = False
        for d in iter_tool_defs(request):
            sub = hide(d.name)
            if sub is not None:
                if (d.description, d.parameters) != sub:
                    d.replace(*sub)
                    changed = True
                continue
            if line_re is not None and d.description and line_re.search(d.description):
                d.replace(line_re.sub(lambda m: f"- {m.group(1)}: [held by mcp-pin]", d.description), d.parameters)
                changed = True
        return {"request": request, "source": PLUGIN_NAME, "reason": "held MCP tool definitions"} if changed else None

    # -- bridge results (tool_execution middleware) --------------------------------------------

    def rewrite_bridge_result(self, tool_name: str = "", args: Any = None, result: Any = None, **_: Any) -> str | None:
        """Rewrite a tool_search bridge result (tool_search, tool_describe, a tool_call schema probe)
        so it carries no held definition; None when there is nothing to hide."""
        if not isinstance(result, str) or not result or not _is_bridge(str(tool_name or "")):
            return None
        held, degraded = self.sync()
        names = set(held) | (set(self._mcp_names) if degraded else set())
        if not names:
            return None
        in_result = [n for n in names if _mentions(result, n)]
        try:
            args_text = json.dumps(args, default=str) if args else ""
        except Exception:
            args_text = str(args)
        in_args = [n for n in names if _mentions(args_text, n) and n != tool_name]
        if not in_result and not in_args:
            return None
        hide = self._hidden(held, degraded)
        try:
            data = json.loads(result)
        except Exception:
            data = None
        if isinstance(data, dict) and isinstance(data.get("tools"), dict) and in_result:
            for n in in_result:
                rec = data["tools"].get(n)
                sub = hide(n)
                if isinstance(rec, dict) and sub is not None:
                    rec["description"] = sub[0]
                    if "parameters" in rec:
                        rec["parameters"] = sub[1]
                    if "required" in rec:
                        rec["required"] = []
            return json.dumps(data, ensure_ascii=False)
        # Any other bridge result about a held tool (a tool_call schema probe or error): replace it.
        if in_result or in_args:
            return json.dumps({"error": f"[mcp-pin] {(in_args or in_result)[0]} is held for review; calls are "
                                        f"blocked until the owner approves it."})
        return None

    def tool_execution(self, tool_name: str = "", args: Any = None, next_call: Callable | None = None, **kw: Any) -> Any:
        """``tool_execution`` middleware: the agent loop runs tool_search / tool_describe through it
        (Hermes does not fire ``transform_tool_result`` for them), so held definitions are hidden in
        their results here."""
        result = next_call(args) if next_call is not None else None
        if _is_bridge(str(tool_name or "")):
            rewritten = self.rewrite_bridge_result(tool_name=tool_name, args=args, result=result)
            if isinstance(rewritten, str):
                return rewritten
        return result

    # -- pre_tool_call -------------------------------------------------------------------------

    def pre_tool_call(self, tool_name: str = "", args: Any = None, **_: Any) -> dict | None:
        if not self._is_mcp(str(tool_name or "")):
            return None  # non-MCP calls cost nothing
        held, degraded = self.sync()
        if degraded:
            return _block(f"[mcp-pin] MCP tool '{tool_name}' was not run: {degraded}.")
        if tool_name in held:
            return _block(
                f"[mcp-pin] MCP tool '{tool_name}' was not run: its definition is new or changed since the "
                f"owner approved this server, and it is held for review. The owner can review it with "
                f"/mcppin diff {tool_name}, which shows the code to approve it with. Do not retry.")
        return None

    # -- who may approve -----------------------------------------------------------------------

    def caller(self, source: Any = None, platform: str = "") -> Caller:
        cron = self._env("HERMES_CRON_SESSION") == "1"
        if source is not None:
            return Caller.from_source(source, platform, cron)
        return Caller(self._env("HERMES_SESSION_PLATFORM") or platform,
                      self._env("HERMES_SESSION_USER_ID"), self._env("HERMES_SESSION_CHAT_TYPE"), cron)

    def may_approve(self, who: Caller) -> bool:
        """Local sessions, and senders Hermes itself lists as admins for this kind of chat."""
        if who.local:
            return True
        if not who.user_id:
            return False
        try:
            admins = self._gateway_admins() or {}
        except Exception as exc:
            logger.warning("mcp-pin: could not read allow_admin_from (%s); only local sessions may approve",
                           type(exc).__name__)
            return False
        dm = frozenset(admins.get(f"{who.platform}:dm", ()))
        group = frozenset(admins.get(f"{who.platform}:group", ()))
        if who.chat_type:
            ids = dm if who.chat_type in DM_CHAT_TYPES else group
        else:
            ids = dm if dm and not group else group
        return who.user_id in ids


def _mentions(text: str, name: str) -> bool:
    """*name* appears in *text* as a whole tool name (``mcp__a__read`` does not match ``mcp__a__read_all``)."""
    return name in text and re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text) is not None


def _block(message: str) -> dict:
    return {"action": "block", "message": message}
