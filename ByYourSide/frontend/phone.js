(() => {
  const wsProto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${wsProto}://${location.host}/ws`);

  const $ = (id) => document.getElementById(id);
  const conn = $("conn");
  const aiOutput = $("ai-output");
  const trace = $("trace");
  const errBanner = $("err-banner");
  const stMode = $("st-mode");
  const stBudget = $("st-budget");
  const stSuppressed = $("st-suppressed");
  const stGaps = $("st-gaps");
  const memList = $("mem-list");
  const kbList = $("kb-list");
  const cfgStatus = $("cfg-status");
  const piText = $("pi-text");

  ws.onopen = () => {
    conn.textContent = "已连接";
    conn.className = "conn on";
    ws.send(JSON.stringify({ type: "identify", client: "phone" }));
  };
  ws.onclose = () => { conn.textContent = "未连接"; conn.className = "conn off"; };

  function escapeHtml(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"]/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function gapLabel(tag, hypothesis) {
    const map = {
      O1: '<span class="tag-o1">[O1 记忆]</span>',
      O2: '<span class="tag-o2">[O2 主动/抑制]</span>',
      O3: '<span class="tag-o3">[O3 带宽编排·假设]</span>',
    };
    const hyp = hypothesis ? '<span class="hyp">假设</span>' : "";
    return (map[tag] || `[${escapeHtml(tag)}]`) + hyp;
  }

  function renderMemory(items) {
    if (!items || items.length === 0) {
      memList.innerHTML = '<li class="empty">（暂无记忆）</li>';
      return;
    }
    memList.innerHTML = items.map(i =>
      `<li><span class="tag-o1">${escapeHtml(i.tag || "")}</span>${escapeHtml(i.value)}</li>`
    ).join("");
  }

  function renderPhoneView(d) {
    aiOutput.textContent = d.ai_output || "（无输出）";

    const rt = d.reasoning_trace || [];
    trace.innerHTML = rt.map(t =>
      `<li>${gapLabel(t.tag, t.hypothesis)} ${escapeHtml(t.detail)}</li>`
    ).join("") || '<li class="empty">（无）</li>';

    stMode.textContent = d.mode || "-";
    stBudget.textContent = (typeof d.budget === "number") ? d.budget : "-";
    stSuppressed.classList.toggle("hidden", !d.suppressed);
    stGaps.innerHTML = (d.gap_tags || []).map(g => `<span>${gapLabel(g, false)}</span>`).join("");

    if (d.error) {
      errBanner.textContent = "⚠ " + d.error;
      errBanner.classList.remove("hidden");
    } else {
      errBanner.classList.add("hidden");
    }

    if (Array.isArray(d.memory)) renderMemory(d.memory);

    // sync mode buttons
    if (d.mode) {
      document.querySelectorAll(".pmode").forEach(b =>
        b.classList.toggle("active", b.dataset.mode === d.mode));
    }

    // O1 export -> download JSON
    if (d.memory_export) {
      const blob = new Blob([d.memory_export], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "goai_memory_export.json"; a.click();
      URL.revokeObjectURL(url);
    }
  }

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "hud_update") renderPhoneView(msg);
    else if (msg.type === "config_echo") {
      cfgStatus.textContent = msg.configured
        ? `已配置：${msg.model_text}` + (msg.model_vision ? ` + ${msg.model_vision}` : "")
        : "未配置（请填写 base_url / api_key / model_text）";
    } else if (msg.type === "hitl_prompt") {
      showHitl(msg);
    } else if (msg.type === "hitl_status") {
      hitlOn = msg.enabled;
      const st = document.getElementById("hitl-state");
      const btn = document.getElementById("btn-hitl");
      if (st) st.textContent = "当前：" + (hitlOn ? "开启" : "关闭");
      if (btn) btn.textContent = hitlOn ? "关闭 HITL 确认" : "开启 HITL 确认";
    } else if (msg.type === "kb_status") {
      cfgStatus.textContent = `知识库：${msg.status}，共 ${msg.count} 篇`;
      ws.send(JSON.stringify({ type: "kb_list" }));
    } else if (msg.type === "kb_list") {
      if (!msg.docs.length) kbList.innerHTML = '<li class="empty">（暂无上传文档）</li>';
      else kbList.innerHTML = msg.docs.map(f => `<li>${escapeHtml(f)}</li>`).join("");
    }
  };

  function send(obj) {
    if (ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify(obj));
  }

  // ---- tabs ----
  document.querySelectorAll(".tab").forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
      $(`tab-${btn.dataset.tab}`).classList.add("active");
    };
  });

  // ---- P3 input (drives the graph) ----
  function sendInput() {
    const v = piText.value.trim();
    if (!v && !pendingImage) return;
    send({ type: "user_input", text: v, image: pendingImage || null });
    piText.value = "";
    pendingImage = null;
    $("btn-pi-img").textContent = "📷";
  }
  $("btn-pi-send").onclick = sendInput;
  piText.addEventListener("keydown", e => { if (e.key === "Enter") sendInput(); });

  let pendingImage = null;
  $("btn-pi-img").onclick = () => $("pi-image").click();
  $("pi-image").onchange = (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => { pendingImage = reader.result; $("btn-pi-img").textContent = "📷✓"; };
    reader.readAsDataURL(f);
  };

  // ---- P2 config ----
  $("btn-save-cfg").onclick = () => {
    send({
      type: "config_model",
      base_url: $("cfg-base").value.trim(),
      api_key: $("cfg-key").value.trim(),
      model_text: $("cfg-text").value.trim(),
      model_vision: $("cfg-vision").value.trim() || null,
    });
  };

  // ---- HITL toggle (O2 分级主动：关键时刻才打断你) ----
  let hitlOn = false;
  const cfgPanel = $("tab-config");
  const hitlWrap = document.createElement("div");
  hitlWrap.className = "hitl-wrap";
  hitlWrap.innerHTML = `
    <h3>人类介入 (HITL)</h3>
    <p class="hint">开启后，眼镜准备主动出声前会先在手机上请你确认（O2 分级主动）。关闭则直接播报。</p>
    <button id="btn-hitl" class="primary">开启 HITL 确认</button>
    <span id="hitl-state" class="cfg-status">当前：关闭</span>`;
  cfgPanel.appendChild(hitlWrap);
  $("btn-hitl").onclick = () => {
    hitlOn = !hitlOn;
    send({ type: "set_hitl", enabled: hitlOn });
  };

  // ---- HITL confirm bar (rendered when the graph pauses for human approval) ----
  let hitlBar = null;
  function showHitl(msg) {
    if (!hitlBar) {
      hitlBar = document.createElement("div");
      hitlBar.id = "hitl-bar";
      hitlBar.className = "hitl-bar hidden";
      hitlBar.innerHTML = `
        <div class="hitl-title">🛑 眼镜想主动播报</div>
        <div class="hitl-prompt"></div>
        <div class="hitl-preview"></div>
        <div class="hitl-actions">
          <button id="hitl-approve" class="primary">确认播报</button>
          <button id="hitl-deny">取消</button>
        </div>`;
      $("phone").appendChild(hitlBar);
      $("hitl-approve").onclick = () => { send({ type: "resume", value: "approve" }); hideHitl(); };
      $("hitl-deny").onclick = () => { send({ type: "resume", value: "deny" }); hideHitl(); };
    }
    hitlBar.querySelector(".hitl-prompt").textContent = msg.prompt || "是否现在播报？";
    hitlBar.querySelector(".hitl-preview").textContent = msg.preview || "";
    hitlBar.classList.remove("hidden");
  }
  function hideHitl() { if (hitlBar) hitlBar.classList.add("hidden"); }

  // ---- P4 knowledge base ----
  $("btn-upload-kb").onclick = () => {
    const f = $("kb-file").files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => send({ type: "upload_kb", filename: f.name, content: reader.result });
    reader.readAsText(f);
  };
  $("btn-kb-list").onclick = () => send({ type: "kb_list" });

  // ---- P5 memory ----
  $("btn-mem-clear").onclick = () => send({ type: "clear_memory" });
  $("btn-mem-export").onclick = () => send({ type: "export_memory" });

  // ---- mode selector (phone sets mode; lens reflects) ----
  document.querySelectorAll(".pmode").forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll(".pmode").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      send({ type: "set_mode", mode: btn.dataset.mode });
    };
  });
})();
