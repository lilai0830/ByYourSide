# -*- coding: utf-8 -*-
"""B7 LangGraph orchestration — upgraded, hands-on architecture.

Flow (StateGraph, with Checkpointer + HITL):

    START -> perceive -> retrieve -> [router] -> subgraph* -> hitl_gate -> orchestrate -> output -> END
                                        |                                   |
                          translate / navigate / identify /               (interrupt: 出声前确认)
                          memory / unknown  (each a nested subgraph)

What this upgrade adds on top of the old straight line (perceive->retrieve->
plan->orchestrate->output):

  * ② Router + conditional edges  : intent classification routes to a subgraph
  * ⑤ Subgraphs                  : each scene is a self-contained mini-graph
  * ⑦ Loop / ReAct               : the navigate subgraph plans -> calls the map
                                    tool -> checks -> loops back until the route
                                    is settled (visible in `tool_log`)
  * ⑧ Checkpointer (MemorySaver) : every turn is persisted per thread_id, so the
                                    O1 memory / O2 budget survive across turns and
                                    HITL pauses can be resumed (swap MemorySaver ->
                                    SqliteSaver for cross-restart persistence)
  * ⑨ HITL interrupt             : before the agent speaks proactively, it can
                                    pause and wait for the PHONE to confirm
                                    ("O2 分级主动：关键时刻才打断你")

Model slot (pluggable, honest):
  * If a real OpenAI-compatible model is configured (docs/显示协议契约 §7),
    the `plan` step calls model.generate.
  * Otherwise a deterministic LOCAL PLANNER (reusing the proven keyword logic
    from agent.py, emitted as a §7 JSON proposal) drives the whole graph so the
    demo runs OFFLINE, with no API key. This is a clearly-labeled stub — not a
    fabricated "real" model answer.

The O1/O2/O3 deterministic gateway (`orchestrate`) is unchanged in spirit: the
LLM/local-planner only *proposes* (§7); the gateway *decides* what reaches the
HUD. The model can never bypass it.
"""
from __future__ import annotations

import time
import uuid
from typing import Annotated, TypedDict, cast

import operator

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import agent
import model as _model
from agent import _budget_remaining, _spend_budget, session
from model import ModelConfig
from rag import retrieve

# ---------------------------------------------------------------------------
# Model-config store (populated by the phone app via the `config_model` msg)
# ---------------------------------------------------------------------------

_MODEL_CFG = ModelConfig()
_HITL_ENABLED = False  # toggled at runtime by the phone (`set_hitl`)


def set_model_config(d: dict) -> None:
    global _MODEL_CFG
    _MODEL_CFG = ModelConfig.from_dict(d)


def get_model_config() -> ModelConfig:
    return _MODEL_CFG


def set_hitl(enabled: bool) -> None:
    """Toggle the human-in-the-loop gate (O2: 关键时刻才打断你)."""
    global _HITL_ENABLED
    _HITL_ENABLED = bool(enabled)


def is_hitl() -> bool:
    return _HITL_ENABLED


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


class GraphState(TypedDict, total=False):
    user_input: str
    image: str | None
    mode: str
    memory: list
    budget_remaining: int
    retrieval: str
    intent: str
    proposal: dict
    hud_update: dict
    plan_iter: int                      # ReAct loop counter (navigate)
    tool_log: Annotated[list, operator.add]   # append-only ReAct tool-call log
    need_confirm: bool
    confirm_preview: str


# ---------------------------------------------------------------------------
# 0) perceive / retrieve (unchanged role)
# ---------------------------------------------------------------------------


def perceive(state: GraphState) -> dict:
    """Pull shared session state (O1 memory / O2 budget / mode) into the graph."""
    return {
        "mode": session.mode,
        "memory": list(session.memory),
        "budget_remaining": _budget_remaining(),
        "plan_iter": 0,
        "tool_log": [],
        "intent": "",
        "need_confirm": False,
        "confirm_preview": "",
    }


