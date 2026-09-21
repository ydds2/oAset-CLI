from pathlib import Path

from oaset.utils import (
    estimate_tokens,
    expand_path,
    find_project_context,
    one_line,
    shell_command_line,
    truncate_text,
    workdir_bucket,
)


def test_truncate_text_keeps_head_and_tail():
    text = "A" * 500 + "B" * 500
    out = truncate_text(text, 100)
    assert out.startswith("A" * 60)
    assert out.endswith("B" * 40)
    assert "truncated" in out
    assert truncate_text("short", 100) == "short"
    assert truncate_text("x" * 50, 0) == "x" * 50  # 0 disables


def test_estimate_tokens_cjk_vs_ascii():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1  # 4 ascii chars ≈ 1 token
    assert estimate_tokens("四个汉字四个汉字") == 8  # CJK ≈ 1 token/char
    assert estimate_tokens("汉字abc") == 3


def test_workdir_bucket_stable_and_sanitized():
    a = workdir_bucket(Path("C:/Users/x/My Project"))
    b = workdir_bucket(Path("C:/Users/x/My Project"))
    assert a == b
    assert a.startswith("wd_My_Project_")
    assert len(a.split("_")[-1]) == 8


def test_expand_path_relative_and_user():
    cwd = Path("C:/ws")
    assert expand_path("a/b.txt", cwd) == Path("C:/ws/a/b.txt")
    assert expand_path("~/x", cwd).name == "x"


def test_find_project_context_precedence(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents rules", encoding="utf-8")
    (tmp_path / "OASET.md").write_text("oaset rules", encoding="utf-8")
    assert find_project_context(tmp_path) == "oaset rules"
    (tmp_path / "OASET.md").unlink()
    assert find_project_context(tmp_path) == "agents rules"
    child = tmp_path / "sub"
    child.mkdir()
    assert find_project_context(child) == "agents rules"  # walks up


def test_one_line():
    assert one_line("a\nb\tc") == "a b c"
    assert len(one_line("x" * 200, 10)) == 10


def test_cell_width_ascii_and_cjk():
    from oaset.utils import cell_width

    assert cell_width("abc") == 3
    assert cell_width("中") == 2
    assert cell_width("中文") == 4
    assert cell_width("") == 0


def test_shell_command_line_uses_flag():
    cmd = shell_command_line("echo hi")
    assert cmd[-1] == "echo hi"
    assert cmd[0]
