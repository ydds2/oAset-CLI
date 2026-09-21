"""Acceptance-evidence tooling: the merge gate must never invent a sign-off.

The real-machine items (IME composition UI, mouse, multi-monitor, UAC) can only
be attested by a person on real hardware. These tests pin the two guarantees
that keep that honest: the merge refuses incomplete attestations, and a
complete one writes the verdicts into the status rows.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import acceptance_probes as probes  # noqa: E402


def _status(tmp_path: Path) -> Path:
    path = tmp_path / "status.md"
    path.write_text(
        "# status\n\n"
        "| ID | 需求 | 状态 | 证据 |\n|---|---|---|---|\n"
        "| TUI-01r | matrix | 机器采集完成，人工签字待确认 | evidence dir |\n"
        "| TUI-02r | ime | 待人工（前置已就绪） | evidence dir |\n"
        "| DESK-02r | desktop | 机器采集完成（单屏），多屏/UAC 待人工 | evidence dir |\n",
        encoding="utf-8")
    return path


def _filled_attestation(tmp_path: Path, attested_by: str = "reviewer") -> Path:
    data = probes.attestation_template()
    data["attested_by"] = attested_by
    data["attested_at"] = "2026-09-12T10:00:00"
    for section in ("host_limited_wt_only", "needs_hardware"):
        for key, item in data[section].items():
            item["verdict"] = "PASS"
            item["notes"] = "recorded on real hardware"
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_template_scopes_the_human_items_honestly():
    data = probes.attestation_template()
    assert data["machine_verified"], "machine evidence must be listed"
    assert data["host_limited_wt_only"], "WT-scoped item must be explicit"
    assert data["needs_hardware"], "hardware-gated items must be explicit"
    for section in ("host_limited_wt_only", "needs_hardware"):
        for item in data[section].values():
            assert item["verdict"] == ""


def test_all_human_items_have_row_targets():
    human_keys = [k for k, _ in probes.HUMAN_ITEMS]
    for row_id, target_keys in probes.ROW_TARGETS.items():
        for key in target_keys:
            assert key in human_keys, f"{row_id}: unknown item {key}"


def test_merge_refuses_unfilled_verdicts(tmp_path):
    status = _status(tmp_path)
    before = status.read_text(encoding="utf-8")
    path = _filled_attestation(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    for section in ("host_limited_wt_only", "needs_hardware"):
        for item in data[section].values():
            item["verdict"] = ""
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    result = probes.merge_attestation(path, status)
    assert result["ok"] is False and result["reason"] == "unfilled verdicts"
    assert status.read_text(encoding="utf-8") == before


def test_merge_refuses_without_attested_by(tmp_path):
    status = _status(tmp_path)
    path = _filled_attestation(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["attested_by"] = ""
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = probes.merge_attestation(path, status)
    assert result["ok"] is False and "attested_by" in result["reason"]


def test_merge_writes_verdicts_into_the_three_rows(tmp_path):
    status = _status(tmp_path)
    path = _filled_attestation(tmp_path)
    result = probes.merge_attestation(path, status)
    assert result["ok"] is True
    assert set(result["rows"]) == {"TUI-01r", "TUI-02r", "DESK-02r"}
    text = status.read_text(encoding="utf-8")
    assert text.count("人工签字 PASS") == 3
    assert "attestation.json" in text


def test_merge_reports_a_failing_verdict_as_fail(tmp_path):
    status = _status(tmp_path)
    path = _filled_attestation(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["needs_hardware"]["multi_monitor_cross_screen"]["verdict"] = "FAIL"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = probes.merge_attestation(path, status)
    assert result["ok"] is True
    assert result["rows"]["DESK-02r"] == "FAIL"
    assert "人工签字 FAIL" in status.read_text(encoding="utf-8")


def test_dry_run_reports_without_touching_status(tmp_path):
    status = _status(tmp_path)
    before = status.read_text(encoding="utf-8")
    path = _filled_attestation(tmp_path)
    result = probes.merge_attestation(path, status, dry_run=True)
    assert result["ok"] is True and result["dry_run"] is True
    assert status.read_text(encoding="utf-8") == before


def test_write_attestation_creates_valid_json_once(tmp_path):
    data = probes.write_attestation(tmp_path)
    path = tmp_path / "attestation.json"
    assert path.is_file()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["machine_verified"]) <= set(loaded.get("machine_verified", {}))


def test_settle_check_detects_a_stable_frame():
    frame = ["row-1", "row-2", "row-3", "row-4", "row-5"]

    def reader(_pid):
        return {"lines": list(frame)}

    result = probes.settle_check(reader, pid=0, samples=3, delay=0.0)
    assert result["settled"] is True
    assert result["masked_bottom_rows"] == 2


def test_settle_check_masks_the_rotating_status_rows():
    """The tip carousel changes by design; layout stability excludes it."""
    counter = {"n": 0}

    def reader(_pid):
        counter["n"] += 1
        return {"lines": ["chat", "input", "status", f"tip-{counter['n']}"]}

    result = probes.settle_check(reader, pid=0, samples=3, delay=0.0)
    assert result["settled"] is True
    assert result["masked_bottom_rows"] == 2


def test_settle_check_flags_real_layout_changes():
    counter = {"n": 0}

    def reader(_pid):
        counter["n"] += 1
        return {"lines": [f"layout-{counter['n']}", "status", "context"]}

    result = probes.settle_check(reader, pid=0, samples=3, delay=0.0)
    assert result["settled"] is False


def test_console_input_mode_decoder_explains_mouse_and_vt():
    vt_only = probes.decode_input_mode(0x0200)
    assert vt_only["vt_input"] is True
    assert vt_only["mouse_input_enabled"] is False
    assert vt_only["injection_supported"] is False

    with_mouse = probes.decode_input_mode(0x0200 | 0x0010)
    assert with_mouse["mouse_input_enabled"] is True
    assert with_mouse["injection_supported"] is True

    failed = probes.decode_input_mode(-1)
    assert failed["mouse_input_enabled"] is False


def test_ime_burst_probe_is_labelled_as_a_simulation():
    source = (SCRIPTS / "acceptance_probes.py").read_text(encoding="utf-8")
    assert "ime_like_burst_simulation" in source
    assert "Microsoft Pinyin composition still requires a human" in source


def test_interactive_attestation_records_verdicts_without_defaulting(tmp_path):
    answers = iter([
        "Li Wei",
        "PASS", "docs/shot-wt.png",
        "", "",
        "PASS", "pinyin ok",
        "FAIL", "only one monitor",
        "", "",
    ])
    result = probes.attest_interactive(tmp_path, ask=lambda _q: next(answers),
                                       echo=lambda *_: None)
    assert result["ok"] is True and result["filled"] == 3
    data = json.loads((tmp_path / "attestation.json").read_text(encoding="utf-8"))
    assert data["attested_by"] == "Li Wei"
    assert data["host_limited_wt_only"]["mouse_selection_and_click_wt"]["verdict"] == "PASS"
    assert data["needs_hardware"]["ime_candidate_ui_real_pinyin"]["verdict"] == "PASS"
    assert data["needs_hardware"]["drag_resize_visual_feel"]["verdict"] == ""
    assert data["needs_hardware"]["multi_monitor_cross_screen"]["verdict"] == "FAIL"
    assert data["needs_hardware"]["uac_elevation_prompt"]["verdict"] == ""


def test_interactive_attestation_requires_a_name(tmp_path):
    result = probes.attest_interactive(tmp_path, ask=lambda _q: "",
                                       echo=lambda *_: None)
    assert result["ok"] is False and "attested_by" in result["reason"]
    assert not (tmp_path / "attestation.json").exists()