def retrieve_node(state: GraphState) -> dict:
    """B6 hook: gather RAG context for the planner."""
    return {"retrieval": retrieve(state.get("user_input", ""), state.get("mode", "步行"))}


# ---------------------------------------------------------------------------
# Planner slot: real model OR offline local planner (both emit §7 JSON)
# ---------------------------------------------------------------------------


async def _planner(state: GraphState, scene: str, iter_n: int) -> dict:
    """Return a §7 proposal. Uses the real model if configured, else the local stub."""
    cfg = get_model_config()
    if cfg.is_configured():
        # _model.generate is referenced via the module so tests can monkeypatch it.
        return await _model.generate(
            cfg,
            text=state.get("user_input", ""),
            image=state.get("image"),
            mode=state.get("mode", "步行"),
            memory=state.get("memory", []),
            budget_remaining=state.get("budget_remaining", 0),
            retrieval=state.get("retrieval", ""),
        )
    return _local_proposal(state, scene, iter_n)


def _local_proposal(state: GraphState, scene: str, iter_n: int) -> dict:
    """Offline §7 proposal generator (clearly-labeled stub, no API needed).

    Reuses the proven keyword logic from agent.py but emits the contract §7 JSON
    shape so the SAME `orchestrate` gateway consumes it.
    """
    text = state.get("user_input", "")
    mode = state.get("mode", "步行")

    # ---- memory write ----
    if scene == "memory" and any(k in text for k in ("记一下", "记住", "记着", "存一下", "记录", "记：", "记 ")):
        body = __import__("re").sub(r"^(记一下|记住|记着|存一下|记录|记：|记)\s*[:：]?", "", text).strip()
        tag = agent._tag_of(body)
        return {
            "HUD_Text": None,
            "HUD_Image": None, "HUD_Map": None,
            "HUD_Badge": f"已记住（{tag}）：{body}",
            "HUD_Vibration": False,
            "Phone_Full": f"已记住：{body}。",
            "Reasoning_Trace": [{"step": 1, "tag": "O1", "detail": f"第一视角记忆写入（{tag}）", "hypothesis": False}],
            "Memory_Delta": [{"action": "add", "content": body}],
            "Budget_Request": {"speak": False, "reason": "记忆写入无需出声"},
            "Mode_Echo": mode, "Error": None,
        }

    # ---- memory recall ----
    if scene == "memory":
        ans, gap = _local_recall(text)
        if ans is not None:
            return {
                "HUD_Text": ans, "HUD_Image": None, "HUD_Map": None, "HUD_Badge": None,
                "HUD_Vibration": False,
                "Phone_Full": ans,
                "Reasoning_Trace": [{"step": 1, "tag": "O1", "detail": "第一视角记忆召回", "hypothesis": False}],
                "Memory_Delta": None,
                "Budget_Request": {"speak": True, "reason": "读记忆给用户听"},
                "Mode_Echo": mode, "Error": None,
            }

    # ---- translate ----
    if scene == "translate":
        obj = "出口 Exit"
        if any(k in text for k in ("菜单", "menu", "外文", "菜")):
            obj = "菜单：招牌牛排 / 坚果过敏提示"
        return {
            "HUD_Text": f"实时翻译：{obj}", "HUD_Image": None, "HUD_Map": None, "HUD_Badge": None,
            "HUD_Vibration": False,
            "Phone_Full": f"翻译结果：{obj}。\n（OCR+翻译 API 桩：真实环境接入翻译服务）",
            "Reasoning_Trace": [{"step": 1, "tag": "O3", "detail": "步行/独处模式带宽充裕，HUD 用大字幕", "hypothesis": True}],
            "Memory_Delta": None,
            "Budget_Request": {"speak": True, "reason": "用户主动要看翻译，可语音"},
            "Mode_Echo": mode, "Error": None,
        }

    # ---- navigate (with ReAct loop on reroute) ----
    if scene == "navigate":
        reroute = any(k in text for k in ("走错", "偏了", "导错了", "错了", "偏离", "走反"))
        if reroute and iter_n >= 2:
            # 2nd pass after a detected deviation -> corrected route
            return {
                "HUD_Text": "已重新规划路线：前方 30 米右转回到主路", "HUD_Image": None,
                "HUD_Map": {"route": "当前位置→主路", "arrow": "right", "eta": "2 分钟"},
                "HUD_Badge": None, "HUD_Vibration": False,
                "Phone_Full": "检测到偏离，已重新规划：前方 30 米右转回到主路（全程约 600m）。",
                "Reasoning_Trace": [
                    {"step": 1, "tag": "O2", "detail": "偏离路线：触发纠偏循环", "hypothesis": False},
                    {"step": 2, "tag": "O2", "detail": "错路升级：即便抑制模式下也出声", "hypothesis": False},
                ],
                "Memory_Delta": None,
                "Budget_Request": {"speak": True, "reason": "错路升级，主动出声纠偏"},
                "Mode_Echo": mode, "Error": None,
            }
        # silent default navigation (O2 分级主动：默认静默)
        return {
            "HUD_Text": "前方 50 米左转到达地铁站", "HUD_Image": None,
            "HUD_Map": {"route": "家→地铁站", "arrow": "left", "eta": "8 分钟"},
            "HUD_Badge": None, "HUD_Vibration": False,
            "Phone_Full": "导航：从家到地铁站，全程约 1.2km。第一步沿当前道路直行 200 米后左转…（完整分段指引）",
            "Reasoning_Trace": [{"step": 1, "tag": "O2", "detail": "导航默认静默，仅箭头+角标", "hypothesis": False}],
            "Memory_Delta": None,
            "Budget_Request": {"speak": False, "reason": "O2 导航默认静默引导"},
            "Mode_Echo": mode, "Error": None,
        }

    # ---- identify (what is this) ----
    if scene == "identify":
        obj = "一件现代艺术雕塑"
        assoc = ""
        if any(k in text for k in ("上次", "这家", "又", "还", "之前")) and session.last_identify:
            assoc = f"（关联记忆）你上次看过：{session.last_identify}。 "
        session.last_identify = obj
        return {
            "HUD_Text": f"{assoc}这是{obj}", "HUD_Image": None, "HUD_Map": None, "HUD_Badge": None,
            "HUD_Vibration": False,
            "Phone_Full": f"{assoc}这是{obj}，请勿触摸。\n（VLM 桩：真实环境接入视觉模型识别）",
            "Reasoning_Trace": [{"step": 1, "tag": "O3", "detail": "看物问答：HUD 用大字幕+角标", "hypothesis": True}],
            "Memory_Delta": None,
            "Budget_Request": {"speak": True, "reason": "用户主动问，可语音"},
            "Mode_Echo": mode, "Error": None,
        }

    # ---- unknown / clarify ----
    return {
        "HUD_Text": "已收到：" + (text or "(空)"), "HUD_Image": None, "HUD_Map": None, "HUD_Badge": None,
        "HUD_Vibration": False,
        "Phone_Full": "我可以帮你翻译、导航、识别，也能记住你的偏好。试试切换场景模式。",
        "Reasoning_Trace": [{"step": 1, "tag": "none", "detail": "意图未识别，澄清", "hypothesis": False}],
        "Memory_Delta": None,
        "Budget_Request": {"speak": False, "reason": "澄清无需出声"},
        "Mode_Echo": mode, "Error": None,
    }


