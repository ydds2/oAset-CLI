"""The last two i18n bypasses from the blind review, pinned.

- ARG_PROMPTS labels are catalogue keys resolved at display time, so an
  English user never sees "cozy — 默认行距" in a picker again;
- /init drives the model with the prompt in the UI language (it is
  user-visible content and shapes the generated AGENTS.md).
"""

from __future__ import annotations

from oaset.i18n import set_language, t
from oaset.providers import MockProvider, MockTurn
from oaset.tui.commands import arg_spec_for, localized_spec


def test_arg_prompt_labels_resolve_per_language():
    spec = arg_spec_for("density")
    assert spec is not None
    set_language("zh")
    title_zh, choices_zh = localized_spec(spec)
    assert ("cozy - 默认行距") in [label for _, label in choices_zh]
    set_language("en")
    title_en, choices_en = localized_spec(spec)
    assert ("cozy - default line spacing") in [label for _, label in choices_en]
    assert title_en == "Chat spacing"
    set_language("zh")
    # values stay stable across languages — only labels translate
    assert [value for value, _ in choices_zh] == [value for value, _ in choices_en]


def test_arg_prompt_labels_are_all_catalogued():
    """No picker may show Chinese copy to a non-Chinese UI: every choice
    label / title containing CJK must be a catalogue key (t() passes
    unknowns through). Em-dash English labels are fine; the /language
    picker is exempt by design — a language list shows each name in its
    own language (中文 / English), which is the universal convention."""
    import re

    from oaset.tui.commands import ARG_PROMPTS

    cjk = re.compile(r"[\u4e00-\u9fff]")
    for name, raw in ARG_PROMPTS.items():
        if name == "language":
            continue
        spec = arg_spec_for(name)
        if spec is None or spec.kind != "choice":
            continue
        for _value, label in spec.choices:
            if cjk.search(label):
                assert t(label) != label, \
                    f"{name}: CJK label {label!r} is not localised"
        if cjk.search(raw.title or ""):
            assert t(spec.title) != spec.title, \
                f"{name}: CJK title {spec.title!r} is not localised"


async def test_init_submits_the_prompt_in_the_ui_language(workspace):
    from oaset.config import default_config
    from oaset.tui.app import OasetApp

    cfg = default_config()
    cfg.ui_language = "en"
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),                   model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.submit_text("/init")
        for _ in range(100):
            await pilot.pause(0.05)
            user = [str(m.content) for m in app.conversation.messages if m.role == "user"]
            if any("AGENTS.md" in u for u in user):
                break
        sent = "\n".join(str(m.content) for m in app.conversation.messages
                         if m.role == "user")
        assert "AGENTS.md" in sent
        assert "Analyse this codebase" in sent      # English build
        assert "请分析当前代码库" not in sent
