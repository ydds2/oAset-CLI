from oaset.agent.prompts import build_system_prompt
from oaset.skills import find_skill, load_skills


def make_skill(base, name, title, description):
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"# {title}\n\n{description}\n\nWhen asked, do the thing.\n", encoding="utf-8"
    )


def test_load_skills_from_home(isolated_home):
    make_skill(isolated_home / "skills", "greeter", "Greeter", "Says hello politely.")
    skills = load_skills(isolated_home)
    by_name = {s.name: s for s in skills}
    assert "Greeter" in by_name
    assert "hello" in by_name["Greeter"].description
    assert any(s.scope == "builtin" for s in skills)


def test_project_skills_and_lookup(workspace, isolated_home):
    make_skill(workspace / ".oaset" / "skills", "tester", "Tester", "Runs the tests.")
    assert find_skill(workspace, "tester") is not None
    assert find_skill(workspace, "TESTER") is not None
    assert find_skill(workspace, "missing") is None


def test_system_prompt_includes_environment_and_tools(tmp_path, isolated_home):
    make_skill(isolated_home / "skills", "greeter", "Greeter", "Says hello.")
    (tmp_path / "OASET.md").write_text("Always answer in haiku.", encoding="utf-8")
    prompt = build_system_prompt(tmp_path, skills=load_skills(tmp_path))
    assert "oAset" in prompt
    assert str(tmp_path) in prompt
    assert "read_file" in prompt and "run_shell" in prompt
    assert "Greeter" in prompt
    assert "Always answer in haiku." in prompt
    assert "Done is observed" in prompt
    assert "Ship complete" in prompt


def test_builtin_idea_forge_skill_loads_with_the_gate_discipline():
    """The dynamic-requirement pipeline skill: refine + gap-fill from
    same-category products, tech map from OSS + market route, and a HARD
    confirmation gate before any product code."""
    from pathlib import Path

    forge = find_skill(Path("."), "idea-forge")
    assert forge is not None and forge.scope == "builtin"
    assert "confirmed plan" in forge.description
    body = forge.body
    # the four-bucket requirement classification drives the gate table
    for marker in ("用户原话", "同类产品标配", "明确排除"):
        assert marker in body
    # grounded, not invented: OSS must be named or declared 自研
    assert "自研" in body and "web_search" in body
    # the hard stop: no product code before an explicit 确认
    assert "Do not write any product code before" in body
    assert ".oaset/plans/" in body, "the plan doc is the single source of truth"


def test_idea_forge_appears_in_the_system_prompt_skill_list(tmp_path, isolated_home):
    from oaset.agent.prompts import build_system_prompt

    prompt = build_system_prompt(tmp_path, skills=load_skills(tmp_path))
    assert "idea-forge" in prompt, "the model must be able to discover the skill"
