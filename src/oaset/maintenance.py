"""体验计划 (Experience Program) — opt-in token-powered self-maintenance.

One run = four phases, all recorded in update history:

1. check    — Update Center cache/TTL check (network only under policy)
2. plan     — catalog plan (compatible targets) + compatibility probe of the
              default model (one tiny completion through the real provider)
3. turn     — ONE budget-capped, read-only model turn (tools disabled) that
              reviews the phase-1/2 facts and emits a JSON maintenance plan
              {search, compatibility, patch_targets, self_update, summary}
4. act      — when cfg.maintenance.allow_apply: install exactly the model's
              patch_targets that exist in the catalog plan, through
              UpdateManager.apply (hash-gated, explicit targets, rollback-
              able, audited). A `self_update: "core"` recommendation joins the
              SAME audited batch via the transactional core apply (download →
              sha256 → backup → atomic replace → rollback); the running
              process keeps the old code until an explicit restart, which the
              result flags as restart_required. When allow_apply is False the
              plan — including the core recommendation — is reported and
              nothing is written.

Budget: the turn runs on the AgentKernel door (budget_tokens=
cfg.maintenance.budget_tokens); exhaustion stops the turn and the result
records it. Every phase failure is structured — a failed maintenance run
never touches user state.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from oaset.i18n import t
from oaset.update import UpdateError, UpdateManager

# Budget policy: a per-run allowance must be a positive number and is capped so
# a mistyped config value cannot authorize an unbounded model spend.
MIN_BUDGET_TOKENS = 1
MAX_BUDGET_TOKENS = 200_000


class MaintenanceError(Exception):
    """Structured policy refusal (feature disabled, invalid budget…).

    Raised BEFORE any work happens: a refused run downloads nothing, calls no
    provider and writes no history.
    """

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


@dataclass
class MaintenanceResult:
    phases: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    applied: list[str] = field(default_factory=list)
    compat: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    requested_apply: bool = False
    effective_apply: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    self_update: str = ""  # model recommendation: "core" / "none" / ""
    self_update_applied: bool = False
    restart_required: bool = False

    def render(self) -> str:
        lines = [t("maint_report_title")]
        for phase in self.phases:
            status = "✓" if phase.get("ok") else "✗"
            lines.append(f"{status} {phase['name']}: {phase.get('detail', '')}")
        if self.plan:
            lines.append(t("maint_plan_line", plan=json.dumps(self.plan, ensure_ascii=False)[:300]))
        if self.applied:
            lines.append(t("maint_applied_line", names=', '.join(self.applied)))
        if self.self_update == "core":
            if self.self_update_applied:
                lines.append(t("maint_core_applied"))
                lines.append(t("maint_restart_line"))
            else:
                lines.append(t("maint_core_reported"))
        if self.blocked_reasons:
            lines.append(t("maint_blocked_line", reasons="；".join(self.blocked_reasons)))
        if self.usage:
            lines.append(t("maint_usage_line", used=self.usage.get('total_tokens', 0), budget=self.usage.get('budget', '?')))
        for err in self.errors:
            lines.append(f"✗ {err}")
        return "\n".join(lines)


def _phase(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}


def _extract_plan_json(text: str) -> dict[str, Any] | None:
    """Pull the first {...} JSON object out of a (possibly fenced) reply."""
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        plan = json.loads(match.group(0))
        return plan if isinstance(plan, dict) else None
    except ValueError:
        return None


class SelfMaintenance:
    def __init__(self, cfg: Any, cwd: Any, *, provider: Any = None,
                 fetch: Any | None = None, home: Any | None = None,
                 gate: Any | None = None, package_dir: Any | None = None):
        self.cfg = cfg
        self.cwd = cwd
        self.provider = provider
        self.fetch = fetch
        self.home = home
        # Optional PermissionGate: when present, an apply needs an auditable
        # approval on top of the config flag — apply=True can never bypass it.
        self.gate = gate
        self.compat: dict[str, Any] = {}
        self.manager = UpdateManager(cfg, home=home, fetch=fetch,
                                     package_dir=package_dir)

    # ------------------------------------------------------------ policy gate

    def _effective_apply(self, apply: bool | None) -> tuple[bool, bool]:
        """Return (requested, effective). `effective` needs BOTH the caller's
        request and the durable config opt-in; a caller flag cannot turn on a
        capability the user's config keeps off."""
        config_allow = bool(getattr(self.cfg.maintenance, "allow_apply", False))
        requested = config_allow if apply is None else bool(apply)
        return requested, (requested and config_allow)

    def _resolve_budget(self, budget_tokens: int | None) -> int:
        raw = self.cfg.maintenance.budget_tokens if budget_tokens is None else budget_tokens
        if isinstance(raw, float) and not raw.is_integer():
            raise MaintenanceError(
                "bad_budget", t("maint_budget_float", raw=repr(raw)),
                t("maint_budget_float_hint"))
        try:
            budget = int(raw)
        except (TypeError, ValueError) as exc:
            raise MaintenanceError(
                "bad_budget", t("maint_budget_float", raw=repr(raw)),
                t("maint_budget_int_hint", max=MAX_BUDGET_TOKENS)) from exc
        if budget < MIN_BUDGET_TOKENS:
            raise MaintenanceError(
                "bad_budget", t("maint_budget_positive", budget=budget),
                t("maint_budget_positive_hint"))
        if budget > MAX_BUDGET_TOKENS:
            raise MaintenanceError(
                "budget_too_large",
                t("maint_budget_too_large", budget=budget, max=MAX_BUDGET_TOKENS),
                t("maint_budget_too_large_hint"))
        return budget

    def _blocked_reasons(self, result: MaintenanceResult) -> list[str]:
        """A plan may only be applied when every precondition actually passed."""
        reasons: list[str] = []
        failed = [p["name"] for p in result.phases if not p.get("ok")]
        if failed:
            reasons.append(t("maint_blocked_phases", failed="、".join(failed)))
        if result.usage.get("budget_exhausted"):
            reasons.append(t("maint_blocked_budget"))
        if result.plan is None:
            reasons.append(t("maint_blocked_no_plan"))
        if self.provider is not None and result.compat and not result.compat.get("ok"):
            reasons.append(t("maint_blocked_compat"))
        return reasons

    async def run(self, *, apply: bool | None = None,
                  budget_tokens: int | None = None) -> MaintenanceResult:
        result = MaintenanceResult()

        # ---- policy gate: evaluated before any I/O --------------------------
        if not bool(getattr(self.cfg.maintenance, "enabled", False)):
            raise MaintenanceError(
                "disabled", t("maint_disabled"),
                t("maint_disabled_hint"))
        budget = self._resolve_budget(budget_tokens)
        result.requested_apply, result.effective_apply = self._effective_apply(apply)

        try:
            return await self._run_phases(result, budget)
        except asyncio.CancelledError:
            # Cancellation is recorded, never swallowed, and never leaves an
            # apply in flight: the manager's transaction rolls itself back.
            result.cancelled = True
            result.errors.append(t("maint_cancelled_error"))
            result.phases.append(_phase("cancelled", False, t("maint_cancelled_phase")))
            self._record(result, budget)
            raise

    async def _run_phases(self, result: MaintenanceResult, budget: int) -> MaintenanceResult:
        # ---- phase 1: check -------------------------------------------------
        try:
            check = await self.manager.check()
            ok = not check.errors
            result.phases.append(_phase(
                "check", ok,
                t("maint_check_detail", count=len(check.entries), cached=check.from_cache)
                + (t("maint_check_errors", errors=check.errors) if check.errors else "")))
            if check.errors:
                result.errors.extend(f"check: {e}" for e in check.errors)
        except UpdateError as exc:
            result.phases.append(_phase("check", False, str(exc)))
            result.errors.append(f"check: {exc}")

        # ---- phase 2: catalog plan + compatibility --------------------------
        plan_entries = []
        try:
            catalog_plan = self.manager.plan()
            plan_entries = [e for e in catalog_plan.targets]
            result.phases.append(_phase(
                "plan", True,
                t("maint_plan_detail", count=len(plan_entries))
                + (t("maint_plan_skipped", count=len(catalog_plan.skipped)) if catalog_plan.skipped else "")))
        except UpdateError as exc:
            result.phases.append(_phase("plan", False, str(exc)))
            result.errors.append(f"plan: {exc}")

        if self.provider is not None:
            result.compat = await self._compat_probe(result)

        # ---- phase 3: budget-capped read-only model turn --------------------
        raw_plan_text, usage = await self._model_turn(
            result, plan_entries, budget)
        result.usage = usage
        result.plan = _extract_plan_json(raw_plan_text)
        if result.plan is None:
            result.errors.append(t("maint_no_plan_json"))
        result.self_update = str((result.plan or {}).get("self_update", "") or "").strip().lower()

        # ---- phase 4: act ---------------------------------------------------
        blocked = self._blocked_reasons(result)
        result.blocked_reasons = blocked
        if blocked:
            # A candidate produced by failed phases or an exhausted budget must
            # never reach the installer — this is the hard gate, not a warning.
            result.phases.append(_phase(
                "apply", False, t("maint_apply_blocked", reasons="；".join(blocked))))
            result.applied = []
        elif not result.effective_apply:
            detail = (t("maint_readonly") if not result.requested_apply
                      else t("maint_no_allow_apply"))
            result.phases.append(_phase("apply", True, detail))
        else:
            await self._apply_phase(result, plan_entries)

        self._record(result, budget)
        return result

    async def _apply_phase(self, result: MaintenanceResult, plan_entries: list) -> None:
        if result.plan is None:
            result.errors.append(t("maint_apply_no_plan"))
            return
        targets = [str(t) for t in (result.plan.get("patch_targets") or [])]
        known = {e.name for e in plan_entries}
        valid = [t for t in targets if t in known]
        unknown = [t for t in targets if t not in known]
        if unknown:
            result.errors.append(
                t("maint_unknown_targets", names=', '.join(unknown)))
        # MAINT-02: a `self_update: "core"` recommendation joins the same
        # audited, transactional apply batch — never a side pip channel.
        core_names = [e.name for e in plan_entries if e.kind == "core"]
        if result.self_update == "core":
            if core_names:
                valid += [n for n in core_names if n not in valid]
            else:
                result.errors.append(
                    t("maint_core_missing_entry"))
        if not valid:
            result.phases.append(_phase("apply", True, t("maint_apply_empty")))
            return
        if not await self._approve_apply(result, valid):
            return
        try:
            applied = await self.manager.apply(valid)
            result.applied = [e.name for e in applied.entries]
            result.errors.extend(applied.errors)
            if core_names and any(name in core_names for name in result.applied):
                result.self_update_applied = True
                result.restart_required = True  # old code runs until restart
            detail = t("maint_apply_detail", count=len(result.applied))
            if result.restart_required:
                detail += t("maint_apply_core_note")
            result.phases.append(_phase(
                "apply", not applied.errors, detail))
        except UpdateError as exc:
            result.errors.append(f"apply: {exc}")
            result.phases.append(_phase("apply", False, str(exc)))

    async def _approve_apply(self, result: MaintenanceResult, targets: list[str]) -> bool:
        """Explicit, auditable approval before any patch is installed.

        With a PermissionGate the decision goes through the same confirmation
        UI as every other write; without one (headless CLI, where --allow-apply
        IS the explicit opt-in) the config flag is the only requirement.
        """
        explicit = ", ".join(targets)
        if self.gate is None:
            result.phases.append(_phase("approve", True, t("maint_approve_cli", targets=explicit)))
            return True
        try:
            decision = await self.gate.request(
                "selfmaint_apply", "write",
                t("maint_approve_summary", count=len(targets), targets=explicit),
                preview=t("maint_approve_preview"))
        except Exception as exc:  # a broken gate must deny, never allow
            result.errors.append(t("maint_approve_failed", kind=type(exc).__name__, exc=exc))
            result.phases.append(_phase("approve", False, t("maint_approve_broken")))
            return False
        if decision in ("allow", "always"):
            result.phases.append(_phase("approve", True, t("maint_approve_ok", targets=explicit)))
            return True
        result.errors.append(t("maint_approve_denied"))
        result.phases.append(_phase("approve", False, t("maint_approve_denied_detail", decision=decision or 'deny')))
        return False

    # -------------------------------------------------------------- internals

    async def _compat_probe(self, result: MaintenanceResult) -> dict[str, Any]:
        """One tiny completion through the real provider — the model can only
        be called 'compatible' after this succeeds."""
        from oaset.config import build_provider
        from oaset.providers.base import ContentDelta

        compat: dict[str, Any] = {"model": self.cfg.default_model, "ok": False}
        try:
            model_cfg = self.cfg.models.get(self.cfg.default_model)
            if model_cfg is None:
                compat["error"] = t("maint_model_missing")
                result.phases.append(_phase("compat", False, compat["error"]))
                return compat
            provider = self.provider or build_provider(self.cfg, model_cfg)
            chunks: list[str] = []
            started = time.monotonic()
            async for event in provider.stream(
                    [{"role": "user", "content": "Reply with exactly: OK"}], tools=[]):
                if isinstance(event, ContentDelta):
                    chunks.append(event.text)
            compat["ok"] = True
            compat["reply"] = "".join(chunks)[:40]
            compat["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            result.phases.append(_phase(
                "compat", True, t("maint_compat_ok", model=self.cfg.default_model)))
        except Exception as exc:
            compat["error"] = f"{type(exc).__name__}: {exc}"
            result.phases.append(_phase("compat", False, compat["error"]))
            result.errors.append(f"compat: {compat['error']}")
        return compat

    async def _model_turn(self, result: MaintenanceResult,
                          plan_entries: list, budget: int) -> tuple[str, dict]:
        """ONE read-only, budget-capped turn. No tools bound: the model can
        only reason over the injected facts — it cannot act on the host.

        Runs through the AgentKernel door (canonical events, sequence
        numbers, busy-guard) like every other surface — it used to build
        an AgentLoop directly and speak the loop's internal dialect."""
        from oaset.agent import Conversation
        from oaset.kernel import AgentKernel
        from oaset.runtime import build_registry

        registry = build_registry(self.cfg, self.cwd, tools=False)
        conversation = Conversation(
            "You are the oAset self-maintenance planner. You receive runtime "
            "facts and must output ONLY a JSON object with keys: search "
            "(string), compatibility (array of strings), patch_targets "
            "(array of catalog entry names worth installing), self_update "
            '("core" or "none"), summary (string).')
        kernel = AgentKernel(
            provider=self.provider,
            registry=registry,
            conversation=conversation,
            max_iterations=3,
            budget_tokens=budget,
            auto_compact=False,  # one-shot planner turn over injected facts
        )
        facts = {
            "oaset_version": _oaset_version(),
            "catalog_plan": [
                {"kind": e.kind, "name": e.name, "version": e.version,
                 "description": e.description} for e in plan_entries],
            "compatibility_probe": self.compat,
            "network_mode": self.cfg.network_mode,
            "allow_apply": self.cfg.maintenance.allow_apply,
        }
        prompt = ("Runtime facts:\n" + json.dumps(facts, ensure_ascii=False)
                  + "\n\nProduce the maintenance plan JSON now.")

        final_text = {"value": ""}
        usage: dict[str, Any] = {"budget": budget, "total_tokens": 0,
                                 "budget_exhausted": False}

        def on_event(event: Any) -> None:
            kind = event.type
            data = dict(event.data or {})
            if kind == "assistant_delta":
                final_text["value"] += str(data.get("text", ""))
            elif kind == "turn_completed":
                usage.update(data.get("usage") or {})
            elif kind == "budget_exhausted":
                usage["budget_exhausted"] = True
                result.errors.append(
                    t("maint_budget_exhausted", used=data.get('used', '?'), budget=data.get('budget', '?'))
                    + t("maint_turn_truncated"))

        try:
            async for event in kernel.chat(prompt):
                on_event(event)
        except asyncio.CancelledError:
            # Stop the provider turn immediately and say why it is incomplete:
            # a half-finished turn must not be mistaken for a plan.
            result.errors.append(t("maint_turn_cancelled"))
            result.phases.append(_phase("turn", False, t("maint_cancelled_tag")))
            raise
        except Exception as exc:
            result.errors.append(f"turn: {type(exc).__name__}: {exc}")
            result.phases.append(_phase("turn", False, str(exc)))
            return final_text["value"], usage
        result.phases.append(_phase(
            "turn", not usage["budget_exhausted"],
            f"{usage.get('total_tokens', 0)}/{budget} tokens"))
        return final_text["value"], usage

    def _record(self, result: MaintenanceResult, budget: int) -> None:
        self.manager._append_history({
            "action": "maintenance",
            "phases_ok": all(p["ok"] for p in result.phases),
            "enabled": bool(getattr(self.cfg.maintenance, "enabled", False)),
            "requested_apply": result.requested_apply,
            "effective_apply": result.effective_apply,
            "blocked": list(result.blocked_reasons),
            "cancelled": result.cancelled,
            "applied": list(result.applied),
            "self_update": result.self_update,
            "self_update_applied": result.self_update_applied,
            "restart_required": result.restart_required,
            "errors": len(result.errors),
            "tokens": result.usage.get("total_tokens", 0),
            "budget": budget,
        })


def _oaset_version() -> str:
    from oaset import __version__

    return str(__version__)
