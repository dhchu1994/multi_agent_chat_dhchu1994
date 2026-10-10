# Interaction Portal v2 - TODO (code review findings)

Reviewed commit: `75df5ba` on `main` (folder `interaction_portal/`). No existing files were changed; this file is the only addition.

## 0. Review scope and limits

Read in full: `README.md`, `Interaction-Portal-Build-Spec_v2.md` (text; embedded images not inspected), `app/main.py`, `app/state_machine.py`, `app/event_store.py`, `app/config_loader.py`, `app/agents/{base,specialist,orchestrator,schemas,topics}.py`, `app/simulation/{card_provenance,chat_router,practice}.py`, `static/js/{card_editor,chat,portal,task,submit,checkin}.js`, `templates/task.html`, `config/config.yaml` (until the connector truncated it inside `practice.steps_ai`), `config/conditions.yaml`, `config/prompts/{specialists_system,orchestrator_plan}.md`, `requirements.txt`.

Only partly read: `app/simulation/session_manager.py` (the connector cut the file off inside `chat_send`, so the rest of `chat_send`, `refresh_panel`, `panel_poll` and the exit hand-off were NOT reviewed).

Not read: `agents/mock_llm.py`, `harnesses/*`, `static/js/practice.js`, `static/css/*`, other templates, `config/content/clients/*`, `config/prompts/panel_levels/*`, `panel_compress.md`, `md_to_pdf.py`, `terminal.py`, `tests/*`. Items marked **(verify)** rest on partial evidence and should be confirmed by running the code.

Severity: **High** = can invalidate data or breaks a spec rule / security; **Medium** = wrong behaviour in realistic situations; **Low** = edge case, cosmetic or latent.

---

## 1. Bugs and incorrect implementations

### B1 (High) - Per-session lock is held during LLM calls and blocks page loads, timer polls and telemetry
- Where: `session_manager.py` (`chat_open`, `chat_send`, `tick`, `client_event`), `main.py` (`/api/timer`, `/screen`, `/api/event`).
- Problem: `chat_open` / `chat_send` hold `self.lock(pid)` (an `RLock`) for the whole exchange (orchestrator call + four specialist calls + panel call, each up to `timeout_seconds` x (1 + retries) = 120 s). Every other request for the same pid (`tick()`, `/api/timer`, `/api/event`, `/api/card/action`, page reload) waits for that lock. This breaks Spec section 10 "Failures: never block the page or the timer" and delays/misorders telemetry (see B2).
- Fix:
  1. Split state mutation from model calls: take the lock, load state, build prompts/snapshot, release; call the LLM; re-take the lock, re-load state, apply results, save.
  2. Add an optimistic version counter to `SessionState` (`version` int, increment in `StateMachine.save`, compare-and-set in `EventStore.update_session`) so a stale writer cannot overwrite newer state.
  3. Make `tick()` / `/api/timer` / `/api/event` use a separate short-lived lock (or no lock for read-only paths) so they never wait for a model call.
  4. Add a test that starts a slow mock LLM (`time.sleep(3)`) in `chat_send` and asserts `/api/timer` answers in < 200 ms.

### B2 (High) - Client telemetry timestamps are server processing time, not event time
- Where: `portal.js` (`Portal.event`), `session_manager.client_event`, `event_store.log`.
- Problem: events carry no client time and are stamped when the server writes the row. With B1 (lock waits), `window_blur`, `scroll_sample`, `fold_open` and `pack_open` rows can be stamped seconds to minutes late and out of order. Blur/focus durations and "time on pack section" analyses become unreliable. Events are also fire-and-forget: a failed POST is lost, and nothing is flushed on tab close.
- Fix:
  1. In `portal.js` add `t_client = Date.now()` and `t_mono = Math.round(performance.now())` to every event and send them in `metadata`; keep the server timestamp as `received_at` (add a column or put both in metadata) and store the client time as the analysis timestamp (clamped to the session window).
  2. Queue events in memory (and `sessionStorage`), batch every 1-2 s via `POST /api/events` (list), flush with `navigator.sendBeacon` on `visibilitychange` / `pagehide`, retry on failure.
  3. Server: accept a list, validate each, insert in one transaction.

