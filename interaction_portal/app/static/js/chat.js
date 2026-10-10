/* Team chat widget (shared by the task page and the practice page).
 * Chat.create({ log, input, sendBtn, pop, warn, typing, agents, orchestrator, agentNames, delay, onSend, you })
 */
(function () {
  "use strict";

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function hhmm(ts) {
    const d = new Date(ts);
    if (isNaN(d)) return "";
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  function Chat(o) {
    this.o = o;
    this.blocks = {};
    this.seen = new Set();
    this.busy = false;
    this.bindInput();
  }

  Chat.prototype.label = function (name) {
    const o = this.o;
    if (name === "Participant") return { name: o.you || "You", role: "" };
    if (name === o.orchestrator) return { name: name, role: o.orchestratorRole || "" };
    const a = (o.agents || {})[name];
    return { name: name, role: a ? a.role : "" };
  };

  Chat.prototype.scroll = function () { this.o.log.scrollTop = this.o.log.scrollHeight; };

  Chat.prototype.makeMsg = function (m, internal) {
    const mine = m.sender === "Participant";
    const wrap = el("div", "msg" + (mine ? " me" : "") + (m.kind === "system" ? " sys" : ""));
    const lab = this.label(m.sender);
    const who = el("div", "who");
    if (internal) {
      const to = (m.to || []).join(", ");
      who.innerHTML = "";
      const b = el("b", "", lab.name); who.appendChild(b);
      who.appendChild(document.createTextNode(" → " + to));
    } else {
      const b = el("b", "", lab.name); who.appendChild(b);
      if (lab.role) who.appendChild(document.createTextNode(" · " + lab.role));
    }
    const bub = el("div", "bubble");
    wrap.append(who, bub);
    return { wrap: wrap, bubble: bub };
  };

  Chat.prototype.reveal = function (bubble, text, animate) {
    const delay = (this.o.delay || 0.012) * 1000;
    if (!animate || delay <= 0) { bubble.textContent = text; return; }
    const interval = Math.max(delay, 10);
    const per = Math.max(1, Math.ceil(text.length * interval / 2500));   // whole reveal takes at most about 2.5 s
    let i = 0;
    const t = setInterval(() => {
      i = Math.min(text.length, i + per);
      bubble.textContent = text.slice(0, i);
      this.scroll();
      if (i >= text.length) clearInterval(t);
    }, interval);
  };

  Chat.prototype.block = function (id, ts) {
    if (this.blocks[id]) return this.blocks[id];
    const box = el("div", "fold");
    const head = el("button", "fold-head"); head.type = "button";
    const label = el("span", "");
    const tog = el("span", "toggle", "▸ show");
    head.append(label, tog);
    const bodyEl = el("div", "fold-body hidden");
    box.append(head, bodyEl);
    const b = { box: box, label: label, tog: tog, body: bodyEl, count: 0, ts: ts, parties: new Set(), id: id };
    head.addEventListener("click", () => {
      const opening = bodyEl.classList.contains("hidden");
      bodyEl.classList.toggle("hidden");
      tog.textContent = opening ? "▾ hide" : "▸ show";
      if (window.Portal) Portal.event(opening ? "fold_open" : "fold_close", { block_id: id });
    });
    this.blocks[id] = b;
    this.o.log.appendChild(box);
    return b;
  };

  Chat.prototype.refreshBlock = function (b) {
    const parties = Array.from(b.parties);
    b.label.textContent = "Orchestrator ↔ " + (parties.length ? parties.join(", ") : "specialists") + " · " +
      b.count + (b.count === 1 ? " message" : " messages") + " · " + hhmm(b.ts);
  };

  Chat.prototype.add = function (m, animate) {
    if (this.seen.has(m.id)) return;
    this.seen.add(m.id);
    if (m.kind === "internal" && m.block_id) {
      const b = this.block(m.block_id, m.ts);
      const { wrap, bubble } = this.makeMsg(m, true);
      b.body.appendChild(wrap);
      bubble.textContent = m.text;
      b.count += 1;
      const orch = this.o.orchestrator;
      [m.sender].concat(m.to || []).forEach((n) => { if (n && n !== orch && n !== "Participant") b.parties.add(n); });
      this.refreshBlock(b);
    } else {
      const { wrap, bubble } = this.makeMsg(m, false);
      this.o.log.appendChild(wrap);
      this.reveal(bubble, m.text, animate && m.sender !== "Participant");
    }
    this.scroll();
  };

  Chat.prototype.warn = function (t) { if (this.o.warn) this.o.warn.textContent = t || ""; };

  /* ---- @ mentions ---------------------------------------------------------------------------- */
  Chat.prototype.bindInput = function () {
    const o = this.o, ta = o.input, pop = o.pop;
    let active = 0, items = [];
    const hide = () => { pop.classList.add("hidden"); items = []; };
    const tokenAt = () => {
      const pos = ta.selectionStart;
      const before = ta.value.slice(0, pos);
      const m = before.match(/(^|\s)@([A-Za-z]*)$/);
      return m ? { q: m[2], start: pos - m[2].length - 1 } : null;
    };
    const render = () => {
      pop.innerHTML = "";
      items.forEach((n, i) => {
        const d = el("div", i === active ? "active" : "", "@" + n);
        d.addEventListener("mousedown", (e) => { e.preventDefault(); choose(n); });
        pop.appendChild(d);
      });
      pop.classList.toggle("hidden", !items.length);
    };
    const choose = (n) => {
      const tk = tokenAt(); if (!tk) return;
      const pos = ta.selectionStart;
      ta.value = ta.value.slice(0, tk.start) + "@" + n + " " + ta.value.slice(pos);
      const np = tk.start + n.length + 2;
      ta.setSelectionRange(np, np);
      hide(); ta.focus();
    };
    ta.addEventListener("input", () => {
      this.warn("");
      const tk = tokenAt();
      if (!tk) { hide(); return; }
      items = (o.agentNames || []).filter((n) => n.toLowerCase().startsWith(tk.q.toLowerCase()));
      active = 0; render();
    });
    ta.addEventListener("keydown", (e) => {
      if (!pop.classList.contains("hidden") && items.length) {
        if (e.key === "ArrowDown") { e.preventDefault(); active = (active + 1) % items.length; render(); return; }
        if (e.key === "ArrowUp") { e.preventDefault(); active = (active - 1 + items.length) % items.length; render(); return; }
        if (e.key === "Tab" || e.key === "Enter") { e.preventDefault(); choose(items[active]); return; }
        if (e.key === "Escape") { hide(); return; }
      }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.submit(); }
    });
    ta.addEventListener("blur", () => setTimeout(hide, 120));
    o.sendBtn.addEventListener("click", () => this.submit());
  };

  Chat.prototype.setBusy = function (b, text) {
    this.busy = b;
    this.o.sendBtn.disabled = b;
    if (this.o.typing) this.o.typing.textContent = b ? (text || "…") : "";
  };

  Chat.prototype.submit = async function () {
    if (this.busy) return;
    const text = this.o.input.value.trim();
    if (!text) return;
    this.warn("");
    const ok = await this.o.onSend(text, this);
    if (ok !== false) this.o.input.value = "";
  };

  /* Read an NDJSON stream, dispatching each event. */
  Chat.prototype.stream = async function (url, payload, handlers) {
    this.setBusy(true, this.o.typingText || "The team is typing…");
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload || {}) });
      if (!r.ok || !r.body) {
        const j = await r.json().catch(() => ({}));
        this.warn(j.error || "Something went wrong.");
        return false;
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
          if (!line) continue;
          let ev; try { ev = JSON.parse(line); } catch (e) { continue; }
          if (handlers[ev.type]) handlers[ev.type](ev);
        }
      }
      return true;
    } catch (e) {
      this.warn("Connection problem. Please try again.");
      return false;
    } finally {
      this.setBusy(false);
    }
  };

  window.Chat = { create: function (o) { return new Chat(o); } };
})();
