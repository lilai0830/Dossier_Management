/* ============================================================
   Dossier_Management - Document Pipeline Frontend Logic

   Flow (no manual upload):
     Step 1  Configure  — enter a project name; the app scans
                         PROJECT_ROOT/<name>/ for dossier files.
     Step 2  Classify  — one click auto-sorts the dossiers in that
                         folder into <name>/CLINICAL | FE | CE.
     Step 3  Run       — ingest + package into a synthesis PDF.
   ============================================================ */

// --- DOM helpers ---
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// --- Logging ---
function log(message, level = "info") {
  const logArea = $("#log-area");
  const time = new Date().toLocaleTimeString();
  const span = document.createElement("span");
  span.className = `log-${level}`;
  span.textContent = `[${time}] ${message}\n`;
  logArea.appendChild(span);
  logArea.scrollTop = logArea.scrollHeight;
}

function setButtonLoading(btn, loading) {
  if (loading) {
    btn.dataset.originalText = btn.textContent;
    btn.innerHTML = '<span class="spinner"></span>' + btn.dataset.originalText;
    btn.disabled = true;
  } else {
    btn.textContent = btn.dataset.originalText || btn.textContent;
    btn.disabled = false;
  }
}

// --- Project name / id ---
function getProjectName() {
  return ($("#project-name") ? $("#project-name").value : "").trim();
}
function getProjectId() {
  return getProjectName() || "default";
}

// Resolved absolute path returned by /project/scan (for display in Step 3).
let lastScannedPath = "";
function showProjectPath(path, name) {
  const el = $("#project-path");
  if (!el) return;
  if (path) {
    el.textContent = `${name}  →  ${path}`;
  } else {
    el.textContent = name ? `Project: ${name}` : "Enter a project name in Step 1 and scan it.";
  }
}

// =================================================================
// Listen-folder config (persisted to listen_folder.txt, NOT logged)
// =================================================================

function getListenFolder() {
  return ($("#listen-folder") ? $("#listen-folder").value : "").trim();
}

async function loadListenFolder() {
  try {
    const res = await fetch("/config/listen-folder");
    const data = await res.json();
    if (data.ok && data.path) {
      $("#listen-folder").value = data.path;
      // Intentionally no path text in the log.
    }
  } catch (err) {
    // config is optional — ignore network errors silently
  }
}

async function saveListenFolder(path) {
  if (!path) return;
  try {
    const res = await fetch("/config/listen-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const data = await res.json();
    if (!data.ok) {
      log("Failed to save listen folder: " + (data.detail || ""), "error");
    }
  } catch (err) {
    log("Save listen folder error: " + err.message, "error");
  }
}

// --- Folder browser (directory picker backed by /browse-folders) ---
let currentBrowsePath = "";
let currentBrowseParent = null;

async function openFolderBrowser() {
  const start = getListenFolder();
  currentBrowsePath = start;
  await folderBrowseNavigate(start);
  await renderSavedPaths();
  $("#folder-modal").classList.remove("hidden");
}

// --- Saved listen-folder history (rendered inside the folder picker) ---
async function renderSavedPaths() {
  const list = $("#saved-paths-list");
  if (!list) return;
  list.innerHTML = "";
  let active = "";
  try {
    const res = await fetch("/config/listen-folders");
    const data = await res.json();
    active = data.active || "";
    const paths = data.paths || [];
    if (!paths.length) {
      const empty = document.createElement("div");
      empty.className = "saved-path-empty muted";
      empty.textContent = "No saved paths yet.";
      list.appendChild(empty);
      return;
    }
    paths.forEach((p) => {
      const row = document.createElement("div");
      row.className = "saved-path-item" + (p === active ? " active" : "");
      const label = document.createElement("span");
      label.className = "saved-path-text";
      label.textContent = p;
      label.title = "Click to browse this folder";
      label.addEventListener("click", () => folderBrowseNavigate(p));
      const del = document.createElement("button");
      del.className = "saved-path-del";
      del.type = "button";
      del.title = "Delete this saved path";
      del.textContent = "🗑️";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSavedPath(p);
      });
      row.appendChild(label);
      row.appendChild(del);
      list.appendChild(row);
    });
  } catch (err) {
    // history is optional — ignore network errors silently
  }
}

