const csrf = document.querySelector('meta[name="csrf-token"]').content;

const state = {
  source: null,
  target: null,
  sourceOrganizations: [],
  targetOrganizations: [],
  workspaces: [],
  queues: [],
  selectedQueues: new Set(),
  admins: [],
  plan: null,
  templateOverrides: {},
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) headers["X-Easy-PRD2-CSRF"] = csrf;
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, method, headers });
  let body = null;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) {
    const error = new Error(body.detail || `Request failed (${response.status})`);
    error.code = body.code;
    error.context = body.context;
    throw error;
  }
  return body;
}

function notice(message, kind = "error") {
  const element = $("#notice");
  if (!message) { element.className = "notice hidden"; return; }
  element.textContent = message;
  element.className = `notice ${kind === "success" ? "success" : ""}`;
  element.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function busy(button, active, label = "Working…") {
  if (active) {
    button.dataset.label = button.innerHTML;
    button.textContent = label;
    button.disabled = true;
  } else {
    button.innerHTML = button.dataset.label || button.innerHTML;
    button.disabled = false;
  }
}

function showStep(number) {
  notice("");
  $$(".step-panel").forEach((panel) => panel.classList.remove("active"));
  $$(".step").forEach((step) => {
    const value = Number(step.dataset.step);
    step.classList.toggle("active", value === number);
    step.classList.toggle("complete", value < number);
  });
  const names = ["connect", "select", "name", "review", "progress"];
  $(`#panel-${names[number - 1]}`).classList.add("active");
  window.scrollTo({ top: 300, behavior: "smooth" });
}

function option(item, selected = false) {
  return `<option value="${item.id}" ${selected ? "selected" : ""}>${escapeHtml(item.name)}</option>`;
}

async function connectSide(side, form) {
  const button = form.querySelector('button[type="submit"]');
  const data = new FormData(form);
  busy(button, true, "Connecting…");
  notice("");
  try {
    const connection = await api("/api/connections", {
      method: "POST",
      body: { api_base: data.get("api_base"), token: data.get("token") },
    });
    state[side] = connection;
    state[`${side}Organizations`] = await api(`/api/connections/${connection.id}/organizations`);
    form.querySelector('input[name="token"]').value = "";
    form.querySelector(".connected").textContent = `✓ Connected${connection.username ? ` as ${connection.username}` : ""}`;
    form.querySelector(".connected").classList.remove("hidden");
    button.textContent = "Reconnect";
    notice(`${side === "source" ? "Source" : "Target"} connected.`, "success");
    $("#to-select").disabled = !(state.source && state.target);
  } catch (error) {
    notice(error.message);
  } finally {
    busy(button, false);
    if (state[side]) button.textContent = "Reconnect";
  }
}

function populateOrganizations() {
  $("#source-org").innerHTML = state.sourceOrganizations.map((item, index) => option(item, index === 0)).join("");
  $("#target-org").innerHTML = state.targetOrganizations.map((item, index) => option(item, index === 0)).join("");
  loadWorkspaces();
  loadAdmins();
}

async function loadWorkspaces() {
  const orgId = $("#source-org").value;
  if (!orgId) return;
  const select = $("#source-workspace");
  select.innerHTML = "<option>Loading workspaces…</option>";
  try {
    state.workspaces = await api(`/api/connections/${state.source.id}/organizations/${orgId}/workspaces`);
    select.innerHTML = '<option value="">Choose a workspace</option>' + state.workspaces.map(option).join("");
    state.queues = [];
    state.selectedQueues.clear();
    renderQueues();
  } catch (error) { notice(error.message); }
}

async function loadAdmins() {
  const orgId = $("#target-org").value;
  if (!orgId) return;
  try {
    state.admins = await api(`/api/connections/${state.target.id}/organizations/${orgId}/admins`);
    const select = $("#hook-owner");
    select.innerHTML = state.admins.length
      ? state.admins.map((item) => `<option value="${item.id}">${escapeHtml(item.username)}</option>`).join("")
      : '<option value="">Authenticated target user</option>';
    const current = state.target.user_id;
    if (state.admins.some((item) => item.id === current)) select.value = String(current);
  } catch (error) { notice(error.message); }
}

async function loadQueues() {
  const workspaceId = $("#source-workspace").value;
  const orgId = $("#source-org").value;
  if (!workspaceId) { state.queues = []; renderQueues(); return; }
  $("#queue-list").className = "queue-list empty-state";
  $("#queue-list").textContent = "Loading queues…";
  try {
    state.queues = await api(`/api/connections/${state.source.id}/organizations/${orgId}/workspaces/${workspaceId}/queues`);
    state.selectedQueues = new Set(state.queues.map((item) => item.id));
    renderQueues();
  } catch (error) { notice(error.message); }
}

function renderQueues() {
  const container = $("#queue-list");
  if (!state.queues.length) {
    container.className = "queue-list empty-state";
    container.textContent = $("#source-workspace").value ? "No queues found in this workspace." : "Choose a workspace to load queues.";
  } else {
    container.className = "queue-list";
    container.innerHTML = state.queues.map((queue) => `
      <label class="queue-row">
        <input type="checkbox" value="${queue.id}" ${state.selectedQueues.has(queue.id) ? "checked" : ""}>
        <span><strong>${escapeHtml(queue.name)}</strong><small>ID ${queue.id}</small></span>
      </label>`).join("");
    container.querySelectorAll('input[type="checkbox"]').forEach((input) => input.addEventListener("change", () => {
      const id = Number(input.value);
      input.checked ? state.selectedQueues.add(id) : state.selectedQueues.delete(id);
      updateQueueSummary();
    }));
  }
  updateQueueSummary();
}

function updateQueueSummary() {
  const count = state.selectedQueues.size;
  $("#queue-summary").textContent = count ? `${count} queue${count === 1 ? "" : "s"} selected` : "No queues selected";
  $("#to-name").disabled = !count || !$("#target-org").value;
  $("#toggle-queues").textContent = count === state.queues.length && count ? "Clear all" : "Select all";
}

function renderNameFields() {
  const workspace = state.workspaces.find((item) => item.id === Number($("#source-workspace").value));
  const selected = state.queues.filter((item) => state.selectedQueues.has(item.id));
  $("#name-fields").innerHTML = `
    <div class="name-row"><span class="name-kind">Workspace</span><span class="name-source">${escapeHtml(workspace.name)}</span><span class="name-arrow">→</span><input id="workspace-target-name" value="${escapeHtml(workspace.name)}" aria-label="Target workspace name"></div>
    ${selected.map((queue) => `<div class="name-row"><span class="name-kind">Queue</span><span class="name-source">${escapeHtml(queue.name)}</span><span class="name-arrow">→</span><input data-queue-name="${queue.id}" value="${escapeHtml(queue.name)}" aria-label="Target name for ${escapeHtml(queue.name)}"></div>`).join("")}`;
}

function copyPayload() {
  const queueNames = {};
  $$('[data-queue-name]').forEach((input) => { queueNames[input.dataset.queueName] = input.value.trim(); });
  return {
    source_connection_id: state.source.id,
    target_connection_id: state.target.id,
    source_organization_id: Number($("#source-org").value),
    target_organization_id: Number($("#target-org").value),
    workspace_id: Number($("#source-workspace").value),
    queue_ids: [...state.selectedQueues],
    target_workspace_name: $("#workspace-target-name").value.trim(),
    target_queue_names: queueNames,
    target_hook_owner_id: $("#hook-owner").value ? Number($("#hook-owner").value) : null,
    hook_template_overrides: state.templateOverrides,
  };
}

async function resolveHookTemplates(requirements) {
  const dialog = document.createElement("dialog");
  dialog.innerHTML = `<form method="dialog" class="dialog-card"><button class="dialog-close" value="cancel">×</button><p class="kicker">HOOK TEMPLATES</p><h2>Match private hooks</h2><p>Choose the target template PRD2 should use for each private hook.</p><div class="template-fields">${requirements.map((req) => `<label>${escapeHtml(req.hook_name)}<select data-hook-template="${req.hook_id}"><option value="">Choose a target template</option>${req.options.map((item) => `<option value="${escapeHtml(item.url)}">${escapeHtml(item.name)}</option>`).join("")}</select></label>`).join("")}</div><div class="dialog-actions"><button value="cancel" class="secondary">Cancel</button><button value="default" class="primary">Continue preview</button></div></form>`;
  document.body.append(dialog);
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => {
    if (dialog.returnValue === "default") {
      const values = {};
      dialog.querySelectorAll("[data-hook-template]").forEach((select) => { if (select.value) values[select.dataset.hookTemplate] = select.value; });
      if (Object.keys(values).length !== requirements.length) {
        notice("Choose a target template for every private hook.");
        resolve(false);
      } else {
        Object.assign(state.templateOverrides, values);
        resolve(true);
      }
    } else resolve(false);
    dialog.remove();
  }, { once: true }));
}

