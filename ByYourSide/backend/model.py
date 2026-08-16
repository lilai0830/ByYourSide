# -*- coding: utf-8 -*-
"""B2 model layer — OpenAI-compatible client that emits the §7 JSON schema.

Design notes
------------
- Zero external deps: uses stdlib ``urllib`` to POST to ``{base_url}/v1/chat/completions``.
  Any OpenAI-compatible endpoint (OpenAI / DeepSeek / Qwen / Ollama / vLLM / Together ...)
  works by just setting ``base_url`` + ``api_key`` + ``model``. (Swap this one ``_chat``
  boundary for the ``openai`` SDK later if desired — nothing else changes.)
- The model is forced (via ``docs/系统提示词模板.md``) to return a single JSON object
  matching ``docs/显示协议契约.md`` §7. This module parses & normalizes it.
- Graceful degradation (NOT a scripted fallback): on any failure it returns an error
  struct — a ``HUD_Badge`` "模型暂不可用" + a phone-side error. It never fabricates an answer.
- Async-friendly: the blocking network call runs in a thread (``asyncio.to_thread``) so it
  drops cleanly into the FastAPI/WebSocket server without blocking the event loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.normpath(os.path.join(BASE_DIR, "..", "docs", "系统提示词模板.md"))
DEFAULT_TIMEOUT = 30

# §7 JSON schema field names (single source of truth for normalization)
_FIELDS = [
    "HUD_Text", "HUD_Image", "HUD_Map", "HUD_Badge", "HUD_Vibration",
    "Phone_Full", "Reasoning_Trace", "Memory_Delta", "Budget_Request",
    "Mode_Echo", "Error",
]


@dataclass
class ModelConfig:
    """OpenAI-compatible model configuration (persisted via ``config_model`` msg)."""

    base_url: str = ""
    api_key: str = ""
    model_text: str = ""
    model_vision: str | None = None
    temperature: float = 0.3
    timeout: int = DEFAULT_TIMEOUT
    json_mode: bool = True

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model_text)

    @classmethod
    def from_dict(cls, d: dict) -> "ModelConfig":
        return cls(
            base_url=(d.get("base_url") or "").strip(),
            api_key=(d.get("api_key") or "").strip(),
            model_text=(d.get("model_text") or "").strip(),
            model_vision=(d.get("model_vision") or None),
            temperature=float(d.get("temperature", 0.3)),
            timeout=int(d.get("timeout", DEFAULT_TIMEOUT)),
            json_mode=bool(d.get("json_mode", True)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# System-prompt rendering (renders docs/系统提示词模板.md placeholders)
# ---------------------------------------------------------------------------


def _read_template() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def render_system_prompt(
    *, mode: str, memory: list, budget_remaining: int, retrieval: str, user_input: str
) -> str:
    """Fill the {{...}} placeholders in the system-prompt template."""
    tpl = _read_template()
    ctx = {
        "mode": mode,
        "memory": memory,
        "budget_remaining": budget_remaining,
        "retrieval": retrieval,
        "user_input": user_input,
    }

    def _repl(m: re.Match) -> str:
        key = m.group(1).strip()
        return str(ctx.get(key, m.group(0)))

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", _repl, tpl)


# ---------------------------------------------------------------------------
# Network boundary (stdlib urllib; the ONLY place that touches the model API)
# ---------------------------------------------------------------------------


def _chat_sync(
    cfg: ModelConfig, *, system: str, user_content: Any, model: str, json_mode: bool
) -> str:
    url = cfg.base_url.rstrip("/") + "/v1/chat/completions"
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": cfg.temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# JSON extraction — robust to markdown fences / surrounding prose
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    raise ValueError("no JSON object found in model output")


# ---------------------------------------------------------------------------
# Normalization & graceful degradation (§7 schema)
# ---------------------------------------------------------------------------


def _normalize(raw: dict, mode: str) -> dict:
    rt = raw.get("Reasoning_Trace")
    if not isinstance(rt, list):
        rt = []
    md = raw.get("Memory_Delta")
    if md is not None and not isinstance(md, list):
        md = None
    return {
        "HUD_Text": raw.get("HUD_Text"),
        "HUD_Image": raw.get("HUD_Image"),
        "HUD_Map": raw.get("HUD_Map"),
        "HUD_Badge": raw.get("HUD_Badge"),
        "HUD_Vibration": bool(raw.get("HUD_Vibration", False)),
        "Phone_Full": raw.get("Phone_Full") or "",
        "Reasoning_Trace": rt,
        "Memory_Delta": md,
        "Budget_Request": raw.get("Budget_Request"),
        "Mode_Echo": raw.get("Mode_Echo") or mode,
        "Error": raw.get("Error"),
    }


def _graceful(msg: str, mode: str, phone_full: str = "") -> dict:
    return {
        "HUD_Text": None,
        "HUD_Image": None,
        "HUD_Map": None,
        "HUD_Badge": "模型暂不可用",
        "HUD_Vibration": False,
        "Phone_Full": phone_full or f"[模型调用失败] {msg}",
        "Reasoning_Trace": [
            {"step": 1, "tag": "none", "detail": f"优雅降级：{msg}", "hypothesis": False}
        ],
        "Memory_Delta": None,
        "Budget_Request": None,
        "Mode_Echo": mode,
        "Error": msg,
    }


# ---------------------------------------------------------------------------
# Public entry — async, returns a §7 dict
# ---------------------------------------------------------------------------


async def generate(
    cfg: ModelConfig,
    *,
    text: str = "",
    image: str | None = None,
    mode: str = "步行",
    memory: list | None = None,
    budget_remaining: int = 0,
    retrieval: str = "",
) -> dict:
    """Call the real model and return a normalized §7 JSON dict.

    Falls back to graceful degradation (never a scripted answer) on any error.
    """
    if not cfg.is_configured():
        return _graceful("模型未配置：请在手机 App 填写 base_url / api_key / model", mode)

    system = render_system_prompt(
        mode=mode,
        memory=memory or [],
        budget_remaining=budget_remaining,
        retrieval=retrieval,
        user_input=text or "(图像输入)",
    )

    # Choose model + build user content (text and/or vision)
    if image and cfg.model_vision:
        model = cfg.model_vision
        user_content: Any = [
            {"type": "text", "text": text or "请看这张图并回答。"},
            {"type": "image_url", "image_url": {"url": image}},
        ]
    else:
        model = cfg.model_text
        if image and not cfg.model_vision:
            # Honest fallback: image present but no vision model configured
            text = (text or "") + "\n[注：用户上传了图像，但未配置视觉模型 model_vision，请仅基于文字回答]"
        user_content = text or "(空)"

    # 1) try with json_mode
    try:
        raw_text = await asyncio.to_thread(
            _chat_sync, cfg, system=system, user_content=user_content,
            model=model, json_mode=cfg.json_mode,
        )
    except urllib.error.HTTPError as e:
        # Some providers reject response_format -> retry once without it
        if cfg.json_mode and e.code == 400:
            try:
                raw_text = await asyncio.to_thread(
                    _chat_sync, cfg, system=system, user_content=user_content,
                    model=model, json_mode=False,
                )
            except Exception as e2:
                return _graceful(f"模型调用失败：{e2}", mode)
        else:
            return _graceful(f"模型调用失败：{e}", mode)
    except Exception as e:  # noqa: BLE001 - surface as graceful degradation
        return _graceful(f"模型调用失败：{e}", mode)

    # 2) parse + normalize
    try:
        raw = _extract_json(raw_text)
    except Exception as e:
        return _graceful(f"模型未返回合法 JSON：{e}", mode, phone_full=raw_text[:500])

    return _normalize(raw, mode)


if __name__ == "__main__":
    # Quick offline smoke (no network): render + normalize a canned response.
    cfg = ModelConfig(base_url="https://api.example.com/v1", api_key="x", model_text="m")
    assert cfg.is_configured()
    sp = render_system_prompt(
        mode="会议", memory=[], budget_remaining=3, retrieval="", user_input="记一下：我花生过敏"
    )
    assert "会议" in sp and "花生过敏" in sp and "{{" not in sp
    canned = {
        "HUD_Badge": "已记下：花生过敏", "Phone_Full": "已记住：你对花生过敏。",
        "Memory_Delta": [{"action": "add", "content": "花生过敏"}], "Mode_Echo": "会议",
    }
    out = _normalize(canned, "会议")
    assert out["HUD_Badge"] == "已记下：花生过敏"
    assert out["Memory_Delta"][0]["content"] == "花生过敏"
    print("model.py offline smoke OK")