async function deleteSavedPath(path) {
  if (!confirm("Delete this saved path?\n" + path)) return;
  try {
    const res = await fetch(
      "/config/listen-folder?path=" + encodeURIComponent(path),
      { method: "DELETE" }
    );
    const data = await res.json();
    if (data.ok) {
      // If the deleted path was the active one, fall the input back to the
      // new active folder (or blank if the list is now empty).
      const input = $("#listen-folder");
      if (input && input.value.trim() === path) {
        input.value = data.active || "";
      }
      renderSavedPaths();
    } else {
      log("Failed to delete path: " + (data.detail || ""), "error");
    }
  } catch (err) {
    log("Delete path error: " + err.message, "error");
  }
}

async function folderBrowseNavigate(path) {
  currentBrowsePath = path || "";
  try {
    const url =
      "/browse-folders" +
      (currentBrowsePath ? "?path=" + encodeURIComponent(currentBrowsePath) : "");
    const res = await fetch(url);
    const data = await res.json();
    if (!data.ok) {
      log("Folder browse failed.", "error");
      return;
    }
    renderFolderList(data);
  } catch (err) {
    log("Folder browse error.", "error");
  }
}

function renderFolderList(data) {
  currentBrowseParent = data.parent || null;
  const cur = $("#folder-current");
  cur.textContent = data.path
    ? data.path
    : data.drives && data.drives.length
    ? "Select a drive"
    : "/";
  const list = $("#folder-list");
  list.innerHTML = "";

  if (data.parent) {
    const up = document.createElement("div");
    up.className = "folder-item folder-up";
    up.textContent = "..";
    up.addEventListener("click", () => folderBrowseNavigate(data.parent));
    list.appendChild(up);
  }
  (data.drives || []).forEach((d) => {
    const item = document.createElement("div");
    item.className = "folder-item";
    item.textContent = d;
    item.addEventListener("click", () => folderBrowseNavigate(d));
    list.appendChild(item);
  });
  (data.dirs || []).forEach((d) => {
    const item = document.createElement("div");
    item.className = "folder-item";
    item.textContent = d;
    item.addEventListener("click", () => folderBrowseNavigate(d));
    list.appendChild(item);
  });
}

if (document.getElementById("btn-browse-folder")) {
  document
    .getElementById("btn-browse-folder")
    .addEventListener("click", openFolderBrowser);
}

if (document.getElementById("btn-save-folder")) {
  document.getElementById("btn-save-folder").addEventListener("click", async () => {
    const p = getListenFolder();
    if (!p) {
      log("Enter or choose a folder path first.", "warn");
      return;
    }
    await saveListenFolder(p);
  });
}

if (document.getElementById("folder-select")) {
  document.getElementById("folder-select").addEventListener("click", async () => {
    if (!currentBrowsePath) {
      log("Navigate into a folder first, then select it.", "warn");
      return;
    }
    $("#listen-folder").value = currentBrowsePath;
    $("#folder-modal").classList.add("hidden");
    await saveListenFolder(currentBrowsePath);
  });
}

if (document.getElementById("folder-up")) {
  document.getElementById("folder-up").addEventListener("click", () => {
    if (currentBrowseParent) folderBrowseNavigate(currentBrowseParent);
  });
}

if (document.getElementById("folder-modal-close")) {
  document
    .getElementById("folder-modal-close")
    .addEventListener("click", () =>
      $("#folder-modal").classList.add("hidden")
    );
}
if (document.getElementById("folder-modal-backdrop")) {
  document
    .getElementById("folder-modal-backdrop")
    .addEventListener("click", () =>
      $("#folder-modal").classList.add("hidden")
    );
}

// =================================================================
// Query management
// =================================================================