### B3 (High) - Insecure defaults can reach production silently
- Where: `config/config.yaml`, `config_loader.load_config`, `state_machine.completion_code_for`.
- Problem: `testing.standalone_mode: true`, `admin_token: "admin-test-token"`, `server.secret_key: "change-me-in-production"`, `qualtrics.completion_code_secret: "change-me-too"` and `host: 0.0.0.0` are the shipped defaults. If the operator forgets one env var, then (a) anyone can open the test launcher and pick any condition, (b) anyone can sign a valid session cookie for any pid (session hijack), (c) `/admin/export/*` is readable with a public token, (d) completion codes are forgeable.
- Fix: in `load_config`, if `standalone` is false (or `PORTAL_ENV=production`), raise at startup when `secret_key`, `admin_token`, or `completion_code_secret` equals a known default or is shorter than 24 chars; log a loud warning when standalone is true and host is not loopback. Move secrets out of YAML entirely (env only). Add a unit test for the guard.

### B4 (High) - Panel hard-trim can delete required sections and break the manipulation
- Where: `orchestrator.generate_panel`.
- Problem: if the text is still > 80 words after the compress call, it is cut at word 80. Sections are ordered "Team status", "On the card", "Why it recurs", so the cut removes exactly the sections that define the TASK and DEV levels; a DEV participant can receive a COORD-like panel. Also nothing checks that a COORD panel does NOT contain diagnosis/principle content, or that forbidden imperatives ("you should", "ask", "try", "make sure") are absent; the result is only logged (`sections_present`).
- Fix:
  1. Validate after generation: required sections present (and in order), no extra sections for lower levels, word count in range, regex check for forbidden imperatives and for any hidden-item text (see I4).
  2. On failure retry generation (max 2) with an explicit error message; only if all retries fail fall back to a per-section hard cap (e.g. trim each section proportionally, never drop a section) and log `panel_degraded=true`.
  3. Log `panel_shown` (spec section 9) with `retried`, `truncated`, `validation_errors`.

### B5 (Medium) - Chat input is wiped when the message was NOT sent (unknown @mention)
- Where: `chat.js` `Chat.prototype.submit` / `stream`, `task.js` handlers.
- Problem: `stream()` resolves `true` whenever the HTTP call succeeds. For an unknown mention the server only yields `{"type":"warning"}` and sends nothing, but `submit()` still clears the textarea, so the participant must retype the message. Same for `error` events.
- Fix: in `task.js` handlers set a flag (`sent = true` on `message` events with `sender === "Participant"`); make `onSend` return `false` when only `warning`/`error` events arrived. Same in `practice.js` (verify).

### B6 (Medium) - Card text is silently truncated at 600 characters
- Where: `card_provenance.set_field_text` (`new_text[:max_chars]`), `card_editor.js` (`buildTextarea` has no `maxLength`).
- Problem: a participant who types more than 600 characters in a field sees their text vanish after the next server response (`renderText` overwrites the textarea when not focused). This destroys user work and distorts the typed-share data.
- Fix: set `ta.maxLength = view.max_span_chars` (expose `max_span_chars` in `card_view`), show a live counter, and make the server return an explicit error instead of truncating.

### B7 (Medium) - `chat_open`: `draft_card=out.draft_card or True` makes the model's value meaningless
- Where: `session_manager.chat_open`.
- Problem: `x or True` is always `True`. In `_run_burst` with `draft_card=True`, every specialist is called with trigger `draft_card`, and the assignment text is ignored by `_user_prompt` for that trigger (the assignment is only visible through the transcript). Assigned specialists may therefore not answer their assignment, and the folded block can show assignments without replies **(verify with the real model)**.
- Fix: use `draft_card=True` explicitly for the opening burst (per prompt contract) and pass the assignment text into the `draft_card` prompt (`"The orchestrator asked: ..."`); add a harness B assertion that every assigned specialist produced a reply or a decline.

### B8 (Medium) - Opening burst is never retried
- Where: `chat_open` sets `chat["opened"] = True` and saves before the model calls.
- Problem: if the orchestrator call fails (network, 429), the participant gets a "did not respond" line and `opened` stays true, so a reload does not retry. The participant starts a 10-minute task with no draft and no panel, which differs from other participants.
- Fix: set `opened = True` only after at least one successful call, or store `opening_attempts` and retry once on the next `/api/chat/state` while time remains; log `agent_error` with attempt number.

### B9 (Medium) - Check-in item 2 (specialist choice) uses a fixed option order
- Where: `session_manager.checkin_items` (`list(self.cfg.specialist_names)`).
- Problem: options always appear as Nia, Theo, Rhys, Mira, while the team page order is randomised per participant to remove position effects (spec section 6.3). The check-in re-introduces a position bias and cannot be compared to the stored `specialist_order`.
- Fix: use `state.specialist_order` (keep "Not sure" last) and log the displayed order in `checkin_answer.metadata.options_order`.