def _local_recall(text: str):
    """Offline recall over session.memory. Returns (answer, gap_tag) or (None, None)."""
    mem = agent.session.memory
    if any(k in text for k in ("过敏", "忌口", "不吃", "能吃", "饮食", "吃吗", "可以吃")):
        items = [m for m in mem if m.get("tag") == "饮食忌口"]
        return ("你记录的忌口：" + "；".join(i["value"] for i in items) if items
                else "暂无忌口记录，可说「记一下：我XX过敏」让我记住。", "O1")
    if any(k in text for k in ("车停", "车在哪", "停哪", "车位", "停哪层", "车位置", "车层")):
        items = [m for m in mem if m.get("tag") == "停车位置"]
        return ("你记过的停车位置：" + "；".join(i["value"] for i in items) if items
                else "还没记录停车位置，可说「记一下：车停在B2层」。", "O1")
    if any(k in text for k in ("住哪", "住哪里", "酒店", "住过", "上次住", "住宿")):
        items = [m for m in mem if m.get("tag") == "住宿"]
        return ("你记过的住宿：" + "；".join(i["value"] for i in items) if items
                else "还没记录住宿信息。", "O1")
    if any(k in text for k in ("点过", "点单", "上次点", "点什么", "点餐")):
        items = [m for m in mem if m.get("tag") == "点单偏好"]
        return ("你记过的点单：" + "；".join(i["value"] for i in items) if items
                else "还没记录点单偏好。", "O1")
    if any(k in text for k in ("这是谁", "见过", "认识吗", "叫什么", "上次见")):
        items = [m for m in mem if m.get("tag") == "联系人"]
        return ("你记录过的人：" + "；".join(i["value"] for i in items) if items
                else "我还没存过联系人，可说「记一下：老王是XX公司的」。", "O1")
    return None, None


