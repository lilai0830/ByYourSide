"""Agent controller for the AI-Glasses HUD demo (upgraded MVP).

This controller *layers the three research goals on top of the three
red-ocean scenes*, per the locked user-research spec
(docs/用户同理分析_用户旅程_用户故事_用户画像.md):

  - 三场景（红海载体）：实时翻译 / 视野导航 / 看物问答
  - 三目标（差异化加成）：
      O1 第一视角长期记忆   -> remember() / recall()  over a session memory store
      O2 分级主动与"抑制"   -> mode gating + distraction budget + error escalation
      O3 带宽感知输出编排   -> orchestrate() picks HUD channels by mode  [团队假设]

It is a *deterministic policy controller* — no external LLM / VLM / maps
call is made, so the competition demo runs reliably offline. The seams
where a real LangGraph + VLM + RAG pipeline plugs in are marked with
TODO so `run_agent` can be swapped without touching the frontend.

Honest labeling (per user-research §0):
  O1 / O2  -> backed by confirmed pain points (调研 §5/§10)
  O3       -> TEAM HYPOTHESIS, demonstrated in-demo, not a market demand
"""
from __future__ import annotations

import json
import re
import time

# ----------------------------------------------------------------------------
# Session state (lives for the process lifetime; resets on restart)
# ----------------------------------------------------------------------------


class Session:
    def __init__(self) -> None:
        self.mode = "步行"            # 步行 / 骑行 / 会议 / 独处
        self.memory: list[dict] = []  # [{"tag","value","ts"}]
        self.last_identify: str | None = None
        self.budget_total = 6         # 打扰预算：每会话最多主动出声次数
        self.budget_used = 0
        self.budget_reset_at = time.time()


MODES = ["步行", "骑行", "会议", "独处"]
session = Session()


# ----------------------------------------------------------------------------
# O2 helper: distraction budget (打扰预算)
# ----------------------------------------------------------------------------


def _budget_remaining() -> int:
    # 预算按小时重置（Demo 中基本不会触发，仅作机制展示）
    if time.time() - session.budget_reset_at > 3600:
        session.budget_used = 0
        session.budget_reset_at = time.time()
    return max(0, session.budget_total - session.budget_used)


def _spend_budget() -> None:
    session.budget_used += 1


# ----------------------------------------------------------------------------
# O3 helper: 按模式压缩字幕（带宽感知，团队假设）
# ----------------------------------------------------------------------------


