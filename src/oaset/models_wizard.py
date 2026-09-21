"""MODEL-01: `oaset models add` interactive provider/model onboarding wizard.

Adds a provider/model pair to config.toml through explicit prompts, then
offers an immediate connectivity probe via the same code path as
`oaset models verify`. Never switches default_model/default_provider and
never writes credentials anywhere except the env-reference the user names.

Prompts follow the configured language (they were Chinese-only regardless of
``ui_language``), and a closed stdin means "cancelled" instead of a traceback.
"""

from __future__ import annotations

from typing import Any


class WizardCancelled(Exception):
    """No answer was available (EOF / Ctrl-C): nothing was changed."""


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    try:
        if secret:
            import getpass as _gp

            value = _gp.getpass(f"{prompt} [{default}]: ").strip()
        else:
            value = input(f"{prompt} [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        # A piped/closed stdin used to raise EOFError out of the wizard; an
        # empty answer is not consent to invent a provider, so abort cleanly.
        raise WizardCancelled(str(exc)) from exc
    return value or default


def _to_int(text: str, default: int) -> int:
    try:
        return int(text)
    except ValueError:
        return default


def run_models_add_wizard(cfg: Any) -> dict[str, Any]:
    """Interactive provider/model onboarding. Returns the applied summary.

    Mutates only the in-memory ``cfg``; persisting is the caller's choice so
    the wizard can be preview-only.

    Raises ValueError on unusable input and WizardCancelled when there is no
    input left to read.
    """
    from oaset.i18n import t

    print(t("models_add_title"))
    print(t("models_add_configured", list=", ".join(sorted(cfg.providers)) or "(none)"))

    provider_name = _ask(t("models_add_provider_prompt")).strip()
    if not provider_name:
        raise ValueError(t("models_add_provider_empty"))

    if provider_name in cfg.providers:
        provider = cfg.providers[provider_name]
        base_url = provider.base_url
        print(t("models_add_reusing", provider=provider_name, url=base_url))
    else:
        base_url = _ask(t("models_add_base_url", provider=provider_name)).rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(t("models_add_base_url_bad", url=base_url))
        from oaset.config import ProviderConfig

        cfg.providers[provider_name] = ProviderConfig(
            name=provider_name, type="openai", api_key="", base_url=base_url,
        )
        print(t("models_add_added", provider=provider_name))

    model_id = _ask(t("models_add_model_id")).strip()
    if not model_id:
        raise ValueError(t("models_add_model_empty"))
    if "/" not in model_id:
        model_id = f"{provider_name}/{model_id}"

    context = _to_int(_ask(t("models_add_context"), "128000"), 128000)
    output = _to_int(_ask(t("models_add_output"), "32768"), 32768)
    caps = [c.strip() for c in _ask(t("models_add_caps"), "tool_use").split(",") if c.strip()]

    from oaset.config import ModelConfig

    cfg.models[model_id] = ModelConfig(
        id=model_id, provider=provider_name, model=model_id.split("/")[-1],
        max_context_size=context, max_output_size=output, capabilities=caps,
        display_name=model_id,
    )
    return {
        "provider": provider_name, "base_url": base_url,
        "model": model_id, "context": context, "capabilities": caps,
    }
