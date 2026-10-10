/* Task page: card editor, team chat, orchestrator panel, reference pack. */
(function () {
  "use strict";
  const T = window.TASK;
  const $ = (id) => document.getElementById(id);

  /* ---- card ---------------------------------------------------------------------------------- */
  const shareLine = $("share-line");
  function onCard(view) {
    CardEditor.syncChecklist(view);
    if (shareLine && (T.role === "generative")) {
      const pct = Math.round((view.typed_share || 0) * 100), min = Math.round(T.minShare * 100);
      shareLine.textContent = T.texts.typed_share.replace("{share}", pct).replace("{min}", min);
      shareLine.classList.toggle("ok", pct >= min);
    }
  }
  const editor = CardEditor.mount($("card-editor"), {
    endpoint: "/api/card/action", view: T.card, fields: T.fieldMeta, role: T.role, texts: T.texts,
    hideLabels: false, onChange: onCard, big: false,
    onError: (m) => { if (shareLine) shareLine.textContent = m; },
  });

  /* ---- hand-in / go to submission --------------------------------------------------------------- */
  const toSub = $("to-submission");
  if (toSub) toSub.addEventListener("click", async () => {
    await editor.flush();
    toSub.disabled = true;
    const r = await Portal.post("/api/task/finish", {});
    if (r.ok) location.reload(); else { toSub.disabled = false; alert(r.error || "Please try again."); }
  });
  const handIn = $("hand-in");
  if (handIn) handIn.addEventListener("click", async () => {
    await editor.flush();
    handIn.disabled = true;
    const r = await Portal.post("/api/card/submit", {});
    if (r.ok) { location.reload(); return; }
    $("submit-err").textContent = r.error || "Please try again.";
    handIn.disabled = false;
  });

  /* ---- orchestrator panel ----------------------------------------------------------------------------- */
  const panelEl = $("panel-text");
  function renderPanel(p) {
    if (!panelEl || !p || !p.text) return;
    panelEl.innerHTML = "";
    const labels = (T.sections || []).slice().sort((a, b) => b.length - a.length);
    let text = p.text;
    // Bold the section labels ("Team status:" -> "Team status.")
    const re = new RegExp("(" + labels.map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")[:.]?", "g");
    let last = 0, m;
    const frag = document.createDocumentFragment();
    if (labels.length) {
      while ((m = re.exec(text)) !== null) {
        frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        const b = document.createElement("b"); b.textContent = m[1] + "."; frag.appendChild(b);
        last = m.index + m[0].length;
      }
    }
    frag.appendChild(document.createTextNode(text.slice(last)));
    panelEl.appendChild(frag);
  }
  if (T.panelOn) setInterval(async () => {
    const j = await Portal.get("/api/panel");
    if (j && j.panel) renderPanel(j.panel);
  }, 20000);

  /* ---- team chat ---------------------------------------------------------------------------------------- */
  if (T.mode !== "team") return;
  const chat = Chat.create({
    log: $("chat-log"), input: $("chat-text"), sendBtn: $("chat-send"), pop: $("mention-pop"),
    warn: $("chat-warn"), typing: $("chat-typing"), agents: T.agents, orchestrator: T.orchestrator,
    orchestratorRole: T.orchestratorRole, agentNames: T.agentNames, delay: T.typingDelay, you: "You",
    onSend: async (text, c) => {
      return c.stream("/api/chat/send", { text: text }, handlers);
    },
  });
  const handlers = {
    message: (ev) => chat.add(ev.message, ev.message.sender !== "Participant"),
    card: (ev) => editor.update(ev.card),
    panel: (ev) => renderPanel(ev.panel),
    warning: (ev) => chat.warn(ev.text),
    error: (ev) => chat.warn(ev.text),
  };

  (async function init() {
    const st = await Portal.get("/api/chat/state");
    if (st) {
      (st.messages || []).forEach((m) => chat.add(m, false));
      if (st.panel) renderPanel(st.panel);
    }
    if (!st || !st.opened) {
      await chat.stream("/api/chat/open", {}, handlers);
    }
  })();
})();
