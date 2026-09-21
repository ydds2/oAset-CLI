"""Thinking-effort adaptation — one dial, every provider's real knob.

Users ask for "more/less thinking", not for ``reasoning_effort`` vs
``budget_tokens`` vs ``thinkingConfig``. This module owns the mapping from
four oAset levels (off / light / medium / heavy) to whatever the active
provider actually accepts, and states honestly when a provider has no knob
(DeepSeek/Kimi pick thinking by MODEL name, not by request parameter —
pretending otherwise would be a fake dial).

Wire format reference (public docs each provider publishes):

- Anthropic (and Bedrock anthropic-style): ``thinking.type=enabled`` +
  ``budget_tokens`` (>=1024, must be < max_tokens, temperature forced to 1).
- OpenAI / Azure chat.completions: ``reasoning_effort`` low|medium|high;
  gpt-5 adds ``minimal`` (the closest thing to "off"), o-series has no off.
- Gemini: ``generationConfig.thinkingConfig.thinkingBudget`` (0 disables on
  Flash; 2.5 Pro cannot go below 128), ``thinkingLevel`` on gemini-3 which
  is adaptive and cannot be disabled.
- GLM (zhipu, OpenAI-compatible): ``thinking.type`` enabled|disabled —
  on/off only, levels collapse.
- Qwen (DashScope, OpenAI-compatible): ``enable_thinking`` bool — on/off
  only.
"""

from __future__ import annotations

LEVELS = ("off", "light", "medium", "heavy")

_ALIASES = {
    "none": "off", "disable": "off", "disabled": "off", "false": "off",
    "low": "light", "minimal": "light", "min": "light",
    "mid": "medium", "default": "medium", "normal": "medium",
    "high": "heavy", "max": "heavy", "deep": "heavy", "ultra": "heavy",
}

# Token budgets for the two budget-style APIs. Light stays above the
# Anthropic 1024 floor; heavy stays under a 32k output ceiling so
# max_tokens adjustments remain valid on every current model.
_BUDGETS = {"light": 2048, "medium": 8192, "heavy": 24576}

_EFFORT = {"light": "low", "medium": "medium", "heavy": "high"}


def normalize_level(raw: object) -> str:
    """''-on-blank, alias-tolerant (low/minimal→light, high/max→heavy)."""
    text = str(raw or "").strip().lower()
    if text in LEVELS:
        return text
    return _ALIASES.get(text, "")


def _parts(model_id: str) -> tuple[str, str]:
    key = (model_id or "").split("/", 1)[0].strip().lower()
    name = (model_id or "").split("/", 1)[-1].strip().lower()
    return key, name


def thinking_params(model_id: str, level: str, api_format: str = "") -> dict:
    """Request fragment for ``level`` on this model, {} when unsupported.

    ``off`` on Anthropic returns an explicit ``{"thinking": None}`` marker —
    the caller must DELETE the key (a capability default may have set it),
    and None is never a valid wire value.
    """
    level = normalize_level(level)
    if not level:
        return {}
    fmt = (api_format or "").strip().lower()
    key, name = _parts(model_id)
    if fmt in ("anthropic", "bedrock"):
        if level == "off":
            return {"thinking": None}
        return {"thinking": {"type": "enabled",
                             "budget_tokens": _BUDGETS[level]}}
    if fmt == "gemini":
        if "gemini-3" in name:
            # adaptive levels; there is no off
            return {"generationConfig": {"thinkingConfig": {
                "thinkingLevel": "low" if level in ("off", "light") else "high",
            }}}
        if level == "off":
            budget = 128 if "pro" in name else 0  # 2.5 Pro cannot disable
            # includeThoughts with a zero budget is self-contradictory
            # (some endpoints reject the pair); thought summaries off too
            return {"generationConfig": {"thinkingConfig": {
                "thinkingBudget": budget, "includeThoughts": False,
            }}}
        budget = _BUDGETS[level]
        return {"generationConfig": {"thinkingConfig": {
            "thinkingBudget": budget, "includeThoughts": True,
        }}}
    # openai / azure / openai-compatible families, by provider key
    if key in ("openai", "azure"):
        if level == "off":
            return {"reasoning_effort": "minimal"} if "gpt-5" in name else {}
        return {"reasoning_effort": _EFFORT[level]}
    if key in ("glm", "zhipu"):
        return {"thinking": {"type": "disabled" if level == "off" else "enabled"}}
    if key == "qwen":
        return {"enable_thinking": level != "off"}
    return {}  # deepseek/moonshot/unknown: no request-level knob


def describe(model_id: str, api_format: str = "") -> tuple[str, str]:
    """(kind, param_text) for honest /think display.

    kind ∈ effort | budget | gemini_budget | toggle | qwen_toggle | none —
    the UI says what will actually be sent (or that levels collapse to
    on/off, or that this model picks thinking by its name, not by us).
    """
    fmt = (api_format or "").strip().lower()
    key, name = _parts(model_id)
    if fmt in ("anthropic", "bedrock"):
        return "budget", "thinking.budget_tokens"
    if fmt == "gemini":
        if "gemini-3" in name:
            return "gemini3_level", "thinkingConfig.thinkingLevel"
        return "gemini_budget", "thinkingConfig.thinkingBudget"
    if key in ("openai", "azure"):
        return "effort", "reasoning_effort"
    if key in ("glm", "zhipu"):
        return "toggle", "thinking.type"
    if key == "qwen":
        return "qwen_toggle", "enable_thinking"
    return "none", ""


def apply_thinking(payload: dict, model_id: str, level: str,
                   api_format: str = "") -> None:
    """Merge the level's fragment into a request payload IN PLACE.

    Deletes ``thinking`` for off (Anthropic style), deep-merges nested
    dicts (Gemini generationConfig may already carry a thinkingConfig from
    a native capability), and raises ``max_tokens`` above the budget —
    Anthropic rejects budget >= max_tokens, and the default 8192 would
    make every heavy request a 400.
    """
    level = normalize_level(level)
    if not level:
        return
    params = thinking_params(model_id, level, api_format)
    fmt = (api_format or "").strip().lower()
    if fmt in ("anthropic", "bedrock"):
        if params.get("thinking") is None:
            payload.pop("thinking", None)
            return
        budget = int(params["thinking"]["budget_tokens"])
        if int(payload.get("max_tokens", 0) or 0) <= budget:
            payload["max_tokens"] = budget + 4096
    for field, value in params.items():
        if isinstance(value, dict) and isinstance(payload.get(field), dict):
            existing = payload[field]
            for sub, subval in value.items():
                if isinstance(subval, dict) and isinstance(existing.get(sub), dict):
                    existing[sub].update(subval)
                else:
                    existing[sub] = subval
        else:
            payload[field] = value