function getQueriesFromUI() {
  return {
    CLINICAL: $("#query-CLINICAL").value.trim(),
    FE: $("#query-FE").value.trim(),
    CE: $("#query-CE").value.trim(),
  };
}

async function loadQueries() {
  try {
    const res = await fetch("/queries");
    const data = await res.json();
    if (data.ok && data.queries) {
      for (const [type, text] of Object.entries(data.queries)) {
        const el = document.querySelector(`#query-${type}`);
        if (el) el.value = text;
      }
      log("Loaded query definitions from queries/*.txt", "info");
    }
  } catch (err) {
    log("Failed to load queries: " + err.message, "warn");
  }
}

async function saveQueries() {
  const queries = getQueriesFromUI();
  try {
    const res = await fetch("/queries/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ queries }),
    });
    const data = await res.json();
    if (data.ok) {
      log(`Saved queries to disk: ${data.saved.join(", ")}`, "success");
    } else {
      log("Failed to save queries: " + (data.detail || ""), "error");
    }
  } catch (err) {
    log("Save queries error: " + err.message, "error");
  }
}

// Toggle collapse/expand
$("#toggle-queries").addEventListener("click", () => {
  const panel = $("#queries-panel");
  const arrow = $(".toggle-arrow");
  const isHidden = panel.classList.contains("hidden");
  if (isHidden) {
    panel.classList.remove("hidden");
    arrow.classList.add("expanded");
  } else {
    panel.classList.add("hidden");
    arrow.classList.remove("expanded");
  }
});

// Save Queries button
$("#btn-save-queries").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  setButtonLoading(btn, true);
  await saveQueries();
  setButtonLoading(btn, false);
});

// =================================================================
// API helpers
// =================================================================

function getProjectOwner() {
  return $("#project-owner").value.trim();
}

function getTargetFormula() {
  return $("#target-formula").value.trim();
}

function getTopN() {
  if ($("#top-n-all").checked) return -1;   // sentinel: no per-type cap (All)
  const raw = $("#top-n").value.trim();
  if (raw === "") return null;          // empty -> server uses config default (12)
  const n = parseInt(raw, 10);
  return Number.isFinite(n) && n > 0 ? n : null;
}

// Toggle the number input when "All" is checked.
$("#top-n-all").addEventListener("change", (e) => {
  $("#top-n").disabled = e.target.checked;
});

// =================================================================
// Wizard navigation (paginated steps)
// =================================================================
let currentStep = 1;
const TOTAL_STEPS = 3;

function showStep(n) {
  n = Math.max(1, Math.min(TOTAL_STEPS, n));
  currentStep = n;

  // Show only the active step page.
  document.querySelectorAll(".step-page").forEach((p) => {
    p.classList.toggle("active", parseInt(p.dataset.step, 10) === n);
  });

  // Update stepper pills (active + done states).
  document.querySelectorAll(".stepper .step-pill").forEach((pill) => {
    const s = parseInt(pill.dataset.goto, 10);
    pill.classList.toggle("active", s === n);
    pill.classList.toggle("done", s < n);
  });

  // Enable/disable Back (first step) and Next (last step).
  document.querySelectorAll(".step-nav").forEach((nav) => {
    const back = nav.querySelector(".btn-back");
    const next = nav.querySelector(".btn-next");
    if (back) back.disabled = n === 1;
    if (next) next.disabled = n === TOTAL_STEPS;
  });

  // Update the "Step X of N" indicator with the current step's label.
  const indicator = document.getElementById("step-indicator");
  if (indicator) {
    const activePill = document.querySelector(`.stepper .step-pill[data-goto="${n}"] .lbl`);
    const label = activePill ? activePill.textContent : "";
    indicator.innerHTML = `Step <b>${n}</b> of ${TOTAL_STEPS}` + (label ? ` &middot; ${label}` : "");
  }

  window.scrollTo({ top: 0, behavior: "smooth" });
}

