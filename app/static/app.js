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

// ---------------------------------------------------------- static demo --
// On the Firebase static snapshot (window.QXC_STATIC is injected only by
// scripts/build_static.py) there's no backend to do real filtering, so the
// Agency/Department/Company/Search controls filter the already-embedded
// rows/cards entirely in the browser instead of round-tripping to a server
// that isn't there. The live Flask app never sets QXC_STATIC, so none of
// this runs there - it keeps using real server-side filtering as before.
if (window.QXC_STATIC) {
  document.addEventListener("DOMContentLoaded", function () {
    initRecordsFilter();
    initHomeFilter();
  });
}

function paramsFromLocation() {
  return new URLSearchParams(window.location.search);
}

function initRecordsFilter() {
  const list = document.getElementById("records-list");
  if (!list) return;

  const form = document.getElementById("filter-form");
  const agencySelect = document.getElementById("agency-select");
  const deptSelect = document.getElementById("department-select");
  const companyInput = document.getElementById("company-input");
  const qInput = document.getElementById("q-input");
  const rows = Array.from(list.querySelectorAll(".record-row"));

  // This page is frozen with limit=all, so every (agency, department) pair
  // that exists is already present in the DOM - no need for a separate
  // embedded map like the home page needs.
  const deptsByAgency = {};
  const allDepts = new Set();
  rows.forEach(function (row) {
    const a = row.dataset.agency;
    const d = row.dataset.department;
    if (!d) return;
    allDepts.add(d);
    if (!a) return;
    if (!deptsByAgency[a]) deptsByAgency[a] = new Set();
    deptsByAgency[a].add(d);
  });

  function rebuildDeptOptions(selectedAgency, selectedDept) {
    const depts = selectedAgency && deptsByAgency[selectedAgency]
      ? Array.from(deptsByAgency[selectedAgency]).sort()
      : Array.from(allDepts).sort();
    deptSelect.innerHTML = '<option value="">All Departments</option>';
    depts.forEach(function (d) {
      const opt = document.createElement("option");
      opt.value = d;
      opt.textContent = d;
      if (d === selectedDept) opt.selected = true;
      deptSelect.appendChild(opt);
    });
  }

  function applyFilter() {
    const agency = agencySelect.value;
    const dept = deptSelect.value;
    const company = companyInput.value.trim().toLowerCase();
    const q = qInput.value.trim().toLowerCase();

    let shown = 0;
    let reviewed = 0;
    rows.forEach(function (row) {
      const visible =
        (!agency || row.dataset.agency === agency) &&
        (!dept || row.dataset.department === dept) &&
        (!company || row.dataset.company.includes(company) || row.dataset.prime.includes(company)) &&
        (!q || row.dataset.search.includes(q));
      row.style.display = visible ? "" : "none";
      if (visible) {
        shown++;
        if (row.classList.contains("is-viewed")) reviewed++;
      }
    });

    const set = function (id, val) {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };
    set("stat-matches", shown);
    set("stat-shown", shown);
    set("stat-reviewed", reviewed);
    set("stat-unreviewed", shown - reviewed);
    const capNote = document.getElementById("cap-note");
    if (capNote) capNote.style.display = "none";

    let jsEmpty = document.getElementById("js-empty-state");
    if (shown === 0) {
      if (!jsEmpty) {
        jsEmpty = document.createElement("div");
        jsEmpty.className = "empty-state";
        jsEmpty.id = "js-empty-state";
        jsEmpty.textContent = "No records match these filters.";
        list.appendChild(jsEmpty);
      }
    } else if (jsEmpty) {
      jsEmpty.remove();
    }
  }

  agencySelect.removeAttribute("onchange");
  deptSelect.removeAttribute("onchange");

  agencySelect.addEventListener("change", function () {
    rebuildDeptOptions(agencySelect.value, "");
    applyFilter();
  });
  deptSelect.addEventListener("change", applyFilter);
  companyInput.addEventListener("input", applyFilter);
  qInput.addEventListener("input", applyFilter);
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    applyFilter();
  });

  const params = paramsFromLocation();
  const initAgency = params.get("agency") || "";
  const initDept = params.get("department") || "";
  if (initAgency) agencySelect.value = initAgency;
  rebuildDeptOptions(initAgency, initDept);
  if (initDept) deptSelect.value = initDept;
  if (params.get("company")) companyInput.value = params.get("company");
  if (params.get("q")) qInput.value = params.get("q");
  applyFilter();
}

function initHomeFilter() {
  const grid = document.getElementById("agency-grid");
  if (!grid) return;

  const sortSelect = document.getElementById("sort-select");
  const agencySelect = document.getElementById("home-agency-select");
  const deptSelect = document.getElementById("home-department-select");
  const cards = Array.from(grid.querySelectorAll(".agency-card"));

  let deptMap = {};
  const deptMapEl = document.getElementById("dept-map-data");
  if (deptMapEl) {
    try {
      deptMap = JSON.parse(deptMapEl.textContent);
    } catch (e) {
      deptMap = {};
    }
  }

  function rebuildDeptOptions(selectedAgency, selectedDept) {
    const depts = selectedAgency && deptMap[selectedAgency] ? deptMap[selectedAgency] : [];
    deptSelect.innerHTML = '<option value="">All Departments</option>';
    depts.forEach(function (d) {
      const opt = document.createElement("option");
      opt.value = d;
      opt.textContent = d;
      if (d === selectedDept) opt.selected = true;
      deptSelect.appendChild(opt);
    });
  }

  const sortKeyFns = {
    combined: function (c) { return -parseFloat(c.dataset.combined); },
    direct: function (c) { return -parseFloat(c.dataset.direct); },
    subaward: function (c) { return -parseFloat(c.dataset.subaward); },
    unreviewed: function (c) { return -parseFloat(c.dataset.unreviewed); },
    records: function (c) { return -parseFloat(c.dataset.records); },
    name: function (c) { return c.dataset.agency.toLowerCase(); },
  };

  function applySort() {
    const fn = sortKeyFns[sortSelect.value] || sortKeyFns.combined;
    cards
      .slice()
      .sort(function (a, b) {
        const ka = fn(a);
        const kb = fn(b);
        return ka < kb ? -1 : ka > kb ? 1 : 0;
      })
      .forEach(function (c) {
        grid.appendChild(c);
      });
  }

  sortSelect.removeAttribute("onchange");
  agencySelect.removeAttribute("onchange");

  sortSelect.addEventListener("change", applySort);
  agencySelect.addEventListener("change", function () {
    rebuildDeptOptions(agencySelect.value, "");
  });

  const params = paramsFromLocation();
  sortSelect.value = params.get("sort") || "combined";
  const initAgency = params.get("agency") || "";
  if (initAgency) agencySelect.value = initAgency;
  rebuildDeptOptions(initAgency, params.get("department") || "");
  applySort();
}
