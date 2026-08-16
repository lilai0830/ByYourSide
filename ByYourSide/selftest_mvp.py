"""Self-test: drive agent.handle_message with the demo-script flows and assert
the O1/O2/O3 behaviors. Runs with the project's own code (no reimplementation).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
import agent
from agent import handle_message

passed, failed = 0, 0
def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")

def h(msg):
    return handle_message(msg)

print("== O2 分级主动：导航默认静默，错路才出声 ==")
# 步行模式
h({"type": "set_mode", "mode": "步行"})
r = h({"type": "user_input", "text": "带我去地铁站"})
check("导航默认不出声(静默引导)", r["tts"] is None, r)
check("导航给箭头", r["arrow"] == "left", r)
check("导航静默给角标", bool(r["badge"]), r)
check("标记 O2", "O2" in r["gap_tags"], r["gap_tags"])

r = h({"type": "user_input", "text": "我走错了"})
check("错路升级出声", r["tts"] is not None, r)
check("错路箭头", r["arrow"] == "left", r)
check("错路标记 O2", "O2" in r["gap_tags"], r["gap_tags"])

print("== O2/O3 会议模式：零语音只角标 ==")
h({"type": "set_mode", "mode": "会议"})
r = h({"type": "user_input", "text": "翻译这个菜单"})
check("会议模式不出声", r["tts"] is None, r)
check("会议模式给角标", bool(r["badge"]), r)
check("会议模式隐藏大字幕", r["subtitle"] == "", r)
check("会议标记 O3(假设)", "O3" in r["gap_tags"], r["gap_tags"])

print("== O2/O3 骑行模式：纯震动+箭头，零语音 ==")
h({"type": "set_mode", "mode": "骑行"})
r = h({"type": "user_input", "text": "带我去地铁站"})
check("骑行不出声", r["tts"] is None, r)
check("骑行触发震动", r["vibration"] is True, r)
check("骑行给角标", bool(r["badge"]), r)

print("== O1 第一视角记忆：存/取/清空 ==")
h({"type": "set_mode", "mode": "独处"})
h({"type": "user_input", "text": "记一下：我坚果过敏"})
r = h({"type": "user_input", "text": "我有什么忌口"})
check("记忆存入饮食忌口", any(m["tag"] == "饮食忌口" for m in r["memory"]), r["memory"])
check("忌口可召回", "坚果" in r["subtitle"], r["subtitle"])
check("召回标记 O1", "O1" in r["gap_tags"], r["gap_tags"])

h({"type": "user_input", "text": "记一下：车停在B2层"})
r = h({"type": "user_input", "text": "车停哪层了"})
check("停车位置可召回", "B2" in r["subtitle"], r["subtitle"])

r = h({"type": "clear_memory"})
check("清空后记忆为空", r["memory"] == [], r["memory"])
check("清空标记 O1", "O1" in r["gap_tags"], r["gap_tags"])

print("== O2 打扰预算：会用声的场景扣减，耗尽则抑制 ==")
# 导航默认静默(不花预算)；用会出声的「翻译」验证预算扣减
h({"type": "set_mode", "mode": "独处"})
agent.session.budget_used = 0  # 干净起点，便于断言
budgets = []
for i in range(agent.session.budget_total + 2):
    r = h({"type": "user_input", "text": "翻译这个路牌"})
    budgets.append(r["budget"])
check("预算从满到耗尽递减", budgets[0] == agent.session.budget_total - 1 and budgets[-1] == 0, budgets)
last = h({"type": "user_input", "text": "翻译这个路牌"})
check("预算耗尽后抑制语音", last["tts"] is None and last["suppressed"] is True, last)

print(f"\n结果：{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
