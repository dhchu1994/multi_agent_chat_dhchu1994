/* Submission page: final look at the card, live status, hand-in. */
(function () {
  "use strict";
  const S = window.SUBMIT;
  const $ = (id) => document.getElementById(id);

  function onCard(view) {
    $("fields-badge").textContent = view.fields_complete + "/" + S.total;
    $("fields-badge").classList.toggle("ok", view.fields_complete === S.total);
    const pct = Math.round((view.typed_share || 0) * 100);
    $("share-badge").textContent = pct + "%";
    $("share-badge").classList.toggle("ok", S.role !== "generative" || pct >= Math.round(S.minShare * 100));
    const empties = S.fields.filter((f) => !((view.text[f.id] || "").trim())).map((f) => f.label);
    $("empty-note").textContent = empties.length ? S.stillEmpty.replace("{fields}", empties.join(", ")) : "";
  }

  const editor = CardEditor.mount($("card-editor"), {
    endpoint: "/api/card/action", view: S.card, fields: S.fields, role: S.role, texts: S.texts,
    compact: true, onChange: onCard, big: false,
    onError: (m) => { $("submit-err").textContent = m; },
  });

  $("hand-in").addEventListener("click", async () => {
    const btn = $("hand-in");
    await editor.flush();
    btn.disabled = true;
    $("submit-err").textContent = "";
    const r = await Portal.post("/api/card/submit", {});
    if (r.ok) { location.reload(); return; }
    $("submit-err").textContent = r.error || "Please try again.";
    btn.disabled = false;
  });
})();
