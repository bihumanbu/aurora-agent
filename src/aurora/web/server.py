"""FastAPI Web 服务器 — 四象限协议的三层装配。

    第一/二象限（unary 上行）：
        POST /api/<method>   body = ClientRequest {type, rpcId, method, payload}
                             resp = ServerResponse {type, rpcId, result:{ok,value|error}}
    第三象限（下行推送）：
        WS /ws 每帧 = ServerRequest {type, rpcId, method, payload}
        method ∈ event.*（loop.tick / session.created / event.* …）
    第四象限（客户端应答）：
        当前版本纯推送；预留 POST /api/respond 通道。

    前端静态托管：GET / → src/ui/index.html（SPA）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aurora.web.hub import Hub
from aurora.web.protocol import ServerRequest, ServerResponse

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def build_app(hub: Hub, ui_dir: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="AuroraAgent", version="1.0.0")
    ui = Path(ui_dir) if ui_dir else UI_DIR

    # ── 第一/二象限：unary 上行 ────────────────────────────────

    @app.post("/api/{method}")
    async def api(method: str, raw: dict[str, Any]) -> JSONResponse:
        rpc_id = raw.get("rpcId", "")
        try:
            value = await hub.call(method, raw.get("payload") or {})
            return _json(ServerResponse(rpc_id=rpc_id, ok=True, value=value))
        except KeyError as e:
            return _json(ServerResponse(
                rpc_id=rpc_id, ok=False,
                error={"code": "unknown-method", "message": str(e)}), 404)
        except Exception as e:  # noqa: BLE001 — 业务异常归一为 ok:false
            return _json(ServerResponse(
                rpc_id=rpc_id, ok=False,
                error={"code": type(e).__name__, "message": str(e)}))

    # ── 第三/四象限：WebSocket 双向（下行推送为主）───────────────

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)

        def broadcaster(kind: str, payload: dict[str, Any]) -> None:
            ev = ServerRequest(method=f"event.{kind}", payload=payload)
            try:
                queue.put_nowait(ev.to_dict())
            except asyncio.QueueFull:  # 背压：丢弃最老的或直接丢新帧
                try:
                    queue.get_nowait()
                    queue.put_nowait(ev.to_dict())
                except asyncio.QueueEmpty:
                    pass

        hub.on_event(broadcaster)

        # 下行消费者
        async def pump() -> None:
            while True:
                frame = await queue.get()
                try:
                    await websocket.send_text(json.dumps(frame, ensure_ascii=False))
                except (RuntimeError, Exception):  # noqa: BLE001
                    break

        task = asyncio.create_task(pump())
        try:
            # 上行：当前版本无需要应答的 server 请求，收到帧仅维持连接
            while True:
                try:
                    data = await websocket.receive_text()
                    if data:
                        _ = json.loads(data)  # 校验 JSON，异常则忽略
                except WebSocketDisconnect:
                    break
                except json.JSONDecodeError:
                    continue
        except Exception:  # noqa: BLE001
            pass
        finally:
            task.cancel()
            if broadcaster in hub._listeners:
                hub._listeners.remove(broadcaster)

    # ── 静态托管 ──────────────────────────────────────────────

    index = ui / "index.html"
    if index.exists():
        app.mount("/static", StaticFiles(directory=ui), name="static")

        @app.get("/")
        async def root() -> FileResponse:
            return FileResponse(index)

    return app


def _json(sp: ServerResponse, status: int = 200) -> JSONResponse:
    return JSONResponse(sp.to_dict(), status_code=status)


def run_server(app: FastAPI, host: str = "127.0.0.1", port: int = 8000) -> None:
    """启动 uvicorn（阻塞）。"""
    import uvicorn

    print("\n  ╭──────────────────────────────────────────────────────╮")
    print(f"  │  AuroraAgent 已在 Web 模式运行                        │")
    print(f"  │  本地访问  http://{host}:{port}                         │")
    print(f"  │  远程访问  cloudflared tunnel --url http://{host}:{port} │")
    print("  ╰──────────────────────────────────────────────────────╯\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")