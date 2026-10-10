/* Practice page: four scripted exercises (two for the no-AI condition). No model is ever called. */
(function () {
  "use strict";
  const P = window.PRACTICE;
  const $ = (id) => document.getElementById(id);
  let S = P.state;
  let chat = null, editor = null, editorReady = false, chatCount = 0;

  function stepOf(state) { return state.step; }
  function lastIndex() { return S.n_steps - 1; }

  function flash(msg, ok) {
    const f = $("pr-flash");
    if (!msg) { f.classList.add("hidden"); return; }
    f.textContent = msg; f.classList.toggle("ok", !!ok); f.classList.remove("hidden");
  }

  async function refresh() {
    const j = await Portal.get("/api/practice/state");
    if (j && j.step) { S = j; render(); }
  }

  /* ---- left column + header ------------------------------------------------------------------------ */
  function renderLeft() {
    const step = stepOf(S);
    const total = P.ai ? 4 : 2;
    const title = P.titles[step.exercise] || step.title;
    $("pr-header").textContent = P.ui.exercise.replace("{n}", step.exercise).replace("{total}", total) + " — " + title;
    $("pr-badge").textContent = step.kind === "submit_and_check" ? P.ui.badgeCheck : P.ui.badge;
    $("pr-title").textContent = step.title;
    $("pr-instruction").textContent = step.kind === "role_action" ? (P.roleInstruction[P.role] || "") : (step.instruction || "");
    const ul = $("pr-steps"); ul.innerHTML = "";
    P.steps.forEach((s, i) => {
      const li = document.createElement("li");
      li.textContent = s.title;
      if (i < S.step_index || (i === S.step_index && S.step_done)) li.className = "done";
      else if (i === S.step_index) li.className = "current";
      ul.appendChild(li);
    });
    const btn = $("pr-next");
    btn.classList.add("hidden"); btn.onclick = null;
    const k = step.kind;
    const needsDone = !S.step_done && (k === "panel_read" || k === "pack_open" || (k === "role_action" && P.role === "passive"));
    if (needsDone) {
      btn.textContent = P.ui.done; btn.classList.remove("hidden");
      btn.onclick = async () => {
        btn.disabled = true;
        const url = k === "panel_read" ? "/api/practice/panel-read" : "/api/practice/done";
        const r = await Portal.post(url, {});
        if (r.error) { btn.disabled = false; flash(r.error); return; }
        flash("");
        if (S.step_index < lastIndex()) {
          const rNext = await Portal.post("/api/practice/next", {});
          btn.disabled = false;
          if (rNext.step) { S = rNext; render(); } else if (rNext.error) flash(rNext.error);
        } else {
          btn.disabled = false;
          await refresh();
        }
      };
    } else if (S.step_done && S.step_index < lastIndex()) {
      btn.textContent = P.ui.next; btn.classList.remove("hidden");
      btn.onclick = async () => {
        btn.disabled = true;
        const r = await Portal.post("/api/practice/next", {});
        btn.disabled = false;
        if (r.step) { S = r; flash(""); render(); } else if (r.error) flash(r.error);
      };
    } else if (S.step_done && S.step_index === lastIndex() && k !== "submit_and_check") {
      btn.textContent = P.ui.finish; btn.classList.remove("hidden");
      btn.onclick = finish;
    }
  }

  async function finish() {
    const r = await Portal.post("/api/page/continue", {});
    if (r.ok) location.reload(); else flash(r.error || "Please try again.");
  }

  /* ---- panes -------------------------------------------------------------------------------------------------- */
  function show(ids) {
    ["pane-chat", "pane-card", "pane-handin", "pane-pack"].forEach((id) => $(id).classList.toggle("hidden", !ids.includes(id)));
  }

  function renderPanes() {
    const k = stepOf(S).kind;
    let pane = "pane-chat";
    if (k === "role_action" || k === "card_write") pane = "pane-card";
    else if (k === "submit_and_check") pane = "pane-handin";
    else if (k === "pack_open") pane = "pane-pack";
    show([pane]);
    const showPanel = P.ai && S.panel && (k === "panel_read" || k === "chat_orchestrator");
    $("pr-right").classList.toggle("hidden", !showPanel);
    $("pr-cols").classList.toggle("two", !showPanel);
    if (showPanel) renderPanelText();
    if (pane === "pane-chat") renderChat();
    if (pane === "pane-handin") renderHandin();
    if (pane === "pane-card") ensureEditor();
  }

  function renderPanelText() {
    const t = S.panel || "", box = $("panel-text");
    box.innerHTML = "";
    const re = /(Team status|On the card|Why it recurs):/g;
    let last = 0, m;
    while ((m = re.exec(t)) !== null) {
      box.appendChild(document.createTextNode(t.slice(last, m.index)));
      const b = document.createElement("b"); b.textContent = m[1] + "."; box.appendChild(b);
      last = m.index + m[0].length;
    }
    box.appendChild(document.createTextNode(t.slice(last)));
  }

  /* chat */
  function ensureChat() {
    if (chat) return;
    chat = Chat.create({
      log: $("pr-chat-log"), input: $("chat-text"), sendBtn: $("chat-send"), pop: $("mention-pop"),
      warn: $("chat-warn"), agents: {}, orchestrator: "Orchestrator", agentNames: P.agentNames, delay: 0.012, you: P.ui.you,
      onSend: async (text, c) => {
        c.setBusy(true, "…");
        const r = await Portal.post("/api/practice/chat", { text: text });
        c.setBusy(false);
        if (r.warning) c.warn(r.warning);
        if (r.error) { c.warn(r.error); return false; }
        (r.messages || []).forEach((m) => addMsg(m, true));
        await refresh();
        return !!r.ok;
      },
    });
  }
  function addMsg(m, animate) {
    chatCount += 1;
    chat.add({ id: "pm" + chatCount, sender: m.sender, to: [], text: m.text, kind: "chat", block_id: null, ts: "" }, animate);
  }
  function renderChat() {
    ensureChat();
    if (chatCount === 0) (S.chat || []).forEach((m) => addMsg(m, false));
    const done = S.step_done;
    $("chat-text").disabled = done; $("chat-send").disabled = done;
  }

  /* card editor */
  function ensureEditor() {
    if (editorReady) return;
    editorReady = true;
    let first = true;
    editor = CardEditor.mount($("card-editor"), {
      endpoint: "/api/practice/card-action", view: S.card, hideLabels: true, role: P.role, texts: P.texts,
      fields: [{ id: "practice", label: "Practice card", hint: "" }], big: true,
      onChange: () => { if (first) { first = false; return; } setTimeout(refresh, 250); },
      onError: (m) => flash(m),
    });
  }

  /* hand-in */
  function renderHandin() {
    const box = $("handin-summary");
    const text = (S.card && S.card.text && S.card.text.practice) || "";
    box.innerHTML = "";
    const lines = text.trim() ? text.split(/\n+/) : [];
    if (!lines.length) { const p = document.createElement("p"); p.className = "muted"; p.textContent = P.texts.empty; box.appendChild(p); }
    lines.forEach((l) => { const p = document.createElement("p"); p.textContent = l; box.appendChild(p); });
    // Passive/evaluative cards render each span on its own line
    if (S.card && S.card.fields && S.card.fields.practice && (P.role === "passive" || P.role === "evaluative")) {
      box.innerHTML = "";
      S.card.fields.practice.filter((s) => s.status !== "deleted" && s.status !== "cut" && s.status !== "sent_back").forEach((s) => {
        const p = document.createElement("p"); p.textContent = s.text; box.appendChild(p);
      });
      if (!box.children.length) { const p = document.createElement("p"); p.className = "muted"; p.textContent = P.texts.empty; box.appendChild(p); }
    }
    $("pr-submit-card").classList.toggle("hidden", !!S.submitted);
    $("role-check").classList.toggle("hidden", !S.submitted);
  }

  $("pr-submit-card").addEventListener("click", async () => {
    const r = await Portal.post("/api/practice/submit-card", {});
    if (r.step) { S = r; render(); } else flash(r.error || "Please try again.");
  });
  $("rc-submit").addEventListener("click", async () => {
    const sel = document.querySelector("input[name=rc]:checked");
    if (!sel) { $("rc-err").textContent = P.ui.pickOne; return; }
    $("rc-err").textContent = "";
    const r = await Portal.post("/api/practice/role-check", { answer: sel.value });
    if (r.ok) { await finish(); }
    else if (r.sent_back) { location.reload(); }
    else $("rc-err").textContent = r.error || "Please try again.";
  });

  function render() { renderLeft(); renderPanes(); }
  document.addEventListener("DOMContentLoaded", render);
})();