def _compress(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    cut = text[:cap]
    # 尽量在句号/逗号等边界截断
    m = re.search(r"[。！？；.!?]", cut[::-1])
    if m:
        idx = cap - m.start()
        return text[:idx].rstrip("，,。 ") + "…"
    return cut.rstrip("，,。 ") + "…"


# ----------------------------------------------------------------------------
# O1: 第一视角长期记忆
# ----------------------------------------------------------------------------


def _tag_of(body: str) -> str:
    if any(k in body for k in ("过敏", "忌口", "不吃", "素食", "饮食", "辣")):
        return "饮食忌口"
    if any(k in body for k in ("车停", "停车", "停在", "车位", "停哪", "车在", "车层")):
        return "停车位置"
    if any(k in body for k in ("住哪", "住哪里", "酒店", "住的", "住过", "住宿", "住进")):
        return "住宿"
    if any(k in body for k in ("点单", "点过", "上次点", "点什么", "点餐")):
        return "点单偏好"
    if any(k in body for k in ("是", "为")) and re.search(r"[\u4e00-\u9fa5]{2,}", body):
        return "联系人"
    return "通用"


def _save_memory(text: str) -> dict:
    body = re.sub(r"^(记一下|记住|记着|存一下|记录|记：|记)\s*[:：]?", "", text).strip()
    item = {"tag": _tag_of(body), "value": body, "ts": int(time.time())}
    session.memory.append(item)
    return item


def _recall(text: str):
    """Return (reply_text, gap_tag) or (None, None) if not a recall query."""
    if any(k in text for k in ("过敏", "忌口", "不吃", "能吃", "饮食", "吃吗", "可以吃")):
        items = [m for m in session.memory if m["tag"] == "饮食忌口"]
        if items:
            return "你记录的忌口：" + "；".join(i["value"] for i in items), "O1"
        return "暂无忌口记录，可说「记一下：我XX过敏」让我记住。", "O1"
    if any(k in text for k in ("车停", "车在哪", "停哪", "车位", "停哪层", "车位置", "车层")):
        items = [m for m in session.memory if m["tag"] == "停车位置"]
        if items:
            return "你记过的停车位置：" + "；".join(i["value"] for i in items), "O1"
        return "还没记录停车位置，可说「记一下：车停在B2层」。", "O1"
    if any(k in text for k in ("住哪", "住哪里", "酒店", "住过", "上次住", "住宿")):
        items = [m for m in session.memory if m["tag"] == "住宿"]
        if items:
            return "你记过的住宿：" + "；".join(i["value"] for i in items), "O1"
        return "还没记录住宿信息。", "O1"
    if any(k in text for k in ("点过", "点单", "上次点", "点什么", "点餐")):
        items = [m for m in session.memory if m["tag"] == "点单偏好"]
        if items:
            return "你记过的点单：" + "；".join(i["value"] for i in items), "O1"
        return "还没记录点单偏好。", "O1"
    if any(k in text for k in ("这是谁", "见过", "认识吗", "叫什么", "上次见")):
        items = [m for m in session.memory if m["tag"] == "联系人"]
        if items:
            return "你记录过的人：" + "；".join(i["value"] for i in items), "O1"
        return "我还没存过联系人，可说「记一下：老王是XX公司的」。", "O1"
    return None, None


# ----------------------------------------------------------------------------
# O3: 编排器 —— 同一答案按模式选择 HUD 通道
# ----------------------------------------------------------------------------


def _orchestrate(base: dict, gap_tags: list[str]) -> dict:
    mode = session.mode
    subtitle = base.get("subtitle", "")
    tts = base.get("tts")
    arrow = base.get("arrow", "none")
    card = base.get("card")
    silent_nav = base.get("silent_nav", False)  # O2：导航默认静默

    badge = None
    vibration = False
    suppressed = False

    # O3 带宽封顶：不同模式允许的字幕长度不同（团队假设）
    cap = {"步行": 22, "骑行": 14, "会议": 12, "独处": 60}[mode]
    subtitle = _compress(subtitle, cap)

    if mode == "会议":
        # O2 US-P1 + O3：静默只角标，不放大字幕、不出声
        if tts:
            suppressed = True
        tts = None
        badge = subtitle
        subtitle = ""  # 角标替代大字幕
    elif mode == "骑行":
        # O2 US-N3 + O3：纯震动 + 箭头 + 压缩字幕，零语音
        if tts:
            suppressed = True
        tts = None
        # 震动仅在有导航/纠偏提示时触发（非导航如翻译只给静默字幕）
        vibration = bool(arrow != "none" or silent_nav)
        if silent_nav:
            badge = subtitle  # 静默导航同时给角标
    else:  # 步行 / 独处：语音允许，但受打扰预算与 O2 导航静默约束
        if silent_nav:
            # O2 分级主动：导航默认只给箭头+角标，不出声；只有错路升级才出声
            badge = subtitle
        elif tts:
            if _budget_remaining() <= 0:
                suppressed = True
                tts = None
                badge = subtitle
            else:
                _spend_budget()

    return {
        "subtitle": subtitle,
        "card": card,
        "arrow": arrow,
        "tts": tts,
        "badge": badge,
        "vibration": vibration,
        "suppressed": suppressed,
        "gap_tags": gap_tags,
    }


# ----------------------------------------------------------------------------
# 场景路由（红海载体）
# ----------------------------------------------------------------------------


def _scene_translate(text: str) -> tuple[dict, list[str]]:
    obj = "出口 Exit"
    if any(k in text for k in ("菜单", "menu", "外文", "菜")):
        obj = "菜单：招牌牛排 / 坚果过敏提示"
    base = {
        "subtitle": f"实时翻译：{obj}",
        "card": {
            "title": "Translation",
            "lines": ["原文：出口 / 菜单", "译文：Exit / Menu", "模式：OCR+翻译(桩)"],
            "source": "translation-api (stub)",
        },
        "arrow": "none",
        "tts": f"翻译结果：{obj}。",
    }
    # O3：输出形态由编排器按模式决定；翻译本身是载体
    return base, ["O3"]


def _scene_navigate(text: str) -> tuple[dict, list[str]]:
    if any(k in text for k in ("走错", "偏了", "导错了", "错了", "偏离", "走反")):
        # O2 错路升级：即便在抑制模式下也出声（关键时刻才打断）
        base = {
            "subtitle": "您已偏离路线，请掉头返回主路",
            "card": {
                "title": "Navigation · 纠偏",
                "lines": ["状态：偏离", "动作：掉头", "O2：错路才主动出声"],
                "source": "maps-api (stub)",
            },
            "arrow": "left",
            "tts": "您已偏离路线，请掉头返回主路。",
        }
        return base, ["O2"]
    # O2 分级主动：默认静默，只给箭头 + 角标，不出声
    base = {
        "subtitle": "前方 50 米左转到达地铁站",
        "card": {
            "title": "Navigation",
            "lines": ["目的地：地铁站", "距离：50 m", "动作：左转", "O2：静默引导"],
            "source": "maps-api (stub)",
        },
        "arrow": "left",
        "tts": None,
        "silent_nav": True,
    }
    return base, ["O2", "O3"]


def _scene_identify(text: str, frame_note: str | None) -> tuple[dict, list[str]]:
    obj = frame_note or "一件现代艺术雕塑"
    gap = ["O3"]
    assoc = ""
    if any(k in text for k in ("上次", "这家", "又", "还", "之前")) and session.last_identify:
        # O1 US-V2：看物关联记忆
        assoc = f"（关联记忆）你上次看过：{session.last_identify}。 "
        gap = ["O1", "O3"]
    session.last_identify = obj
    base = {
        "subtitle": f"{assoc}这是{obj}",
        "card": {
            "title": "Object",
            "lines": ["类别：雕塑 / 现代艺术", "置信度：0.91", "提示：请勿触摸"],
            "source": "vlm (stub)",
        },
        "arrow": "none",
        "tts": f"{assoc}这是{obj}，请勿触摸。",
    }
    return base, gap


# ----------------------------------------------------------------------------
# 顶层入口
# ----------------------------------------------------------------------------


def _finalize(base: dict, gap_tags: list[str], trace: str) -> dict:
    orch = _orchestrate(base, gap_tags)
    return {
        "subtitle": orch["subtitle"],
        "card": orch["card"],
        "arrow": orch["arrow"],
        "tts": orch["tts"],
        "badge": orch["badge"],
        "vibration": orch["vibration"],
        "suppressed": orch["suppressed"],
        "gap_tags": orch["gap_tags"],
        "trace": trace,
        "memory": session.memory,
        "budget": _budget_remaining(),
        "mode": session.mode,
    }


def run_agent(user_input: str = "", modality: str = "text", frame_note: str | None = None) -> dict:
    """Return a structured HUD payload for the frontend to render.

    Contract (frontend never changes when backend grows):
        subtitle / card / arrow / tts  (原有)
        + badge / vibration / suppressed / gap_tags / memory / budget / mode  (Gap 加成)
    """
    text = (user_input or "").strip()
    gap_tags: list[str] = []

    # TODO: replace keyword routing with LangGraph intent classification.
    # TODO: wire VLM(frame_note) for "what is this" / OCR for menus.
    # TODO: wire translation API / maps API / user RAG memory.

    # 1) O1 记忆写入
    if any(k in text for k in ("记一下", "记住", "记着", "存一下", "记录", "记：", "记 ")):
        item = _save_memory(text)
        base = {
            "subtitle": f"已记住（{item['tag']}）：{item['value']}",
            "card": {
                "title": "O1 第一视角记忆",
                "lines": [f"类别：{item['tag']}", f"内容：{item['value']}", "可随时「清空记忆」"],
                "source": "first-person memory (O1)",
            },
            "arrow": "none",
            "tts": f"已记住，{item['tag']}。",
        }
        return _finalize(base, ["O1"], f"memory.save[{item['tag']}] -> O1")

    # 2) O1 记忆读取
    recalled, g = _recall(text)
    if recalled is not None:
        base = {
            "subtitle": recalled,
            "card": {
                "title": "O1 第一视角记忆",
                "lines": ["来源：本会话已沉淀记忆", "随时可清空/导出"],
                "source": "O1 memory recall",
            },
            "arrow": "none",
            "tts": recalled,
        }
        return _finalize(base, [g], "memory.recall -> O1")

    # 3) 三场景
    if any(k in text for k in ("翻译", "translate", "什么意思", "menu", "路牌", "菜单", "外文")):
        base, gap = _scene_translate(text)
        return _finalize(base, gap, "intent[translate] -> OCR+translate (stub) -> O3 orchestrate")
    if any(k in text for k in ("导航", "怎么走", "在哪里", "地铁", "navigate", "where", "带我去",
                               "左转", "右转", "走错", "偏了", "导错了", "错了", "偏离")):
        base, gap = _scene_navigate(text)
        return _finalize(base, gap, "intent[navigate] -> O2 graded proactivity")
    if any(k in text for k in ("这是什么", "what is", "认识", "识别", "是什么", "认不", "看物", "这家")):
        base, gap = _scene_identify(text, frame_note)
        return _finalize(base, gap, "intent[identify] -> VLM (stub) -> O1/O3")

    # 4) 默认：保持循环
    base = {
        "subtitle": "已收到：" + (text or "(空)"),
        "card": {
            "title": "Assistant",
            "lines": ["可说：翻译/导航/这是什么", "记忆：记一下 X / 我过敏吗", "模式：切换步行·骑行·会议·独处"],
            "source": "agent-core",
        },
        "arrow": "none",
        "tts": "我可以帮你翻译、导航、识别，也能记住你的偏好。试试切换场景模式。",
    }
    return _finalize(base, [], "intent[unknown] -> clarify")


# ----------------------------------------------------------------------------
# WebSocket 消息分发（set_mode / clear_memory / export_memory）
# ----------------------------------------------------------------------------


def handle_message(msg: dict) -> dict:
    """Single entry point for the /ws endpoint.

    Returns a dict that the server wraps as {"type": "hud_update", **payload}.
    """
    t = msg.get("type")

    if t == "set_mode":
        if msg.get("mode") in MODES:
            session.mode = msg["mode"]
        return {
            "subtitle": f"场景模式：{session.mode}",
            "card": {
                "title": "Mode",
                "lines": [f"当前：{session.mode}", "O2 抑制 + O3 通道编排已切换"],
                "source": "O2/O3 controller",
            },
            "arrow": "none",
            "tts": None,
            "badge": None,
            "vibration": False,
            "suppressed": False,
            "gap_tags": ["O2", "O3"],
            "trace": f"set_mode -> {session.mode} (O2 抑制策略 + O3 通道编排)",
            "memory": session.memory,
            "budget": _budget_remaining(),
            "mode": session.mode,
        }

    if t == "clear_memory":
        session.memory = []
        session.last_identify = None
        return {
            "subtitle": "记忆已清空（O1 合规：数据自主可控）",
            "card": {"title": "O1 记忆", "lines": ["全部记忆已清除"], "source": "O1 compliance"},
            "arrow": "none",
            "tts": None,
            "badge": None,
            "vibration": False,
            "suppressed": False,
            "gap_tags": ["O1"],
            "trace": "memory.clear (O1 合规：记忆可一键清除)",
            "memory": session.memory,
            "budget": _budget_remaining(),
            "mode": session.mode,
        }

    if t == "export_memory":
        return {
            "subtitle": "记忆已导出（O1 合规：可带走你的数据）",
            "card": {"title": "O1 记忆", "lines": [f"共 {len(session.memory)} 条"], "source": "O1 export"},
            "arrow": "none",
            "tts": None,
            "badge": None,
            "vibration": False,
            "suppressed": False,
            "gap_tags": ["O1"],
            "trace": "memory.export (O1 合规：记忆可导出)",
            "memory": session.memory,
            "memory_export": json.dumps(session.memory, ensure_ascii=False),
            "budget": _budget_remaining(),
            "mode": session.mode,
        }

    # default: user_input
    return run_agent(msg.get("text", ""), msg.get("modality", "text"), msg.get("frame_note"))
