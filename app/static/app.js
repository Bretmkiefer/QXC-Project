document.addEventListener("click", function (e) {
  const btn = e.target.closest(".viewed-toggle");
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();

  const type = btn.dataset.recordType;
  const id = btn.dataset.recordId;
  const nextState = btn.dataset.viewed !== "true";

  fetch(`/api/records/${encodeURIComponent(type)}/${encodeURIComponent(id)}/viewed`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ viewed: nextState }),
  })
    .then((r) => r.json())
    .then((data) => {
      btn.dataset.viewed = data.viewed ? "true" : "false";
      btn.textContent = data.viewed ? "Viewed" : "Mark viewed";
      btn.classList.toggle("is-viewed", data.viewed);
      const row = btn.closest(".record-row");
      if (row) row.classList.toggle("is-viewed", data.viewed);
    });
});
