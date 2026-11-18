"use strict";

const $ = (selector) => document.querySelector(selector);
const state = { session: null, bundle: null, rollback: null, rollbackRequest: null, waveRequest: null };
const notice = $("#notice");

function showError(error) {
  notice.textContent = (typeof error === "string" ? error : error?.message) || "The request failed.";
  notice.classList.remove("hidden");
}

function clearError() {
  notice.classList.add("hidden");
  notice.textContent = "";
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.method === "POST") {
    headers["Content-Type"] = "application/json";
    headers["X-Console-CSRF"] = state.session.csrf;
  }
  const response = await fetch(`/console/api${path}`, {
    method: options.method || "GET",
    headers,
    credentials: "same-origin",
    body: options.method === "POST" ? JSON.stringify(options.body || {}) : undefined,
  });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof data?.detail === "string" ? data.detail : `Request failed (${response.status}).`;
    throw new Error(detail);
  }
  return data;
}

async function perform(action) {
  clearError();
  try { await action(); } catch (error) { showError(error); }
}

function formValues(form) {
  return Object.fromEntries([...new FormData(form)].map(([key, value]) => [key, String(value).trim()]));
}

function display(target, data) {
  target.textContent = JSON.stringify(data, null, 2);
  target.classList.remove("hidden");
}

function addMessage(role, text) {
  const welcome = $(".welcome");
  if (welcome) welcome.remove();
  const card = document.createElement("article");
  card.className = `message ${role}`;
  const heading = document.createElement("strong");
  heading.textContent = role === "user" ? "You" : "Query agent";
  card.append(heading);
  if (text) {
    const body = document.createElement("p");
    body.textContent = text;
    card.append(body);
  }
  $("#messages").append(card);
  card.scrollIntoView({ block: "nearest" });
  return card;
}

function renderAnswer(card, answer) {
  const title = document.createElement("p");
  title.textContent = `${answer.intent.replaceAll("_", " ")} · Observed ${answer.observed_at}`;
  card.append(title);
  const result = document.createElement("pre");
  result.textContent = JSON.stringify(answer.result, null, 2);
  card.append(result);
  for (const [label, items] of [["Evidence", answer.evidence], ["Limitations", answer.limitations]]) {
    if (!Array.isArray(items) || !items.length) continue;
    const heading = document.createElement("strong");
    heading.textContent = label;
    card.append(heading);
    const list = document.createElement("ul");
    for (const item of items) {
      const line = document.createElement("li");
      try {
        const url = new URL(item);
        if (url.protocol === "https:") {
          const link = document.createElement("a");
          link.href = url.href;
          link.rel = "noopener noreferrer";
          link.target = "_blank";
          link.textContent = item;
          line.append(link);
        } else line.textContent = item;
      } catch {
        line.textContent = item;
      }
      list.append(line);
    }
    card.append(list);
  }
  card.scrollIntoView({ block: "nearest" });
}

function setBundle(bundle) {
  state.bundle = bundle;
  $("#bundle-id").value = bundle.fingerprint;
  display($("#bundle-result"), bundle);
  $("#bundle-approve").disabled = !state.session.can_approve || bundle.status !== "draft";
}

function setRollback(plan, preview) {
  state.rollback = plan;
  $("#rollback-id").value = plan.id;
  display($("#rollback-result"), { plan, preview });
  $("#rollback-approve").disabled = plan.status !== "draft" || !state.session.can_approve;
  $("#rollback-execute").disabled = plan.status !== "approved" || !state.session.can_approve || !state.session.write_enabled;
}

async function loadRollback(id) {
  state.rollback = null;
  $("#rollback-approve").disabled = true;
  $("#rollback-execute").disabled = true;
  const plan = await api(`/rollbacks/${encodeURIComponent(id)}`);
  const preview = await api(`/rollbacks/${encodeURIComponent(id)}/preview`);
  setRollback(plan, preview);
}

document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.remove("active"));
  document.querySelectorAll(".panel").forEach((panel) => panel.classList.add("hidden"));
  item.classList.add("active");
  document.getElementById(item.dataset.panel).classList.remove("hidden");
  clearError();
}));

document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => {
  $("#question").value = button.dataset.example;
  $("#question").focus();
}));

$("#query-form").addEventListener("submit", (event) => {
  event.preventDefault();
  perform(async () => {
    const form = event.currentTarget;
    const values = formValues(form);
    const payload = Object.fromEntries(Object.entries(values).filter(([, value]) => value));
    const submit = form.querySelector('button[type="submit"]');
    addMessage("user", payload.question);
    form.elements.question.value = "";
    submit.disabled = true;
    try {
      const answer = await api("/query", { method: "POST", body: payload });
      renderAnswer(addMessage("assistant"), answer);
    } catch (error) {
      addMessage("assistant", error.message);
    } finally {
      submit.disabled = !state.session.query_enabled;
    }
  });
});