# ---------------------------------------------------------------------------
# Subgraph factory:  plan -> tool -> (loop) -> assemble
# ---------------------------------------------------------------------------


def _build_subgraph(scene: str):
    """A scene subgraph: plan (calls model/local-planner) -> tool (external API stub)
    -> [navigate: conditional loop back to plan] -> assemble."""
    tool_name = {
        "translate": "OCR+翻译 API", "navigate": "地图 API", "identify": "VLM 视觉识别",
        "memory": "记忆存储", "unknown": "澄清",
    }[scene]

    async def s_plan(state: GraphState) -> dict:
        it = state.get("plan_iter", 0) + 1
        prop = await _planner(state, scene, it)
        return {"proposal": prop, "plan_iter": it, "intent": scene}

    def s_tool(state: GraphState) -> dict:
        return {"tool_log": [f"{tool_name}：第 {state.get('plan_iter', 1)} 次调用（ReAct 工具步）"]}

    def s_assemble(state: GraphState) -> dict:
        return {}

    g = StateGraph(GraphState)
    g.add_node("s_plan", s_plan)
    g.add_node("s_tool", s_tool)
    g.add_node("s_assemble", s_assemble)
    g.add_edge(START, "s_plan")
    g.add_edge("s_plan", "s_tool")

    if scene == "navigate":
        def s_check(state: GraphState) -> str:
            # ReAct loop: only one extra planning pass when a deviation is detected.
            text = state.get("user_input", "")
            reroute = any(k in text for k in ("走错", "偏了", "导错了", "错了", "偏离", "走反"))
            if state.get("plan_iter", 0) == 1 and reroute:
                return "s_plan"
            return "s_assemble"

        g.add_conditional_edges("s_tool", s_check)
    else:
        g.add_edge("s_tool", "s_assemble")

    g.add_edge("s_assemble", END)
    return g.compile()


SUB_TRANSLATE = _build_subgraph("translate")
SUB_NAVIGATE = _build_subgraph("navigate")
SUB_IDENTIFY = _build_subgraph("identify")
SUB_MEMORY = _build_subgraph("memory")
SUB_UNKNOWN = _build_subgraph("unknown")

_RUNNERS = {
    "run_translate": SUB_TRANSLATE,
    "run_navigate": SUB_NAVIGATE,
    "run_identify": SUB_IDENTIFY,
    "run_memory": SUB_MEMORY,
    "run_unknown": SUB_UNKNOWN,
}


