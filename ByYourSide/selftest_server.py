# -*- coding: utf-8 -*-
"""Self-test for backend/server.py pure helpers (two-client view split).

No network: exercises _legacy_to_hud / _lens_payload / _phone_payload offline.
Run:  .venv/Scripts/python.exe selftest_server.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
import server as S

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


def test_legacy_to_hud():
    legacy = {
        "subtitle": "场景模式：会议", "card": {"title": "Mode", "lines": ["会议"], "source": "x"},
        "arrow": "none", "tts": None, "badge": None, "vibration": False,
        "suppressed": False, "gap_tags": ["O2", "O3"], "trace": "set_mode -> 会议",
        "memory": [{"tag": "饮食忌口", "value": "花生过敏"}], "budget": 5, "mode": "会议",
    }
    hud = S._legacy_to_hud(legacy)
    lens, phone = hud["lens"], hud["phone"]
    check("legacy->lens bubbles", lens["bubbles"][0]["text"] == "场景模式：会议")
    check("legacy->lens 无地图(arrow none)", lens["map"] is None)
    check("legacy->lens 无角标", lens["badge"] is None)
    check("legacy->lens 无震动", lens["vibration"] is False)
    check("legacy->phone ai_output", phone["ai_output"] == "场景模式：会议")
    check("legacy->phone trace tag O2", phone["reasoning_trace"][0]["tag"] == "O2")
    check("legacy->phone memory 回显", any("花生过敏" in m["value"] for m in phone["memory"]))
    check("legacy->phone budget", phone["budget"] == 5)
    check("legacy->phone mode", phone["mode"] == "会议")


def test_payload_split():
    full = {
        "lens": {"bubbles": [{"kind": "notify", "text": "前方左转"}], "map": None,
                 "side_output": None, "vibration": False, "badge": None},
        "phone": {"ai_output": "完整输出", "reasoning_trace": [], "memory": [],
                  "budget": 3, "mode": "步行", "kb_status": "stub", "config_echo": {}, "error": None},
        "gap_tags": ["O3"], "suppressed": False, "mode": "步行", "ts": 123,
    }
    lp = S._lens_payload(full)
    pp = S._phone_payload(full)
    check("lens payload client", lp["client"] == "lens")
    check("lens payload bubbles", lp["bubbles"][0]["text"] == "前方左转")
    check("lens payload mode", lp["mode"] == "步行")
    check("lens payload suppressed", lp["suppressed"] is False)
    check("lens payload 含 reasoning_trace", "reasoning_trace" in lp)
    check("phone payload client", pp["client"] == "phone")
    check("phone payload ai_output", pp["ai_output"] == "完整输出")
    check("phone payload budget", pp["budget"] == 3)
    check("phone payload 不含 lens 字段", "bubbles" not in pp)


if __name__ == "__main__":
    test_legacy_to_hud()
    test_payload_split()
    print(f"\n=== server.py self-test: {PASS} passed, {FAIL} failed ===")
    raise SystemExit(1 if FAIL else 0)
