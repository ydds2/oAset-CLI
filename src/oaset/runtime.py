"""RuntimeFactory — the single assembly path for provider/registry/context.

Every entrypoint (TUI, SDK, headless runner, one-shot CLI) builds the agent
runtime through :func:`build_registry`, so permission gates, tool toggles,
network mode, shell config and permission rules behave identically everywhere.
Before this module existed the TUI, SDK and runner each assembled these by
hand and had drifted (e.g. the SDK ignored disabled_tools).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oaset.agents import load_agents
from oaset.config import AppConfig, build_provider_chain, resolve_model, shell_config
from oaset.tools import AutoGate, ReadOnlyGate, ToolContext, ToolRegistry


@dataclass
class Runtime:
    """One assembled agent runtime; callers own provider lifetime (aclose)."""

    provider: Any
    fallbacks: list[Any]
    registry: ToolRegistry
    model_cfg: Any
    agents: dict[str, Any] = field(default_factory=dict)


def build_registry(cfg: AppConfig, cwd: Path, *, gate=None, mode: str | None = None,
                   tools: bool = True) -> ToolRegistry:
    """Create + bind the ToolRegistry exactly the same way in every client.

    gate: custom PermissionGate (the TUI passes its UIGate); None selects
    AutoGate/ReadOnlyGate from yolo-equivalent mode.
    """
    auto = mode == "auto" if mode is not None else cfg.permission_mode == "auto"
    registry = ToolRegistry(gate=gate if gate is not None else (AutoGate() if auto else ReadOnlyGate()))
    registry.set_toggles(
        enabled=getattr(cfg, "enabled_tools", None) or None,
        disabled=set(getattr(cfg, "disabled_tools", []) or []),
    )
    registry.bind(ToolContext(
        cwd=cwd,
        mode=mode or ("auto" if auto else "default"),
        output_limit=cfg.tool_output_limit,
        network_mode=cfg.network_mode,
        session_state={"shell_config": shell_config(cfg)},
        rules=list(getattr(cfg, "permission_rules", [])),
        config=cfg,
    ))
    if not tools:
        registry.tools = {}  # plain conversation, no tools
    return registry


def build_runtime(cfg: AppConfig, cwd: Path, *, model_id: str | None = None,
                  provider: Any = None, gate=None, mode: str | None = None,
                  tools: bool = True) -> Runtime:
    """Resolve model + provider chain and assemble the registry around them."""
    model_cfg = resolve_model(cfg, model_id)
    if provider is not None:
        fallbacks: list[Any] = []
    else:
        provider, fallbacks = build_provider_chain(cfg, model_cfg)
    registry = build_registry(cfg, cwd, gate=gate, mode=mode, tools=tools)
    from oaset.capabilities import install_native_capabilities, native_skills_for

    native_on = cfg.search.native != "off"
    if tools:
        install_native_capabilities(registry, model_cfg.provider, enabled=native_on)
    # Policy → mechanism bridge: each provider instance (active + fallbacks)
    # carries its own native-skill set so serializers know what to declare.
    for candidate in (provider, *fallbacks):
        model_ref = getattr(candidate, "model_id", "") or model_cfg.id
        skills = set(native_skills_for(model_ref)) if native_on else set()
        known = cfg.models.get(model_ref)
        if known is not None and "thinking" in skills and not known.has("thinking"):
            skills.discard("thinking")
        candidate.native_skills = frozenset(skills)
        candidate.native_web_search = "web_search" in skills
    agents = {a.name.lower(): a for a in load_agents(cwd)}
    if tools:
        registry.ctx.session_state["agents"] = agents
    return Runtime(provider=provider, fallbacks=fallbacks, registry=registry,
                   model_cfg=model_cfg, agents=agents)