document.querySelectorAll(".btn-next").forEach((b) =>
  b.addEventListener("click", () => showStep(currentStep + 1))
);
document.querySelectorAll(".btn-back").forEach((b) =>
  b.addEventListener("click", () => showStep(currentStep - 1))
);
document.querySelectorAll(".btn-home").forEach((b) =>
  b.addEventListener("click", () => showStep(1))
);
document.querySelectorAll(".stepper .step-pill").forEach((pill) =>
  pill.addEventListener("click", () =>
    showStep(parseInt(pill.dataset.goto, 10))
  )
);

// Initialise to step 1 (sync pill/back/next states with the markup).
showStep(1);

// =================================================================
// Step 1 — Scan project folder
// =================================================================

if (document.getElementById("btn-scan")) {
  document.getElementById("btn-scan").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const name = getProjectName();
    if (!name) {
      log("Enter a project name first.", "warn");
      $("#project-name").focus();
      return;
    }
    setButtonLoading(btn, true);
    try {
      // Persist the Listen Folder (if set) so the scan resolves the right
      // base directory — the path itself is never written to the log.
      const lf = getListenFolder();
      if (lf) {
        await saveListenFolder(lf);
      }
      log(`Scanning project folder for "${name}"...`);
      const res = await fetch("/project/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_name: name }),
      });
      const data = await res.json();
      if (!data.ok) {
        log(`Scan failed: ${data.detail || "unknown error"}`, "error");
        return;
      }
      lastScannedPath = data.folder;
      showProjectPath(data.folder, data.project_name);
      const wrap = document.getElementById("scan-result");
      const pathEl = document.getElementById("scan-path");
      const list = document.getElementById("scan-file-list");
      pathEl.textContent = `Found ${data.count} dossier file(s) in: ${data.folder}`;
      list.innerHTML = "";
      (data.files || []).forEach((f) => {
        const item = document.createElement("div");
        item.className = "file-item";
        item.innerHTML = `<span>${f.filename} (${f.size_kb} KB &middot; ${f.type})</span>`;
        list.appendChild(item);
      });
      wrap.classList.remove("hidden");
      if (data.count === 0) {
        log(`No dossier files found in ${data.folder}.`, "warn");
      } else {
        log(`Scanned ${data.count} dossier file(s) in "${name}".`, "success");
      }
    } catch (err) {
      log(`Scan error: ${err.message}`, "error");
    }
    setButtonLoading(btn, false);
  });
}

// =================================================================
// Step 3 — Run (ingest + package)
// =================================================================

$("#btn-run").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  setButtonLoading(btn, true);
  try {
    const name = getProjectName();
    if (!name) {
      log("Enter a project name in Step 1 first.", "warn");
      setButtonLoading(btn, false);
      return;
    }
    showProjectPath(lastScannedPath, name);
    log("Starting full pipeline (ingest + package)...");
    const body = {
      project_id: getProjectId(),
      queries: getQueriesFromUI(),
      top_n: getTopN(),
      project_owner: getProjectOwner(),
      target_formula: getTargetFormula(),
    };
    const res = await fetch("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.ok) {
      log(
        `Pipeline complete! Ingested ${data.pages_ingested} pages. ` +
        `Output: ${data.output_file}`,
        "success"
      );
      showDownloadLink(data.project_id, data.output_file);
      // Output is now rendered inline within Step 3 — no step advance needed.
    } else {
      log(`Pipeline failed: ${data.detail || "unknown error"}`, "error");
    }
  } catch (err) {
    log(`Pipeline error: ${err.message}`, "error");
  }
  setButtonLoading(btn, false);
});

$("#btn-status").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  setButtonLoading(btn, true);
  try {
    const pid = getProjectId();
    const res = await fetch(`/status?project_id=${encodeURIComponent(pid)}`);
    const data = await res.json();
    if (data.ok) {
      log(
        `Status: ${data.pages_indexed} pages indexed (lexical)`
      );
    }
  } catch (err) {
    log(`Status error: ${err.message}`, "error");
  }
  setButtonLoading(btn, false);
});

