#!/usr/bin/env python3
"""
极光 Agent OS (AuroraAgent) — 主入口。

启动 Web 服务（四象限 RPC 协议上层 + Loop 可视化面板）。

用法:
    python run.py                          # 读取 .env 或环境变量
    python run.py --mock                   # 无真实 API key，内置脚本化 FakeLLM 演示
    python run.py --api-base <url> --api-key <key> --model <name>
    python run.py --port 8001              # 自定义端口

远程暴露（面试官访问）:
    cloudflared tunnel --url http://127.0.0.1:8000
    # 或用 scripts/demo.sh 一键完成
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AuroraAgent — 极光 Agent OS")
    # Web 服务
    p.add_argument("--host", type=str, default=os.environ.get("AURORA_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("AURORA_PORT", "8000")))
    # LLM API（多厂商 OpenAI 兼容）
    p.add_argument("--api-base", type=str, default=os.environ.get("AURORA_API_BASE", ""))
    p.add_argument("--api-key", type=str, default=os.environ.get("AURORA_API_KEY", ""))
    p.add_argument("--model", type=str, default=os.environ.get("AURORA_MODEL", "deepseek-chat"))
    p.add_argument("--provider", type=str, default="openai_compatible",
                   choices=["openai_compatible", "openai", "deepseek"])
    p.add_argument("--mock", action="store_true", help="使用内置 FakeLLM 演示模式（无需 API key）")
    p.add_argument("--ui-dir", type=str, default="",
                   help="前端静态资源目录（默认 src/ui）")
    return p.parse_args()


def _load_env() -> None:
    """极简 .env 加载：AURORA_* 环境变量，不覆盖已存在的环境变量。"""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    _load_env()
    args = parse_args()

    # 保证 import 路径
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

    from aurora.web.server import build_app, run_server
    from aurora.web.hub import create_hub
    from aurora.runtime.registry import ToolRegistry
    from aurora.tools import register_all_tools

    # ── 组装核心 ──────────────────────────────────────────────
    registry = ToolRegistry()
    register_all_tools(registry)

    hub = create_hub(registry=registry)
    if args.mock or not args.api_key:
        if not args.mock and not args.api_key:
            print("  ⚠ 未配置 AURORA_API_KEY — 使用 --mock 演示模式")
        from aurora.llm.fake import build_demo_gateway

        # 演示剧本（定义于 build_demo_gateway，与 llm.use_mock 共用）
        hub.set_gateway(build_demo_gateway())
    else:
        from aurora.llm.gateway import LLMConfig, ModelGateway
        from aurora.llm.clients import OpenAICompatibleClient
        cfg = LLMConfig(mock=False, api_base=args.api_base, api_key=args.api_key, model=args.model)
        client = OpenAICompatibleClient(cfg)
        gateway = ModelGateway(cfg, backend=client)
        hub.set_gateway(gateway)

    cfg = hub.gateway.config if hub.gateway else None
    app = build_app(hub)
    run_server(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())