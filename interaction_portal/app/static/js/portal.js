/* Interaction Portal: shared behaviour (no dependencies).
 * Portal.post/get   JSON helpers
 * telemetry         window blur/focus, scroll samples, viewport check
 * countdowns        any element with data-countdown="<seconds>"
 * continue buttons  any button with data-continue
 * accordions        reference-pack sections (pack_open / pack_close events)
 */
(function () {
  "use strict";
  const body = document.body;

  const Portal = {
    async post(url, data) {
      try {
        const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
        const j = await r.json().catch(() => ({}));
        if (!r.ok && j.ok === undefined) j.ok = false;
        if (!r.ok && !j.error) j.error = "Something went wrong (" + r.status + ").";
        return j;
      } catch (e) {
        return { ok: false, error: "Connection problem. Please try again." };
      }
    },
    async get(url) {
      try {
        const r = await fetch(url, { headers: { "Accept": "application/json" }, cache: "no-store" });
        return await r.json();
      } catch (e) { return null; }
    },
    event(type, metadata) { return Portal.post("/api/event", { type: type, metadata: metadata || {} }); },
    fmt(seconds) {
      const s = Math.max(0, Math.ceil(seconds));
      return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
    },
  };
  window.Portal = Portal;

  /* ---- viewport / device check ---------------------------------------------------- */
  function viewportCheck() {
    if (!body.dataset.pid) return;
    Portal.post("/api/viewport", { width: window.innerWidth, height: window.innerHeight }).then((r) => {
      if (r && r.ok === false && r.message && body.dataset.allowMobile !== "true") {
        document.querySelectorAll(".shell").forEach((el) => el.classList.add("hidden"));
        const blk = document.getElementById("viewport-block");
        document.getElementById("viewport-msg").textContent = r.message;
        blk.classList.remove("hidden");
      }
    });
  }

  /* ---- attention telemetry ------------------------------------------------------------ */
  function telemetry() {
    if (!body.dataset.pid) return;
    window.addEventListener("blur", () => Portal.event("window_blur"));
    window.addEventListener("focus", () => Portal.event("window_focus"));
    if (body.dataset.telemetry !== "true") return;
    const targets = Array.from(document.querySelectorAll("[data-scroll-sample]"));
    if (!targets.length) targets.push(document.scrollingElement || document.documentElement);
    const last = new Map();
    setInterval(() => {
      if (document.hidden) return;
      targets.forEach((el) => {
        const top = Math.round(el.scrollTop);
        if (last.get(el) === top) return;
        last.set(el, top);
        Portal.event("scroll_sample", { scroll_top: top, scroll_height: el.scrollHeight, viewport_height: el.clientHeight });
      });
    }, 3000);
  }

  /* ---- countdowns ------------------------------------------------------------------------- */
  function countdowns() {
    document.querySelectorAll("[data-countdown]").forEach((el) => {
      let deadline = Date.now() + parseFloat(el.dataset.countdown) * 1000;
      const fmt = el.dataset.fmt || "{time}";
      const low = parseFloat(el.dataset.low || "0");
      let fired = false;
      function tick() {
        const left = (deadline - Date.now()) / 1000;
        el.textContent = fmt.replace("{time}", Portal.fmt(left));
        if (low && left <= low) el.classList.add("low");
        if (left <= 0 && !fired) {
          fired = true;
          const z = el.dataset.zero || "";
          if (z === "reload") { setTimeout(() => location.reload(), 400); }
          else if (z.startsWith("enable:")) {
            const b = document.querySelector(z.slice(7));
            if (b) b.disabled = false;
            el.remove();
          }
        }
      }
      tick();
      setInterval(tick, 250);
      if (el.dataset.sync) {   // re-sync with the authoritative server clock
        setInterval(async () => {
          const j = await Portal.get("/api/timer");
          if (!j) return;
          if (j.page && j.page !== body.dataset.page) { location.reload(); return; }
          if (typeof j[el.dataset.sync] === "number") deadline = Date.now() + j[el.dataset.sync] * 1000;
        }, 10000);
      }
    });
  }

  /* ---- plain Continue buttons ----------------------------------------------------------------- */
  function continueButtons() {
    document.querySelectorAll("[data-continue]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (btn.disabled) return;
        btn.disabled = true;
        const r = await Portal.post("/api/page/continue", {});
        if (r.ok) location.reload();
        else { btn.disabled = false; alert(r.error || "Please try again."); }
      });
    });
  }

  /* ---- accordions (reference pack) ---------------------------------------------------------------- */
  function accordions() {
    document.querySelectorAll(".acc").forEach((acc) => {
      const head = acc.querySelector(".acc-head");
      const bodyEl = acc.querySelector(".acc-body");
      const tog = acc.querySelector(".toggle");
      head.addEventListener("click", () => {
        const opening = bodyEl.classList.contains("hidden");
        bodyEl.classList.toggle("hidden");
        tog.textContent = opening ? "close" : "open";
        Portal.event(opening ? "pack_open" : "pack_close", { section_name: acc.dataset.section });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    viewportCheck(); telemetry(); countdowns(); continueButtons(); accordions();
  });
})();