async function buildPlan() {
  const button = $("#build-plan");
  const payload = copyPayload();
  if (!payload.target_workspace_name || Object.values(payload.target_queue_names).some((name) => !name)) {
    notice("Every target copy needs a name."); return;
  }
  busy(button, true, "Building preview…");
  notice("");
  try {
    state.plan = await api("/api/plans", { method: "POST", body: payload });
    renderPlan();
    showStep(4);
  } catch (error) {
    if (error.code === "hook_templates_required") {
      const resolved = await resolveHookTemplates(error.context.requirements);
      if (resolved) { busy(button, false); return buildPlan(); }
    } else notice(error.message);
  } finally { busy(button, false); }
}

function warningBlock(title, items) {
  if (!items.length) return "";
  return `<div class="warning-block"><strong>${escapeHtml(title)}</strong><ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`;
}

function renderPlan() {
  const plan = state.plan;
  $("#review-target").textContent = `PRD2 will create the following in ${plan.target_organization.name}.`;
  $("#plan-stats").innerHTML = Object.entries(plan.counts).map(([type, count]) => `<div class="stat"><b>${count}</b> ${escapeHtml(type)}${count === 1 ? "" : "s"}</div>`).join("");
  $("#plan-warnings").innerHTML = warningBlock("Warnings", [...plan.warnings, ...plan.duplicate_names]) + warningBlock("After the copy", plan.manual_follow_ups);
  $("#plan-items").innerHTML = plan.items.map((item) => `<tr><td><span class="type-chip">${escapeHtml(item.type)}</span></td><td>${escapeHtml(item.source_name)}<br><small>ID ${item.source_id}</small></td><td><b>${escapeHtml(item.target_name)}</b></td><td>${item.dependencies.length ? item.dependencies.map(escapeHtml).join(", ") : "—"}</td></tr>`).join("");
  $("#execute-plan").textContent = `Create copy in ${plan.target_organization.name}`;
}

