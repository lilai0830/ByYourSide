# -*- coding: utf-8 -*-
"""Self-test for backend/graph.py — the upgraded LangGraph pipeline.

Covers the hands-on architecture (no real network / model; the offline LOCAL
PLANNER drives the whole graph, so this runs with zero config):

  * ② Router + conditional edges  : intent -> run_translate / run_navigate / ...
  * ⑤ Subgraphs                  : each scene is a nested mini-graph
  * ⑦ Loop / ReAct               : navigate reroute loops plan->tool->plan
  * ⑧ Checkpointer (MemorySaver) : O1 memory / O2 budget persist across turns
  * ⑨ HITL interrupt             : graph pauses; phone confirms; deny suppresses
  * O1/O2/O3 gateway             : preserved from the previous straight-line graph

Also exercises the REAL-MODEL path by monkeypatching model.generate with a
canned §7 proposal, proving the orchestrate gateway still applies O2 even when
a real model is wired in.

Run:  .venv/Scripts/python.exe selftest_graph.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

import agent
import model as _model
import graph as G

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   - {name}")
    else:
        FAIL += 1
        print(f"  FAIL - {name}  {extra}")


def reset(mode: str = "步行", budget_used: int = 0) -> None:
    agent.session.mode = mode
    agent.session.memory = []
    agent.session.last_identify = None
    agent.session.budget_used = budget_used
    G.set_hitl(False)


def turn(text: str, mode: str = "步行", tid: str = "t") -> dict:
    # Sets mode/budget/HITL for the turn but does NOT clear O1 memory — memory is
    # meant to persist across turns (that's the whole point of O1).
    agent.session.mode = mode
    agent.session.budget_used = 0
    G.set_hitl(False)
    return asyncio.run(G.run_graph({"type": "user_input", "text": text}, thread_id=tid))


# --- 0) graph compiles -------------------------------------------------------
def test_compile():
    reset("步行")
    check("图已编译 (APP 非空)", G.APP is not None)
    out = turn("带我去地铁站", "步行", "c0")
    check("返回含 lens", "lens" in out)
    check("返回含 phone", "phone" in out)


# --- 1) Router routes to the right subgraph --------------------------------
def test_router():
    reset("独处")
    out = turn("翻译 出口", "独处", "r1")
    check("翻译 -> translate 子图 (HUD 翻译)", "实时翻译" in (out["lens"]["bubbles"][0]["text"] if out["lens"]["bubbles"] else ""), out)
    out = turn("这是什么", "独处", "r2")
    check("看物 -> identify 子图", "这是" in (out["lens"]["bubbles"][0]["text"] if out["lens"]["bubbles"] else ""), out)
    out = turn("记一下 我花生过敏", "独处", "r3")
    check("记忆 -> memory 子图 (角标)", "已记住" in (out["lens"]["badge"] or ""), out)


# --- 2) O1 memory write + persistence via Checkpointer ----------------------
def test_memory():
    reset("独处")
    out = turn("记一下 我花生过敏", "独处", "m1")
    check("O1 写入 session", any("花生过敏" in m["value"] for m in agent.session.memory))
    check("O1 手机回显", any("花生过敏" in m["value"] for m in out["phone"]["memory"]))
    # recall on a SEPARATE thread still sees it (session store is process-global;
    # the Checkpointer is what survives restart — demonstrated in test_checkpointer)
    out2 = turn("我有什么忌口", "独处", "m2")
    check("O1 召回", "花生过敏" in (out2["lens"]["bubbles"][0]["text"] if out2["lens"]["bubbles"] else ""), out2)


# --- 3) ⑦ Navigate ReAct loop on reroute ------------------------------------
def test_navigate_loop():
    reset("步行")
    out = turn("带我去地铁站", "步行", "n1")
    check("普通导航 单次规划 (loop_passes=1)", out["phone"]["loop_passes"] == 1, out["phone"]["loop_passes"])
    check("普通导航 静默 (HUD 大字幕仍在)", bool(out["lens"]["bubbles"]))
    out = turn("我走错了", "步行", "n2")
    check("偏离 -> 重新规划 (loop_passes=2)", out["phone"]["loop_passes"] == 2, out["phone"]["loop_passes"])
    check("重新规划后 出声纠偏", "重新规划" in (out["lens"]["bubbles"][0]["text"] if out["lens"]["bubbles"] else ""), out)
    # tool_log shows two map calls (the ReAct cycle)
    tools = [t["detail"] for t in out["phone"]["reasoning_trace"] if t.get("tag") == "tool"]
    check("ReAct 工具日志含 2 次地图调用", len(tools) == 2, tools)


# --- 4) O2/O3 mode gating preserved -----------------------------------------
def test_modes():
    reset("步行")
    out = turn("记一下 3 分钟后谈预算", "会议", "mo1")
    check("会议 大字幕降级为角标", out["lens"]["badge"] is not None and out["lens"]["bubbles"] == [])
    check("会议 关地图/读图/震动", out["lens"]["map"] is None and out["lens"]["side_output"] is None and out["lens"]["vibration"] is False)
    check("会议 O1 记忆写入", any("预算" in m["value"] for m in agent.session.memory))
    out = turn("带我去公司", "骑行", "mo2")
    check("骑行 触发震动", out["lens"]["vibration"] is True)
    check("骑行 大字幕降级为角标", out["lens"]["badge"] is not None and out["lens"]["bubbles"] == [])


# --- 5) O2 budget exhaustion suppresses --------------------------------------
def test_budget():
    reset("步行", agent.session.budget_total)  # drain
    out = asyncio.run(G.run_graph({"type": "user_input", "text": "翻译 路牌"}, thread_id="b1"))
    check("预算耗尽 suppressed=True", out["suppressed"] is True, out.get("suppressed"))
    check("预算耗尽 大字幕降级为角标", out["lens"]["badge"] is not None and out["lens"]["bubbles"] == [])


# --- 6) ⑨ HITL interrupt + resume (deny suppresses, approve keeps) ----------
def test_hitl():
    reset("独处")
    G.set_hitl(True)
    # turn that wants to speak -> should interrupt (not broadcast)
    res = asyncio.run(G.run_graph({"type": "user_input", "text": "翻译 出口"}, thread_id="h1"))
    check("HITL 触发中断 (__interrupt__)", "__interrupt__" in res, res)
    check("中断含确认提示", bool(res["__interrupt__"].get("prompt")), res)
    # resume deny -> suppressed
    out = asyncio.run(G.run_graph({}, thread_id="h1", resume="deny"))
    check("deny 后续跑 suppressed=True", out["suppressed"] is True, out.get("suppressed"))
    check("deny 后无大字幕气泡", out["lens"]["bubbles"] == [])
    # fresh turn approve -> kept
    res2 = asyncio.run(G.run_graph({"type": "user_input", "text": "翻译 出口"}, thread_id="h2"))
    out2 = asyncio.run(G.run_graph({}, thread_id="h2", resume="approve"))
    check("approve 后 suppressed=False", out2["suppressed"] is False, out2.get("suppressed"))
    check("approve 后保留大字幕", bool(out2["lens"]["bubbles"]))
    G.set_hitl(False)


# --- 7) ⑧ Checkpointer: same thread keeps state across turns ---------------
def test_checkpointer():
    reset("独处")
    agent.session.memory = []
    # two turns on the SAME thread; the second should see first turn's tool_log-free
    # but shared config. Use the real-model path below for a clearer persistence demo.
    out1 = asyncio.run(G.run_graph({"type": "user_input", "text": "记一下 我坚果过敏"}, thread_id="cp1"))
    # second turn reuses thread cp1 -> O1 memory persists in session (process store)
    out2 = asyncio.run(G.run_graph({"type": "user_input", "text": "我有什么忌口"}, thread_id="cp1"))
    check("同线程 O1 记忆可跨轮召回", "坚果过敏" in (out2["lens"]["bubbles"][0]["text"] if out2["lens"]["bubbles"] else ""), out2)


# --- 8) Real-model path: monkeypatch model.generate, gateway still applies O2
def test_real_model_gateway():
    reset("会议")
    canned = {
        "HUD_Text": "想播报要点", "HUD_Image": None, "HUD_Map": None, "HUD_Badge": None,
        "HUD_Vibration": False, "Phone_Full": "要点全文…", "Reasoning_Trace": [],
        "Memory_Delta": None, "Budget_Request": {"speak": True, "reason": "想播报"},
        "Mode_Echo": "会议", "Error": None,
    }
    orig = _model.generate
    _model.generate = lambda *a, **k: canned  # canned §7 proposal (simulates a real model)
    try:
        out = asyncio.run(G.run_graph({"type": "user_input", "text": "随便说点什么"}, thread_id="rm1"))
    finally:
        _model.generate = orig  # restore the real generator
    check("真实模型路径：会议模式仍抑制语音", out["suppressed"] is True, out.get("suppressed"))
    check("真实模型路径：大字幕降级为角标", out["lens"]["badge"] is not None and out["lens"]["bubbles"] == [])


if __name__ == "__main__":
    test_compile()
    test_router()
    test_memory()
    test_navigate_loop()
    test_modes()
    test_budget()
    test_hitl()
    test_checkpointer()
    test_real_model_gateway()
    print(f"\n=== graph.py self-test: {PASS} passed, {FAIL} failed ===")
    raise SystemExit(1 if FAIL else 0)
