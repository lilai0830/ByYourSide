(() => {
  const wsProto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${wsProto}://${location.host}/ws`);

  const conn = document.getElementById("conn");
  const card = document.getElementById("card");
  const arrow = document.getElementById("arrow");
  const bubbleCol = document.getElementById("ai-bubbles");
  const trace = document.getElementById("trace");
  const textInput = document.getElementById("text-input");
  const badge = document.getElementById("badge");
  const vib = document.getElementById("vib");
  const budget = document.getElementById("budget");
  const memoryList = document.getElementById("memory-list");

  ws.onopen = () => {
    conn.textContent = "已连接"; conn.className = "conn on";
    // declare this client so the backend broadcasts the lens view (docs §2)
    ws.send(JSON.stringify({ type: "identify", client: "lens" }));
  };
  ws.onclose = () => { conn.textContent = "未连接"; conn.className = "conn off"; };

  function escapeHtml(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"]/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function speak(text) {
    if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.lang = "zh-CN";
      window.speechSynthesis.speak(u);
    }
  }

  function gapLabel(tag) {
    if (tag === "O1") return '<span class="tag-o1">[O1 记忆]</span>';
    if (tag === "O2") return '<span class="tag-o2">[O2 主动/抑制]</span>';
    if (tag === "O3") return '<span class="tag-o3">[O3 带宽编排·假设]</span>';
    return `[${escapeHtml(tag)}]`;
  }

  function renderMemory(items) {
    if (!items || items.length === 0) {
      memoryList.innerHTML = '<li class="empty">（暂无记忆）</li>';
      return;
    }
    memoryList.innerHTML = items.map(i =>
      `<li><span class="tag">${escapeHtml(i.tag)}</span>${escapeHtml(i.value)}</li>`
    ).join("");
  }

  // Render the NEW lens view (docs §4/§7): bubbles / badge / map / side_output / vibration
  function renderHud(d) {
    const lens = d; // server sends the lens-shaped payload at top level

    const bubbles = lens.bubbles || [];
    if (bubbles.length) {
      bubbleCol.innerHTML = bubbles
        .map(b => `<div class="bubble">${escapeHtml(b.text)}</div>`)
        .join("");
    }

    // map -> arrow glyph
    const map = lens.map || null;
    const arrowName = map && map.arrow ? map.arrow : "none";
    arrow.className = "arrow " + arrowName;
    if (arrowName !== "none") arrow.classList.add("show");

    // side_output (image or text) -> card area
    const so = lens.side_output;
    if (so) {
      if (typeof so === "string" && (so.startsWith("data:image") || so.startsWith("http"))) {
        card.innerHTML = `<img src="${escapeHtml(so)}" style="max-width:100%;border-radius:8px;" />`;
      } else {
        card.innerHTML = `<div class="src">${escapeHtml(so)}</div>`;
      }
      card.classList.remove("hidden");
    } else {
      card.classList.add("hidden");
    }

    if (lens.badge) {
      badge.textContent = lens.badge;
      badge.classList.remove("hidden"); badge.classList.add("show");
    } else {
      badge.classList.remove("show"); badge.classList.add("hidden");
    }

    if (lens.vibration) {
      vib.classList.remove("hidden"); vib.classList.add("show");
      setTimeout(() => { vib.classList.remove("show"); vib.classList.add("hidden"); }, 1100);
    }

    // lens speaks the bubble text only when NOT suppressed (O2)
    if (!d.suppressed && bubbles[0] && bubbles[0].text) {
      speak(bubbles[0].text);
    }

    if (typeof d.budget === "number") budget.textContent = `打扰预算 ${d.budget}`;
    if (Array.isArray(d.memory)) renderMemory(d.memory);

    // trace panel: render reasoning_trace with Gap tags
    const rt = d.reasoning_trace || [];
    if (rt.length) {
      const head = (d.gap_tags || []).map(gapLabel).join(" ");
      const lines = rt.map(t =>
        `${gapLabel(t.tag)}${t.hypothesis ? "[假设]" : ""} ${escapeHtml(t.detail)}`
      ).join("\n");
      trace.innerHTML += `\n• ${head}\n${lines}`;
      trace.scrollTop = trace.scrollHeight;
    }

    if (d.memory_export) {
      const blob = new Blob([d.memory_export], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "goai_memory_export.json"; a.click();
      URL.revokeObjectURL(url);
    }

    // sync mode bar with shared session state
    if (d.mode) {
      document.querySelectorAll("#mode-bar .mode").forEach(b =>
        b.classList.toggle("active", b.dataset.mode === d.mode));
    }
  }

  function send(obj) {
    if (ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify(obj));
  }

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "hud_update") renderHud(msg);
  };

  document.getElementById("btn-send").onclick = () => {
    const v = textInput.value.trim();
    if (!v) return;
    send({ type: "user_input", text: v, modality: "text" });
    textInput.value = "";
  };
  textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("btn-send").click();
  });

  document.querySelectorAll("#mode-bar .mode").forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll("#mode-bar .mode").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      send({ type: "set_mode", mode: btn.dataset.mode });
    };
  });

  document.getElementById("btn-clear").onclick = () => send({ type: "clear_memory" });
  document.getElementById("btn-export").onclick = () => send({ type: "export_memory" });

  document.getElementById("btn-cam").onclick = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
      const cam = document.getElementById("cam");
      cam.srcObject = stream;
      cam.style.display = "block";
      document.getElementById("view-placeholder").style.display = "none";
    } catch (err) {
      alert("无法访问摄像头（需用 http://localhost 或 https 打开）：" + err.message);
    }
  };

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SR) {
    const rec = new SR();
    rec.lang = "zh-CN"; rec.interimResults = false;
    const micBtn = document.createElement("button");
    micBtn.textContent = "🎤 语音";
    micBtn.onclick = () => rec.start();
    rec.onresult = (e) => send({ type: "user_input", text: e.results[0][0].transcript, modality: "voice" });
    document.getElementById("controls").insertBefore(micBtn, document.getElementById("btn-send"));
  }
})();
