/* Card editor: behaves differently per role (the interface enforces the role the participant was told).
 *   passive     read-only: the team's text, nothing to change
 *   evaluative  Keep / Cut / Send back (a reason is required to send back)
 *   generative  free text editor with the specialists' suggestions offered as Insert chips
 *   none        free text editor (no team, or no-AI condition)
 *
 * CardEditor.mount(rootEl, { endpoint, view, fields:[{id,label,hint}], role, texts, hideLabels, onChange })
 *   endpoint: URL that accepts {action, span_id, reason, field, text} and returns {ok, card}
 */
(function () {
  "use strict";

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function CardEditor(root, opts) {
    this.root = root;
    this.o = opts;
    this.view = opts.view;
    this.texts = opts.texts || {};
    this.timers = {};
    this.pending = {};      // field -> last text typed locally and not yet confirmed
    this.refs = {};         // field -> {textarea, suggBox}
    this.build();
    this.update(this.view);
  }

  CardEditor.prototype.fill = function (tpl, vars) {
    return String(tpl || "").replace(/\{(\w+)\}/g, (m, k) => (vars && vars[k] !== undefined ? vars[k] : m));
  };

  CardEditor.prototype.build = function () {
    const o = this.o;
    this.root.innerHTML = "";
    this.fieldEls = {};
    o.fields.forEach((f) => {
      const wrap = el("div", "cfield");
      wrap.dataset.field = f.id;
      if (!o.hideLabels) wrap.appendChild(el("div", "flabel", f.label));
      const body = el("div", "fbody");
      wrap.appendChild(body);
      this.fieldEls[f.id] = body;
      this.root.appendChild(wrap);
    });
    this.saveLine = el("div", "save-line");
    this.root.appendChild(this.saveLine);
    if (o.role === "generative" || o.role === "none") {
      o.fields.forEach((f) => this.buildTextarea(f));
    }
  };

  CardEditor.prototype.buildTextarea = function (f) {
    const body = this.fieldEls[f.id];
    const ta = el("textarea", this.o.fields.length === 1 && this.o.big ? "write-area big" : "");
    ta.placeholder = f.hint && this.o.role !== "none" ? f.hint : (this.texts.write_here || "");
    ta.rows = this.o.fields.length === 1 ? 5 : 3;
    ta.addEventListener("input", () => {
      this.pending[f.id] = ta.value;
      this.setSaveLine("…");
      clearTimeout(this.timers[f.id]);
      this.timers[f.id] = setTimeout(() => this.sendText(f.id), 700);
    });
    ta.addEventListener("blur", () => { clearTimeout(this.timers[f.id]); if (f.id in this.pending) this.sendText(f.id); });
    body.appendChild(ta);
    const sugg = el("div", "suggs");
    body.appendChild(sugg);
    this.refs[f.id] = { ta: ta, sugg: sugg };
  };

  CardEditor.prototype.setSaveLine = function (t) { if (this.saveLine) this.saveLine.textContent = t || ""; };

  CardEditor.prototype.call = async function (payload) {
    const r = await Portal.post(this.o.endpoint, payload);
    if (r && r.card) { this.update(r.card); }
    else if (r && r.ok === false && this.o.onError) { this.o.onError(r.error || "Could not save."); }
    return r;
  };

  CardEditor.prototype.sendText = async function (fieldId) {
    const text = this.pending[fieldId];
    if (text === undefined) return;
    delete this.pending[fieldId];
    this.inflight = (this.inflight || 0) + 1;
    const r = await this.call({ action: "type", field: fieldId, text: text });
    this.inflight--;
    this.setSaveLine(r && r.ok === false ? (r.error || "") : "");
  };

  CardEditor.prototype.update = function (view) {
    this.view = view;
    const role = this.o.role;
    this.o.fields.forEach((f) => {
      if (role === "passive" || role === "evaluative") this.renderSpans(f.id);
      else this.renderText(f.id);
    });
    if (this.o.onChange) this.o.onChange(view);
  };

  CardEditor.prototype.authorLine = function (span) {
    const t = this.texts;
    const st = (t.status && t.status[span.status]) || "";
    const from = this.fill(t.from, { name: String(span.author).toUpperCase() });
    if (this.o.compact) return [st, from].filter(Boolean).join(" · ");
    return this.fill(t.written_by, { name: String(span.author).toUpperCase() });
  };

  CardEditor.prototype.renderSpans = function (fid) {
    const body = this.fieldEls[fid];
    body.innerHTML = "";
    const spans = (this.view.fields[fid] || []).filter((s) => s.status !== "deleted");
    if (!spans.length) { body.appendChild(el("div", "empty-card", this.texts.empty || "")); return; }
    spans.forEach((s) => {
      const box = el("div", "cspan " + s.status);
      const head = el("div", "author", this.authorLine(s));
      if (!this.o.compact && this.o.role === "evaluative" && this.texts.status[s.status] && s.status !== "proposed") {
        const chip = el("span", "status", this.texts.status[s.status]);
        head.appendChild(chip);
      }
      box.appendChild(head);
      box.appendChild(el("div", "stext", s.text));
      if (this.o.role === "evaluative" && !this.o.readonly) {
        const acts = el("div", "acts");
        [["keep", "kept"], ["cut", "cut"], ["send_back", "sent_back"]].forEach(([act, st]) => {
          const b = el("button", "pillbtn" + (s.status === st ? " on" : ""), this.texts[act]);
          b.type = "button";
          b.addEventListener("click", () => this.evaluate(box, s, act));
          acts.appendChild(b);
        });
        box.appendChild(acts);
      }
      body.appendChild(box);
    });
  };

  CardEditor.prototype.evaluate = function (box, span, act) {
    if (act !== "send_back" || !this.view.send_back_needs_reason) {
      return this.call({ action: act, span_id: span.span_id, reason: "" });
    }
    if (box.querySelector(".reason-row")) return;
    const row = el("div", "reason-row");
    const inp = el("input");
    inp.placeholder = this.texts.reason;
    inp.maxLength = 300;
    const ok = el("button", "pillbtn on", this.texts.ok); ok.type = "button";
    const no = el("button", "pillbtn", this.texts.cancel); no.type = "button";
    const go = () => {
      if (!inp.value.trim()) { inp.focus(); return; }
      this.call({ action: "send_back", span_id: span.span_id, reason: inp.value.trim() });
    };
    ok.addEventListener("click", go);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    no.addEventListener("click", () => row.remove());
    row.append(inp, ok, no);
    box.appendChild(row);
    inp.focus();
  };

  CardEditor.prototype.renderText = function (fid) {
    const ref = this.refs[fid];
    if (!ref) return;
    const serverText = (this.view.text && this.view.text[fid]) || "";
    const typingNow = document.activeElement === ref.ta || (fid in this.pending);
    if (!typingNow && ref.ta.value !== serverText) ref.ta.value = serverText;
    ref.sugg.innerHTML = "";
    ((this.view.suggestions && this.view.suggestions[fid]) || []).forEach((s) => {
      const box = el("div", "sugg");
      box.appendChild(el("div", "author", this.fill(this.texts.suggests, { name: s.author })));
      box.appendChild(el("div", "", s.text));
      const b = el("button", "pillbtn", this.texts.insert); b.type = "button";
      b.style.marginTop = "4px";
      b.addEventListener("click", () => this.call({ action: "insert", span_id: s.span_id }));
      box.appendChild(b);
      ref.sugg.appendChild(box);
    });
  };

  CardEditor.prototype.flush = async function () {
    for (const fid of Object.keys(this.pending)) { clearTimeout(this.timers[fid]); await this.sendText(fid); }
  };

  window.CardEditor = {
    mount: function (root, opts) { return new CardEditor(root, opts); },
    /* Shared helper: tick the checklist in the left column and show the typed-share line. */
    syncChecklist: function (view, listId) {
      const list = document.getElementById(listId || "field-list");
      if (!list) return;
      list.querySelectorAll("li").forEach((li) => {
        const t = (view.text && view.text[li.dataset.field]) || "";
        li.classList.toggle("done", t.trim().length > 0);
      });
    },
  };
})();