### B10 (Medium) - Check-in item 2 is shown in the NOAI condition
- Where: `config.yaml` `checkin.items` (`ci2` has no `ai_only`), `checkin_items`.
- Problem: the spec says only item 5 is skipped for NOAI, so this follows the spec, but NOAI participants never met Nia/Theo/Rhys/Mira (pack sections are titled "Client notes" etc.), so the question is meaningless or confusing and the answers are noise.
- Fix: ask the researchers; either set `ai_only: true` on `ci2` (and update the README "four questions" text) or show the pack-section titles as options for NOAI.

### B11 (Medium) - Evaluative card: AI spans can be glued together without a separator
- Where: `card_provenance.add_ai_text`, `field_text` (`"".join(...)`).
- Problem: `add_ai_text` only deletes the same author's spans with status `proposed`. A span the participant already KEPT stays, and a new proposal from the same specialist is appended, so a field can contain `kept` text + new `proposed` text; when the participant keeps both, `field_text` concatenates them with no space or newline.
- Fix: when a specialist proposes again for a field, mark the author's older `kept` span `superseded` (keep in history, excluded from the card) or join included spans with `"\n"`. Add a unit test: propose -> keep -> propose again -> keep -> text must not contain a glued word boundary.

### B12 (Medium) - OpenAI client fallback does not work for providers that reject `stream_options`
- Where: `agents/base.py` `OpenAICompatClient.complete`.
- Problem: `except TypeError` never fires for an HTTP 400 from a compatible endpoint that rejects `stream_options` / `response_format`; the call fails and (after one retry) every agent shows "did not respond". Also `max_tokens` and `temperature` are rejected by some newer model families (they require `max_completion_tokens`, fixed temperature), so changing the pinned model can break all calls.
- Fix: catch `openai.BadRequestError`, drop the unsupported kwarg and retry once; add config flags `llm.supports_stream_usage`, `llm.token_param` (`max_tokens` | `max_completion_tokens`), `llm.send_temperature`. Close the stream in a `finally`.

### B13 (Medium) - Tablet detection misses iPads (and any large touch device)
- Where: `main.py` `MOBILE_RE`, `portal.js viewportCheck`, `session_manager.record_viewport`.
- Problem: iPadOS Safari sends a desktop (Macintosh) user agent, and a landscape iPad is >= 900 px wide, so it passes both checks. The spec requires blocking tablets / requiring a physical keyboard.
- Fix: send `navigator.maxTouchPoints`, `matchMedia('(pointer: coarse)').matches` and `navigator.userAgentData?.mobile` with the viewport; block when `maxTouchPoints > 1` and the UA contains `Macintosh`, or when the primary pointer is coarse. Also enforce server-side (return 403 on API calls when the last viewport check failed and `allow_mobile` is false) instead of only hiding `.shell` in JS.

### B14 (Medium) - Inconsistent and wrong documentation / participant text
- `config.yaml` `orientation.rows_*` say "Six clients" while the study has seven (exit text says "all seven clients"). Participants are told six, then get a seventh. Fix: "Seven clients: six with the team, the last one alone" in both `rows_ai` and `rows_noai`.
- README claims the portal tracks "keystrokes"; no keystroke/paste telemetry exists (only debounced card diffs after 700 ms and chat messages). Fix the README or implement I6.
- README says `panel_compress.md` lives in `prompts/panel_levels/`; it is in `prompts/` (the loader keys it as `panel_compress`; moving it would silently break it).
- README says the default host is `127.0.0.1`; `config.yaml` sets `0.0.0.0`.
- README says "37-test" in one place and "46 total" in another.
- `EventStore.backup` says "run nightly from cron: see README", but README has no backup instructions and no CLI calls `backup` (see I9).
- README tree omits `app/terminal.py`; README gives Windows-only run instructions (add Linux/macOS `export` equivalents and `uvicorn` production command).

### B15 (Low) - Admin token accepted in the query string; documented as the "easiest" method
- Where: `main.admin_ok`, README section 3.3.
- Problem: tokens in URLs end up in server logs, browser history and referrers; the README example uses the shipped default token.
- Fix: accept only the `x-admin-token` header (or `Authorization: Bearer`), remove query-string support and the examples with `admin-test-token`; add basic rate limiting on failed attempts.

