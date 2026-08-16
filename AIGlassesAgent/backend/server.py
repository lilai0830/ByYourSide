"""FastAPI server: serves the HUD frontend + phone App, bridges them to the agent.

Run (from project root):
    python backend/server.py
Then open:
    http://localhost:8000/        -> 镜片 HUD (client: lens)
    http://localhost:8000/phone.html -> 手机 App (client: phone)

Both pages connect to the same WebSocket at /ws, declare their `client` type, and
share one session state (mode / O1 memory / O2 budget / model config / KB). On a
turn, the backend broadcasts the *lens* view to lens clients and the *phone* view
to phone clients (docs/显示协议契约 §2).

WebSocket messages (frontend -> backend):
    {"type":"identify",      "client":"lens"|"phone"}
    {"type":"user_input",    "text":..., "image"?: "data:image/...;base64,..."}
    {"type":"set_mode",      "mode":"步行"|"骑行"|"会议"|"独处"}
    {"type":"config_model",  "base_url":..., "api_key":..., "model_text":..., "model_vision"?:...}
    {"type":"upload_kb",     "filename":..., "content":...}
    {"type":"kb_list"}
    {"type":"clear_memory"}
    {"type":"export_memory"}
"""
from __future__ import annotations

import os
import time

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent import handle_message
from graph import get_model_config, is_hitl, run_graph, set_hitl, set_model_config
from rag import add_document, list_documents

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "frontend"))

app = FastAPI(title="GOAI Glasses HUD Demo")

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# Two-client registry (docs §2): each WS declares its client type after connect.
CLIENTS: dict[str, set] = {"lens": set(), "phone": set()}

# HITL: the thread_id of a turn that is currently paused at a human-confirm gate.
HITL_THREAD: str | None = None


async def _send_phone(payload: dict) -> None:
    """Send a message to phone clients only (used for HITL confirm prompts)."""
    for ws in list(CLIENTS["phone"]):
        try:
            await ws.send_json(payload)
        except Exception:
            CLIENTS["phone"].discard(ws)


# ---------------------------------------------------------------------------
# hud_update assembly helpers (pure — unit-tested in selftest_server.py)
# ---------------------------------------------------------------------------


def _lens_payload(hud: dict) -> dict:
    lens = hud.get("lens", {}) or {}
    phone = hud.get("phone", {}) or {}
    return {
        "type": "hud_update",
        "client": "lens",
        **lens,
        "memory": phone.get("memory"),
        "reasoning_trace": phone.get("reasoning_trace"),
        "budget": phone.get("budget"),
        "mode": hud.get("mode"),
        "gap_tags": hud.get("gap_tags"),
        "suppressed": hud.get("suppressed"),
        "ts": hud.get("ts"),
    }


def _phone_payload(hud: dict) -> dict:
    phone = hud.get("phone", {}) or {}
    return {
        "type": "hud_update",
        "client": "phone",
        **phone,
        "mode": hud.get("mode"),
        "gap_tags": hud.get("gap_tags"),
        "suppressed": hud.get("suppressed"),
        "ts": hud.get("ts"),
    }


def _legacy_to_hud(legacy: dict) -> dict:
    """Wrap the legacy flat payload (set_mode/clear_memory/export_memory) into a
    contract-shaped hud_update with lens + phone views."""
    subtitle = legacy.get("subtitle", "")
    arrow = legacy.get("arrow")
    lens = {
        "bubbles": [{"kind": "status", "text": subtitle}] if subtitle else [],
        "map": (
            {"route": subtitle, "arrow": arrow, "eta": ""}
            if arrow not in (None, "none") else None
        ),
        "side_output": None,
        "vibration": bool(legacy.get("vibration")),
        "badge": legacy.get("badge"),
    }
    phone = {
        "ai_output": subtitle,
        "reasoning_trace": [
            {
                "step": 1,
                "tag": (legacy.get("gap_tags") or ["none"])[0],
                "detail": legacy.get("trace", ""),
                "hypothesis": False,
            }
        ],
        "memory": legacy.get("memory", []),
        "budget": legacy.get("budget"),
        "mode": legacy.get("mode"),
        "kb_status": "stub (B6)",
        "config_echo": get_model_config().to_dict(),
        "error": None,
    }
    if "memory_export" in legacy:
        phone["memory_export"] = legacy["memory_export"]
    return {
        "lens": lens,
        "phone": phone,
        "gap_tags": legacy.get("gap_tags", []),
        "suppressed": legacy.get("suppressed", False),
        "mode": legacy.get("mode"),
        "ts": int(time.time()),
    }


async def _broadcast(hud: dict) -> None:
    lp, pp = _lens_payload(hud), _phone_payload(hud)
    for ctype, payload in (("lens", lp), ("phone", pp)):
        for ws in list(CLIENTS[ctype]):
            try:
                await ws.send_json(payload)
            except Exception:
                CLIENTS[ctype].discard(ws)


# ---------------------------------------------------------------------------
# HTTP + WS
# ---------------------------------------------------------------------------


@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/phone.html")
async def phone_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "phone.html"))


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    global HITL_THREAD
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            t = data.get("type")

            if t == "identify":
                c = data.get("client")
                if c in CLIENTS:
                    CLIENTS[c].add(websocket)
                    await websocket.send_json({"type": "identified", "client": c})
                continue

            if t == "user_input":
                # Real LangGraph pipeline -> broadcast lens/phone views
                hud = await run_graph(data)
                if "__interrupt__" in hud:
                    # HITL pause: ask the PHONE to confirm before speaking.
                    HITL_THREAD = hud.get("thread_id")
                    await _send_phone({
                        "type": "hitl_prompt",
                        "prompt": hud["__interrupt__"].get("prompt", "是否现在播报？"),
                        "preview": hud["__interrupt__"].get("preview", ""),
                    })
                    continue
                await _broadcast(hud)
                continue

            if t == "set_hitl":
                # Toggle the human-in-the-loop gate (O2: 关键时刻才打断你).
                set_hitl(bool(data.get("enabled", False)))
                await websocket.send_json({"type": "hitl_status", "enabled": is_hitl()})
                continue

            if t == "resume":
                # Continue a paused HITL turn on the same thread.
                if HITL_THREAD is None:
                    await websocket.send_json({"type": "error", "message": "没有等待确认的任务"})
                    continue
                hud = await run_graph({}, thread_id=HITL_THREAD, resume=data.get("value", "deny"))
                HITL_THREAD = None
                if "__interrupt__" in hud:
                    # Should not happen, but stay safe.
                    continue
                await _broadcast(hud)
                continue

            if t == "config_model":
                set_model_config(data)
                cfg = get_model_config()
                await websocket.send_json(
                    {"type": "config_echo", **cfg.to_dict(), "configured": cfg.is_configured()}
                )
                continue

            if t == "upload_kb":
                add_document(data.get("filename", "doc"), data.get("content", ""))
                await websocket.send_json(
                    {"type": "kb_status", "status": "received", "count": len(list_documents())}
                )
                continue

            if t == "kb_list":
                await websocket.send_json({"type": "kb_list", "docs": list_documents()})
                continue

            if t in ("set_mode", "clear_memory", "export_memory"):
                # Session-management messages: update shared state, then broadcast
                legacy = handle_message(data)
                hud = _legacy_to_hud(legacy)
                await _broadcast(hud)
                continue

            await websocket.send_json({"type": "error", "message": f"unknown type: {t}"})
    except WebSocketDisconnect:
        for s in CLIENTS.values():
            s.discard(websocket)
        return


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
