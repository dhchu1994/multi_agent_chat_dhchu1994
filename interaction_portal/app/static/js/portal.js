/* Interaction Portal: shared behaviour (no dependencies).
 * Portal.post/get   JSON helpers
 * telemetry         window blur/focus, scroll samples, viewport check
 * countdowns        any element with data-countdown="<seconds>"
 * continue buttons  any button with data-continue
 * accordions        reference-pack sections (pack_open / pack_close events)
 * theme             system default, light/dark switcher, PID persistence
 */
(function () {
  "use strict";
  const body = document.body;

  let eventQueue = [];
  let flushTimer = null;
  const STORAGE_KEY = "portal_events_queue";

  try {
    const saved = sessionStorage.getItem(STORAGE_KEY);
    if (saved) {
      const parsed = JSON.parse(saved);
      if (Array.isArray(parsed)) eventQueue = parsed;
    }
  } catch (e) {}

  function saveQueue() {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(eventQueue));
    } catch (e) {}
  }

  function scheduleFlush() {
    if (flushTimer) return;
    flushTimer = setTimeout(flushEvents, 1500);
  }

  async function flushEvents() {
    if (flushTimer) {
      clearTimeout(flushTimer);
      flushTimer = null;
    }
    if (!eventQueue.length) return;
    const toSend = eventQueue.slice();
    try {
      const r = await fetch("/api/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(toSend),
      });
      if (r.ok) {
        eventQueue = eventQueue.slice(toSend.length);
        saveQueue();
      } else {
        scheduleFlush();
      }
    } catch (e) {
      scheduleFlush();
    }
  }

  function flushBeacon() {
    if (!eventQueue.length) return;
    const payload = JSON.stringify(eventQueue);
    if (navigator.sendBeacon) {
      const blob = new Blob([payload], { type: "application/json" });
      const ok = navigator.sendBeacon("/api/events", blob);
      if (ok) {
        eventQueue = [];
        try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) {}
      }
    } else {
      fetch("/api/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
        keepalive: true
      }).catch(() => {});
      eventQueue = [];
      try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) {}
    }
  }

  window.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushBeacon();
  });
  window.addEventListener("pagehide", () => {
    flushBeacon();
  });

  const Portal = {
    async post(url, data) {
      try {
        const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
        const j = await r.json().catch(() => ({}));
        if (!r.ok && j.ok === undefined) j.ok = false;
        if (!r.ok && !j.error) j.error = "Something went wrong (" + r.status + ").";
        if (r.status === 403 && body && body.dataset && body.dataset.allowMobile !== "true") {
          const blk = document.getElementById("viewport-block");
          if (blk) {
            document.querySelectorAll(".shell").forEach((el) => el.classList.add("hidden"));
            const msg = document.getElementById("viewport-msg");
            if (msg) msg.textContent = j.detail || j.message || j.error || "Access restricted on mobile / tablet devices.";
            blk.classList.remove("hidden");
          }
        }
        return j;
      } catch (e) {
        return { ok: false, error: "Connection problem. Please try again." };
      }
    },
    async get(url) {
      try {
        const r = await fetch(url, { headers: { "Accept": "application/json" }, cache: "no-store" });
        if (r.status === 403 && body && body.dataset && body.dataset.allowMobile !== "true") {
          const blk = document.getElementById("viewport-block");
          if (blk) {
            document.querySelectorAll(".shell").forEach((el) => el.classList.add("hidden"));
            const msg = document.getElementById("viewport-msg");
            if (msg) msg.textContent = "Access restricted on mobile / tablet devices.";
            blk.classList.remove("hidden");
          }
        }
        return await r.json();
      } catch (e) { return null; }
    },
    event(type, metadata) {
      const m = Object.assign({}, metadata || {});
      const nowClient = Date.now();
      const mono = window.performance ? Math.round(performance.now()) : 0;
      if (m.t_client === undefined) m.t_client = nowClient;
      if (m.t_mono === undefined) m.t_mono = mono;
      const item = {
        type: type,
        metadata: m,
        timestamp: new Date(nowClient).toISOString(),
        t_client: nowClient,
        t_mono: mono
      };
      eventQueue.push(item);
      saveQueue();
      if (eventQueue.length >= 10) {
        flushEvents();
      } else {
        scheduleFlush();
      }
      return Promise.resolve({ ok: true });
    },
    flush: flushEvents,
    fmt(seconds) {
      const s = Math.max(0, Math.ceil(seconds));
      return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
    },
  };
  window.Portal = Portal;

  /* ---- theme manager ------------------------------------------------------------------------- */
  function initTheme() {
    function getPID() {
      if (document.body && document.body.dataset && document.body.dataset.pid) {
        const p = document.body.dataset.pid.trim();
        if (p) return p;
      }
      const urlParams = new URLSearchParams(window.location.search);
      const qPid = urlParams.get("pid");
      if (qPid && qPid.trim()) return qPid.trim();
      const m = window.location.pathname.match(/\/session\/([^/]+)/);
      if (m && m[1].trim()) return m[1].trim();
      const pidInput = document.getElementById("pid");
      if (pidInput && pidInput.value && pidInput.value.trim()) return pidInput.value.trim();
      return "";
    }

    function getStoredPreference(pid) {
      if (pid) {
        try {
          const pVal = localStorage.getItem("portal_theme_" + pid);
          if (pVal) return pVal;
        } catch (e) {}
      }
      try {
        const gVal = localStorage.getItem("portal_theme_global");
        if (gVal) return gVal;
      } catch (e) {}
      return "system";
    }

    function savePreference(pid, pref) {
      if (pid) {
        try { localStorage.setItem("portal_theme_" + pid, pref); } catch (e) {}
        try {
          document.cookie = "portal_theme_" + encodeURIComponent(pid) + "=" + encodeURIComponent(pref) + "; path=/; max-age=31536000; SameSite=Lax";
        } catch (e) {}
      }
      try { localStorage.setItem("portal_theme_global", pref); } catch (e) {}
      try {
        document.cookie = "portal_theme_global=" + encodeURIComponent(pref) + "; path=/; max-age=31536000; SameSite=Lax";
      } catch (e) {}
    }

    function resolveTheme(pref) {
      if (pref === "dark") return "dark";
      if (pref === "light") return "light";
      const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      return prefersDark ? "dark" : "light";
    }

    function updateUI(pref, effectiveTheme) {
      const root = document.documentElement;
      root.setAttribute("data-theme", effectiveTheme);
      root.setAttribute("data-theme-preference", pref);
      if (effectiveTheme === "dark") {
        root.classList.add("theme-dark");
        root.classList.remove("theme-light");
        if (document.body) {
          document.body.classList.add("theme-dark");
          document.body.classList.remove("theme-light");
        }
      } else {
        root.classList.add("theme-light");
        root.classList.remove("theme-dark");
        if (document.body) {
          document.body.classList.add("theme-light");
          document.body.classList.remove("theme-dark");
        }
      }

      // Update button icon & text
      const btnIcon = document.querySelector("#theme-btn .theme-btn-icon");
      if (btnIcon) {
        if (pref === "dark") btnIcon.textContent = "🌙";
        else if (pref === "light") btnIcon.textContent = "☀️";
        else btnIcon.textContent = "🌓";
      }

      // Update active option in dropdown
      document.querySelectorAll(".theme-option").forEach((opt) => {
        const optTheme = opt.dataset.theme;
        if (optTheme === pref) {
          opt.classList.add("active");
          opt.setAttribute("aria-selected", "true");
        } else {
          opt.classList.remove("active");
          opt.setAttribute("aria-selected", "false");
        }
      });
    }

    function applyTheme(pref, persist) {
      const effectiveTheme = resolveTheme(pref);
      updateUI(pref, effectiveTheme);
      const pid = getPID();
      if (persist) {
        savePreference(pid, pref);
        if (pid) {
          Portal.event("theme_change", { preference: pref, effective: effectiveTheme, pid: pid });
        }
      }
    }

    // Expose helpers on window.Portal for programmatic use and testing
    Portal.setTheme = function(pref) { applyTheme(pref, true); };
    Portal.getTheme = function() { return getStoredPreference(getPID()); };

    const control = document.getElementById("theme-control");
    const btn = document.getElementById("theme-btn");
    const dropdown = document.getElementById("theme-dropdown");

    if (btn && dropdown) {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const opening = dropdown.classList.contains("hidden");
        if (opening) {
          dropdown.classList.remove("hidden");
          btn.setAttribute("aria-expanded", "true");
        } else {
          dropdown.classList.add("hidden");
          btn.setAttribute("aria-expanded", "false");
        }
      });

      document.querySelectorAll(".theme-option").forEach((opt) => {
        opt.addEventListener("click", (e) => {
          e.stopPropagation();
          const targetTheme = opt.dataset.theme;
          if (targetTheme) {
            applyTheme(targetTheme, true);
          }
          dropdown.classList.add("hidden");
          btn.setAttribute("aria-expanded", "false");
        });
      });

      document.addEventListener("click", (e) => {
        if (control && !control.contains(e.target)) {
          dropdown.classList.add("hidden");
          btn.setAttribute("aria-expanded", "false");
        }
      });

      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !dropdown.classList.contains("hidden")) {
          dropdown.classList.add("hidden");
          btn.setAttribute("aria-expanded", "false");
        }
      });
    }

    // Media query listener for dynamic system theme changes
    if (window.matchMedia) {
      const mql = window.matchMedia("(prefers-color-scheme: dark)");
      const onChange = () => {
        const curPref = getStoredPreference(getPID());
        if (curPref === "system") {
          applyTheme("system", false);
        }
      };
      if (mql.addEventListener) mql.addEventListener("change", onChange);
      else if (mql.addListener) mql.addListener(onChange);
    }

    // Sync across tabs
    window.addEventListener("storage", (e) => {
      if (e.key && e.key.startsWith("portal_theme_")) {
        const curPid = getPID();
        if (e.key === "portal_theme_" + curPid || e.key === "portal_theme_global") {
          applyTheme(getStoredPreference(curPid), false);
        }
      }
    });

    // On launcher page, if user types in PID, load that PID's saved theme
    const pidInput = document.getElementById("pid");
    if (pidInput) {
      const handlePidChange = () => {
        const typedPid = pidInput.value.trim();
        if (typedPid) {
          try {
            const typedPref = localStorage.getItem("portal_theme_" + typedPid);
            if (typedPref) applyTheme(typedPref, false);
          } catch (e) {}
        }
      };
      pidInput.addEventListener("input", handlePidChange);
      pidInput.addEventListener("change", handlePidChange);
    }

    // Initial setup on page load
    const initialPid = getPID();
    const initialPref = getStoredPreference(initialPid);
    applyTheme(initialPref, false);
  }

  /* ---- viewport / device check ---------------------------------------------------- */
  function viewportCheck() {
    if (!body.dataset.pid) return;
    const maxTouch = navigator.maxTouchPoints || 0;
    const coarse = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
    const isMobile = !!(
      (navigator.userAgentData && navigator.userAgentData.mobile) ||
      /android|ipad|iphone|ipod/i.test(navigator.userAgent) ||
      (maxTouch > 1 && /macintosh/i.test(navigator.userAgent))
    );
    Portal.post("/api/viewport", {
      width: window.innerWidth,
      height: window.innerHeight,
      maxTouchPoints: maxTouch,
      pointer_coarse: coarse,
      is_mobile: isMobile
    }).then((r) => {
      if (r && r.ok === false && r.message && body.dataset.allowMobile !== "true") {
        document.querySelectorAll(".shell").forEach((el) => el.classList.add("hidden"));
        const blk = document.getElementById("viewport-block");
        if (blk) {
          const msg = document.getElementById("viewport-msg");
          if (msg) msg.textContent = r.message;
          blk.classList.remove("hidden");
        }
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
    initTheme(); viewportCheck(); telemetry(); countdowns(); continueButtons(); accordions();
  });
})();
