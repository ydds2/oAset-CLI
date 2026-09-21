"""Windows 实机人工矩阵清单（IME/剪贴板/resize/多屏/DPI）。

运行 `python scripts/manual_matrix.py` 生成 markdown 模板清单，
人工逐项执行后把结果填入 docs/evidence/manual-matrix.md 的对应行。
此脚本仅生成清单，不自动执行任何 GUI 操作。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

TERMINALS = ["Windows Terminal", "conhost"]
SIZES = ["80x24", "100x30", "120x40", "160x50"]
CHECKS = [
    "中文微软拼音输入并上屏（Enter 提交候选）",
    "Ctrl+V 粘贴多行文本（观察大粘贴折叠）",
    "Ctrl+K 打开命令面板并搜索 model",
    "Esc 取消面板/审批，焦点回到输入区",
    "拖拽 resize 后聊天区/侧栏/输入区无重叠",
    "流式输出时滚动向上→停止追尾→点击回底部",
    "工具审批弹层确认与拒绝路径",
    "错误提示可读且有恢复动作",
]


def build_checklist() -> str:
    lines = ["# Windows TUI 人工实机矩阵", "",
             f"生成时间: {time.strftime('%Y-%m-%d %H:%M')}  填写: 每项 PASS/FAIL + 截图路径", ""]
    for terminal in TERMINALS:
        lines.append(f"## {terminal}")
        for size in SIZES:
            lines.append(f"### 尺寸 {size}")
            for check in CHECKS:
                lines.append(f"- [ ] {check}")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("manual_matrix.md")
    out.write_text(build_checklist(), encoding="utf-8")
    print(f"checklist written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
