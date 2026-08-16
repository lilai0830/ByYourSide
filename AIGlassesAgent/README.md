# GOAI 无界应用 · AI 眼镜 — 出行辅助 Agent

面向 **AI 眼镜** 的出行辅助智能体 Demo。后端用 **LangGraph** 编排多节点状态机，
通过 **FastAPI** 同时驱动「镜片 HUD」与「手机 App」两块前端，模拟一副 AI 眼镜
在真实出行场景下的感知—决策—反馈闭环。

> 赛道：GOAI 无界应用 · AI+眼镜 ｜ 方向：出行辅助 Agent

---

## 三个 Demo 场景（红海"标配三件套"，靠 Gap 加成而非替换）

| 场景 | 说明 |
|------|------|
| **实时翻译** | 镜片实时显示对话译文，手机端保留完整上下文与会话记忆。 |
| **视野导航** | 基于当前模式（步行/骑行/会议/独处）输出轻量、可分心的导航提示。 |
| **看物问答** | 用户上传图片，Agent 结合 RAG 知识库回答"眼前这是什么"。 |

---

## 目录结构

```
AIGlassesAgent/
├── backend/              # Python 后端
│   ├── server.py        # FastAPI 服务：托管 HUD/手机前端 + WebSocket 桥接
│   ├── graph.py         # LangGraph 编排：状态机 / MemorySaver / HITL 中断
│   ├── agent.py         # 意图路由 + 预算控制 + 会话记忆
│   ├── model.py         # OpenAI 兼容模型层（任意 OpenAI 风格端点可插拔）
│   └── rag.py           # RAG 检索层（内置 demo 语料 + 用户上传 KB）
├── frontend/            # HUD 模拟器
│   ├── index.html       # 镜片 HUD（client: lens）
│   ├── phone.html       # 手机 App（client: phone）
│   ├── app.js / phone.js / style.css / phone.css
├── prompt/              # 系统提示词模板（模型层 system prompt 来源，需随仓库发布）
├── docs/                # 方案文档（分析 / 调研 / 协议契约）
├── requirements.txt     # 后端依赖（已锁版本）
└── README.md
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动后端

在项目根目录运行：

```bash
python backend/server.py
```

### 3. 打开前端

启动后访问：

- **镜片 HUD**：<http://localhost:8000/> （`client: lens`）
- **手机 App**：<http://localhost:8000/phone.html> （`client: phone`）

两块页面连到同一个 WebSocket（`/ws`），分别声明自己的 `client` 类型，
共享同一份会话状态（模式 / O1 记忆 / O2 预算 / 模型配置 / KB）。

---

## 前后端协议（摘要）

前端 → 后端 的 WebSocket 消息：

```jsonc
{"type":"identify",     "client":"lens"|"phone"}
{"type":"user_input",   "text": "...", "image": "data:image/...;base64,..."}  // image 可选
{"type":"set_mode",     "mode":"步行"|"骑行"|"会议"|"独处"}
{"type":"config_model", "base_url":"...", "api_key":"...", "model_text":"...", "model_vision":"..."}
{"type":"upload_kb",    "filename":"...", "content":"..."}
{"type":"kb_list"}
{"type":"clear_memory"}
{"type":"export_memory"}
```

后端在每个回合分别向 **lens** 客户端推送镜片视图、向 **phone** 客户端推送手机视图
（详见 `docs/显示协议契约.md`）。

**HITL（人在回路）**：当某回合需要在手机端人工确认时，后端会暂停 LangGraph 状态机
（`interrupt`），仅向手机端下发确认提示，确认后继续。

---

## 模型接入

`model.py` 是一个 **OpenAI 兼容** 模型层，不绑定具体厂商。在手机端「模型配置」里
填入任意 OpenAI 风格端点的 `base_url` / `api_key` / `model_text`（可选 `model_vision`），
即可切换为真实模型；未配置时走内置的占位逻辑，方便纯前端联调。

---

## 文档导航

| 文件 | 内容 |
|------|------|
| `GOAI比赛分析与执行规划.md` | 比赛拆解与执行路线 |
| `PPT提纲_初赛方案.md` | 初赛方案提纲 |
| `市场调研_AI眼镜竞品分析与MarketGap.md` | 竞品分析与市场空白 |
| `用户同理分析_用户旅程_用户故事_用户画像.md` | 用户研究 |
| `显示协议契约.md` | 前后端显示协议（§2 / §7 等） |
| `prompt/系统提示词模板.md` | 系统提示词模板（模型层 system prompt 来源，位于项目根 `prompt/`，需随仓库发布） |
