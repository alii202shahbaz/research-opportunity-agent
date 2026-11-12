const $ = (id) => document.getElementById(id);
let csrfToken = "";
let authMode = "login";
let selectedParentPage = null;
let pageCache = [];

function toast(message, error = false) {
  const el = $("toast");
  el.textContent = message;
  el.style.background = error ? "#8f3038" : "#172235";
  el.classList.add("show");
  window.setTimeout(() => el.classList.remove("show"), 3500);
}
function readCookie(name) {
  const prefix = `${name}=`;
  const part = document.cookie.split(";").map(x => x.trim()).find(x => x.startsWith(prefix));
  return part ? decodeURIComponent(part.slice(prefix.length)) : "";
}
async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const method = (options.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    csrfToken = readCookie("__Host-csrf") || readCookie("csrf_token") || csrfToken;
    headers["X-CSRF-Token"] = csrfToken;
  }
  const response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}
async function initCsrf() {
  const data = await api("/api/csrf");
  csrfToken = data.csrf_token || readCookie("__Host-csrf");
}
function setAuthMode(mode) {
  authMode = mode;
  $("loginTab").classList.toggle("active", mode === "login");
  $("registerTab").classList.toggle("active", mode === "register");
  $("authSubmit").textContent = mode === "login" ? "Sign in" : "Create account";
  $("passwordHint").classList.toggle("hidden", mode !== "register");
  $("authPassword").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("authError").textContent = "";
}
function parseCsv(value) { return value.split(/[,;\n]/).map(x => x.trim()).filter(Boolean); }
function renderHourOptions() {
  const select = $("updateHour");
  for (let h = 0; h < 24; h++) {
    const option = document.createElement("option");
    option.value = String(h);
    option.textContent = `${String(h).padStart(2, "0")}:00`;
    if (h === 8) option.selected = true;
    select.appendChild(option);
  }
}
function showDashboard() {
  $("authView").classList.add("hidden");
  $("dashboardView").classList.remove("hidden");
  $("accountNav").classList.remove("hidden");
}
function showAuth() {
  $("authView").classList.remove("hidden");
  $("dashboardView").classList.add("hidden");
  $("accountNav").classList.add("hidden");
}
function fillProfile(p) {
  if (!p) return;
  const configured = (p.interests || []).length > 0;
  $("degree").value = p.degree || "";
  $("university").value = p.university || "";
  $("cgpa").value = p.cgpa ?? "";
  $("cgpaScale").value = String(p.cgpa_scale ?? 4.0);
  $("cgpa").max = String(p.cgpa_scale ?? 4.0);
  $("currentYear").value = p.current_year || "";
  $("currentSemester").value = p.current_semester ?? "";
  $("graduationYear").value = p.graduation_year ?? "";
  $("citizenship").value = p.citizenship || "";
  $("interests").value = (p.interests || []).join(", ");
  $("countries").value = (p.preferred_countries || []).join(", ");
  $("funding").value = p.funding_preference || "funded_only";
  $("intakeSeason").value = p.intake_season || "Summer";
  $("intakeYear").value = p.intake_year || 2027;
  const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  // A newly registered empty profile still has the database's UTC default;
  // prefer the browser's local timezone until the user has saved a profile.
  $("timezone").value = configured ? (p.timezone_name || localTimezone) : localTimezone;
  $("updateHour").value = String(p.update_hour ?? 8);
  $("automation").checked = p.automation_enabled !== false;
  $("deadlineEmailEnabled").checked = p.deadline_email_enabled === true;
  $("deadlineEmails").value = (p.deadline_emails || []).join(", ");
  $("profileSaved").textContent = configured ? "Configured" : "Not configured";
  $("profileSaved").classList.toggle("ok", configured);
  if (p.last_run_summary) $("runSummary").textContent = `${p.last_run_status || "Last run"}: ${p.last_run_summary}`;
}
function renderNotion(n) {
  if (!n || !n.connected) {
    $("notionDisconnected").classList.remove("hidden");
    $("notionConnected").classList.add("hidden");
    $("notionStatus").textContent = "Not connected";
    $("notionStatus").classList.remove("ok");
    return;
  }
  $("notionDisconnected").classList.add("hidden");
  $("notionConnected").classList.remove("hidden");
  $("notionStatus").textContent = "Connected";
  $("notionStatus").classList.add("ok");
  $("workspaceName").textContent = n.workspace_name || "Notion workspace connected";
  if (n.database_ready) {
    $("databaseReady").classList.remove("hidden");
    $("databaseSetup").classList.add("hidden");
    $("databaseLink").href = n.database_url || "https://www.notion.so";
  } else {
    $("databaseReady").classList.add("hidden");
    $("databaseSetup").classList.remove("hidden");
  }
}
async function loadDashboard() {
  const me = await api("/api/me");
  $("userEmail").textContent = me.email;
  fillProfile(me.profile);
  renderNotion(me.notion);
  showDashboard();
  try {
    await loadOpportunities();
  } catch (err) {
    toast("Your workspace loaded, but opportunities could not be refreshed: " + err.message, true);
  }
}
function renderOpportunities(items) {
  const target = $("opportunitiesList");
  $("opportunityCount").textContent = `${items.length} found`;
  if (!items.length) {
    target.className = "empty-state";
    target.innerHTML = '<div class="empty-icon">⌕</div><h3>No opportunities yet</h3><p>Save your research profile and run discovery. Results are only included when there is a relevant source to inspect.</p>';
    return;
  }
  target.className = "opportunity-list";
  target.replaceChildren();
  for (const item of items) {
    const card = document.createElement("article");
    card.className = "opportunity-card";
    const left = document.createElement("div");
    const title = document.createElement("h3"); title.textContent = item.program_name;
    const meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = `${item.institution} · ${item.country} · ${item.intake_year} intake`;
    left.append(title, meta);
    const right = document.createElement("div");
    const link = document.createElement("a"); link.className = "link"; link.href = item.application_url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "Official page ↗";
    const statusLabel = document.createElement("label"); statusLabel.className = "status-select-label"; statusLabel.textContent = "Application status";
    const statusSelect = document.createElement("select"); statusSelect.className = "status-select";
    for (const status of ["Pending", "Waiting", "Done", "Deadline Passed", "Accepted", "Rejected"]) {
      const option = document.createElement("option"); option.value = status; option.textContent = status; statusSelect.append(option);
    }
    statusSelect.value = item.application_status || "Pending";
    statusSelect.addEventListener("change", async () => {
      statusSelect.disabled = true;
      try {
        await api(`/api/opportunities/${encodeURIComponent(item.id)}/application-status`, { method: "PATCH", body: JSON.stringify({ status: statusSelect.value }) });
        toast(`Application status updated: ${statusSelect.value}`);
      } catch (err) { statusSelect.value = item.application_status || "Pending"; toast(err.message, true); }
      finally { statusSelect.disabled = false; }
    });
    right.append(link, statusLabel, statusSelect);
    const details = document.createElement("div"); details.className = "details";
    const tags = [item.funding_type, item.deadline ? `Deadline: ${item.deadline}` : "Deadline unknown", item.verification_status, `Match ${Math.round(item.relevance_score*100)}%`];
    if (item.domains?.length) tags.push(...item.domains);
    for (const text of tags) { const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = text; if (text === "Fully funded" || text === "Official Source") tag.classList.add("green"); if (text === "Needs Review" || text === "Not disclosed") tag.classList.add("amber"); details.append(tag); }
    card.append(left, right, details);
    target.append(card);
  }
}
async function loadOpportunities() {
  const items = await api("/api/opportunities");
  renderOpportunities(items);
}
async function refreshMe() { await loadDashboard(); }