// Single merged "Reset" (Step 1): SAFE reset only — clears this project's
// index, screenshots and generated PDF. Never deletes the dossier files in
// the project folder (they live in an OneDrive-synced directory and are the
// user's source of truth).
$("#btn-reset").addEventListener("click", async (e) => {
  const name = getProjectName();
  if (!name) {
    log("Enter a project name first.", "warn");
    return;
  }
  if (
    !confirm(
      `Safe reset for "${name}": this clears the page index, screenshots ` +
      `and the generated PDF ONLY. Your dossier files in the project folder ` +
      `are NOT deleted. Continue?`
    )
  ) {
    return;
  }
  const btn = e.currentTarget;
  setButtonLoading(btn, true);
  try {
    const pid = getProjectId();
    const res = await fetch(
      `/clear-reset?project_id=${encodeURIComponent(pid)}`,
      { method: "POST" }
    );
    const data = await res.json();
    if (data.ok) {
      log(
        `Cleared ${data.cleared} derived item(s) (index + screenshots + ` +
        `output PDF). Dossier files untouched.`,
        "warn"
      );
      hideDownloadLink();
      const tbl = document.getElementById("classify-table-wrap");
      if (tbl) tbl.classList.add("hidden");
      classifyResults = [];
    } else {
      log(`Reset failed: ${data.detail || ""}`, "error");
    }
  } catch (err) {
    log(`Reset error: ${err.message}`, "error");
  }
  setButtonLoading(btn, false);
});

// =================================================================
// Download link
// =================================================================

function showDownloadLink(projectId, filename) {
  $("#output-placeholder").classList.add("hidden");
  const resultDiv = $("#output-result");
  resultDiv.classList.remove("hidden");
  const link = $("#download-link");
  link.href = `/download/${encodeURIComponent(projectId)}`;
  link.textContent = `Download ${filename}`;
}

function hideDownloadLink() {
  $("#output-result").classList.add("hidden");
  $("#output-placeholder").classList.remove("hidden");
}

// =================================================================
// Step 2 — Classify (auto-classify + confirm & sort, one button)
// =================================================================

let classifyResults = [];      // last /classify response results

