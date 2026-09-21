"""Provider-native capabilities — the self-running skills engine.

Mainstream providers ship server-side skills. Declaring them costs one small
schema or parameter, gets a channel the provider is licensed to operate,
better result quality, and fewer tokens (our local tool's schema and its
result-echo leave the request). When a native skill activates, the matching
LOCAL tool steps back.

Three mechanisms cover every mainstream shape:

  client_tool — declared as a tool; the model emits a ``$``-prefixed call,
      WE reply empty, and the platform executes (Kimi $web_search).
  server_tool — declared in provider-native format; the platform executes
      inline and the answer already contains the results; no client round
      trip (Anthropic web_search/web_fetch, Gemini google_search).
  param       — a plain request parameter flips the skill on
      (OpenAI web_search_options, Qwen enable_search, Claude thinking).

Policy lives in runtime (flag per provider instance, honouring
``[search] native = off``); mechanism lives in each provider's serializer.
``verified`` marks capabilities exercised against the REAL api from a real
machine — the rest are wired to the published spec and marked so in doctor.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from oaset.tools.base import READ, Tool, ToolContext, ToolResult


@dataclass(frozen=True)
class NativeCapability:
    skill: str
    provider: str
    mechanism: str  # client_tool | server_tool | param
    tool_schema: dict | None = None
    params: dict | None = None
    verified: bool = False
    note: str = ""
    # Local tools that leave the request when this skill is on.
    replaces: tuple[str, ...] = ()
    # Extra beta-header values required to declare the tool.
    headers: tuple[str, ...] = field(default_factory=tuple)


CAPABILITIES: tuple[NativeCapability, ...] = (
    # --- web_search --------------------------------------------------------
    NativeCapability(
        "web_search", "moonshot", "client_tool",
        tool_schema={"type": "builtin_function", "function": {"name": "$web_search"}},
        verified=True, note="Kimi 服务端执行；空回复契约",
        replaces=("web_search",),
    ),
    NativeCapability(
        "web_search", "openai", "param",
        params={"web_search_options": {"search_context_size": "low"}},
        note="chat.completions web_search_options（按规范接线）",
        replaces=("web_search",),
    ),
    NativeCapability(
        "web_search", "qwen", "param",
        params={"enable_search": True},
        note="DashScope OpenAI 兼容 enable_search（按规范接线）",
        replaces=("web_search",),
    ),
    NativeCapability(
        "web_search", "anthropic", "server_tool",
        tool_schema={"type": "web_search_20250305", "name": "web_search",
                     "max_uses": 5},
        note="Claude web_search 服务端工具（按规范接线）",
        replaces=("web_search",),
    ),
    NativeCapability(
        "web_search", "gemini", "server_tool",
        tool_schema={"google_search": {}},
        note="Gemini google_search grounding（按规范接线）",
        replaces=("web_search",),
    ),
    # --- web_fetch / url_context ------------------------------------------
    NativeCapability(
        "web_fetch", "anthropic", "server_tool",
        tool_schema={"type": "web_fetch_20250910", "name": "web_fetch",
                     "max_uses": 5},
        note="Claude web_fetch 服务端工具（按规范接线）",
        replaces=("web_fetch",),
        headers=("web-fetch-2025-09-10",),
    ),
    NativeCapability(
        "url_context", "gemini", "server_tool",
        tool_schema={"url_context": {}},
        note="Gemini url_context grounding（按规范接线）",
        replaces=("web_fetch",),
    ),
    # --- code execution (server sandbox; local run_shell stays) -----------
    NativeCapability(
        "code_execution", "anthropic", "server_tool",
        tool_schema={"type": "code_execution_20250825", "name": "code_execution"},
        note="Claude 服务端 code_execution（按规范接线）",
        headers=("code-execution-2025-08-25",),
    ),
    NativeCapability(
        "code_execution", "gemini", "server_tool",
        tool_schema={"code_execution": {}},
        note="Gemini code_execution（按规范接线）",
    ),
    # --- extended thinking / include thoughts -----------------------------
    NativeCapability(
        "thinking", "anthropic", "param",
        params={"thinking": {"type": "enabled", "budget_tokens": 5000}},
        note="Claude extended thinking（按规范接线）",
    ),
    NativeCapability(
        "thinking", "gemini", "param",
        params={"generationConfig": {"thinkingConfig": {"includeThoughts": True}}},
        note="Gemini thinkingConfig.includeThoughts（按规范接线）",
    ),
    # 本机 computer-use（UIA 截图 + SendInput）是 oAset 自己的实现，作为
    # 普通函数工具（computer / bash / str_replace_based_edit_tool）提供给所有
    # 支持工具调用的模型 —— 不声明任何厂商的专有 computer-use 协议。
    # glm 走 zhipu 引擎通道（tools API），非 anthropic 端点服务端工具——
    # 端点支持未经证实，通道效果等价、合规同源。
    # DeepSeek: 官方未开放原生搜索；reasoner 走 reasoning_content，不是声明式技能。
    # OpenAI code_interpreter / file_search 在 Responses/Assistants API，
    # 不在 chat.completions，故不接线。
)


def capabilities_for(provider: str, skill: str | None = "web_search") -> list[NativeCapability]:
    """Caps for a provider. ``skill=None`` returns every skill that provider has."""
    key = (provider or "").split("/", 1)[0].lower()
    if not key:
        return []
    rows = [cap for cap in CAPABILITIES if cap.provider == key]
    if skill is not None:
        rows = [cap for cap in rows if cap.skill == skill]
    return rows


def native_web_search_supported(provider: str) -> bool:
    """True when the provider has ANY native web-search capability."""
    return bool(capabilities_for(provider, "web_search"))


def native_skills_for(provider: str) -> frozenset[str]:
    """Every native skill name the provider can declare."""
    return frozenset(cap.skill for cap in capabilities_for(provider, None))


def native_params(provider: str, skill: str | None = "web_search") -> dict:
    """Request parameters for param-mechanism capabilities.

    Default ``skill="web_search"`` keeps the original accessor; pass ``None``
    to merge every param-mechanism skill the provider has.
    """
    out: dict = {}
    for cap in capabilities_for(provider, skill):
        if cap.mechanism == "param" and cap.params:
            _deep_merge(out, cap.params)
    return out


def native_server_tool(provider: str, skill: str = "web_search") -> dict | None:
    """The provider-format server tool declaration for one skill, if it has one."""
    for cap in capabilities_for(provider, skill):
        if cap.mechanism == "server_tool" and cap.tool_schema:
            return dict(cap.tool_schema)
    return None


def native_server_tools(provider: str, skills: set[str] | None = None) -> list[dict]:
    """Every server-tool schema the provider should declare for ``skills``."""
    out: list[dict] = []
    for cap in capabilities_for(provider, None):
        if skills is not None and cap.skill not in skills:
            continue
        if cap.mechanism == "server_tool" and cap.tool_schema:
            out.append(dict(cap.tool_schema))
    return out


def native_beta_headers(provider: str, skills: set[str] | None = None) -> tuple[str, ...]:
    """Deduplicated extra ``anthropic-beta`` values required by the selected skills."""
    seen: list[str] = []
    for cap in capabilities_for(provider, None):
        if skills is not None and cap.skill not in skills:
            continue
        for header in cap.headers:
            if header not in seen:
                seen.append(header)
    return tuple(seen)


def active_native_skills(provider_obj) -> frozenset[str]:
    """Skills this provider instance should declare (runtime flag + test back-compat)."""
    skills = set(getattr(provider_obj, "native_skills", ()) or ())
    if getattr(provider_obj, "native_web_search", False):
        skills.add("web_search")
    return frozenset(skills)


def native_payload(provider: str, skills: set[str]) -> tuple[list[dict], dict, tuple[str, ...]]:
    """(server_tools, request_params, beta_headers) for the selected skills."""
    return (
        native_server_tools(provider, skills),
        _params_for(provider, skills),
        native_beta_headers(provider, skills),
    )


def _params_for(provider: str, skills: set[str]) -> dict:
    out: dict = {}
    for cap in capabilities_for(provider, None):
        if cap.skill not in skills:
            continue
        if cap.mechanism == "param" and cap.params:
            _deep_merge(out, cap.params)
    return out


def _deep_merge(dst: dict, src: dict) -> dict:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value
    return dst


def is_native_call(name: str) -> bool:
    """$-prefixed tool calls are executed by the provider, not by us."""
    return name.startswith("$")


def split_native_tools(tools: list[dict]) -> tuple[list[dict], list[dict]]:
    """(normal tools, native/$-prefixed tools) for request serialization."""
    native: list[dict] = []
    normal: list[dict] = []
    for tool in tools or []:
        fn = (tool.get("function") or {}) if isinstance(tool, dict) else {}
        (native if str(fn.get("name", "")).startswith("$") else normal).append(tool)
    return normal, native


class NativeWebSearchTool(Tool):
    """client_tool mechanism: the server-side $web_search tool.

    The model emits the call; we reply with an EMPTY result and Moonshot's
    platform performs the search and continues — that empty tool message is
    the documented builtin contract.
    """

    name = "$web_search"
    description = "Provider-native web search (executed server-side)."
    permission = READ
    required: list[str] = []
    parameters: dict = {}

    def schema(self) -> dict:
        return {"type": "builtin_function", "function": {"name": "$web_search"}}

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult("")  # empty reply = "platform, run it"


def install_native_capabilities(registry, provider_name: str, *, enabled: bool = True) -> list[str]:
    """Activate the provider's native skills on this registry.

    Returns the LOCAL tools that stepped back. client_tool capabilities also
    register their empty-reply contract tool.
    """
    caps = capabilities_for(provider_name, None)
    if not enabled or not caps:
        return []
    for cap in caps:
        if cap.mechanism == "client_tool" and cap.tool_schema:
            contract_name = cap.tool_schema.get("function", {}).get("name", "")
            if contract_name and contract_name not in registry.tools:
                if contract_name == "$web_search":
                    registry.add_tool(NativeWebSearchTool())
    stepped: list[str] = []
    disabled = set(registry.disabled)
    for cap in caps:
        for name in cap.replaces:
            if name in registry.tools and name not in disabled:
                disabled.add(name)
                stepped.append(name)
    if stepped:
        registry.set_toggles(disabled=disabled)
    return stepped


def capability_report(provider: str) -> list[str]:
    """One-line-per-capability text for doctor (mechanism + verification)."""
    rows = []
    for cap in capabilities_for(provider, None):
        mark = "已真机验证" if cap.verified else "按规范接线"
        rows.append(f"{cap.skill}:{cap.mechanism} ({mark})")
    return rows


def audit_record(tool: str, arguments: str, is_error: bool, elapsed_ms: float,
                 session: str = "") -> dict:
    """One audit-log line: what ran, with what, how long, in which session."""
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session": session,
        "tool": tool,
        "args": arguments[:2000],
        "error": bool(is_error),
        "ms": round(float(elapsed_ms), 1),
    }
