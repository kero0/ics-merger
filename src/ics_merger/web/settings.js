const state = {
  config: null,
  providers: {},
  revision: null,
};

const form = document.querySelector("#settings-form");
const saveButton = document.querySelector("#save");
const saveState = document.querySelector("#save-state");
const errorNotice = document.querySelector("#settings-error");
const restartNotice = document.querySelector("#restart-notice");
const rowTemplate = document.querySelector("#calendar-row-template");

function showError(message) {
  errorNotice.textContent = message;
  errorNotice.hidden = !message;
}

function setBusy(busy, message) {
  saveButton.disabled = busy;
  saveState.textContent = message;
}

function setValue(selector, value) {
  document.querySelector(selector).value = value ?? "";
}

function numberValue(selector) {
  return Number(document.querySelector(selector).value);
}

function calendarRow(kind, calendar) {
  const row = rowTemplate.content.firstElementChild.cloneNode(true);
  row.dataset.kind = kind;
  row.querySelector('[data-field="name"]').value = calendar.name ?? "";
  const value = row.querySelector('[data-field="value"]');
  value.value = kind === "remote" ? calendar.url ?? "" : calendar.calendar_id ?? "";
  row.querySelector(".value-label").textContent = kind === "remote" ? "ICS URL" : "Calendar ID";
  row.querySelector('[data-field="ttl_seconds"]').value = calendar.ttl_seconds ?? 3600;
  row.querySelector(".remove-button").addEventListener("click", () => row.remove());
  return row;
}

function renderCalendars(kind, calendars) {
  const container = document.querySelector(`#${kind}-calendars`);
  container.replaceChildren(...calendars.map((calendar) => calendarRow(kind, calendar)));
}

function setProviderState(provider, enabled) {
  const body = document.querySelector(`#${provider}-body`);
  body.classList.toggle("disabled", !enabled);
  body.querySelectorAll("input").forEach((input) => { input.disabled = !enabled; });
  document.querySelector(`#${provider}-enabled`).checked = enabled;
  updateProviderStatus(provider);
}

function updateProviderStatus(provider) {
  const status = state.providers[provider] ?? { configured: false, connected: false };
  const element = document.querySelector(`#${provider}-status`);
  element.className = `provider-status ${status.connected ? "connected" : status.configured ? "configured" : ""}`;
  element.textContent = status.connected
    ? "Connected"
    : status.configured
      ? "Ready to sign in"
      : "Credentials unavailable";
  document.querySelector(`#${provider}-sign-in`).disabled = !status.configured;
  document.querySelector(`#${provider}-sign-out`).disabled = !status.connected;
}

function render() {
  const config = state.config;
  setValue("#future-horizon", config.calendar.future_horizon_days);
  document.querySelector("#include-free-time").checked = config.calendar.include_free_time;
  setValue("#public-base-url", config.server.public_base_url);
  setValue("#host", config.server.host);
  setValue("#port", config.server.port);
  setValue("#env-file", config.env_file);
  setValue("#token-file", config.storage.token_file_path);

  const remote = config.remote_ics;
  document.querySelector("#allow-private-networks").checked = remote.allow_private_networks;
  setValue("#max-response-bytes", remote.max_response_bytes);
  setValue("#connect-timeout", remote.connect_timeout_seconds);
  setValue("#read-timeout", remote.read_timeout_seconds);
  setValue("#write-timeout", remote.write_timeout_seconds);
  setValue("#pool-timeout", remote.pool_timeout_seconds);
  renderCalendars("remote", remote.calendars);

  setProviderState("google", Boolean(config.google));
  renderCalendars("google", config.google?.calendars ?? []);
  setProviderState("microsoft", Boolean(config.microsoft));
  setValue("#microsoft-tenant", config.microsoft?.tenant ?? "common");
  renderCalendars("microsoft", config.microsoft?.calendars ?? []);
}

function readCalendars(kind) {
  return [...document.querySelectorAll(`#${kind}-calendars .calendar-row`)].map((row) => {
    const calendar = {
      name: row.querySelector('[data-field="name"]').value.trim(),
      ttl_seconds: Number(row.querySelector('[data-field="ttl_seconds"]').value),
    };
    const value = row.querySelector('[data-field="value"]').value.trim();
    if (kind === "remote") calendar.url = value;
    else calendar.calendar_id = value;
    return calendar;
  });
}