async def _run_sub(sub, state: GraphState) -> dict:
    fields = ("user_input", "image", "mode", "memory", "budget_remaining", "retrieval", "intent")
    sub_in = {k: state.get(k) for k in fields}
    sub_in["plan_iter"] = 0  # subgraph counts its own ReAct passes from 0
    res = await sub.ainvoke(sub_in)
    return {
        "proposal": res.get("proposal"),
        "tool_log": res.get("tool_log", []),
        "plan_iter": res.get("plan_iter", 0),
        "intent": res.get("intent", state.get("intent")),
    }


async def run_translate(state: GraphState) -> dict:
    return await _run_sub(SUB_TRANSLATE, state)


async def run_navigate(state: GraphState) -> dict:
    return await _run_sub(SUB_NAVIGATE, state)


async def run_identify(state: GraphState) -> dict:
    return await _run_sub(SUB_IDENTIFY, state)


async def run_memory(state: GraphState) -> dict:
    return await _run_sub(SUB_MEMORY, state)


async def run_unknown(state: GraphState) -> dict:
    return await _run_sub(SUB_UNKNOWN, state)


# ---------------------------------------------------------------------------
# Router (conditional edge from `retrieve`)
# ---------------------------------------------------------------------------


def route_intent(state: GraphState) -> str:
    """Classify the user turn into a subgraph. Lightweight keyword router — no
    model call needed for routing (swap for an LLM classifier later)."""
    t = state.get("user_input", "")
    # O1 memory write takes precedence
    if any(k in t for k in ("记一下", "记住", "记着", "存一下", "记录", "记：", "记 ")):
        return "run_memory"
    # O1 memory recall
    if any(k in t for k in ("过敏", "忌口", "不吃", "能吃", "饮食", "车停", "停哪", "车位",
                            "住哪", "住哪里", "酒店", "点过", "点单", "上次点", "这是谁",
                            "认识吗", "叫什么", "上次见")):
        return "run_memory"
    if any(k in t for k in ("翻译", "translate", "什么意思", "menu", "路牌", "菜单", "外文")):
        return "run_translate"
    if any(k in t for k in ("导航", "怎么走", "在哪里", "地铁", "navigate", "where", "带我去",
                            "左转", "右转", "走错", "偏了", "导错了", "错了", "偏离", "走反")):
        return "run_navigate"
    if any(k in t for k in ("这是什么", "what is", "认识", "识别", "是什么", "认不", "看物", "这家")):
        return "run_identify"
    return "run_unknown"


# ---------------------------------------------------------------------------
# ⑨ HITL gate (interrupt before proactive speech)
# ---------------------------------------------------------------------------


def hitl_gate(state: GraphState) -> dict:
    """Pause and ask the PHONE to confirm before the agent speaks proactively.

    Only triggers when HITL is enabled AND the proposal wants to speak AND the
    mode is interruptible (步行/独处). 会议/骑行 are already force-silent by the
    O2 gateway, so no extra confirmation there.
    """
    if not _HITL_ENABLED:
        return {}
    proposal = state.get("proposal") or {}
    speak = (proposal.get("Budget_Request") or {}).get("speak", False)
    if not speak or state.get("mode") not in ("步行", "独处"):
        return {}
    decision = interrupt({
        "prompt": "是否现在播报？（O2 分级主动：关键时刻才打断你）",
        "preview": proposal.get("HUD_Text") or proposal.get("HUD_Badge") or "",
    })
    # resume payload: "approve" | "deny"
    if decision == "deny":
        prop = dict(proposal)
        br = dict(prop.get("Budget_Request") or {})
        br["speak"] = False
        prop["Budget_Request"] = br
        prop["_hitl_denied"] = True
        return {"proposal": prop, "need_confirm": True, "confirm_preview": "已取消播报"}
    return {"need_confirm": True, "confirm_preview": "已确认播报"}


# ---------------------------------------------------------------------------
# ⑧ deterministic O1/O2/O3 gateway (unchanged spirit)
# ---------------------------------------------------------------------------