### B16 (Low) - `card_submit` advances the page before validating the card
- Where: `session_manager.card_submit`.
- Problem: from the task page it calls `sm.advance(...)` (task -> submit) and only then checks `can_submit()`; on failure the participant has already left the task page (timer effectively ended) and the browser still shows the task UI until the next 10 s timer sync. Currently latent (only "alone"/role `none` has a hand-in button on the task page and `none` never fails `can_submit`), but any config change exposes it.
- Fix: run `card.can_submit()` first; advance only when it passes.

### B17 (Low) - `break_end` event logs an unscaled duration on timer expiry
- Where: `session_manager.tick` (`seconds_elapsed=self.cfg.timers.break_seconds`).
- Fix: use `self.cfg.scaled(self.cfg.timers.break_seconds)` (consistent with `end_break`).

### B18 (Low) - Malformed check-in payload gives HTTP 500
- Where: `main.checkin_submit` / `session_manager.checkin_submit`.
- Problem: if `answers` is a list or string, `answers.get` raises `AttributeError`. `durations` values are not bounded, so a client can store arbitrary numbers.
- Fix: validate `isinstance(answers, dict)`, cast durations to `0 <= int <= 3_600_000`, return 400 otherwise. Also reject booleans for scale items.

### B19 (Low) - Unbounded client event metadata and no rate limit
- Where: `session_manager.client_event`, `main.client_event`.
- Problem: allowed keys are filtered but values are not type- or size-checked (`block_id`/`section_name` can be megabytes), and `/api/event` has no rate limit or body-size cap, so a client can bloat the append-only table (rows cannot be deleted).
- Fix: cap string values at 64 chars, coerce numerics, limit body size (e.g. 4 KB), and rate-limit per pid (e.g. 20 events/s).

### B20 (Low) - Multi-word reasoning on `format_transcript(limit=60)` contradicts "full transcript"
- Where: `agents/base.format_transcript` (default `limit=60`; panel uses 25).
- Problem: spec section 7.2 says specialists receive the full transcript incl. folded blocks. With many bursts, early disclosures fall out of the context window, so a specialist can contradict or re-disclose something said earlier.
- Fix: keep the full transcript (10 minutes of chat fits easily in the context) or summarise older turns deterministically; log `transcript_messages_sent` per call.

### B21 (Low) - Keyword heuristics for `asked_for` are brittle
- Where: `agents/topics.py`, `config.yaml` `hidden_keywords`.
- Problem: exact-token match only (no stemming: "budgets", "limited" miss "budget"/"limit"; hyphenated words are split), while some keywords are very broad ("say", "want", "important", "client"). `asked_for` is the ground truth for Harness A rule 2, so mislabelled probes make the harness unreliable. `best_domain` has two identical branches (dead code).
- Fix: see I4 (LLM-judge or human-coded gold labels); at minimum add light stemming and unit tests with positive/negative examples per specialist.

### B22 (Low) - Runtime-state risks not documented
- The per-pid locks and `_POOL` are in-process; running `uvicorn --workers N` (N > 1) breaks mutual exclusion and can corrupt `sessions.state`. `_locks` is never cleaned up. Document "single worker only" in the README and enforce at startup (refuse `WEB_CONCURRENCY > 1`), or move to a DB-based lease (I3).
- Streaming chat endpoints need `X-Accel-Buffering: no` in the response headers or nginx buffers the NDJSON and the "live typing" effect and the "typing..." indicator break behind a reverse proxy.
- `Set-Cookie` uses `secure=request.url.scheme == "https"`; behind a TLS-terminating proxy the scheme is `http`, so the cookie is sent without `Secure`. Run uvicorn with `--proxy-headers --forwarded-allow-ips` or add a `PORTAL_COOKIE_SECURE` setting.

### B23 (Low) - Late agent output can land after the task ended
- Where: `chat_send` / `_run_burst` / `_revise_after_send_back`.
- Problem: the time check happens at the start of `_require_team_task`; an exchange that finishes after the deadline still adds AI spans to the card (visible on the submission page) and writes `agent_reply` events after `timer_expired`.
- Fix: re-check `task_remaining` before applying proposals; if expired, store the reply in the transcript flagged `after_deadline=true` but do not touch the card.

---

## 2. Significant improvements

### I1 - Signed entry links and recoverable single-use sessions
- Why: the condition is an unauthenticated query parameter (`?pid=...&condition=...`), so a participant can change it; a participant who loses the cookie (new browser, cleared cookies, Qualtrics in-app browser) is locked out permanently.
- How: add `sig = HMAC(secret, pid|condition|exp)` to the Qualtrics link (generated with an embedded-data JavaScript/Web Service step); `entry()` verifies `sig` and `exp`. Issue a per-session resume token (shown on the screen and emailed/stored by Qualtrics) that re-sets the cookie only within `resume_gap_seconds`. Log `entry_rejected` reasons (`bad_sig`, `expired`, `already_used`).

