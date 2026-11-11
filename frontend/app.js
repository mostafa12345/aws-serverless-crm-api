const state = {
  mode: "local",
  apiBase: "",
  authMode: "signin",
  token: sessionStorage.getItem("crm_id_token") || "",
  customers: [],
};

const els = {
  badgeEnv: document.getElementById("badge-env"),
  badgeCache: document.getElementById("badge-cache"),
  badgeTrace: document.getElementById("badge-trace"),
  btnLogout: document.getElementById("btn-logout"),
  authSection: document.getElementById("auth-section"),
  appSection: document.getElementById("app-section"),
  tabSignin: document.getElementById("tab-signin"),
  tabSignup: document.getElementById("tab-signup"),
  groupName: document.getElementById("group-name"),
  authForm: document.getElementById("auth-form"),
  authName: document.getElementById("auth-name"),
  authEmail: document.getElementById("auth-email"),
  authPassword: document.getElementById("auth-password"),
  authSubmit: document.getElementById("auth-submit"),
  authMessage: document.getElementById("auth-message"),
  hostedUiBox: document.getElementById("hosted-ui-box"),
  hostedUiLink: document.getElementById("hosted-ui-link"),
  customerForm: document.getElementById("customer-form"),
  formTitle: document.getElementById("form-title"),
  custId: document.getElementById("cust-id"),
  custName: document.getElementById("cust-name"),
  custEmail: document.getElementById("cust-email"),
  custCompany: document.getElementById("cust-company"),
  custTier: document.getElementById("cust-tier"),
  custStatus: document.getElementById("cust-status"),
  custPhone: document.getElementById("cust-phone"),
  cancelEditBtn: document.getElementById("cancel-edit-btn"),
  filterTier: document.getElementById("filter-tier"),
  customersTbody: document.getElementById("customers-tbody"),
  auditList: document.getElementById("audit-list"),
  refreshCustomersBtn: document.getElementById("refresh-customers-btn"),
  refreshAuditBtn: document.getElementById("refresh-audit-btn"),
};

function showAuthMsg(msg, isOk = false) {
  els.authMessage.textContent = msg;
  els.authMessage.classList.remove("hidden", "ok");
  if (isOk) els.authMessage.classList.add("ok");
}

function updateTelemetry(headers) {
  const xCache = headers.get("X-Cache") || "Miss";
  const traceId = headers.get("X-Amzn-Trace-Id") || "--";
  els.badgeCache.textContent = `X-Cache: ${xCache}`;
  els.badgeCache.classList.toggle("badge-hit", xCache.toLowerCase().includes("hit"));
  els.badgeTrace.textContent = `X-Ray: ${traceId.slice(0, 22)}...`;
  els.badgeTrace.title = traceId;
}

async function apiFetch(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  if (state.token) {
    headers.Authorization = `Bearer ${state.token}`;
  }
  const resp = await fetch(`${state.apiBase}${path}`, { ...options, headers });
  updateTelemetry(resp.headers);
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 401 && state.token) {
    logout();
  }
  if (!resp.ok) {
    throw new Error(data.error || data.message || `HTTP ${resp.status}`);
  }
  return data;
}

function setAuthTab(mode) {
  state.authMode = mode;
  const isSignup = mode === "signup";
  els.tabSignin.classList.toggle("active", !isSignup);
  els.tabSignup.classList.toggle("active", isSignup);
  els.groupName.classList.toggle("hidden", !isSignup);
  els.authSubmit.textContent = isSignup ? "Create Cognito Account" : "Sign In with Cognito";
  els.authMessage.classList.add("hidden");
}

function showDashboard() {
  els.authSection.classList.add("hidden");
  els.appSection.classList.remove("hidden");
  els.btnLogout.classList.remove("hidden");
  loadCustomers();
  loadAuditLogs();
}

function logout() {
  state.token = "";
  sessionStorage.removeItem("crm_id_token");
  els.appSection.classList.add("hidden");
  els.btnLogout.classList.add("hidden");
  els.authSection.classList.remove("hidden");
}

function renderCustomers(items) {
  els.customersTbody.replaceChildren();
  if (!items.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.className = "empty";
    td.textContent = "No customer records found.";
    tr.appendChild(td);
    els.customersTbody.appendChild(tr);
    return;
  }

  items.forEach((cust) => {
    const tr = document.createElement("tr");

    const tdName = document.createElement("td");
    tdName.textContent = cust.name;

    const tdEmail = document.createElement("td");
    tdEmail.textContent = cust.email;

    const tdCompany = document.createElement("td");
    tdCompany.textContent = cust.company;

    const tdTier = document.createElement("td");
    const tierPill = document.createElement("span");
    tierPill.className = `pill tier-${cust.tier}`;
    tierPill.textContent = cust.tier;
    tdTier.appendChild(tierPill);

    const tdStatus = document.createElement("td");
    const statusPill = document.createElement("span");
    statusPill.className = `pill status-${cust.status}`;
    statusPill.textContent = cust.status;
    tdStatus.appendChild(statusPill);

    const tdActions = document.createElement("td");
    tdActions.className = "actions";

    const editBtn = document.createElement("button");
    editBtn.className = "btn btn-small";
    editBtn.textContent = "Edit";
    editBtn.onclick = () => startEdit(cust);

    const delBtn = document.createElement("button");
    delBtn.className = "btn btn-small btn-danger";
    delBtn.textContent = "Delete";
    delBtn.onclick = () => deleteCustomer(cust.customerId);

    tdActions.append(editBtn, delBtn);
    tr.append(tdName, tdEmail, tdCompany, tdTier, tdStatus, tdActions);
    els.customersTbody.appendChild(tr);
  });
}

