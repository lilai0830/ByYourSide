# -*- coding: utf-8 -*-
"""终端体验版：不依赖浏览器，直接在命令行感受升级后的 LangGraph。

展示的能力：
  · ② Router + 子图   : 翻译 / 导航 / 看物 / 记忆 自动分流
  · ⑦ Loop / ReAct    : 说「我走错了」触发导航重规划循环
  · ⑧ Checkpointer    : O1 记忆跨轮次保持
  · ⑨ HITL            : 开启后，眼镜想主动出声会先请你确认

无需任何 API Key：未配置模型时自动走内置离线 planner（清晰标注的桩）。

运行：  .venv/Scripts/python.exe demo_graph.py
（浏览器体验请看 backend/server.py，启动后开 localhost:8000 与 phone.html）
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

import agent
import graph as G


def show(title: str):
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


async def main():
    agent.session.memory = []
    agent.session.mode = "独处"

    show("1) 路由 + 子图：翻译（route -> run_translate）")
    out = await G.run_graph({"type": "user_input", "text": "翻译 出口"})
    print("  镜片大字幕:", out["lens"]["bubbles"][0]["text"] if out["lens"]["bubbles"] else "(无)")
    print("  手机全量  :", out["phone"]["ai_output"].splitlines()[0])
    print("  gap 标签  :", out["gap_tags"])

    show("2) 路由 + 子图：看物（route -> run_identify）")
    out = await G.run_graph({"type": "user_input", "text": "这是什么"})
    print("  镜片大字幕:", out["lens"]["bubbles"][0]["text"] if out["lens"]["bubbles"] else "(无)")
    print("  gap 标签  :", out["gap_tags"])

    show("3) ⑦ ReAct 循环：导航偏离「我走错了」")
    out = await G.run_graph({"type": "user_input", "text": "我走错了"})
    print("  规划轮数 loop_passes =", out["phone"]["loop_passes"], "(偏离触发一次重规划)")
    print("  镜片大字幕:", out["lens"]["bubbles"][0]["text"] if out["lens"]["bubbles"] else "(无)")
    print("  ReAct 工具日志:")
    for t in out["phone"]["reasoning_trace"]:
        if t.get("tag") == "tool":
            print("    -", t["detail"])

    show("4) ⑧ Checkpointer：O1 记忆跨轮次保持")
    await G.run_graph({"type": "user_input", "text": "记一下 我花生过敏"})
    out = await G.run_graph({"type": "user_input", "text": "我有什么忌口"})
    print("  召回结果:", out["lens"]["bubbles"][0]["text"] if out["lens"]["bubbles"] else "(无)")

    show("5) ⑨ HITL：开启后，主动出声前先请你确认")
    G.set_hitl(True)
    res = await G.run_graph({"type": "user_input", "text": "翻译 菜单"}, thread_id="demo_hitl")
    print("  图暂停于 HITL，向手机发确认：", res["__interrupt__"]["prompt"])
    print("  预览内容:", res["__interrupt__"]["preview"])
    print("  >> 模拟你在手机点「取消」(deny)")
    out = await G.run_graph({}, thread_id="demo_hitl", resume="deny")
    print("  结果 suppressed =", out["suppressed"], "| 大字幕气泡 =", out["lens"]["bubbles"])
    print("  >> 模拟你在手机点「确认播报」(approve)")
    res = await G.run_graph({"type": "user_input", "text": "翻译 出口"}, thread_id="demo_hitl2")
    out = await G.run_graph({}, thread_id="demo_hitl2", resume="approve")
    print("  结果 suppressed =", out["suppressed"], "| 大字幕 =",
          out["lens"]["bubbles"][0]["text"] if out["lens"]["bubbles"] else "(无)")
    G.set_hitl(False)

    show("完成。想用浏览器+HUD 模拟器体验，请运行 backend/server.py")
    print("  打开 http://localhost:8000  (镜片) 与 http://localhost:8000/phone.html (手机)")


if __name__ == "__main__":
    asyncio.run(main())