$("#bundle-id").addEventListener("input", () => {
  state.bundle = null;
  $("#bundle-approve").disabled = true;
});
$("#bundle-load").addEventListener("submit", (event) => {
  event.preventDefault();
  perform(async () => setBundle(await api(`/bundles/${encodeURIComponent($("#bundle-id").value.trim())}`)));
});
$("#bundle-create").addEventListener("submit", (event) => {
  event.preventDefault();
  perform(async () => {
    const data = formValues(event.currentTarget);
    data.job_id = Number(data.job_id);
    setBundle(await api("/bundles", { method: "POST", body: data }));
  });
});
$("#bundle-approve").addEventListener("click", () => perform(async () => {
  if (!state.bundle || state.bundle.fingerprint !== $("#bundle-id").value.trim()) throw new Error("Load and review a bundle first.");
  if (!window.confirm("Approve this bundle? This records approval but does not deploy.")) return;
  setBundle(await api(`/bundles/${encodeURIComponent(state.bundle.fingerprint)}/approval`, { method: "POST" }));
}));
$("#drift-form").addEventListener("submit", (event) => {
  event.preventDefault();
  perform(async () => {
    if (!state.bundle || state.bundle.fingerprint !== $("#bundle-id").value.trim()) throw new Error("Load a bundle first.");
    const application = formValues(event.currentTarget).application;
    display($("#drift-result"), await api(`/bundles/${encodeURIComponent(state.bundle.fingerprint)}/drift/${encodeURIComponent(application)}`));
  });
});

$("#rollback-id").addEventListener("input", () => {
  state.rollback = null;
  $("#rollback-approve").disabled = true;
  $("#rollback-execute").disabled = true;
});
$("#rollback-create").addEventListener("input", () => { state.rollbackRequest = null; });
$("#rollback-create").addEventListener("submit", (event) => {
  event.preventDefault();
  perform(async () => {
    const data = formValues(event.currentTarget);
    state.rollbackRequest ||= crypto.randomUUID();
    data.request_id = state.rollbackRequest;
    const plan = await api("/rollbacks", { method: "POST", body: data });
    state.rollbackRequest = null;
    $("#rollback-id").value = plan.id;
    await loadRollback(plan.id);
  });
});
$("#rollback-load").addEventListener("submit", (event) => {
  event.preventDefault();
  perform(async () => loadRollback($("#rollback-id").value.trim()));
});
$("#rollback-approve").addEventListener("click", () => perform(async () => {
  const plan = state.rollback;
  if (!plan || plan.id !== $("#rollback-id").value.trim()) throw new Error("Load and review a plan first.");
  if (!window.confirm("Approve the displayed rollback plan and fingerprint?")) return;
  await api(`/rollbacks/${encodeURIComponent(plan.id)}/approval`, { method: "POST", body: { fingerprint: plan.fingerprint } });
  await loadRollback(plan.id);
}));
$("#rollback-execute").addEventListener("click", () => perform(async () => {
  const plan = state.rollback;
  if (!plan || plan.id !== $("#rollback-id").value.trim()) throw new Error("Load and review a plan first.");
  if (!window.confirm("Create a GitLab branch and draft merge request for this approved rollback?")) return;
  const result = await api(`/rollbacks/${encodeURIComponent(plan.id)}/execute`, { method: "POST" });
  state.rollback = result;
  display($("#rollback-result"), { plan: result });
  $("#rollback-approve").disabled = true;
  $("#rollback-execute").disabled = true;
}));

$("#wave-form").addEventListener("input", () => { state.waveRequest = null; });
$("#wave-form").addEventListener("submit", (event) => {
  event.preventDefault();
  perform(async () => {
    if (!window.confirm("Record this wave approval? No deployment will be triggered.")) return;
    const data = formValues(event.currentTarget);
    data.project_id = Number(data.project_id);
    state.waveRequest ||= crypto.randomUUID();
    data.request_id = state.waveRequest;
    const result = await api("/wave-approvals", { method: "POST", body: data });
    state.waveRequest = null;
    $("#wave-status").elements.event_id.value = result.event_id;
    display($("#wave-result"), result);
  });
});
$("#wave-status").addEventListener("submit", (event) => {
  event.preventDefault();
  perform(async () => display($("#wave-result"), await api(`/wave-approvals/${encodeURIComponent(formValues(event.currentTarget).event_id)}`)));
});
$("#logout").addEventListener("click", () => perform(async () => {
  await api("/logout", { method: "POST" });
  window.location.assign("/console");
}));

perform(async () => {
  try {
    state.session = await api("/session");
  } catch (error) {
    if (error.message !== "Sign in to continue" && error.message !== "Session expired or access revoked") throw error;
    $("#signin").classList.remove("hidden");
    return;
  }
  $("#identity").textContent = `Entra ID · ${state.session.actor}`;
  $("#login-link").classList.add("hidden");
  $("#logout").classList.remove("hidden");
  $("#workspace").classList.remove("hidden");
  document.querySelectorAll("[data-approver]").forEach((button) => { button.disabled = !state.session.can_approve; });
  if (!state.session.query_enabled) {
    $("#query-form button[type=submit]").disabled = true;
    showError("Query service is disabled. Configure it before asking questions.");
  } else if (!state.session.can_approve) {
    showError("Signed in with read-only access. Approval actions require an authorized approver.");
  }
});
