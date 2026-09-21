"""Provider vendor catalog + API-format vocabulary for the TUI setup flow.

One declarative source of truth so the wizard, `/config` output and provider
construction cannot drift apart:

- API_FORMATS maps the user-visible format id to the provider implementation
  that actually speaks it. An unknown format is REJECTED — never silently
  served as OpenAI-compatible.
- VENDORS is the built-in directory offered by the picker (name, default base
  URL, API format, docs hint). Anything not listed is reachable through the
  "custom provider" path with the same fields.
"""

from __future__ import annotations

from dataclasses import dataclass

# Format id -> human label key (i18n) — the provider class routing lives in
# config.build_provider, which maps format -> implementation.
API_FORMATS: dict[str, str] = {
    "openai": "api_format_openai",        # OpenAI-compatible /chat/completions
    "azure": "api_format_azure",          # Azure OpenAI deployment URL + api-version
    "anthropic": "api_format_anthropic",  # Anthropic Messages API
    "gemini": "api_format_gemini",        # Google Generative Language API
    "bedrock": "api_format_bedrock",      # AWS Bedrock Converse
}

DEFAULT_API_FORMAT = "openai"


@dataclass(frozen=True)
class Vendor:
    id: str
    label: str
    base_url: str
    api_format: str
    hint: str = ""
    # well-known limits, used when the provider's /models listing has none, so
    # the user never has to type context/output sizes
    context: int = 0
    max_output: int = 0


VENDORS: tuple[Vendor, ...] = (
    Vendor("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "openai",
           "OpenAI-compatible; reasoning via reasoning_content", 128000, 8192),
    Vendor("moonshot", "Moonshot / Kimi", "https://api.moonshot.cn/v1", "openai",
           "OpenAI-compatible", 131072, 16384),
    Vendor("openai", "OpenAI", "https://api.openai.com/v1", "openai",
           "Official OpenAI endpoint", 128000, 16384),
    Vendor("azure", "Azure OpenAI", "https://YOUR-RESOURCE.openai.azure.com", "azure",
           "deployment name = the model field; key from the Azure portal"),
    Vendor("anthropic", "Anthropic", "https://api.anthropic.com", "anthropic",
           "Native Messages API", 200000, 8192),
    Vendor("gemini", "Google Gemini", "https://generativelanguage.googleapis.com",
           "gemini", "Native Generative Language API", 1000000, 65536),
    Vendor("bedrock", "AWS Bedrock", "https://bedrock-runtime.us-east-1.amazonaws.com",
           "bedrock", "Native Converse API; region from AWS_REGION", 200000, 8192),
    Vendor("glm", "Zhipu GLM", "https://open.bigmodel.cn/api/anthropic", "anthropic",
           "Anthropic-compatible endpoint", 128000, 8192),
    Vendor("qwen", "Alibaba Qwen (DashScope)",
           "https://dashscope.aliyuncs.com/compatible-mode/v1", "openai",
           "OpenAI-compatible mode", 131072, 8192),
    Vendor("ark", "Volcengine Ark (Doubao)",
           "https://ark.cn-beijing.volces.com/api/v3", "openai",
           "OpenAI-compatible; the model field is the ENDPOINT ID (ep-...)",
           131072, 16384),
    Vendor("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "openai",
           "OpenAI-compatible aggregator", 128000, 16384),
    Vendor("siliconflow", "SiliconFlow", "https://api.siliconflow.cn/v1", "openai",
           "OpenAI-compatible", 131072, 8192),
    Vendor("groq", "Groq", "https://api.groq.com/openai/v1", "openai",
           "OpenAI-compatible", 131072, 8192),
    Vendor("together", "Together AI", "https://api.together.xyz/v1", "openai",
           "OpenAI-compatible", 131072, 8192),
    Vendor("mistral", "Mistral", "https://api.mistral.ai/v1", "openai",
           "OpenAI-compatible", 131072, 8192),
    Vendor("xai", "xAI Grok", "https://api.x.ai/v1", "openai",
           "OpenAI-compatible", 131072, 16384),
    Vendor("minimax", "MiniMax", "https://api.minimax.chat/v1", "openai",
           "OpenAI-compatible", 245760, 8192),
    Vendor("ollama", "Ollama (local)", "http://127.0.0.1:11434/v1", "openai",
           "Local server; OpenAI-compatible", 32768, 8192),
    Vendor("lmstudio", "LM Studio (local)", "http://127.0.0.1:1234/v1", "openai",
           "Local server; OpenAI-compatible", 32768, 8192),
)

CUSTOM_VENDOR = "__custom__"


def vendor(vendor_id: str) -> Vendor | None:
    for item in VENDORS:
        if item.id == vendor_id:
            return item
    return None


def default_base_url(vendor_id: str) -> str:
    found = vendor(vendor_id)
    return found.base_url if found else ""


def default_api_format(vendor_id: str) -> str:
    found = vendor(vendor_id)
    return found.api_format if found else DEFAULT_API_FORMAT


def validate_api_format(fmt: str) -> str:
    fmt = (fmt or "").strip().lower()
    if fmt not in API_FORMATS:
        raise ValueError(
            f"unsupported API format {fmt!r}; supported: {', '.join(sorted(API_FORMATS))}")
    return fmt


def validate_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"base_url must start with http(s):// — got {url!r}")
    return url


def vendor_limits(vendor_id: str) -> tuple[int, int] | None:
    """(context, max_output) for a known vendor, or None when unknown."""
    found = vendor(vendor_id)
    if found is None or not (found.context or found.max_output):
        return None
    return found.context, found.max_output
