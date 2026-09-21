"""Model/provider management UI (P4-1 split from app.py).

Methods move verbatim; they keep working through `self` (OasetApp
inherits this mixin). Imports here cover what the moved bodies use;
anything else is imported locally inside a method, as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape
from textual import work

from oaset.config import build_provider, is_mock_provider, resolve_model, save_config
from oaset.i18n import t
from oaset.tui.widgets.status_bar import fmt_k
from oaset.utils import run_io

if TYPE_CHECKING:
    from oaset.tui.notice import UiNotice


def _has_credential(provider: str) -> bool:
    """True when the credentials store holds a key for `provider`."""
    from oaset.credentials import load_credential

    try:
        return bool(load_credential(provider))
    except Exception:
        return False


def _parse_caps(raw: str) -> list[str]:
    return [c.strip() for c in (raw or "").split(",") if c.strip()]


class ModelAdminMixin:
    if TYPE_CHECKING:
        last_error: "UiNotice | None"

    """Model/provider management UI (P4-1 split from app.py)."""

    @work(group="models", exclusive=False)
    async def _discover_local_models(self) -> None:
        """Ask loopback servers which tags they actually have (Ollama / LM Studio)."""
        from oaset.local_models import refresh_local_models

        try:
            await refresh_local_models(self.cfg, network_mode=self.cfg.network_mode)
        except Exception:
            return

    @work(group="command", exclusive=True)
    async def cmd_model(self, args: str) -> None:
        """`/model <id>` switches directly; bare `/model` picks, adds or lists.

        The picker keeps the common case (switch) one keystroke away and puts
        the guided setup + inventory in the same list, so `/model` is a
        complete surface rather than a switch-only stub.
        """
        sub = args.strip()
        if sub.lower() in ("add", "new"):
            await self._onboarding_flow()
            return
        if sub.lower() in ("list", "ls"):
            self._models_list()
            return
        if sub.lower() == "verify":
            await self._verify_model_picker()
            return
        if sub:
            await self._switch_model_verified(sub)
            return
        options = self._model_picker_options()
        choice = await self._pick(t("picker_model"), options)
        if choice == "__add__":
            await self._onboarding_flow()
        elif choice == "__list__":
            self._models_list()
        elif choice:
            await self._switch_model_verified(choice)

    @work(group="command", exclusive=True)
    async def cmd_models(self, args: str) -> None:
        """`/models` — same surface as /model, kept as a direct entry point.

        The registry has one entry for this surface (/model, with /md and
        /models as aliases), so dispatch normally lands on cmd_model. This
        handler stays for direct callers and is now a thin delegate: it used to
        carry a second, divergent model menu of its own (_models_menu), which
        meant two implementations of the same screen drifting apart.
        """
        sub = args.strip().lower()
        if sub in ("list", "ls"):
            self._models_list()
            return
        if sub in ("add", "new"):
            await self._onboarding_flow()
            return
        if sub == "verify":
            await self._verify_model_picker()
            return
        if sub:
            await self._switch_model_verified(args.strip())
            return
        # cmd_model is @work (returns a Worker, not a coroutine), so it is
        # invoked, not awaited.
        self.cmd_model("")

    def _models_list(self) -> None:
        lines = [f"[b]{t('models_menu_list')}[/b]"]
        for name, p in sorted(self.cfg.providers.items()):
            from oaset.config import provider_credential_state

            ready = provider_credential_state(self.cfg, name) == "ready"
            key_state = t("models_key_stored") if ready else t("models_key_missing")
            # The offline mock has no endpoint to show: a fake URL here made
            # users try to open it and made the switch path look broken.
            where = t("models_mock_builtin") if is_mock_provider(name, p) \
                else escape(p.base_url)
            lines.append(f"  · {escape(name)}  {escape(p.effective_api_format())}  "
                         f"{where}  {key_state}")
        for mid, m in sorted(self.cfg.models.items()):
            marker = "●" if mid == self.cfg.default_model else " "
            lines.append(f"{marker} {escape(mid)}  ctx={m.max_context_size} "
                         f"caps={escape(','.join(m.capabilities)) or '-'}")
        self.chat.add_card("\n".join(lines))

    async def _verify_model_picker(self) -> None:
        picked = await self._pick(
            t("picker_model"),
            [(mid, f"{m.display_name or mid} · {mid}")
             for mid, m in sorted(self.cfg.models.items())],
        )
        if picked:
            await self._verify_model(picked)

    async def _switch_model_verified(self, model_id: str) -> None:
        """Verify the candidate provider BEFORE touching the live runtime.

        A failed probe (bad key, wrong base URL, unsupported format) must leave
        the current model, provider, session metadata and defaults untouched —
        the invalid switch in a 401 screenshot must not break the session.
        """
        current = self.model_cfg.id
        from oaset.config import model_availability

        state, provider_name = model_availability(self.cfg, model_id)
        if state == "needs_key":
            self.notify_error(
                t("model_needs_key_msg", model=model_id, provider=provider_name),
                code="model.no_credential", source="model",
                hint=t("model_needs_key_hint"))
            return
        try:
            model_cfg = resolve_model(self.cfg, model_id)
            from oaset.config import build_provider_chain

            provider = build_provider(self.cfg, model_cfg)
            fallbacks = build_provider_chain(self.cfg, model_cfg)[1]
        except (KeyError, ValueError) as exc:
            self.notify_error(exc, code="model.unknown", source="model",
                              hint=t("model_unknown_hint"))
            return
        self.chat.add_notice(t("model_switch_verifying", model=model_id), "info")
        ok, detail = await self._probe_provider(provider)
        if not ok:
            self.notify_error(
                f"{t('model_switch_failed', model=model_id, current=current)} — {detail}",
                code="model.verify_failed", source="model",
                hint=t("wizard_verify_hint"))
            return
        persisted = self._apply_model_switch(model_cfg, provider, fallbacks)
        self.chat.add_notice(
            t("model_switch_ok_persisted" if persisted else "model_switch_ok_session_only",
              model=model_id),
            "info")
        if detail:
            self.chat.add_notice(f"✓ {detail}", "info")

    def _apply_model_switch(self, model_cfg, provider, fallbacks) -> bool:
        """Atomically install the verified provider as the live runtime.

        Returns whether the new default was persisted to config.toml (the
        session switch itself always happens).
        """
        self.model_cfg = model_cfg
        old = self.provider
        self.provider = provider
        self._fallbacks = fallbacks
        self._kernel.provider = provider
        # the kernel hands ITS fallback chain to the agent loop: leaving it on
        # the previous model's chain made provider errors fail over to the
        # model the user just switched away from
        self._kernel.fallbacks = fallbacks
        self._kernel.context_max_tokens = model_cfg.max_context_size
        # re-seed the thinking level from the NEW model's config unless the
        # user turned the dial themselves this session (/think sets the flag)
        _state = self.registry.ctx.session_state
        if not _state.get("thinking_level_set"):
            from oaset.thinking import normalize_level

            _state.pop("thinking_level", None)
            _seed = normalize_level(getattr(model_cfg, "thinking", "") or "")
            if _seed:
                _state["thinking_level"] = _seed
        self.status_bar.set_thinking(
            str(_state.get("thinking_level") or ""))
        if old is not None and old is not provider:
            aclose = getattr(old, "aclose", None)
            if callable(aclose):
                self._deferred_provider_close = self._chain_close(
                    getattr(self, "_deferred_provider_close", None), aclose)
        self.session.meta.model = model_cfg.id
        if "subagent_runner" in self.registry.ctx.session_state:
            self.registry.ctx.session_state["subagent_runner"] = self._make_subagent_runner(
                provider, model_cfg.max_context_size)
        # A verified, user-initiated switch becomes the persisted default - but
        # never silently: a failed write used to leave the session on the new
        # model while the next launch reverted, with nothing said.
        self.cfg.default_model = model_cfg.id
        self.cfg.default_provider = model_cfg.provider
        persisted = True
        try:
            save_config(self.cfg)
        except Exception as exc:
            persisted = False
            self.notify_error(exc, code="config.write_failed", source="config",
                              hint=t("config_write_failed_hint"))
        self.status_bar.set_model(model_cfg.id, model_cfg.provider)
        self.status_bar.set_needs_key(self._model_needs_key())
        return persisted

    async def cmd_think(self, args: str) -> None:
        """/think [off|light|medium|heavy] — reasoning depth, per provider.

        One dial, mapped to whatever the active model actually accepts
        (Anthropic/Bedrock thinking budget, OpenAI/Azure reasoning_effort,
        Gemini thinkingBudget, GLM/Qwen on-off). The display states the
        real knob — models whose API has NO request-level control (DeepSeek
        and Kimi pick thinking by model name) are told as such instead of
        silently pretending.
        """
        from oaset.thinking import LEVELS, describe, normalize_level

        state = self.registry.ctx.session_state
        raw = args.strip().lower()
        api_format = ""
        try:
            api_format = self.cfg.providers[self.model_cfg.provider].effective_api_format()
        except Exception:
            pass
        kind, param = describe(self.model_cfg.id, api_format)
        if not self.model_cfg.has("thinking"):
            # capabilities decide; the provider gates on the same flag — a
            # model without the capability 400s on the thinking parameter
            kind, param = "none", ""
        knob = param or t("think_no_knob")
        notes = []
        if kind in ("toggle", "qwen_toggle"):
            notes.append(t("think_toggle_only"))
        elif kind == "none":
            notes.append(t("think_model_only"))
        elif kind == "budget":
            notes.append(t("think_budget_note"))

        if not raw:
            current = normalize_level(state.get("thinking_level")) \
                or t("think_provider_default")
            lines = [
                f"[b]/think — {t('think_title')}[/b]",
                f"  {t('think_current', level=current)}",
                f"  {t('think_knob', param=knob)}",
                "",
                f"  {t('think_hint', levels=' / '.join(LEVELS))}",
            ]
            lines.extend(f"  [dim]{note}[/dim]" for note in notes)
            self.chat.add_card("\n".join(lines))
            return

        level = normalize_level(raw)
        if not level:
            self.chat.add_notice(
                t("think_unknown", levels=" / ".join(LEVELS)), "warn")
            return
        state["thinking_level"] = level
        # the user turned the dial: /model switches stop re-seeding it
        state["thinking_level_set"] = True
        self.status_bar.set_thinking(level)
        self.chat.add_notice(
            t("think_set", level=level) + "  " + t("think_knob", param=knob),
            "info")
        for note in notes:
            self.chat.add_notice(note, "warn")

    async def _probe_provider(self, provider) -> tuple[bool, str]:
        """One minimal completion; shared by switch and the setup wizard."""
        from oaset.providers.mock import MockProvider

        if isinstance(provider, MockProvider):
            # Offline by construction: probing it over the network made
            # /model mock switches fail with Connection error on a machine
            # that was working exactly as designed.
            return True, t("probe_mock_ok")
        try:
            chunks: list[str] = []
            async for ev in provider.stream(
                [{"role": "user", "content": "Reply with exactly: OK"}], tools=[],
            ):
                if type(ev).__name__ == "ContentDelta":
                    chunks.append(ev.text)
            text = "".join(chunks).strip()
            if text:
                return True, text[:30]
            return False, "empty response"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    async def _verify_model(self, model_id: str) -> None:
        try:
            model_cfg = resolve_model(self.cfg, model_id)
            provider = build_provider(self.cfg, model_cfg)
        except (KeyError, ValueError) as exc:
            self.notify_error(exc, code="model.unknown", source="model",
                              hint=t("model_unknown_hint"))
            return
        ok, detail = await self._probe_provider(provider)
        if ok:
            self.chat.add_notice(t("wizard_verify_ok", model=model_id)
                                 + (f" — {detail}" if detail else ""), "info")
        else:
            self.notify_error(
                t("wizard_verify_failed", model=model_id, error=detail),
                code="model.verify_failed", source="model",
                hint=t("wizard_verify_hint"))

    # ------------------------------------------------- guided provider/model setup

    async def _onboarding_flow(self) -> None:
        """Guided setup: vendor → base URL → API format → model → key → save.

        Every step resolves through InlinePicker/InlineInput, so Escape cancels
        and nothing is written until the final confirmation. The API key goes
        to the credentials store, never into config.toml.
        """
        from oaset.provider_catalog import (
            API_FORMATS,
            CUSTOM_VENDOR,
            VENDORS,
            default_api_format,
            default_base_url,
            validate_api_format,
            validate_base_url,
        )

        configured = list(self.cfg.providers)
        options: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name in sorted(configured):
            pcfg = self.cfg.providers[name]
            from oaset.config import provider_credential_state

            cfg_state = provider_credential_state(self.cfg, name)
            state_label = t("wizard_vendor_ready") if cfg_state == "ready"                 else t("wizard_vendor_needs_key")
            options.append((f"cfg:{name}",
                            f"{name} · {pcfg.effective_api_format()} · {pcfg.base_url} "
                            f"({t('wizard_vendor_configured')}·{state_label})"))
            seen.add(name)
        for item in VENDORS:
            if item.id in seen:
                continue
            options.append((f"vendor:{item.id}",
                            f"{item.label} · {item.api_format} · {item.base_url}"))
        options.append((CUSTOM_VENDOR, t("wizard_vendor_custom")))

        choice = await self._pick(t("wizard_vendor_title"), options)
        if not choice:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return

        # ---- provider identity + transport --------------------------------
        if choice.startswith("cfg:"):
            provider_name = choice[4:]
            pcfg = self.cfg.providers[provider_name]
            api_format = pcfg.effective_api_format()
            base_url = pcfg.base_url
        else:
            if choice == CUSTOM_VENDOR:
                raw = await self._input(t("wizard_provider_name"))
                if not raw:
                    self.chat.add_notice(t("wizard_cancelled"), "info")
                    return
                provider_name = raw.strip()
                vendor_id = provider_name
            else:
                vendor_id = choice.split(":", 1)[1]
                provider_name = vendor_id
            # plan order: Base URL (vendor default or custom) → API format
            picked_url = await self._pick_base_url(
                provider_name, default_base_url(vendor_id), validate_base_url)
            if picked_url is None:
                self.chat.add_notice(t("wizard_cancelled"), "info")
                return
            base_url = picked_url
            chosen_format = await self._pick(
                t("wizard_api_format_title", provider=provider_name),
                [(fmt, t(label)) for fmt, label in API_FORMATS.items()])
            if not chosen_format:
                self.chat.add_notice(t("wizard_cancelled"), "info")
                return
            try:
                api_format = validate_api_format(chosen_format)
            except ValueError as exc:
                self.notify_error(exc, code="model.bad_format", source="model")
                return
        if not api_format:
            api_format = validate_api_format(default_api_format(provider_name))

        # ---- credential first: discovery and the first turn both need it ----
        key = await self._input(
            t("wizard_api_key", provider=provider_name), "", password=True)
        if key is None:
            # Esc means cancel — an empty string is "keep the existing key"
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return

        # ---- model identity ------------------------------------------------
        raw_model = await self._input(
            t("wizard_model_id"), "provider/model or model-name")
        if not raw_model:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        model_id = raw_model.strip()
        if "/" not in model_id:
            model_id = f"{provider_name}/{model_id}"

        # ---- limits: discovered, then vendor defaults, then generic --------
        effective_key = (key or "").strip() or self._existing_key(provider_name)
        context, max_output, limits_source = await self._resolve_model_limits(
            provider_name, base_url, effective_key, model_id, api_format)
        self.chat.add_notice(
            t("wizard_limits_resolved", context=context, output=max_output,
              source=t(f"wizard_limits_from_{limits_source}")), "info")

        caps_raw = await self._input(t("wizard_capabilities"), "tool_use")
        if caps_raw is None:
            self.chat.add_notice(t("wizard_cancelled"), "warn")
            return
        display = await self._input(t("wizard_display_name"), model_id)
        if display is None:
            self.chat.add_notice(t("wizard_cancelled"), "warn")
            return
        reasoning = await self._input(t("wizard_reasoning_key"), "")
        if reasoning is None:
            self.chat.add_notice(t("wizard_cancelled"), "warn")
            return

        # ---- preview + confirm --------------------------------------------
        def preview_text() -> str:
            return "\n".join([
                f"[b]{t('wizard_preview_title')}[/b]",
                f"  provider : {provider_name}",
                f"  api format: {api_format}",
                f"  base url : {base_url or '(provider default)'}",
                f"  model    : {model_id}",
                f"  context  : {context} / output {max_output}"
                f"  [{t('wizard_limits_from_' + limits_source)}]",
                f"  caps     : {', '.join(_parse_caps(caps_raw)) or '-'}",
                f"  display  : {display or model_id}",
                f"  reasoning: {reasoning or '-'}",
                f"  api key  : {'(will be stored)' if key else t('wizard_api_key_kept')}",
            ])

        # the confirm step loops so "edit limits" returns here with new values
        while True:
            action = await self._pick(preview_text(), [
                ("save", t("wizard_save")),
                ("save_verify", t("wizard_save_verify")),
                ("edit_limits", t("wizard_edit_limits")),
                ("cancel", t("wizard_cancel")),
            ])
            if action != "edit_limits":
                break
            edited_context = await self._input_positive_int(
                t("wizard_context"), str(context))
            if edited_context is None:
                return
            edited_output = await self._input_positive_int(
                t("wizard_max_output"), str(max_output))
            if edited_output is None:
                return
            context, max_output, limits_source = edited_context, edited_output, "manual"
        if action not in ("save", "save_verify"):
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return

        from oaset.config import ModelConfig, ProviderConfig

        existing = self.cfg.providers.get(provider_name)
        self.cfg.providers[provider_name] = ProviderConfig(
            name=provider_name,
            type=api_format,
            api_key=existing.api_key if existing else "",
            base_url=base_url,
            prompt_cache=existing.prompt_cache if existing else False,
            api_format=api_format,
        )
        self.cfg.models[model_id] = ModelConfig(
            id=model_id, provider=provider_name, model=model_id.split("/")[-1],
            max_context_size=context, max_output_size=max_output,
            capabilities=_parse_caps(caps_raw),
            display_name=display or model_id,
            reasoning_key=reasoning or None,
        )
        if key:
            from oaset.credentials import save_credential

            save_credential(provider_name, key.strip())
            self.chat.add_notice(t("wizard_api_key_saved"), "info")
        try:
            save_config(self.cfg)
        except Exception as exc:
            self.notify_error(exc, code="config.write_failed", source="config")
            return
        self.chat.add_notice(
            t("wizard_saved", provider=provider_name, model=model_id), "info")
        if action == "save_verify":
            await self._verify_model(model_id)

    async def cmd_insights(self, args: str) -> None:
        try:
            days = int(args) if args.strip().isdigit() else 7
        except ValueError:
            days = 7
        # reads every session file on disk — off the UI thread (S2)
        effective_days = min(max(days, 1), 90)
        rows = await run_io(self.store.usage_by_days, days=effective_days)
        if not rows:
            self.chat.add_notice(t("no_usage"), "info")
            return
        lines = []
        for day, a in rows:
            bar = "▇" * max(1, min(20, a["total_tokens"] // 5000))
            lines.append(
                f"{day}  {bar} {fmt_k(a['total_tokens'])} tok · {a['turns']} turns "
                f"(in {fmt_k(a['prompt_tokens'])} / out {fmt_k(a['completion_tokens'])})"
            )
        # the title must state the window actually scanned, not the raw ask
        self.chat.add_card("[b]/insights — " + t("insights_days", days=effective_days)
                           + "[/b]\n" + "\n".join(lines))

    async def cmd_usage(self, args: str) -> None:
        s = await run_io(self.store.session_usage, self.session.meta.session_id)
        line = t("usage_line", entries=s["entries"], prompt=s["prompt_tokens"],
                 completion=s["completion_tokens"], total=s["total_tokens"])
        # cumulative cost estimate, only when the current model is priced
        priced = self.cfg.models.get(self.model_cfg.id, self.model_cfg)
        cost = priced.cost(s.get("prompt_tokens"), s.get("completion_tokens"))
        if cost is not None:
            shown = f"${cost:.4f}" if cost < 0.01 else f"${cost:.2f}"
            line += "\n" + t("usage_cost", cost=shown)
        self.chat.add_notice(line, "info")

    async def cmd_login(self, args: str) -> None:
        """/login: store a provider API key locally (P3-5, cloud as a
        first-class path — local-first must not become a ceiling)."""
        from oaset.credentials import save_credential

        providers = sorted(self.cfg.providers)
        provider = args.strip().lower()
        if not provider:
            choice = await self._pick(t("login_pick_provider"),
                                      [(p, p) for p in providers])
            if not choice:
                return
            provider = choice
        if provider not in self.cfg.providers:
            self.notify_error(t("login_unknown_provider", provider=provider,
                                known=", ".join(providers)),
                              code="login.unknown_provider", source="login")
            return
        key = await self._input(t("login_key_title", provider=provider),
                                t("login_key_placeholder"), password=True)
        if not key:
            self.chat.add_notice(t("login_cancelled"), "info")
            return
        try:
            await run_io(save_credential, provider, key)
        except Exception as exc:
            self.notify_error(exc, source="login", code="login.save_failed",
                              hint=t("login_failed_hint"))
            return
        self.chat.add_notice(t("login_stored", provider=provider), "info")
        if self.model_cfg.provider == provider:
            self._activate_logged_in_provider(provider)
        else:
            self.chat.add_notice(t("login_next_step", provider=provider), "info")

    def _activate_logged_in_provider(self, provider: str) -> None:
        """Rebuild the live client so /login is enough — no extra /model hop."""
        try:
            model_cfg = resolve_model(self.cfg, self.model_cfg.id)
            from oaset.config import build_provider_chain

            new_provider = build_provider(self.cfg, model_cfg)
            fallbacks = build_provider_chain(self.cfg, model_cfg)[1]
        except Exception as exc:
            self.notify_error(exc, source="login", code="login.activate_failed",
                              hint=t("login_next_step", provider=provider))
            return
        old = self.provider
        self.provider = new_provider
        self._fallbacks = fallbacks
        self._kernel.provider = new_provider
        self._kernel.fallbacks = fallbacks
        if old is not None and old is not new_provider:
            aclose = getattr(old, "aclose", None)
            if callable(aclose):
                self._deferred_provider_close = self._chain_close(
                    getattr(self, "_deferred_provider_close", None), aclose)
        if "subagent_runner" in self.registry.ctx.session_state:
            self.registry.ctx.session_state["subagent_runner"] = self._make_subagent_runner(
                new_provider, model_cfg.max_context_size)
        self.status_bar.clear_error()
        self.last_error = None
        self.status_bar.set_model(model_cfg.id, model_cfg.provider)
        self.status_bar.set_needs_key(self._model_needs_key())
        self._refresh_welcome_after_login()
        self.chat.add_notice(t("login_ready"), "info")

    def _refresh_welcome_after_login(self) -> None:
        self._refresh_welcome()

    def _model_needs_key(self) -> bool:
        from oaset.config import model_availability

        state, _ = model_availability(self.cfg, self.model_cfg.id)
        return state != "ready"

    def _model_ready_for_send(self) -> bool:
        """No-key preflight (blind-review fix 1): never fire a real request
        with placeholder credentials. The startup warning exists but clears on
        the first submit (the user "moved on"), so the gate lives in the send
        path — the message is refused locally with an actionable hint
        (/login) instead of surfacing a raw 401."""
        if not self._model_needs_key():
            return True
        if str(getattr(self.provider, "model_id", "")).startswith("mock/"):
            return True  # injected mock provider: offline by contract
        self.notify_error(
            t("send_needs_key", model=self.model_cfg.id),
            code="model.no_credential", source="model",
            hint=t("send_needs_key_hint", provider=self.model_cfg.provider))
        return False
