/* Check-in: collects answers plus the time spent on each question. */
(function () {
  const form = document.getElementById("checkin-form");
  const err = document.getElementById("checkin-err");
  const durations = {};
  let lastAt = Date.now();
  form.addEventListener("change", (e) => {
    const name = e.target.name;
    if (!name) return;
    const now = Date.now();
    durations[name] = (durations[name] || 0) + (now - lastAt);
    lastAt = now;
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    const answers = {};
    form.querySelectorAll(".q").forEach((q) => {
      const checked = q.querySelector("input:checked");
      if (checked) answers[q.dataset.item] = checked.value;
    });
    const btn = document.getElementById("checkin-btn");
    btn.disabled = true;
    const r = await Portal.post("/api/checkin/submit", { answers: answers, durations: durations });
    if (r.ok) { location.reload(); return; }
    err.textContent = r.error || "Please answer every question.";
    btn.disabled = false;
  });
})();
