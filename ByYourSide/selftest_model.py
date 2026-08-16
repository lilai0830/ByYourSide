# -*- coding: utf-8 -*-
"""Self-test for backend/model.py (B2 model layer).

No real network calls: the network boundary ``model._chat_sync`` is monkeypatched,
so we exercise the project's actual ``generate()`` code path end-to-end.

Run:  .venv/Scripts/python.exe selftest_model.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
import model as M

PASS = 0
FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  - {name}")
    else:
        FAIL += 1
        print(f"FAIL  - {name}")


# --- 1) not configured -> graceful degradation ---------------------------------
def test_not_configured():
    cfg = M.ModelConfig()  # all empty
    out = asyncio.run(M.generate(cfg, text="hi", mode="步行"))
    check("未配置返回 error", out["Error"] is not None)
    check("未配置 HUD_Badge=模型暂不可用", out["HUD_Badge"] == "模型暂不可用")
    check("未配置不含脚本答案", "模型调用失败" not in (out["Phone_Full"] or "") or "模型未配置" in out["Error"])


# --- 2) template rendering ------------------------------------------------------
def test_render():
    sp = M.render_system_prompt(
        mode="会议", memory=[{"tag": "饮食忌口", "value": "花生过敏"}],
        budget_remaining=2, retrieval="RAG: 地铁2号线", user_input="我花生过敏",
    )
    check("mode 占位符渲染", "会议" in sp)
    check("memory 占位符渲染", "花生过敏" in sp)
    check("budget 占位符渲染", "2" in sp)
    check("retrieval 占位符渲染", "地铁2号线" in sp)
    check("user_input 占位符渲染", "我花生过敏" in sp)
    check("无残留占位符", "{{" not in sp)


# --- 3) JSON extraction robustness --------------------------------------------
def test_extract():
    check("纯 JSON", isinstance(M._extract_json('{"a":1}'), dict))
    check("markdown 围栏", M._extract_json("```json\n{\"a\":1}\n```")["a"] == 1)
    check("前后散文包裹", M._extract_json("好的：\n{\"a\":1}\n以上。")["a"] == 1)
    raised = False
    try:
        M._extract_json("完全没有 JSON")
    except Exception:
        raised = True
    check("非法 JSON 抛错", raised)


# --- 4) normalization ----------------------------------------------------------
def test_normalize():
    raw = {"Phone_Full": "完整", "HUD_Vibration": "true", "Reasoning_Trace": "bad", "Memory_Delta": "bad"}
    out = M._normalize(raw, "步行")
    check("HUD_Vibration  coercion", out["HUD_Vibration"] is True)
    check("Reasoning_Trace 非 list->[]", out["Reasoning_Trace"] == [])
    check("Memory_Delta 非 list->None", out["Memory_Delta"] is None)
    check("Phone_Full 透传", out["Phone_Full"] == "完整")
    check("Mode_Echo 回退 mode", out["Mode_Echo"] == "步行")
    # defaults filled when keys absent
    out2 = M._normalize({}, "独处")
    for f in M._FIELDS:
        check(f"字段 {f} 存在", f in out2)


# --- 5) full generate (valid canned JSON, offline) ----------------------------
def test_generate_valid():
    cfg = M.ModelConfig(base_url="https://x/v1", api_key="k", model_text="m")
    canned = json.dumps({
        "HUD_Text": None, "HUD_Image": None, "HUD_Map": None,
        "HUD_Badge": "已记下", "HUD_Vibration": False,
        "Phone_Full": "已记住花生过敏", "Reasoning_Trace": [],
        "Memory_Delta": [{"action": "add", "content": "花生过敏"}],
        "Budget_Request": {"speak": False, "reason": "会议静音"},
        "Mode_Echo": None, "Error": None,
    }, ensure_ascii=False)
    M._chat_sync = lambda *a, **k: canned
    out = asyncio.run(M.generate(cfg, text="我花生过敏", mode="会议", budget_remaining=2))
    check("generate 返回 Memory_Delta", out["Memory_Delta"][0]["content"] == "花生过敏")
    check("generate Mode_Echo 回退", out["Mode_Echo"] == "会议")
    check("generate 无 Error", out["Error"] is None)


# --- 6) json_mode retry path (400 -> retry without response_format) -----------
def test_generate_retry():
    cfg = M.ModelConfig(base_url="https://x/v1", api_key="k", model_text="m", json_mode=True)
    calls = {"n": 0}

    def _fake(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError("https://x/v1/chat/completions", 400, "bad", None, None)
        return json.dumps({"Phone_Full": "重试成功", "HUD_Badge": "ok"}, ensure_ascii=False)

    M._chat_sync = _fake
    out = asyncio.run(M.generate(cfg, text="hi", mode="步行", budget_remaining=3))
    check("400 后重试一次", calls["n"] == 2)
    check("重试成功得到结果", out["Phone_Full"] == "重试成功" and out["Error"] is None)


# --- 7) model returns non-JSON -> graceful ------------------------------------
def test_generate_bad_json():
    cfg = M.ModelConfig(base_url="https://x/v1", api_key="k", model_text="m")
    M._chat_sync = lambda *a, **k: "我觉得你应该左转，但是我不知道格式"
    out = asyncio.run(M.generate(cfg, text="怎么走", mode="步行", budget_remaining=3))
    check("非法 JSON -> Error 降级", out["Error"] is not None)
    check("降级 HUD_Badge", out["HUD_Badge"] == "模型暂不可用")


# --- 8) network error -> graceful ---------------------------------------------
def test_generate_network_error():
    cfg = M.ModelConfig(base_url="https://x/v1", api_key="k", model_text="m")

    def _boom(*a, **k):
        raise OSError("connection refused")

    M._chat_sync = _boom
    out = asyncio.run(M.generate(cfg, text="hi", mode="步行", budget_remaining=3))
    check("网络异常 -> Error 降级", out["Error"] is not None)
    check("降级不黑屏", out["HUD_Badge"] == "模型暂不可用")


if __name__ == "__main__":
    test_not_configured()
    test_render()
    test_extract()
    test_normalize()
    test_generate_valid()
    test_generate_retry()
    test_generate_bad_json()
    test_generate_network_error()
    print(f"\n=== model.py self-test: {PASS} passed, {FAIL} failed ===")
    raise SystemExit(1 if FAIL else 0)