$("loginTab").addEventListener("click", () => setAuthMode("login"));
$("registerTab").addEventListener("click", () => setAuthMode("register"));
$("authForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("authError").textContent = "";
  const payload = { email: $("authEmail").value, password: $("authPassword").value };
  try {
    await api(authMode === "login" ? "/api/login" : "/api/register", { method: "POST", body: JSON.stringify(payload) });
    await initCsrf();
    showDashboard();
    await loadDashboard();
    toast(authMode === "login" ? "Signed in." : "Account created. Complete your profile to begin.");
  } catch (err) { $("authError").textContent = err.message; }
});
$("logoutBtn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", body: "{}" }); } catch (_) {}
  showAuth(); await initCsrf();
});
$("cgpaScale").addEventListener("change", () => {
  $("cgpa").max = $("cgpaScale").value;
  if ($("cgpa").value !== "" && Number($("cgpa").value) > Number($("cgpa").max)) $("cgpa").value = "";
});
$("profileForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    degree: $("degree").value, university: $("university").value,
    cgpa: $("cgpa").value === "" ? null : Number($("cgpa").value),
    cgpa_scale: Number($("cgpaScale").value),
    current_year: $("currentYear").value,
    current_semester: $("currentSemester").value === "" ? null : Number($("currentSemester").value),
    graduation_year: $("graduationYear").value === "" ? null : Number($("graduationYear").value),
    citizenship: $("citizenship").value,
    interests: parseCsv($("interests").value), preferred_countries: parseCsv($("countries").value),
    funding_preference: $("funding").value, intake_year: Number($("intakeYear").value),
    intake_season: $("intakeSeason").value, timezone_name: $("timezone").value,
    update_hour: Number($("updateHour").value), automation_enabled: $("automation").checked,
    deadline_email_enabled: $("deadlineEmailEnabled").checked, deadline_emails: parseCsv($("deadlineEmails").value),
  };
  try {
    const data = await api("/api/profile", { method: "PUT", body: JSON.stringify(payload) });
    fillProfile(data.profile);
    $("profileMessage").textContent = "Saved.";
    toast("Your research profile has been saved.");
  } catch (err) { $("profileMessage").textContent = err.message; toast(err.message, true); }
});
$("connectNotionBtn").addEventListener("click", async () => {
  try { const data = await api("/api/notion/connect", { method: "POST", body: "{}" }); window.location.href = data.authorization_url; }
  catch (err) { toast(err.message, true); }
});
$("searchPagesBtn").addEventListener("click", async () => {
  try {
    const data = await api(`/api/notion/pages?q=${encodeURIComponent($("pageSearch").value)}`);
    pageCache = data.pages || []; selectedParentPage = null; $("createDatabaseBtn").disabled = true;
    const list = $("pageResults"); list.replaceChildren();
    if (!pageCache.length) { const p = document.createElement("p"); p.className = "hint"; p.textContent = "No accessible pages found. Confirm you shared a page during Notion authorization."; list.append(p); return; }
    for (const page of pageCache) {
      const label = document.createElement("label"); label.className = "page-option";
      const radio = document.createElement("input"); radio.type = "radio"; radio.name = "parentPage"; radio.value = page.id;
      radio.addEventListener("change", () => { selectedParentPage = page.id; $("createDatabaseBtn").disabled = false; });
      const text = document.createElement("span"); text.textContent = page.title;
      label.append(radio, text); list.append(label);
    }
  } catch (err) { toast(err.message, true); }
});
$("createDatabaseBtn").addEventListener("click", async () => {
  if (!selectedParentPage) return;
  try {
    const data = await api("/api/notion/database", { method: "POST", body: JSON.stringify({ parent_page_id: selectedParentPage }) });
    toast("Notion database created.");
    await refreshMe();
  } catch (err) { toast(err.message, true); }
});
$("disconnectNotionBtn").addEventListener("click", async () => {
  if (!window.confirm("Disconnect Notion? The agent will stop writing to your workspace.")) return;
  try { const data = await api("/api/notion/disconnect", { method: "POST", body: "{}" }); await refreshMe(); toast(data.warning || "Notion disconnected."); }
  catch (err) { toast(err.message, true); }
});
$("runNowBtn").addEventListener("click", async () => {
  try { const data = await api("/api/run-now", { method: "POST", body: "{}" }); toast(data.message); }
  catch (err) { toast(err.message, true); }
});
$("deleteAccountBtn").addEventListener("click", async () => {
  if (!window.confirm("Permanently delete your account, academic profile, stored Notion credentials and saved opportunities? This cannot be undone.")) return;
  try { await api("/api/account", { method: "DELETE" }); showAuth(); await initCsrf(); toast("Your account data has been deleted."); }
  catch (err) { toast(err.message, true); }
});

(async function boot() {
  renderHourOptions();
  $("timezone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  try { await initCsrf(); await loadDashboard(); }
  catch (_) { showAuth(); }
  finally { $("loadingView").classList.add("hidden"); }
  const params = new URLSearchParams(window.location.search);
  if (params.get("notion") === "connected") { toast("Notion connected. Choose a page to create your database."); history.replaceState({}, "", "/"); try { await refreshMe(); } catch (_) {} }
  if (params.get("notion_error")) { toast("Notion connection did not complete. You can try again.", true); history.replaceState({}, "", "/"); }
})();