function renderAuditLogs(events) {
  els.auditList.replaceChildren();
  if (!events.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "No stream events recorded yet.";
    els.auditList.appendChild(li);
    return;
  }
  events.forEach((evt) => {
    const li = document.createElement("li");
    const meta = document.createElement("div");
    meta.className = "audit-meta";
    const evType = document.createElement("strong");
    evType.textContent = evt.eventName;
    const evTime = document.createElement("span");
    evTime.textContent = evt.timestamp;
    meta.append(evType, evTime);

    const desc = document.createElement("div");
    desc.textContent = evt.summary;
    li.append(meta, desc);
    els.auditList.appendChild(li);
  });
}

async function loadCustomers() {
  const tier = els.filterTier.value;
  const query = tier ? `?tier=${encodeURIComponent(tier)}` : "";
  try {
    const data = await apiFetch(`/api/customers${query}`);
    state.customers = data.customers || [];
    renderCustomers(state.customers);
  } catch (err) {
    alert(`Failed to load customers: ${err.message}`);
  }
}

async function loadAuditLogs() {
  try {
    const data = await apiFetch("/api/audit?limit=15");
    renderAuditLogs(data.auditEvents || []);
  } catch (err) {
    console.warn("Audit fetch skipped:", err.message);
  }
}

function startEdit(cust) {
  els.custId.value = cust.customerId;
  els.custName.value = cust.name;
  els.custEmail.value = cust.email;
  els.custEmail.disabled = true;
  els.custCompany.value = cust.company;
  els.custTier.value = cust.tier;
  els.custStatus.value = cust.status;
  els.custPhone.value = cust.phone || "";
  els.formTitle.textContent = "Update Customer Record";
  els.cancelEditBtn.classList.remove("hidden");
}

function resetCustomerForm() {
  els.customerForm.reset();
  els.custId.value = "";
  els.custEmail.disabled = false;
  els.formTitle.textContent = "Add Customer Record";
  els.cancelEditBtn.classList.add("hidden");
}

async function deleteCustomer(customerId) {
  try {
    await apiFetch(`/api/customers/${encodeURIComponent(customerId)}`, {
      method: "DELETE",
    });
    await Promise.all([loadCustomers(), loadAuditLogs()]);
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
}

els.tabSignin.onclick = () => setAuthTab("signin");
els.tabSignup.onclick = () => setAuthTab("signup");
els.btnLogout.onclick = logout;
els.cancelEditBtn.onclick = resetCustomerForm;
els.filterTier.onchange = loadCustomers;
els.refreshCustomersBtn.onclick = loadCustomers;
els.refreshAuditBtn.onclick = loadAuditLogs;

els.authForm.onsubmit = async (e) => {
  e.preventDefault();
  const email = els.authEmail.value.trim();
  const password = els.authPassword.value;
  const name = els.authName.value.trim();
  try {
    if (state.authMode === "signup") {
      await apiFetch("/auth/signup", {
        method: "POST",
        body: JSON.stringify({ email, password, name }),
      });
      setAuthTab("signin");
      showAuthMsg("Account confirmed in Cognito! You can now sign in.", true);
      return;
    }
    const res = await apiFetch("/auth/signin", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    state.token = res.idToken;
    sessionStorage.setItem("crm_id_token", state.token);
    showDashboard();
  } catch (err) {
    showAuthMsg(err.message);
  }
};

els.customerForm.onsubmit = async (e) => {
  e.preventDefault();
  const id = els.custId.value;
  const payload = {
    name: els.custName.value.trim(),
    email: els.custEmail.value.trim(),
    company: els.custCompany.value.trim(),
    tier: els.custTier.value,
    status: els.custStatus.value,
    phone: els.custPhone.value.trim(),
  };

  try {
    if (id) {
      delete payload.email;
      await apiFetch(`/api/customers/${encodeURIComponent(id)}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
    } else {
      await apiFetch("/api/customers", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    }
    resetCustomerForm();
    await Promise.all([loadCustomers(), loadAuditLogs()]);
  } catch (err) {
    alert(`Save failed: ${err.message}`);
  }
};

async function bootstrap() {
  if (window.location.hash.includes("id_token=")) {
    const hashParams = new URLSearchParams(window.location.hash.slice(1));
    const token = hashParams.get("id_token");
    if (token) {
      state.token = token;
      sessionStorage.setItem("crm_id_token", token);
      window.history.replaceState({}, document.title, window.location.pathname);
    }
  }

  try {
    const cfgResp = await fetch("/config.json");
    if (cfgResp.ok) {
      const cfg = await cfgResp.json();
      state.mode = cfg.mode || "local";
      state.apiBase = cfg.apiBase || "";
      els.badgeEnv.textContent = `Mode: ${state.mode.toUpperCase()} (${cfg.region || "us-east-1"})`;

      if (cfg.mode === "cognito" && cfg.hostedUiUrl) {
        els.authForm.classList.add("hidden");
        els.hostedUiBox.classList.remove("hidden");
        els.hostedUiLink.href = cfg.hostedUiUrl;
      }
    }
  } catch {
    els.badgeEnv.textContent = "Mode: LOCAL";
  }

  if (state.token) {
    showDashboard();
  }
}

bootstrap();