function collectConfig() {
  const config = structuredClone(state.config);
  config.server = {
    host: document.querySelector("#host").value.trim(),
    port: numberValue("#port"),
    public_base_url: document.querySelector("#public-base-url").value.trim(),
  };
  config.calendar = {
    future_horizon_days: numberValue("#future-horizon"),
    include_free_time: document.querySelector("#include-free-time").checked,
  };
  const envFile = document.querySelector("#env-file").value.trim();
  if (envFile) config.env_file = envFile;
  else delete config.env_file;
  config.storage = { token_file_path: document.querySelector("#token-file").value.trim() };
  config.remote_ics = {
    calendars: readCalendars("remote"),
    allow_private_networks: document.querySelector("#allow-private-networks").checked,
    max_response_bytes: numberValue("#max-response-bytes"),
    connect_timeout_seconds: numberValue("#connect-timeout"),
    read_timeout_seconds: numberValue("#read-timeout"),
    write_timeout_seconds: numberValue("#write-timeout"),
    pool_timeout_seconds: numberValue("#pool-timeout"),
  };
  if (document.querySelector("#google-enabled").checked) {
    config.google = { calendars: readCalendars("google") };
  } else {
    delete config.google;
  }
  if (document.querySelector("#microsoft-enabled").checked) {
    config.microsoft = {
      tenant: document.querySelector("#microsoft-tenant").value.trim(),
      calendars: readCalendars("microsoft"),
    };
  } else {
    delete config.microsoft;
  }
  return config;
}

async function loadSettings() {
  setBusy(true, "Loading");
  showError("");
  try {
    const response = await fetch("/api/config", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`Settings request failed (${response.status})`);
    const payload = await response.json();
    state.config = payload.config;
    state.providers = payload.providers;
    state.revision = payload.revision;
    render();
    setBusy(false, "Up to date");
  } catch (error) {
    showError(error.message);
    setBusy(true, "Unavailable");
  }
}

async function saveSettings(event) {
  event.preventDefault();
  if (!form.reportValidity()) return;
  setBusy(true, "Saving");
  showError("");
  try {
    const response = await fetch("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json", "If-Match": state.revision },
      body: JSON.stringify(collectConfig()),
    });
    const payload = await response.json();
    if (!response.ok) {
      const detail = Array.isArray(payload.detail)
        ? payload.detail.map((item) => `${item.location}: ${item.message}`).join("\n")
        : payload.detail;
      throw new Error(detail || `Save failed (${response.status})`);
    }
    state.config = payload.config;
    state.providers = payload.providers;
    state.revision = payload.revision;
    restartNotice.hidden = !payload.restartRequired;
    render();
    setBusy(false, "Saved");
  } catch (error) {
    showError(error.message);
    setBusy(false, "Not saved");
  }
}

async function refreshProviders() {
  const response = await fetch("/api/auth/providers", { headers: { Accept: "application/json" } });
  if (!response.ok) return;
  state.providers = await response.json();
  updateProviderStatus("google");
  updateProviderStatus("microsoft");
}

function signIn(provider) {
  window.open(`/api/auth/${provider}/login`, `${provider}-calendar-auth`, "popup,width=620,height=760");
}

async function signOut(provider) {
  const response = await fetch(`/api/auth/${provider}/token`, { method: "DELETE" });
  if (!response.ok) {
    const payload = await response.json();
    showError(payload.detail || `Sign out failed (${response.status})`);
    return;
  }
  await refreshProviders();
}

document.querySelectorAll("[data-add]").forEach((button) => {
  button.addEventListener("click", () => {
    const kind = button.dataset.add;
    const calendar = kind === "remote"
      ? { name: "", url: "", ttl_seconds: 3600 }
      : { name: "", calendar_id: "primary", ttl_seconds: 3600 };
    document.querySelector(`#${kind}-calendars`).append(calendarRow(kind, calendar));
  });
});

for (const provider of ["google", "microsoft"]) {
  document.querySelector(`#${provider}-enabled`).addEventListener("change", (event) => {
    setProviderState(provider, event.target.checked);
    const list = document.querySelector(`#${provider}-calendars`);
    if (event.target.checked && !list.children.length) {
      list.append(calendarRow(provider, { name: "", calendar_id: "primary", ttl_seconds: 3600 }));
    }
  });
  document.querySelector(`#${provider}-sign-in`).addEventListener("click", () => signIn(provider));
  document.querySelector(`#${provider}-sign-out`).addEventListener("click", () => signOut(provider));
}

window.addEventListener("message", (event) => {
  if (event.origin === window.location.origin && event.data === "ics-merger-auth-complete") {
    refreshProviders();
  }
});
window.addEventListener("focus", refreshProviders);
form.addEventListener("submit", saveSettings);
loadSettings();