def _gap_tags(trace: list) -> list:
    tags: list[str] = []
    for t in trace:
        tg = (t or {}).get("tag")
        if tg and tg not in tags:
            tags.append(tg)
    return tags


def orchestrate(state: GraphState) -> dict:
    """§8 gateway: apply O1/O2/O3 deterministic裁决 to the model/local proposal."""
    proposal = state.get("proposal") or {}
    mode = state.get("mode", "步行")
    budget = state.get("budget_remaining", 0)

    # ---- O1: persist Memory_Delta into the shared session store ----
    delta = proposal.get("Memory_Delta")
    if isinstance(delta, list):
        for item in delta:
            if not isinstance(item, dict):
                continue
            action = item.get("action")
            content = item.get("content", "")
            if action == "forget":
                session.memory = [m for m in session.memory if m.get("value") != content]
            else:  # add / update
                session.memory.append(
                    {"tag": agent._tag_of(content), "value": content, "ts": int(time.time())}
                )
    memory_now = list(session.memory)

    # ---- O2 + O3 gateway over the §7 proposal (docs §8) ----
    err = proposal.get("Error")
    hud_text = proposal.get("HUD_Text")
    hud_badge = proposal.get("HUD_Badge")
    hud_image = proposal.get("HUD_Image")
    hud_map = proposal.get("HUD_Map")
    hud_vib = bool(proposal.get("HUD_Vibration", False))

    suppressed = False
    trace = list(proposal.get("Reasoning_Trace") or [])
    # surface the ReAct tool log as reasoning steps
    for step in state.get("tool_log", []) or []:
        trace.append({"step": len(trace) + 1, "tag": "tool", "detail": step, "hypothesis": False})
    if state.get("need_confirm"):
        trace.append({"step": len(trace) + 1, "tag": "O2",
                      "detail": f"HITL 人类确认：{state.get('confirm_preview','')}", "hypothesis": False})
    speak_req = (proposal.get("Budget_Request") or {}).get("speak", True)

    # O3 channel gating by mode
    if mode == "会议":
        if hud_text and not hud_badge:
            hud_badge = hud_text
        hud_text = None
        hud_image = None
        hud_map = None
        hud_vib = False
        trace.append({"step": len(trace) + 1, "tag": "O3",
                      "detail": "会议模式：仅角标，关闭大字幕/图/地图/震动", "hypothesis": True})
    elif mode == "骑行":
        if hud_text and not hud_badge:
            hud_badge = hud_text
        hud_text = None
        hud_image = None
        hud_vib = bool(hud_vib or hud_map)
        trace.append({"step": len(trace) + 1, "tag": "O3",
                      "detail": "骑行模式：仅震动+地图+角标，关闭读图与大字幕", "hypothesis": True})
    else:  # 步行 / 独处：全通道开放
        trace.append({"step": len(trace) + 1, "tag": "O3",
                      "detail": f"{mode}模式：全通道开放（大字幕+图+地图）", "hypothesis": True})

    # O2 graded proactivity (docs §8)
    if mode in ("会议", "骑行"):
        suppressed = True
        if speak_req:
            trace.append({"step": len(trace) + 1, "tag": "O2",
                          "detail": f"{mode}模式：否决模型出声请求（分级主动）", "hypothesis": False})
    else:  # 步行 / 独处：预算门控的主动出声
        if speak_req:
            if budget > 0:
                _spend_budget()
            else:
                suppressed = True
                if hud_text and not hud_badge:
                    hud_badge = hud_text
                hud_text = None
                trace.append({"step": len(trace) + 1, "tag": "O2",
                              "detail": "打扰预算耗尽：抑制语音并降级为角标", "hypothesis": False})
        # HITL 人类拒绝播报 -> 完整抑制（O2 分级主动：尊重人的决定）
        if proposal.get("_hitl_denied"):
            suppressed = True
            if hud_text and not hud_badge:
                hud_badge = hud_text
            hud_text = None
            trace.append({"step": len(trace) + 1, "tag": "O2",
                          "detail": "HITL 人类拒绝播报：已抑制语音与大字幕", "hypothesis": False})

    budget_now = _budget_remaining()

    # ---- assemble lens + phone views (contract §4 / §7) ----
    lens = {
        "bubbles": [{"kind": "notify", "text": hud_text}] if hud_text else [],
        "map": hud_map,
        "side_output": hud_image,
        "vibration": hud_vib,
        "badge": hud_badge,
    }
    phone = {
        "ai_output": proposal.get("Phone_Full") or "",
        "reasoning_trace": trace,
        "memory": memory_now,
        "budget": budget_now,
        "mode": mode,
        "kb_status": "stub (B6)",
        "loop_passes": state.get("plan_iter", 0),
        "config_echo": get_model_config().to_dict(),
        "error": err,
    }
    hud_update = {
        "lens": lens,
        "phone": phone,
        "gap_tags": _gap_tags(trace),
        "suppressed": suppressed,
        "mode": mode,
        "ts": int(time.time()),
        "client": "both",
    }
    return {"hud_update": hud_update}