async function executePlan() {
  const button = $("#execute-plan");
  busy(button, true, "Starting…");
  try {
    const job = await api(`/api/plans/${state.plan.id}/execute`, { method: "POST" });
    showStep(5);
    pollJob(job.id);
  } catch (error) { notice(error.message); busy(button, false); }
}

async function pollJob(jobId, rollback = false) {
  const bar = $("#progress-bar");
  let progress = 30;
  const timer = setInterval(() => { progress = Math.min(progress + 5, 88); bar.style.width = `${progress}%`; }, 900);
  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 900));
    let job;
    try { job = await api(`/api/jobs/${jobId}`); } catch (error) { clearInterval(timer); notice(error.message); return; }
    $("#progress-phase").textContent = job.phase;
    if (["succeeded", "partial", "failed"].includes(job.status)) {
      clearInterval(timer); bar.style.width = "100%";
      const run = await api(`/api/history/${job.run_id}`);
      renderResult(run, rollback);
      loadHistory();
      return;
    }
  }
}

function renderResult(run, rollback) {
  const success = ["succeeded", "rolled_back"].includes(run.status);
  $("#progress-icon").classList.add(success ? "done" : "failed");
  $("#progress-title").textContent = rollback
    ? (success ? "Rollback complete" : "Rollback needs attention")
    : (success ? "Your copy is ready" : run.status === "partial" ? "Copy partially completed" : "Copy could not be completed");
  $("#progress-phase").textContent = `${run.created_objects.length} object${run.created_objects.length === 1 ? "" : "s"} created`;
  const error = run.error || run.rollback_error;
  $("#result-details").innerHTML = `<div class="result-card"><b>${escapeHtml(run.workspace_name)}</b><br>${escapeHtml(run.source_organization_name)} → ${escapeHtml(run.target_organization_name)}${error ? `<br><br><b>Attention:</b> ${escapeHtml(error)}` : ""}${run.warnings.length ? `<br><br><b>Follow-up:</b><ul>${run.warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}</div>`;
  $("#new-copy").classList.remove("hidden");
}

async function loadHistory() {
  try {
    const runs = await api("/api/history");
    $("#history-count").textContent = runs.length;
    const container = $("#history-list");
    if (!runs.length) { container.innerHTML = '<div class="empty-history">No runs yet. Your completed copies will appear here.</div>'; return; }
    container.innerHTML = runs.map((run) => `<article class="history-card"><div class="history-main"><div><h3>${escapeHtml(run.workspace_name)}</h3><div class="history-meta"><span class="status ${run.status}">${escapeHtml(run.status.replaceAll("_", " "))}</span>${escapeHtml(run.source_organization_name)} → ${escapeHtml(run.target_organization_name)} · ${new Date(run.created_at).toLocaleString()}</div><div class="history-meta" style="margin-top:8px">${run.created_objects.length} created object${run.created_objects.length === 1 ? "" : "s"}${run.error ? ` · ${escapeHtml(run.error)}` : ""}</div></div><div class="history-actions">${run.rollback_eligible ? `<button class="secondary rollback" data-run="${run.id}">Rollback</button>` : ""}<button class="secondary delete-run" data-run="${run.id}">Delete record</button></div></div></article>`).join("");
    $$(".delete-run").forEach((button) => button.addEventListener("click", () => deleteRun(button.dataset.run)));
    $$(".rollback").forEach((button) => button.addEventListener("click", () => showRollback(button.dataset.run)));
  } catch (error) { notice(error.message); }
}

async function deleteRun(runId) {
  try { await api(`/api/history/${runId}`, { method: "DELETE" }); loadHistory(); }
  catch (error) { notice(error.message); }
}

async function showRollback(runId) {
  if (!state.target) { notice("Reconnect the original target environment before rolling back."); switchView("copy"); return; }
  try {
    const plan = await api(`/api/history/${runId}/rollback-plan`, { method: "POST", body: { target_connection_id: state.target.id } });
    const dialog = $("#rollback-dialog");
    dialog.dataset.run = runId;
    $("#rollback-description").textContent = `This will permanently delete ${plan.objects.length} object${plan.objects.length === 1 ? "" : "s"} created by this run from ${plan.target_organization_name}.`;
    $("#rollback-objects").innerHTML = plan.objects.map((item) => `<div class="rollback-object"><span>${escapeHtml(item.type)} · ${escapeHtml(item.name || item.id)}</span><small>ID ${item.id}</small></div>`).join("");
    $("#rollback-blocked").innerHTML = warningBlock("Rollback blocked: these objects changed after deployment", plan.blocked_changes.map((item) => `${item.type} ${item.name || item.id}`));
    $("#confirm-rollback").disabled = Boolean(plan.blocked_changes.length) || !plan.objects.length;
    dialog.showModal();
  } catch (error) { notice(error.message); }
}

async function confirmRollback(event) {
  event.preventDefault();
  const dialog = $("#rollback-dialog");
  const button = $("#confirm-rollback");
  busy(button, true, "Starting rollback…");
  try {
    const job = await api(`/api/history/${dialog.dataset.run}/rollback`, { method: "POST", body: { target_connection_id: state.target.id } });
    dialog.close();
    switchView("copy"); showStep(5);
    $("#progress-title").textContent = "Rolling back copied objects…";
    pollJob(job.id, true);
  } catch (error) { notice(error.message); }
  finally { busy(button, false); }
}

function switchView(view) {
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `${view}-view`));
  $$(".tab").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  if (view === "history") loadHistory();
}

$("#source-form").addEventListener("submit", (event) => { event.preventDefault(); connectSide("source", event.currentTarget); });
$("#target-form").addEventListener("submit", (event) => { event.preventDefault(); connectSide("target", event.currentTarget); });
$$('.reveal').forEach((button) => button.addEventListener("click", () => {
  const input = button.parentElement.querySelector("input"); input.type = input.type === "password" ? "text" : "password"; button.textContent = input.type === "password" ? "Show" : "Hide";
}));
$("#to-select").addEventListener("click", () => { populateOrganizations(); showStep(2); });
$("#source-org").addEventListener("change", loadWorkspaces);
$("#target-org").addEventListener("change", () => { loadAdmins(); updateQueueSummary(); });
$("#source-workspace").addEventListener("change", loadQueues);
$("#toggle-queues").addEventListener("click", () => { state.selectedQueues = state.selectedQueues.size === state.queues.length ? new Set() : new Set(state.queues.map((item) => item.id)); renderQueues(); });
$("#to-name").addEventListener("click", () => { renderNameFields(); showStep(3); });
$("#build-plan").addEventListener("click", buildPlan);
$("#execute-plan").addEventListener("click", executePlan);
$("#new-copy").addEventListener("click", () => window.location.reload());
$$('[data-back]').forEach((button) => button.addEventListener("click", () => showStep(Number(button.dataset.back))));
$$('.tab').forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
$("#clear-history").addEventListener("click", async () => { try { await api("/api/history", { method: "DELETE" }); loadHistory(); } catch (error) { notice(error.message); } });
$("#confirm-rollback").addEventListener("click", confirmRollback);

loadHistory();