### I2 - Server-side verification of the completion code
- Why: the code is a deterministic HMAC of the pid with a default secret; Qualtrics cannot verify it, so a participant could invent codes.
- How: add `GET /verify?pid=...&code=...` (admin-token protected or one-time signed URL) that Qualtrics calls via Web Service, returning `{valid: true, end_state: "completed"}`; store the code issue time and use count.

### I3 - Non-blocking model orchestration and load safety for 30 concurrent sessions
- Why: a shared `ThreadPoolExecutor(16)` and a lock per pid mean 30 participants x 5 calls per message queue up; a slow provider or a 429 spike makes replies exceed the timer.
- How: (1) implement B1; (2) use the async OpenAI client with a global `asyncio.Semaphore` and per-model rate limiter; (3) retry 429/5xx with exponential backoff + jitter, capped by remaining task time; (4) log `queue_wait_ms`, `provider_status` and `retry_after` in `agent_reply`/`agent_error`; (5) add a Locust/pytest-asyncio load test with a mock LLM that has 1-6 s latency and 5 % failures for 30 sessions; (6) expose `/admin/health` with queue length and p95 latency.

### I4 - Deterministic disclosure detection and a pre-send guardrail for hidden items
- Why: the spec says the behaviour rules "matter more than anything else", but `disclosed_hidden_items` is self-reported by the model and `asked_for` is a keyword heuristic. A model that leaks a hidden item without tagging it is invisible in the data, and the participant has already seen it.
- How:
  1. In `config_loader`, keep `hidden_passages` (already parsed). Add `agents/disclosure.py` with `detect_disclosure(reply, passages)` using normalized n-gram overlap plus an embedding or LLM-judge fallback (cheap model, temperature 0).
  2. In `SpecialistAgent.reply`, if the detector finds a hidden passage and `asked_for` is false, regenerate once with a stricter instruction; if it still leaks, replace the reply with a decline line. Log `leak_detected`, `leak_blocked`, `model_reported_ids`, `detected_ids` so both can be compared.
  3. Replace `asked_for` by an LLM judge ("does this participant message ask about topic X?") with the keyword version as a baseline; keep 50-100 human-labelled probes as a regression set for Harness A.
  4. Apply the same detector to the panel text (panel must never state client facts) and to `card_proposal` text (prompt says never include undisclosed hidden items).

### I5 - Panel and prompt validators plus a fixed-seed regression suite
- Why: three conditions differ only by the panel's content; unvalidated panels are a threat to internal validity.
- How: implement B4; add `panel_validator.py` (sections, order, word budget, imperative-language regex, no-client-facts, no mention of unasked specialists when `name_unasked: false`); store `validation_errors` in `panels`; build a golden-file test set (transcript -> expected sections) run in CI against the mock and, nightly or before launch, against the real model with temperature 0.

### I6 - Richer, trustworthy behavioural telemetry
- Why: the research question is about control and critical thinking; currently only 700 ms-debounced diffs of card text and chat messages are recorded.
- How: add events `keystroke_summary` (counts, inter-key intervals aggregated per 5 s, no key content), `paste`, `cut`, `drop`, `compositionend`, `focus_field` / `blur_field`, `card_span_hover` (optional), `chat_input_started` (time to first character after an agent reply), `panel_visible` (IntersectionObserver dwell time on the panel), `visibility_change`. Respect the privacy rule (no personal data; log lengths and timing, not keystroke content). Include B2's client timestamps. Document each event in a data dictionary.

### I7 - Anti-gaming for the generative minimum typed share
- Why: typed share counts any characters authored by `user`. Participants can paste suggestions retyped, type filler ("aaaa..."), or paste from the chat, and still pass the 25 % gate; this contaminates GEN vs EVA/PAS comparisons.
- How: (1) detect `inputType` = `insertFromPaste` / `insertFromDrop` in `card_editor.js`, send `paste_chars` with the `type` action and either block paste (config `editor.allow_paste: false`) or classify pasted text as `pasted` (not counted as typed); (2) server-side similarity check: if a typed span has >= 80 % normalized overlap with an AI suggestion or chat message, label `user_copied_ai` for analysis; (3) minimum distinct-word / language sanity check before the span counts; (4) log both `typed_share_raw` and `typed_share_strict`.

