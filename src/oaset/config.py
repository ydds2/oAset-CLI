"""Configuration: ~/.oaset/config.toml (providers / models / permissions / UI).

Shape: providers with type/api_key/base_url, models with capabilities
and a reasoning_key for thinking streams. API keys support ``env:NAME``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oaset.utils import oaset_home

CONFIG_VERSION = 1



@dataclass
class ProviderConfig:
    name: str
    type: str = "openai"
    api_key: str = ""
    base_url: str = ""
    # Anthropic-format prompt-cache markers. On by default: the rolling
    # breakpoint's cache write costs 1.25x once and every later turn of the
    # loop reads it at a 90% discount, which an agent session almost always
    # wins. Serialized explicitly so an opt-out survives round-trips.
    prompt_cache: bool = True
    # "" = Anthropic's default 5-minute cache TTL; "1h" opts into the
    # extended-TTL beta (cache writes cost 2x — for slow-paced sessions
    # whose idle gaps keep outliving the five minutes).
    prompt_cache_ttl: str = ""
    # Explicit API format chosen in the setup wizard. Empty = derive from
    # `type` (legacy configs). Kept separate so a vendor's wire format is a
    # visible, validated field instead of an implied one.
    api_format: str = ""
    # HTTP read timeout for streaming calls, seconds. 0 = provider default
    # (300s). A stalled endpoint otherwise looks like a frozen TUI for up to
    # five minutes per retry.
    timeout: float = 0.0
    # Watchdog for "connected but silent": no headers/first chunk within this
    # many seconds aborts the attempt as retryable. 0 = provider default (45s).
    first_byte_timeout: float = 0.0

    def effective_api_format(self) -> str:
        return (self.api_format or self.type or "openai").strip().lower()


@dataclass
class ModelConfig:
    id: str  # "<provider>/<model>" key used in --model and /model
    provider: str
    model: str
    max_context_size: int = 128000
    max_output_size: int = 32768
    capabilities: list[str] = field(default_factory=list)
    display_name: str = ""
    reasoning_key: str | None = None
    # Thinking depth for this model: off / light / medium / heavy ("" =
    # provider default, no parameter sent). Mapped per provider by
    # oaset.thinking; runtime override via /think.
    thinking: str = ""
    # Cost transparency (USD per 1M tokens; 0 = unpriced, nothing shown).
    # Deliberately NOT bundled with values: prices change — the user sets
    # today's numbers in config.toml and owns the estimate.
    price_in: float = 0.0
    price_out: float = 0.0

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def cost(self, prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
        """Estimated USD for one usage record; None when the model is unpriced."""
        if self.price_in <= 0 and self.price_out <= 0:
            return None
        p = prompt_tokens or 0
        c = completion_tokens or 0
        return (p / 1_000_000) * self.price_in + (c / 1_000_000) * self.price_out


# The offline mock is NOT an HTTP endpoint. It used to be declared as
# openai + http://127.0.0.1:8399/v1, which made /model switch and the setup
# wizard build a real HTTP client for it — offline that meant a Connection
# error ("model.verify_failed") and a bogus URL shown in the model list.
MOCK_BASE_URL = "mock://offline"
_LEGACY_MOCK_URLS = frozenset({"http://127.0.0.1:8399/v1", "http://127.0.0.1:8399"})


def is_mock_provider(name: str, pcfg: Any = None) -> bool:
    """True when this provider is the built-in offline mock.

    Recognised by name, by the ``mock://`` scheme, or by the legacy localhost
    placeholder so configs written before this change keep working.
    """
    if str(name or "").strip().lower() == "mock":
        return True
    url = str(getattr(pcfg, "base_url", "") or "").strip().lower()
    return url.startswith("mock://") or url.rstrip("/") in _LEGACY_MOCK_URLS


@dataclass
class UiConfig:
    theme: str = "oaset-dark"
    editor: str = ""  # external editor for Ctrl+G; empty = $VISUAL/$EDITOR
    density: str = "cozy"  # cozy | compact: spacing of cards/answers in the chat
    # Accessibility: freeze spinners, cursor blink and the palette caret.
    # Follows the same intent as the OS "reduce motion" setting.
    reduce_motion: bool = False
    # Turn-end signal: "off" | "bell" (terminal bell) | "desktop"
    # (OSC 9/777 toast — Windows Terminal / iTerm2 / tmux render it).
    notify: str = "off"


@dataclass
class MaintenanceConfig:
    """体验计划 (Experience Program): opt-in token-powered self-maintenance.

    enabled       — user explicitly opted in; nothing runs without it
    budget_tokens — fixed per-run token allowance for the maintenance turn
    allow_apply   — when True the plan's patch targets are installed through
                    UpdateManager.apply (hash-gated, explicit targets,
                    rollback-able); when False the turn is read-only
    """

    enabled: bool = False
    budget_tokens: int = 20000
    allow_apply: bool = False


@dataclass
class SearchConfig:
    """Web-search settings (`[search]` in config.toml).

    backend        — "auto" (SearXNG when a URL is set, else the zero-config
                     Bing RSS fallback) or an explicit backend name. An explicit
                     name wins even when it is wrong: silently rerouting search
                     traffic to another engine is a privacy event, not a
                     convenience (the conservative default).
    searxng_url    — your own SearXNG instance (falls back to $SEARXNG_URL).
    per_turn_*     — optional caps per agent turn. 0 = unlimited (the default);
                     set a number only when you want an explicit safety cap.
    cache_ttl_minutes — 0 disables the cache.
    allow_private_urls — opt-in for localhost/LAN fetches. Cloud metadata
                     addresses stay blocked regardless.
    """

    backend: str = "auto"
    searxng_url: str = ""
    # Provider-native skills (search, fetch, code execution, thinking):
    # "auto" declares every skill the active provider ships; "off" forces
    # local tools and omits the native declarations.
    native: str = "auto"
    max_results: int = 8
    cache_ttl_minutes: int = 60
    per_turn_search_budget: int = 0  # 0 = unlimited
    per_turn_fetch_budget: int = 0  # 0 = unlimited
    allow_private_urls: bool = False
    proxy: str = ""  # http(s)/socks URL for search+fetch egress (empty = direct)


@dataclass
class AppConfig:
    default_provider: str = "mock"
    default_model: str = "mock/mock-echo"
    permission_mode: str = "default"  # default | auto
    tool_output_limit: int = 2000
    # 用户决定（2026-09-14）：默认不限预算。loop 对 <=0 即无限；identical-call
    # 熔断保留（防死循环，非预算）。需要上限的用户在 config.toml 显式设置。
    max_iterations: int = 0  # 0 = unlimited (default); set a number to cap
    max_tool_calls: int = 0  # 0 = unlimited (default); identical-call breaker always on
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    models: dict[str, ModelConfig] = field(default_factory=dict)
    ui: UiConfig = field(default_factory=UiConfig)
    ui_language: str = "zh"
    # False only on a brand-new install (see load_config). In-memory defaults
    # and existing configs are treated as already chosen so tests / upgrades
    # never re-prompt.
    language_chosen: bool = True
    network_mode: str = "pull_only"   # local_only | pull_only | full
    hooks: dict[str, str] = field(default_factory=dict)
    enabled_tools: list[str] = field(default_factory=list)   # empty = all
    disabled_tools: list[str] = field(default_factory=list)
    shell_backend: str = "local"          # local | ssh | docker
    ssh_target: str = ""
    docker_container: str = ""
    singularity_image: str = ""
    modal_container: str = ""
    daytona_target: str = ""
    shell_sandbox_default: bool = True
    shell_sandbox_memory_mb: int = 2048
    allow_tools: list[str] = field(default_factory=list)  # durable "always allow" tools
    fallback_models: list[str] = field(default_factory=list)  # "<provider>/<model>" chain
    permission_rules: list[Any] = field(default_factory=list)  # PermissionRule list
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    # SUPPLY-01: publisher public keys (base64) for catalog/artifact
    # signatures, keyed by key_id. Public data only — safe to keep in config.
    update_trusted_keys: dict[str, str] = field(default_factory=dict)
    # Mirror for the update catalog (the default lives on GitHub Pages, which
    # is unreliable in mainland China). Empty = official URL.
    update_catalog_url: str = ""


def config_path() -> Path:
    return oaset_home() / "config.toml"


def default_config() -> AppConfig:
    cfg = AppConfig()
    cfg.default_provider = "deepseek"
    cfg.default_model = "deepseek/deepseek-chat"
    cfg.providers = {
        "deepseek": ProviderConfig("deepseek", "openai", "env:DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
        "moonshot": ProviderConfig("moonshot", "openai", "env:MOONSHOT_API_KEY", "https://api.moonshot.cn/v1"),
        "openai": ProviderConfig("openai", "openai", "env:OPENAI_API_KEY", "https://api.openai.com/v1"),
        "mock": ProviderConfig("mock", "openai", "mock", MOCK_BASE_URL),
        "anthropic": ProviderConfig("anthropic", "anthropic", "env:ANTHROPIC_API_KEY", "https://api.anthropic.com"),
        "bedrock": ProviderConfig("bedrock", "bedrock", "env:AWS_ACCESS_KEY_ID", "https://bedrock-runtime.us-east-1.amazonaws.com"),
        "gemini": ProviderConfig("gemini", "gemini", "env:GEMINI_API_KEY", "https://generativelanguage.googleapis.com"),
        "glm": ProviderConfig("glm", "anthropic", "env:GLM_API_KEY", "https://open.bigmodel.cn/api/anthropic"),
        "ollama": ProviderConfig("ollama", "openai", "", "http://127.0.0.1:11434/v1"),
        "lmstudio": ProviderConfig("lmstudio", "openai", "", "http://127.0.0.1:1234/v1"),
    }
    cfg.models = {
        "deepseek/deepseek-chat": ModelConfig("deepseek/deepseek-chat", "deepseek", "deepseek-chat", 128000, capabilities=["tool_use"], display_name="DeepSeek Chat"),
        "deepseek/deepseek-reasoner": ModelConfig("deepseek/deepseek-reasoner", "deepseek", "deepseek-reasoner", 128000, capabilities=["thinking", "tool_use"], display_name="DeepSeek Reasoner", reasoning_key="reasoning_content"),
        "moonshot/kimi-k2": ModelConfig("moonshot/kimi-k2", "moonshot", "kimi-k2", 131072, capabilities=["thinking", "tool_use"], display_name="Kimi K2", reasoning_key="reasoning_content"),
        "openai/gpt-4o": ModelConfig("openai/gpt-4o", "openai", "gpt-4o", 128000, capabilities=["tool_use"], display_name="GPT-4o"),
        "mock/mock-echo": ModelConfig("mock/mock-echo", "mock", "mock-echo", 32000, capabilities=["tool_use"], display_name="Mock Echo (offline)"),
        "anthropic/claude-sonnet": ModelConfig("anthropic/claude-sonnet", "anthropic", "claude-sonnet", 200000, capabilities=["thinking", "tool_use", "image_in"], display_name="Claude Sonnet (native)"),
        "bedrock/claude": ModelConfig("bedrock/claude", "bedrock", "claude", 200000, capabilities=["tool_use"], display_name="Bedrock Claude"),
        "gemini/gemini-flash": ModelConfig("gemini/gemini-flash", "gemini", "gemini-flash", 1000000, capabilities=["thinking", "tool_use"], display_name="Gemini Flash (native)"),
        "glm/glm-5.3-flash": ModelConfig("glm/glm-5.3-flash", "glm", "glm-5.3-flash", 128000, capabilities=["thinking", "tool_use"], display_name="GLM 5.3 Flash", reasoning_key="reasoning_content"),
        "ollama/llama": ModelConfig("ollama/llama", "ollama", "llama", 128000, capabilities=["tool_use"], display_name="Ollama (local)"),
        "lmstudio/local": ModelConfig("lmstudio/local", "lmstudio", "local", 128000, capabilities=["tool_use"], display_name="LM Studio (local)"),
    }
    return cfg


class ConfigError(Exception):
    """config.toml 解析失败（带文件定位），由 CLI 友好呈现。"""


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """Narrow a possibly-missing or non-table TOML key to a plain dict."""
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def load_config(create: bool = True) -> AppConfig:
    path = config_path()
    if not path.exists():
        cfg = default_config()
        cfg.language_chosen = False  # first real launch asks 中文 / English
        if create:
            save_config(cfg)
        return cfg
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    cfg = default_config()
    cfg.default_provider = raw.get("default_provider", cfg.default_provider)
    cfg.default_model = raw.get("default_model", cfg.default_model)
    cfg.permission_mode = raw.get("permission_mode", cfg.permission_mode)
    cfg.tool_output_limit = int(raw.get("tool_output_limit", cfg.tool_output_limit))
    cfg.max_iterations = int(raw.get("max_iterations", cfg.max_iterations))
    cfg.max_tool_calls = int(raw.get("max_tool_calls", cfg.max_tool_calls))
    if isinstance(raw.get("ui"), dict):
        cfg.ui.theme = raw["ui"].get("theme", cfg.ui.theme)
        cfg.ui_language = raw["ui"].get("language", cfg.ui_language)
        if "language_chosen" in raw["ui"]:
            cfg.language_chosen = bool(raw["ui"].get("language_chosen"))
        else:
            # Existing installs already have a language in config — do not
            # re-prompt. Only a brand-new home (no config file) is first-run.
            cfg.language_chosen = True
        cfg.ui.editor = str(raw["ui"].get("editor", cfg.ui.editor) or "")
        cfg.ui.density = str(raw["ui"].get("density", cfg.ui.density) or "cozy")
        cfg.ui.reduce_motion = bool(raw["ui"].get("reduce_motion", False))
        cfg.ui.notify = str(raw["ui"].get("notify", "off") or "off")
    from oaset.network import normalize_mode

    cfg.network_mode = normalize_mode(str(raw.get("network", {}).get("mode", "pull_only"))         if isinstance(raw.get("network"), dict) else "pull_only")
    from oaset.hooks import EVENTS

    hooks = raw.get("hooks", {})
    if isinstance(hooks, dict):
        cfg.hooks = {str(k): str(v) for k, v in hooks.items() if k in EVENTS and v}
    cfg.enabled_tools = [str(t) for t in raw.get("enabled_tools", [])]
    cfg.disabled_tools = [str(t) for t in raw.get("disabled_tools", [])]
    shell = _table(raw, "shell")
    cfg.shell_backend = str(shell.get("backend", raw.get("shell_backend", "local")))
    cfg.ssh_target = str(shell.get("ssh_target", raw.get("ssh_target", "")))
    cfg.docker_container = str(shell.get("docker_container", raw.get("docker_container", "")))
    cfg.singularity_image = str(shell.get("singularity_image", raw.get("singularity_image", "")))
    cfg.modal_container = str(shell.get("modal_container", raw.get("modal_container", "")))
    cfg.daytona_target = str(shell.get("daytona_target", raw.get("daytona_target", "")))
    cfg.shell_sandbox_default = bool(shell.get("sandbox_default", True))
    cfg.shell_sandbox_memory_mb = int(shell.get("sandbox_memory_mb", 2048) or 2048)
    maint_raw = raw.get("maintenance")
    maint = maint_raw if isinstance(maint_raw, dict) else {}
    cfg.maintenance.enabled = bool(maint.get("enabled", False))
    cfg.maintenance.budget_tokens = int(maint.get("budget_tokens", 20000) or 20000)
    cfg.maintenance.allow_apply = bool(maint.get("allow_apply", False))
    search = _table(raw, "search")
    cfg.search.backend = str(search.get("backend", cfg.search.backend) or "auto")
    # [search] native was SAVED but never LOADED — the documented
    # "disable provider-native skills" switch silently did nothing
    cfg.search.native = str(search.get("native", cfg.search.native) or "auto")
    cfg.search.searxng_url = str(
        search.get("searxng_url", os.environ.get("SEARXNG_URL", "")) or "")
    cfg.search.max_results = int(search.get("max_results", cfg.search.max_results) or 8)
    cfg.search.cache_ttl_minutes = int(
        search.get("cache_ttl_minutes", cfg.search.cache_ttl_minutes) or 0)
    cfg.search.per_turn_search_budget = int(
        search.get("per_turn_search_budget", cfg.search.per_turn_search_budget) or 0)
    cfg.search.per_turn_fetch_budget = int(
        search.get("per_turn_fetch_budget", cfg.search.per_turn_fetch_budget) or 0)
    cfg.search.allow_private_urls = bool(
        search.get("allow_private_urls", cfg.search.allow_private_urls))
    cfg.search.proxy = str(search.get("proxy", cfg.search.proxy) or "")
    update_tbl = _table(raw, "update")
    keys = update_tbl.get("trusted_keys")
    if isinstance(keys, dict):
        cfg.update_trusted_keys = {str(k): str(v) for k, v in keys.items() if v}
    cfg.update_catalog_url = str(update_tbl.get("catalog_url", "") or "")
    permissions = _table(raw, "permissions")
    allow_raw = raw.get("allow_tools") or permissions.get("allow_tools") or []
    if isinstance(allow_raw, list):
        cfg.allow_tools = [str(t) for t in allow_raw]
    fb_raw = raw.get("fallback_models") or []
    if isinstance(fb_raw, list):
        cfg.fallback_models = [str(m) for m in fb_raw]
    from oaset.tools.base import parse_permission_rules

    perm = _table(raw, "permission")
    cfg.permission_rules = parse_permission_rules(
        perm.get("rules") or raw.get("permission_rules") or [])
    cfg.providers = {}
    for name, p in raw.get("providers", {}).items():
        pcfg = ProviderConfig(
            name=name,
            type=p.get("type", "openai"),
            api_key=p.get("api_key", ""),
            base_url=p.get("base_url", ""),
            prompt_cache=bool(p.get("prompt_cache", True)),
            prompt_cache_ttl=str(p.get("prompt_cache_ttl", "") or ""),
            api_format=str(p.get("api_format", "")),
        )
        if is_mock_provider(name, pcfg):
            pcfg.base_url = MOCK_BASE_URL  # migrate the old localhost placeholder
        cfg.providers[name] = pcfg
    cfg.models = {}
    for mid, m in raw.get("models", {}).items():
        cfg.models[mid] = ModelConfig(
            id=mid,
            provider=m.get("provider", cfg.default_provider),
            model=m.get("model", mid.split("/")[-1]),
            max_context_size=int(m.get("max_context_size", 128000)),
            max_output_size=int(m.get("max_output_size", 32768)),
            capabilities=list(m.get("capabilities", ["tool_use"])),
            display_name=m.get("display_name", mid),
            reasoning_key=m.get("reasoning_key"),
            thinking=str(m.get("thinking", "") or ""),
            price_in=float(m.get("price_in", 0) or 0),
            price_out=float(m.get("price_out", 0) or 0),
        )
    return cfg


def save_config(cfg: AppConfig) -> None:
    raw: dict = {
        "default_provider": cfg.default_provider,
        "default_model": cfg.default_model,
        "permission_mode": cfg.permission_mode,
        "tool_output_limit": cfg.tool_output_limit,
        "max_iterations": cfg.max_iterations,
        "max_tool_calls": cfg.max_tool_calls,
        "ui": {
            "theme": cfg.ui.theme,
            "language": cfg.ui_language,
            "language_chosen": bool(cfg.language_chosen),
            **({"editor": cfg.ui.editor} if cfg.ui.editor else {}),
            **({"density": cfg.ui.density} if cfg.ui.density != "cozy" else {}),
            **({"reduce_motion": True} if cfg.ui.reduce_motion else {}),
            **({"notify": cfg.ui.notify} if cfg.ui.notify != "off" else {}),
        },
        "network": {"mode": cfg.network_mode},
        "maintenance": {
            "enabled": cfg.maintenance.enabled,
            "budget_tokens": cfg.maintenance.budget_tokens,
            "allow_apply": cfg.maintenance.allow_apply,
        },
        "search": {
            "backend": cfg.search.backend,
            "searxng_url": cfg.search.searxng_url,
            **({"native": cfg.search.native} if cfg.search.native != "auto" else {}),
            "max_results": cfg.search.max_results,
            "cache_ttl_minutes": cfg.search.cache_ttl_minutes,
            "per_turn_search_budget": cfg.search.per_turn_search_budget,
            "per_turn_fetch_budget": cfg.search.per_turn_fetch_budget,
            "allow_private_urls": cfg.search.allow_private_urls,
            "proxy": cfg.search.proxy,
        },
        **({"hooks": dict(cfg.hooks)} if cfg.hooks else {}),
        # ONE "update" key: two **{"update": ...} entries meant the second
        # replaced the first, wiping trusted_keys on any save.
        **({"update": {
            **({"trusted_keys": dict(cfg.update_trusted_keys)}
               if cfg.update_trusted_keys else {}),
            **({"catalog_url": cfg.update_catalog_url}
               if cfg.update_catalog_url else {}),
        }} if (cfg.update_trusted_keys or cfg.update_catalog_url) else {}),
        **({"enabled_tools": list(cfg.enabled_tools)} if cfg.enabled_tools else {}),
        **({"disabled_tools": list(cfg.disabled_tools)} if cfg.disabled_tools else {}),
        **({"allow_tools": list(cfg.allow_tools)} if cfg.allow_tools else {}),
        **({"fallback_models": list(cfg.fallback_models)} if cfg.fallback_models else {}),
        **({"permission": {"rules": [
            {"decision": r.decision, "pattern": r.pattern, "scope": r.scope, **({"reason": r.reason} if r.reason else {})}
            for r in cfg.permission_rules
        ]}} if cfg.permission_rules else {}),
        "shell": {
            "backend": cfg.shell_backend,
            "sandbox_default": cfg.shell_sandbox_default,
            **({"sandbox_memory_mb": cfg.shell_sandbox_memory_mb}
               if cfg.shell_sandbox_memory_mb != 2048 else {}),
            **({"ssh_target": cfg.ssh_target} if cfg.ssh_target else {}),
            **({"docker_container": cfg.docker_container} if cfg.docker_container else {}),
            **({"singularity_image": cfg.singularity_image} if cfg.singularity_image else {}),
            **({"modal_container": cfg.modal_container} if cfg.modal_container else {}),
            **({"daytona_target": cfg.daytona_target} if cfg.daytona_target else {}),
        },
        "providers": {
            name: {
                "type": p.type,
                "api_key": p.api_key,
                "base_url": p.base_url,
                **({"api_format": p.api_format} if p.api_format else {}),
                "prompt_cache": p.prompt_cache,
                **({"prompt_cache_ttl": p.prompt_cache_ttl}
                   if p.prompt_cache_ttl else {}),
            }
            for name, p in cfg.providers.items()
        },
        "models": {
            mid: {
                "provider": m.provider,
                "model": m.model,
                "max_context_size": m.max_context_size,
                "max_output_size": m.max_output_size,
                "capabilities": m.capabilities,
                "display_name": m.display_name,
                **({"reasoning_key": m.reasoning_key} if m.reasoning_key else {}),
                **({"thinking": m.thinking} if m.thinking else {}),
                **({"price_in": m.price_in, "price_out": m.price_out}
                   if m.price_in > 0 or m.price_out > 0 else {}),
            }
            for mid, m in cfg.models.items()
        },
    }
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # atomic + cross-process locked: a crash mid-save must not corrupt the
    # user's config, and a TUI + CLI saving concurrently must serialize
    # (last-writer-wins stands — the lock only excludes interleaved writes).
    import tomli_w  # deferred: only writes pay for it, every start reads

    from oaset.utils import file_lock

    with file_lock(path, timeout=10.0):
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_bytes(tomli_w.dumps(raw).encode("utf-8"))
        os.replace(tmp, path)


def resolve_api_key(value: str) -> str:
    """Resolve `env:NAME` references against the environment."""
    if value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def provider_credential_state(cfg: AppConfig, provider_name: str) -> str:
    """Is this provider actually usable right now?

    - "ready"     : a usable credential exists (literal key, set env var, stored
                    credential) or it is a local server that needs no key
    - "needs_key" : configured, but nothing to authenticate with
    - "missing"   : not configured at all

    Built-in presets ship as `env:NAME` references, so on a fresh machine they
    are "needs_key" — the UI must say so instead of presenting them as ready.
    """
    pcfg = cfg.providers.get(provider_name)
    if pcfg is None:
        return "missing"
    base = (pcfg.base_url or "").lower()
    if any(host in base for host in LOCAL_HOSTS):
        return "ready"  # local model server: no credential involved
    raw = pcfg.api_key or ""
    if raw and raw != "mock":
        if not raw.startswith("env:"):
            return "ready"
        if resolve_api_key(raw).strip():
            return "ready"
    elif raw == "mock":
        return "ready"
    try:
        from oaset.credentials import load_credential

        if load_credential(provider_name).strip():
            return "ready"
    except Exception:
        pass
    return "needs_key"


def model_availability(cfg: AppConfig, model_id: str) -> tuple[str, str]:
    """(state, provider_name) for a model id — state per provider_credential_state."""
    model_cfg = cfg.models.get(model_id)
    if model_cfg is None:
        return "missing", ""
    return provider_credential_state(cfg, model_cfg.provider), model_cfg.provider


def available_model_ids(cfg: AppConfig) -> list[str]:
    return sorted(cfg.models)


def resolve_model(cfg: AppConfig, model_id: str | None = None) -> ModelConfig:
    mid = model_id or cfg.default_model
    if mid in cfg.models:
        return cfg.models[mid]
    known = ", ".join(available_model_ids(cfg)) or "(none)"
    raise KeyError(f"Unknown model '{mid}'. Known models: {known}")


def build_provider(cfg: AppConfig, model_cfg: ModelConfig):
    """Build a provider instance from config (OpenAI-compatible only, by design).

    API key resolution order: config value (env: refs supported) → credentials
    store (~/.oaset/credentials/<provider>.json, written by /login).
    """
    from oaset.providers.openai_compat import OpenAICompatProvider

    if model_cfg.provider not in cfg.providers:
        raise KeyError(
            f"Model '{model_cfg.id}' references unknown provider '{model_cfg.provider}'."
        )
    pcfg = cfg.providers[model_cfg.provider]
    if is_mock_provider(model_cfg.provider, pcfg):
        # Offline mock: never construct a real HTTP client for it.
        from oaset.providers.mock import MockProvider

        return MockProvider(model_id=model_cfg.id)
    api_key = resolve_api_key(pcfg.api_key)
    if not api_key:
        from oaset.credentials import load_credential

        api_key = load_credential(model_cfg.provider)
    from oaset.network import NetworkPolicy

    policy = NetworkPolicy(mode=getattr(cfg, "network_mode", "pull_only"))
    from oaset.provider_catalog import API_FORMATS

    api_format = pcfg.effective_api_format()
    if api_format not in API_FORMATS:
        raise ValueError(
            f"provider '{model_cfg.provider}': unsupported API format "
            f"'{api_format}' (supported: {', '.join(sorted(API_FORMATS))})")
    if api_format == "bedrock":
        from oaset.providers.bedrock_native import BedrockNativeProvider

        provider: Any = BedrockNativeProvider(
            model_id=model_cfg.id,
            access_key=api_key or "",
            secret_key=resolve_api_key(pcfg.base_url or "") or os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
            region=os.environ.get("AWS_REGION", "us-east-1"),
            policy=policy,
            prompt_cache=pcfg.prompt_cache,
        )
        provider.thinking_supported = model_cfg.has("thinking")
        return provider
    if api_format == "gemini":
        from oaset.providers.gemini_native import GeminiNativeProvider

        provider = GeminiNativeProvider(
            model_id=model_cfg.id,
            api_key=api_key or "EMPTY",
            base_url=pcfg.base_url or "https://generativelanguage.googleapis.com",
            reasoning_key=model_cfg.reasoning_key,
            policy=policy,
            timeout=pcfg.timeout or 300.0,
        )
        provider.thinking_supported = model_cfg.has("thinking")
        return provider
    if api_format == "anthropic":
        from oaset.providers.anthropic_native import AnthropicNativeProvider

        provider = AnthropicNativeProvider(
            model_id=model_cfg.id,
            api_key=api_key or "EMPTY",
            base_url=pcfg.base_url or "https://api.anthropic.com",
            reasoning_key=model_cfg.reasoning_key,
            policy=policy,
            prompt_cache=pcfg.prompt_cache,
            prompt_cache_ttl=pcfg.prompt_cache_ttl,
            timeout=pcfg.timeout or 300.0,
        )
        provider.thinking_supported = model_cfg.has("thinking")
        return provider
    if api_format == "azure":
        import os as _os

        from oaset.providers.azure_compat import AzureCompatProvider

        provider = AzureCompatProvider(
            model_id=model_cfg.id,
            api_key=api_key or "EMPTY",
            base_url=pcfg.base_url,
            deployment=model_cfg.model,
            api_version=_os.environ.get("OASET_AZURE_API_VERSION", "2024-10-21"),
            reasoning_key=model_cfg.reasoning_key,
            policy=policy,
            timeout=pcfg.timeout or 300.0,
            first_byte_timeout=pcfg.first_byte_timeout or 45.0,
        )
        provider.thinking_supported = model_cfg.has("thinking")
        return provider
    provider = OpenAICompatProvider(
        model_id=model_cfg.id,
        api_key=api_key or "EMPTY",
        base_url=pcfg.base_url,
        reasoning_key=model_cfg.reasoning_key,
        policy=policy,
        timeout=pcfg.timeout or 300.0,
        first_byte_timeout=pcfg.first_byte_timeout or 45.0,
    )
    provider.thinking_supported = model_cfg.has("thinking")
    return provider


def build_provider_chain(cfg: AppConfig, model_cfg: ModelConfig) -> tuple[Any, list[Any]]:
    """Primary provider + fallback chain (cfg.fallback_models order; the primary
    id and unresolvable ids are skipped)."""
    primary = build_provider(cfg, model_cfg)
    fallbacks: list[Any] = []
    for mid in cfg.fallback_models:
        if mid == model_cfg.id:
            continue
        try:
            fb_cfg = resolve_model(cfg, mid)
        except KeyError:
            continue
        fallbacks.append(build_provider(cfg, fb_cfg))
    return primary, fallbacks


def shell_config(cfg: AppConfig) -> dict[str, Any]:
    """Canonical run_shell backend dict shared by TUI, runner, gateway, cron, batch."""
    return {
        "backend": cfg.shell_backend,
        "ssh_target": cfg.ssh_target,
        "docker_container": cfg.docker_container,
        "singularity_image": cfg.singularity_image,
        "modal_container": cfg.modal_container,
        "daytona_target": cfg.daytona_target,
        "sandbox_default": cfg.shell_sandbox_default,
        "sandbox_memory_mb": cfg.shell_sandbox_memory_mb,
    }
