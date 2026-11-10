"""MCP Pin: hold MCP tools whose definition changed since you approved them.

An MCP server can change a tool's description or parameters at any time (a "rug pull", or tool
poisoning hidden in the description). MCP Pin fingerprints every MCP tool definition as Hermes registered it,
and from then on hides any changed definition from the model (in the tool list, the tool_search catalog
and tool_describe results) and refuses calls to it until the owner reviews the change with /mcppin.
"""

from __future__ import annotations

from typing import Any

__all__ = ["register"]


def register(ctx: Any) -> None:
    from .commands import Commands
    from .runtime import Runtime

    runtime = Runtime(ctx.get_config)
    commands = Commands(runtime)
    ctx.register_middleware("llm_request", runtime.llm_request)
    ctx.register_middleware("tool_execution", runtime.tool_execution)
    ctx.register_hook("pre_tool_call", runtime.pre_tool_call)
    ctx.register_command(
        "mcppin",
        handler=commands.handle,
        description="Review MCP tools whose definition changed since you approved them",
        args_hint="[diff <tool>|approve <tool|server|all>|forget <tool|server>]",
    )