### I8 - Operator dashboard and tidy exports
- Why: researchers need to monitor a 30-seat lab session and analyse data "without developer help" (spec section 10). Today there are three raw dumps (`events.csv`, `cards.json`, `all.json`) and the main tables are only available inside nested JSON.
- How: add `/admin` (token-protected, header only) with: live sessions (condition, page, client index, time left, last activity, error count), agent latency and failure rates, counts per condition (to spot imbalance), and a button to end/abandon a session. Add flat exports: `transcripts.csv`, `panels.csv`, `card_spans.csv` (one row per span with author/status/history), `checkins.csv` (wide, one row per pid x client), `sessions.csv`, plus a `data_dictionary.md` generated from the code.

### I9 - Automated nightly backup, integrity check and restore drill
- Why: spec requires a nightly backup; `EventStore.backup()` exists but nothing calls it.
- How: add `python -m app.tools.backup --dest backups/ --keep 14` (uses `EventStore.backup`, `PRAGMA integrity_check`, gzip, SHA-256 file); document cron / Windows Task Scheduler entries; add `python -m app.tools.verify_db` that checks the append-only triggers exist (`sqlite_master`) and counts rows; test restore in CI.

### I10 - Reproducibility and audit trail for every model call
- Why: results must be attributable to an exact configuration; `config_version` is a hand-bumped string and prompts are not hashed.
- How: compute `config_sha256` (YAML + prompts + client content) at startup and store it on every session and `session_start`; log `prompt_sha256`, `system_fingerprint` (from the API), `seed` (set `seed` in the request where supported), and optionally the full system+user prompt in a separate `llm_calls` table (not in the participant-visible exports). Pin dependencies: replace `>=` ranges in `requirements.txt` with exact versions (add `requirements.lock` via `pip-compile`), and move `pytest`, `xhtml2pdf`, `markdown`, `colorama` into `requirements-dev.txt`.

### I11 - Counterbalanced assignment of client order (and condition balance check)
- Why: `rng.shuffle` with a random seed per participant gives random but unbalanced orders; with n ~ 30 per run, order effects (fatigue, learning) can be confounded with condition.
- How: use a balanced Latin square / block randomisation over C1-C6 keyed on a counter stored in the DB (`assignments` table), log the assigned cell, and show per-condition counts in the dashboard (I8). Keep the seed in `session_start`.

### I12 - CI, test depth and a full-session smoke test
- Why: `tests/` has eleven small files; the most complex flow (`chat_send`, panel refresh, send-back revisions, timer expiry during streaming) is hard to cover by hand, and the README test counts disagree.
- How: add GitHub Actions (`pytest -q`, harness A/B with the mock client for all 10 conditions, `ruff`/`mypy` optional); add tests for B1, B5-B9, B11, B13, B16, B18; add a Playwright smoke test that runs one full session per role in standalone mode with `timer_scale=100`; fail CI if README test counts or file layout drift (simple script).

### I13 - PII safeguard for participant text
- Why: spec/README say no personal data may enter the system, but only a reminder is shown; everything typed is stored forever in an append-only table.
- How: run a lightweight regex/NER check (email, phone, IBAN, URL, "my name is ...") on chat messages and card text before logging; if matched, show a soft inline warning ("Please do not enter personal information") and log `pii_flag` with the type only; optionally store the redacted text in exports and the raw text only in a restricted column. Add a retention setting and a documented deletion procedure (note: triggers block deletes, so provide an admin-only maintenance script that drops and recreates triggers inside a logged transaction).

### I14 - Chat stream resilience
- Why: the NDJSON stream runs in a background thread and completes even if the browser disconnects, but the UI does not recover mid-burst: after a network blip the participant sees nothing until a reload, and `Chat.busy` can stay true.
- How: on stream error, poll `/api/chat/state` every 2 s until the server-side exchange is finished and render new messages (the `seen` set already de-duplicates); add `pending` / `exchange_id` to `chat_state` so the client knows an exchange is still running; show a visible connection indicator; add `X-Accel-Buffering: no` (B22).

---

## 3. Suggested order of work

1. Production safety and data validity: B3, B1, B2, B4, I4 (guardrail), B13.
2. Participant-facing correctness: B5, B6, B7, B8, B9, B11, B14, B16.
3. Operations: I9, I8, I3, I12, I10.
4. Research-quality upgrades: I1, I2, I5, I6, I7, I11, I13, I14.
