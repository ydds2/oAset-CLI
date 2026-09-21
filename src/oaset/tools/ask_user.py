"""Structured user questions and explicit plan mode.

- ask_user: pause the turn and ask the human a question with 2-4 options
  (rendered as an inline picker in the TUI); the answer flows back to the
  model as the tool result.
- enter_plan_mode / exit_plan_mode: explicit planning tools (Kimi parity);
  plan mode itself stays read-only.
"""

from __future__ import annotations

import asyncio

from oaset.i18n import t
from oaset.tools.base import READ, Tool, ToolResult

# An unanswered question must not park the turn forever: headless/gateway
# contexts have no Esc key (S1 in docs/PLAN.zh-CN.md).
ASK_USER_TIMEOUT = 300.0


class AskUserQuestionTool(Tool):
    name = "ask_user"
    description = (
        "Ask the human a question and wait for the answer. Use when requirements "
        "are ambiguous and a wrong guess is costly. Provide 2-4 short options."
    )
    permission = READ
    required = ["question"]
    parameters = {
        "question": {"type": "string", "description": "The question for the user"},
        "choices": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-4 answer options for the user to pick from",
        },
    }

    async def run(self, args, ctx):
        question = str(args["question"]).strip()
        raw = args.get("choices") or []
        choices = [str(c) for c in raw if str(c).strip()][:4]
        gateway = ctx.session_state.get("ask_user")
        if gateway is None:
            return ToolResult(
                "No interactive user available in this context; answer the "
                "question yourself with your best assumption and say so.",
                is_error=True,
            )
        if not choices:
            choices = [t("ask_user_confirm"), t("ask_user_cancel")]
        try:
            answer = await asyncio.wait_for(gateway(question, choices),
                                            timeout=ASK_USER_TIMEOUT)
        except asyncio.TimeoutError:
            return ToolResult(
                t("ask_user_timeout", seconds=f"{ASK_USER_TIMEOUT:.0f}"),
                is_error=True,
            )
        if not answer:
            return ToolResult(t("ask_user_no_answer"), is_error=True)
        return ToolResult(t("ask_user_answered", answer=answer))

    def gate_summary(self, args, ctx):  # pragma: no cover - never gated (READ + interactive)
        return f"ask_user: {args.get('question', '')}"


class EnterPlanModeTool(Tool):
    name = "enter_plan_mode"
    description = (
        "Switch to plan mode (read-only) to explore and design before making "
        "changes. Present the plan to the user, then call exit_plan_mode."
    )
    permission = READ
    required = []
    parameters: dict = {}

    async def run(self, args, ctx):
        setter = ctx.session_state.get("set_mode")
        if setter is None:
            return ToolResult("Plan mode is not available in this context.", is_error=True)
        setter("plan")
        return ToolResult(
            "Plan mode ON: read-only. Explore the code, design an approach, "
            "then call exit_plan_mode when the plan is ready."
        )


class ExitPlanModeTool(Tool):
    name = "exit_plan_mode"
    description = (
        "Leave plan mode and return to the previous permission mode. Call this "
        "after the user has seen your plan."
    )
    permission = READ
    required = []
    parameters: dict = {}

    async def run(self, args, ctx):
        restore = ctx.session_state.get("restore_mode")
        setter = ctx.session_state.get("set_mode")
        if restore is not None:
            restored = restore()
            label = restored or "default"
        elif setter is not None:
            # no recorded previous mode: land on the safe default — "restore"
            # is not a mode, and an invalid ctx.mode silently broke gating
            setter("default")
            label = "default"
        else:
            return ToolResult("Plan mode is not available in this context.", is_error=True)
        return ToolResult(
            f"Plan mode OFF: {label} permissions restored."
        )
