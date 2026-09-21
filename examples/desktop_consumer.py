"""桌面端最小消费示例：用共享协议消费 oAset 的模型与 computer-use 能力。

这个示例展示桌面客户端如何通过 RuntimeFactory/protocol 与 oAset 交互：
1. oaset models verify —— 模型接入的连通性自动探测（MODEL-01 验证半边）；
2. oaset.computer 的 GatedDesktopPage —— 桌面窗口枚举 + 审批门；
3. oaset.protocol —— 跨客户端共享的事件与审批 schema。

运行前提：安装了 oAset CLI 并完成 provider 配置（或使用 mock 模型）。
"""

from __future__ import annotations

import argparse
import asyncio

from oaset.config import default_config
from oaset.models_wizard import run_models_add_wizard
from oaset.protocol import (
    PROTOCOL_VERSION,
    event_from_payload,
    validate_event,
)
from oaset.runtime import build_runtime


async def demo_shared_runtime(cfg, cwd) -> None:
    """桌面客户端与 TUI 共用同一 RuntimeFactory 装配（SDK-01）。"""
    rt = build_runtime(cfg, cwd, model_id=None, tools=True)
    print(f"[runtime] model={rt.model_cfg.id} tools={len(rt.registry.tools)}")


async def demo_protocol_schema() -> None:
    """协议 schema：事件在客户端侧的校验示例。"""
    event = event_from_payload({
        "type": "turn_started", "sequence": 1,
        "event_id": "e1", "session_id": "s1",
    })
    problems = validate_event(event)
    print(f"[protocol] v{PROTOCOL_VERSION} event ok, problems={problems}")


async def demo_model_verify(cfg, model_id) -> None:
    """MODEL-01 验证半边：一个真实微补全确认连通性。"""
    from oaset.cli import cmd_models_verify

    rc = await cmd_models_verify(cfg, model_id, json_out=True)
    print(f"[verify] {model_id} rc={rc}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="oAset desktop minimal consumer demo")
    parser.add_argument("--add-model", action="store_true",
                        help="先运行 MODEL-01 的交互式接入向导")
    parser.add_argument("--verify-model", metavar="MODEL_ID",
                        help="接入后立即连通性验证")
    args = parser.parse_args()

    from pathlib import Path

    cfg = default_config()
    if args.add_model:
        summary = run_models_add_wizard(cfg)
        cfg.models[summary["model"]] = cfg.models[summary["model"]]  # 已注册
        await demo_model_verify(cfg, summary["model"])
        return
    cwd = Path.cwd()
    await demo_shared_runtime(cfg, cwd)
    await demo_protocol_schema()


if __name__ == "__main__":
    asyncio.run(main())