def output(state: GraphState) -> dict:
    """Terminal node: noop merge — hud_update already built by orchestrate."""
    return {}


# ---------------------------------------------------------------------------
# Compile (with Checkpointer) + run
# ---------------------------------------------------------------------------


def _build_graph():
    g = StateGraph(GraphState)
    g.add_node("perceive", perceive)
    g.add_node("retrieve", retrieve_node)
    g.add_node("run_translate", run_translate)
    g.add_node("run_navigate", run_navigate)
    g.add_node("run_identify", run_identify)
    g.add_node("run_memory", run_memory)
    g.add_node("run_unknown", run_unknown)
    g.add_node("hitl_gate", hitl_gate)
    g.add_node("orchestrate", orchestrate)
    g.add_node("output", output)

    g.add_edge(START, "perceive")
    g.add_edge("perceive", "retrieve")
    g.add_conditional_edges("retrieve", route_intent)  # -> run_*
    g.add_edge("run_translate", "hitl_gate")
    g.add_edge("run_navigate", "hitl_gate")
    g.add_edge("run_identify", "hitl_gate")
    g.add_edge("run_memory", "hitl_gate")
    g.add_edge("run_unknown", "hitl_gate")
    g.add_edge("hitl_gate", "orchestrate")
    g.add_edge("orchestrate", "output")
    g.add_edge("output", END)
    return g.compile(checkpointer=MemorySaver())


APP = _build_graph()


async def run_graph(
    msg: dict,
    cfg: ModelConfig | None = None,
    thread_id: str | None = None,
    resume: str | None = None,
) -> dict:
    """Server entry point: run one user turn (or resume a paused HITL turn).

    Returns the contract-shaped hud_update (lens + phone views). If the turn hit
    a HITL interrupt, returns {"__interrupt__": {...payload...}, "thread_id": ...}
    so the server can ask the phone to confirm and later resume.
    """
    if cfg is not None:
        set_model_config(cfg.to_dict())

    config = {"configurable": {"thread_id": thread_id or f"t-{uuid.uuid4().hex}"}}

    if resume is not None:
        # Resume a paused HITL turn on the SAME thread.
        result = await APP.ainvoke(Command(resume=resume), config=config)
    else:
        initial: GraphState = {
            "user_input": msg.get("text", ""),
            "image": msg.get("image"),
        }
        result = await APP.ainvoke(initial, config=config)

    # HITL pause: langgraph returns the intermediate state with __interrupt__.
    if "__interrupt__" in result and result["__interrupt__"]:
        intr = result["__interrupt__"][0]
        payload = intr.value if hasattr(intr, "value") else intr
        return {"__interrupt__": payload, "thread_id": config["configurable"]["thread_id"]}

    return result.get("hud_update", {})