if (document.getElementById("btn-classify")) {
  document.getElementById("btn-classify").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const name = getProjectName();
    if (!name) {
      log("Enter a project name in Step 1 first.", "warn");
      $("#project-name").focus();
      return;
    }
    setButtonLoading(btn, true);
    try {
      log(`Running auto-classification on project folder "${name}"...`);
      const pid = getProjectId();
      const res = await fetch(`/classify?project_id=${encodeURIComponent(pid)}`, {
        method: "POST",
      });
      const data = await res.json();
      if (!data.ok) {
        log(`Classification failed: ${data.detail || ""}`, "error");
        return;
      }
      classifyResults = data.results || [];
      renderClassifyTable(classifyResults);
      // Surface any file the backend could NOT classify (failed pptx/docx
      // conversion, lock file, unsupported type) so the count can never
      // silently diverge from the upload count without an explanation.
      if (data.unprocessed && data.unprocessed.length) {
        const names = data.unprocessed
          .map((u) => `${u.filename} (${u.reason})`)
          .join("; ");
        log(
          `Warning: ${data.unprocessed.length} file(s) could NOT be classified: ${names}.`,
          "warn"
        );
      }
      const auto = classifyResults.filter((r) => r.archived).length;
      const review = classifyResults.length - auto;
      log(
        `Classified ${classifyResults.length} file(s): ` +
        `${auto} auto-archived, ${review} need review. Sorting all into folders...`,
        "success"
      );

      // Step 2 — confirm & sort using the predicted types (already-correct
      // files are skipped server-side, so this is safe to run in one click).
      const decisions = classifyResults.map((r) => ({
        filename: r.filename,
        report_type: r.report_type,
      }));
      const cres = await fetch(
        `/classify/confirm?project_id=${encodeURIComponent(pid)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decisions }),
        }
      );
      const cdata = await cres.json();
      if (cdata.ok) {
        log(
          `Confirmed & sorted ${cdata.count} file(s) into ` +
          `${name}/CLINICAL|FE|CE/.`,
          "success"
        );
        log("Now go to Step 3 (Configure & Run) and click 'Run (Ingest & Package)'.", "info");
      } else {
        log(`Confirm failed: ${cdata.detail || ""}`, "error");
      }
    } catch (err) {
      log(`Classification error: ${err.message}`, "error");
    }
    setButtonLoading(btn, false);
  });
}

function renderClassifyTable(results) {
  const wrap = document.getElementById("classify-table-wrap");
  const tbody = document.getElementById("classify-tbody");
  if (!wrap || !tbody) return;
  tbody.innerHTML = "";
  results.forEach((r, i) => {
    const tr = document.createElement("tr");
    const statusTxt = r.archived ? "Auto-archived" : "Needs review";
    const statusCls = r.archived ? "status-ok" : "status-warn";
    const opts = ["CLINICAL", "FE", "CE", "UNKNOWN"].map((t) =>
      `<option value="${t}" ${t === r.report_type ? "selected" : ""}>${t}</option>`
    ).join("");
    tr.innerHTML = `
      <td>${r.filename}</td>
      <td><select class="type-select" data-idx="${i}">${opts}</select></td>
      <td>${r.confidence}</td>
      <td class="${statusCls}">${statusTxt}</td>`;
    tbody.appendChild(tr);
  });
  wrap.classList.remove("hidden");
}

// --- Type profiles (classification anchors) ---
function getProfilesFromUI() {
  return {
    CLINICAL: document.getElementById("profile-CLINICAL").value.trim(),
    FE: document.getElementById("profile-FE").value.trim(),
    CE: document.getElementById("profile-CE").value.trim(),
  };
}

async function loadProfiles() {
  try {
    const res = await fetch("/classify/profiles");
    const data = await res.json();
    if (data.ok && data.profiles) {
      for (const [type, text] of Object.entries(data.profiles)) {
        const el = document.getElementById(`profile-${type}`);
        if (el) el.value = text;
      }
      log("Loaded type profiles from classify/*.txt", "info");
    }
  } catch (err) {
    log("Failed to load profiles: " + err.message, "warn");
  }
}

async function saveProfiles() {
  const profiles = getProfilesFromUI();
  try {
    const res = await fetch("/classify/profiles/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profiles }),
    });
    const data = await res.json();
    if (data.ok) {
      log(`Saved profiles: ${data.saved.join(", ")}`, "success");
    } else {
      log("Failed to save profiles: " + (data.detail || ""), "error");
    }
  } catch (err) {
    log("Save profiles error: " + err.message, "error");
  }
}

if (document.getElementById("toggle-profiles")) {
  document.getElementById("toggle-profiles").addEventListener("click", () => {
    const panel = document.getElementById("profiles-panel");
    const arrow = document.querySelector("#toggle-profiles .toggle-arrow");
    const isHidden = panel.classList.contains("hidden");
    if (isHidden) {
      panel.classList.remove("hidden");
      if (arrow) arrow.classList.add("expanded");
    } else {
      panel.classList.add("hidden");
      if (arrow) arrow.classList.remove("expanded");
    }
  });
}

if (document.getElementById("btn-save-profiles")) {
  document.getElementById("btn-save-profiles").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    setButtonLoading(btn, true);
    await saveProfiles();
    setButtonLoading(btn, false);
  });
}

// =================================================================

// Load queries + profiles from backend on startup
loadQueries();
loadProfiles();
loadListenFolder();

log("Dossier_Management ready.", "info");
log("Wizard: 3 steps — Configure (project name + scan) → Classify → Run.");
log("Step 1: enter a project name and click 'Scan Folder'; the app reads dossiers from PROJECT_ROOT/<name>/.");
log("Step 2: click 'Classify' to sort dossiers into <name>/CLINICAL|FE|CE; tune Type Profiles if needed.");
log("Step 3: tune queries, Run (Ingest & Package); the PDF downloads inline here.");
