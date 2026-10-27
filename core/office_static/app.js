"use strict";
(() => {
  const root = document.getElementById("app");
  // One-click login link: /office#key=... stores the key for this tab and removes it from the
  // address bar. A URL fragment is never sent to the server.
  const linkKey = (() => {
    const m = location.hash.match(/^#key=([A-Za-z0-9._~%-]+)$/);
    if (!m) return "";
    try { return decodeURIComponent(m[1]); } catch (e) { return ""; }
  })();
  if (linkKey) {
    sessionStorage.setItem("ragleap_key", linkKey);
    history.replaceState(null, "", location.pathname + location.search);
  }
  let key = sessionStorage.getItem("ragleap_key") || "";
  let tab = "overview";
  let timer = null;
  let armed = false;
  let view = null;
  let stamp = null;
  let me = null;
  let newKey = null;
  let userFormEl = null;
  let userListEl = null;
  let userBannerEl = null;
  let userMsgEl = null;
  const tabButtons = {};
  const expanded = new Set();
  const TABS = [["setup", "Setup"], ["overview", "Overview"], ["approvals", "Approvals"], ["employees", "Employees"], ["tasks", "Tasks"], ["documents", "Documents"], ["files", "Files"], ["proactive", "Proactive"], ["sources", "Connectors"], ["runs", "Agent runs"], ["log", "Activity"], ["settings", "Settings"], ["audit", "Audit"], ["users", "Users"]];
  // The lowest role that can use each tab (the server enforces the same rules in core/permissions.py).
  const RANK = { viewer: 0, operator: 1, admin: 2, owner: 3 };
  const TAB_ROLE = { setup: "admin", overview: "viewer", approvals: "operator", employees: "viewer", tasks: "operator", documents: "viewer",
    files: "viewer", proactive: "viewer", sources: "operator", runs: "operator", log: "operator", settings: "admin", audit: "admin", users: "owner" };
  // An older server without /me shows the tabs it always had, and no Audit or Users tab.
  const canSee = (id) => (me ? RANK[me.role] >= RANK[TAB_ROLE[id]] : id !== "audit" && id !== "users");

  // All dynamic text goes in as text nodes; nothing here ever builds HTML from data.
  const add = (el, kid) => {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach((k) => add(el, k)); return; }
    el.appendChild(kid.nodeType ? kid : document.createTextNode(String(kid)));
  };
  const h = (tag, props, ...kids) => {
    const el = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (k === "class") el.className = v;
      else if (k === "on") Object.entries(v).forEach(([ev, fn]) => el.addEventListener(ev, fn));
      else if (k === "disabled") el.disabled = !!v;
      else if (k === "value") el.value = String(v);
      else if (["type", "placeholder", "title", "autocomplete", "rows", "maxlength", "accept"].includes(k)) el.setAttribute(k, String(v));
    });
    kids.forEach((kid) => add(el, kid));
    return el;
  };
  const clip = (text, n) => { const s = String(text === null || text === undefined ? "" : text); return s.length > n ? s.slice(0, n) + " ..." : s; };
  const ago = (iso) => {
    if (!iso) return "never";
    const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 90) return s + "s ago";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  };
  const when = (iso) => (iso ? new Date(iso).toLocaleString() : "-");
  const num = (n) => Number(n || 0).toLocaleString();
  const toggle = (id) => { if (expanded.has(id)) expanded.delete(id); else expanded.add(id); refresh(); };

  const api = async (method, path, body) => {
    const init = { method, cache: "no-store", headers: { "x-api-key": key } };
    if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
    const res = await fetch(path, init);
    if (res.status === 401) { lock("That key was not accepted."); throw new Error("unauthorized"); }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) throw new Error((data && data.detail) || ("Request failed (" + res.status + ")"));
    return data;
  };

  const lock = (msg) => {
    key = "";
    taskFormEl = null;
    boardEl = null;
    proFormEl = null;
    proListEl = null;
    srcFormEl = null;
    srcListEl = null;
    settingsEl = null;
    me = null;
    newKey = null;
    userFormEl = null;
    userListEl = null;
    userBannerEl = null;
    sessionStorage.removeItem("ragleap_key");
    if (timer) { clearInterval(timer); timer = null; }
    showLogin(msg);
  };

  const showLogin = (msg) => {
    root.replaceChildren();
    const input = h("input", { type: "password", placeholder: "API key", autocomplete: "off" });
    const go = () => { key = input.value.trim(); if (!key) return; sessionStorage.setItem("ragleap_key", key); start(); };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    add(root, h("section", { class: "login" },
      h("h1", null, "RagLeap AI Office"),
      h("p", null, "Enter the API key for this server. It stays in this browser tab only."),
      input,
      h("button", { type: "button", on: { click: go } }, "Open"),
      msg ? h("p", { class: "err" }, msg) : null));
    input.focus();
  };

  const stat = (label, value, sub, warn) => h("div", { class: "card" + (warn ? " warn" : "") },
    h("div", { class: "lab" }, label), h("div", { class: "big" }, value), sub ? h("div", { class: "sub" }, sub) : null);
  const counts = (o) => { const k = Object.keys(o || {}); return k.length ? k.map((s) => s + " " + o[s]).join(" / ") : "none"; };

  const renderOverview = (ov) => {
    const a = ov.autonomy || {};
    const t = ov.triggers || {};
    const r = ov.roles || {};
    const u = ov.usage || {};
    return h("div", { class: "grid" },
      stat("Waiting for approval", ov.pending_approvals || 0, a.approval_target_set ? "you get a Telegram ping" : "no approval ping set up", (ov.pending_approvals || 0) > 0),
      stat("Autonomy mode", a.mode || "?", a.mode === "full" ? "actions run WITHOUT asking" : (a.mode === "off" ? "actions are skipped" : "actions wait for you"), a.mode === "full"),
      stat("AI employees", r.active || 0, "of " + (r.total || 0) + " roles"),
      stat("Agent runs", counts(ov.runs), ""),
      stat("Tasks", counts(ov.tasks), ""),
      stat("Triggers", (t.active || 0) + " active", t.next_run_at ? "next " + when(t.next_run_at) : "none scheduled"),
      stat("Tokens today", num(u.day_tokens), "this month " + num(u.month_tokens)));
  };

  const approvalButton = (label, kind, path, msg) => {
    const b = h("button", { type: "button", class: kind }, label);
    let timeout = null;
    b.addEventListener("click", async () => {
      if (!b.dataset.armed) {
        b.dataset.armed = "1";
        armed = true;
        b.textContent = "Click again to confirm";
        timeout = setTimeout(() => { delete b.dataset.armed; b.textContent = label; armed = false; }, 6000);
        return;
      }
      clearTimeout(timeout);
      armed = false;
      b.disabled = true;
      try {
        const r = await api("POST", path);
        msg.textContent = r.result || "Done.";
        setTimeout(refresh, 800);
      } catch (e) {
        msg.textContent = "Failed: " + clip(e.message, 120);
        b.disabled = false;
      }
    });
    return b;
  };

  const approvalCard = (p) => {
    const msg = h("span", { class: "sub" }, "");
    const base = "/autonomy/pending/" + encodeURIComponent(p.action_id);
    return h("article", { class: "item" },
      h("div", { class: "row" }, h("strong", null, p.action_type), p.role ? h("span", { class: "tag" }, p.role) : null,
        h("span", { class: "tag" }, p.channel), h("span", { class: "sub" }, ago(p.created_at))),
      p.target ? h("div", { class: "sub" }, "target: " + clip(p.target, 200)) : null,
      h("pre", null, p.content),
      h("div", { class: "row" }, approvalButton("Approve", "ok", base + "/approve", msg),
        approvalButton("Reject", "bad", base + "/reject", msg), msg));
  };
  const renderApprovals = (items) => (items.length
    ? h("div", { class: "list" }, items.map(approvalCard))
    : h("p", { class: "empty" }, "Nothing is waiting for approval."));

  const runCard = (r) => {
    const isOpen = expanded.has(r.id);
    const statusClass = r.status === "done" ? "ok" : (r.status === "waiting_approval" ? "warn" : (r.status === "running" ? "" : "bad"));
    const body = isOpen ? h("div", null,
      r.summary ? h("p", null, r.summary) : null,
      (r.steps || []).map((s, i) => h("div", { class: "step" },
        h("div", { class: "row" }, h("strong", null, (i + 1) + ". " + s.tool), h("span", { class: "tag" }, s.status),
          s.forced_approval ? h("span", { class: "tag warn" }, "approval forced") : null),
        s.target ? h("div", { class: "sub" }, clip(s.target, 200)) : null,
        s.observation ? h("pre", null, s.observation) : null))) : null;
    return h("article", { class: "item" },
      h("div", { class: "row click", on: { click: () => toggle(r.id) } },
        h("strong", null, clip(r.query, 90)), h("span", { class: "tag " + statusClass }, r.status),
        r.tainted ? h("span", { class: "tag warn" }, "saw web content") : null,
        h("span", { class: "sub" }, (r.steps || []).length + " step(s), " + ago(r.updated_at))),
      body);
  };
  const renderRuns = (runs) => (runs.length ? h("div", { class: "list" }, runs.map(runCard)) : h("p", { class: "empty" }, "No agent runs yet."));

  const logCard = (r) => {
    const id = "log" + r.id;
    const isOpen = expanded.has(id);
    return h("article", { class: "item" },
      h("div", { class: "row click", on: { click: () => toggle(id) } },
        h("strong", null, r.action_type), r.role ? h("span", { class: "tag" }, r.role) : null,
        h("span", { class: "tag " + (r.approved ? "ok" : "bad") }, r.approved ? "approved" : "rejected"),
        h("span", { class: "sub" }, when(r.created_at))),
      h("div", { class: "sub" }, clip(r.result, isOpen ? 4000 : 140)),
      r.decided_by ? h("div", { class: "sub" }, (r.approved ? "approved" : "rejected") + " by " + clip(r.decided_by, 80) + (r.decided_by_role ? " (" + r.decided_by_role + ")" : "")) : null,
      isOpen ? h("pre", null, r.content) : null);
  };
  const renderLog = (rows) => (rows.length ? h("div", { class: "list" }, rows.map(logCard)) : h("p", { class: "empty" }, "No actions logged yet."));

  const meter = (used, limit) => (limit > 0 ? Math.min(100, Math.round((used * 100) / limit)) + "% of " + num(limit) : "no cap");
  const roleCard = (r) => {
    const id = "role" + r.role;
    const isOpen = expanded.has(id);
    return h("article", { class: "item" },
      h("div", { class: "row click", on: { click: () => toggle(id) } },
        h("strong", null, r.display_name || r.role),
        r.sensitive ? h("span", { class: "tag warn" }, "sensitive") : null,
        h("span", { class: "tag " + (r.is_active ? "ok" : "bad") }, r.is_active ? "active" : "off"),
        r.open_tasks ? h("span", { class: "tag" }, r.open_tasks + " open task(s)") : null,
        h("span", { class: "sub" }, "today " + num(r.day_used) + " | month " + num(r.month_used))),
      isOpen ? h("div", null,
        r.skills_summary ? h("p", null, r.skills_summary) : null,
        h("div", { class: "sub" }, "skills: " + ((r.skill_tags || []).join(", ") || "none")),
        h("div", { class: "sub" }, "channels: " + ((r.channels || []).join(", ") || "none")),
        h("div", { class: "sub" }, "daily budget: " + meter(r.day_used, r.day_limit) + " | monthly: " + meter(r.month_used, r.month_limit)),
        h("div", { class: "sub" }, "last learned: " + (r.last_learned_at ? when(r.last_learned_at) : "never"))) : null);
  };
  const renderOrg = (org) => h("div", { class: "list" },
    h("div", { class: "card" },
      h("div", { class: "row" }, h("strong", null, "You (owner)"), h("span", { class: "tag" }, "autonomy: " + org.owner.mode)),
      h("div", { class: "sub" }, org.router.note)),
    org.departments.map((d) => h("section", { class: "dept" },
      h("h2", null, d.name + " (" + d.roles.length + ")"), h("div", { class: "list" }, d.roles.map(roleCard)))));

  const TASK_COLS = [["open", "Open"], ["in_progress", "In progress"], ["blocked", "Blocked"], ["done", "Done"]];
  let taskFormEl = null;
  let boardEl = null;
  const moveTask = async (t, status, msg) => {
    try { await api("PATCH", "/tasks/" + encodeURIComponent(t.id), { status: status }); refresh(); }
    catch (e) { msg.textContent = "Failed: " + clip(e.message, 100); }
  };
  const taskCard = (t) => {
    const msg = h("span", { class: "sub" }, "");
    const i = TASK_COLS.findIndex((c) => c[0] === t.status);
    const btns = [];
    if (i > 0) btns.push(h("button", { type: "button", class: "ghost", on: { click: () => moveTask(t, TASK_COLS[i - 1][0], msg) } }, "< " + TASK_COLS[i - 1][1]));
    if (i >= 0 && i < TASK_COLS.length - 1) btns.push(h("button", { type: "button", on: { click: () => moveTask(t, TASK_COLS[i + 1][0], msg) } }, TASK_COLS[i + 1][1] + " >"));
    return h("article", { class: "item" },
      h("strong", null, clip(t.title, 120)),
      h("div", { class: "row" },
        h("span", { class: "tag " + (t.priority === "urgent" ? "bad" : (t.priority === "high" ? "warn" : "")) }, t.priority),
        h("span", { class: "tag" }, t.assigned_role || "unassigned"), h("span", { class: "sub" }, ago(t.created_at))),
      t.description ? h("div", { class: "sub" }, clip(t.description, 220)) : null,
      t.result ? h("div", { class: "sub" }, "result: " + clip(t.result, 220)) : null,
      h("div", { class: "row" }, btns, msg));
  };
  const buildTaskForm = (roleNames) => {
    const title = h("input", { type: "text", placeholder: "New task title", maxlength: 200 });
    const desc = h("textarea", { rows: 2, placeholder: "Details (optional)" });
    const prio = h("select", null, ["normal", "low", "high", "urgent"].map((p) => h("option", { value: p }, p)));
    const who = h("select", null, h("option", { value: "" }, "unassigned"), roleNames.map((r) => h("option", { value: r }, r)));
    const msg = h("span", { class: "sub" }, "");
    const go = h("button", { type: "button", class: "ok", on: { click: async () => {
      const body = { title: title.value.trim(), description: desc.value.trim(), priority: prio.value };
      if (who.value) body.assigned_role = who.value;
      if (!body.title) { msg.textContent = "Give the task a title."; return; }
      try { await api("POST", "/tasks", body); title.value = ""; desc.value = ""; msg.textContent = "Created."; refresh(); }
      catch (e) { msg.textContent = "Failed: " + clip(e.message, 100); }
    } } }, "Add task");
    return h("div", { class: "card" }, h("div", { class: "row" }, title, prio, who), desc, h("div", { class: "row" }, go, msg));
  };
  const refreshTasks = async () => {
    if (!taskFormEl) {
      const roleNames = (await api("GET", "/employees?active_only=true")).roles.map((r) => r.role);
      taskFormEl = buildTaskForm(roleNames);
      boardEl = h("div", null);
    }
    if (view.firstChild !== taskFormEl) view.replaceChildren(taskFormEl, boardEl);
    const tasks = (await api("GET", "/tasks")).tasks;
    boardEl.replaceChildren(h("div", { class: "grid" }, TASK_COLS.map((col) => {
      const items = tasks.filter((t) => t.status === col[0]);
      return h("section", null, h("h2", null, col[1] + " (" + items.length + ")"), h("div", { class: "list" }, items.map(taskCard)));
    })));
  };

  let docFormEl = null;
  let docListEl = null;
  let docMsgEl = null;
  let docInputEl = null;
  let docHelpEl = null;
  let docUrlRow = null;
  let docUrlParts = [];
  const docConfirm = new Map();
  const DOC_CONFIRM_MS = 10000;
  const uploadDocument = async (file) => {
    const body = new FormData();
    body.append("file", file);
    const res = await fetch("/upload", { method: "POST", cache: "no-store", headers: { "x-api-key": key }, body });
    if (res.status === 401) { lock("That key was not accepted."); throw new Error("unauthorized"); }
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok) throw new Error(clip(data.detail || ("upload failed (" + res.status + ")"), 200));
    return data;
  };
  const buildDocForm = () => {
    const input = h("input", { type: "file", accept: ".txt,.pdf,.docx" });
    docMsgEl = h("span", { class: "sub" }, "");
    const btn = h("button", { type: "button", on: { click: async () => {
      const f = input.files && input.files[0];
      if (!f) { docMsgEl.textContent = "Choose a file first."; return; }
      btn.disabled = true;
      docMsgEl.textContent = "Uploading and indexing " + clip(f.name, 60) + " (this can take a while)...";
      try {
        const r = await uploadDocument(f);
        docMsgEl.textContent = "Added " + clip(f.name, 60) + ": " + r.chunks_stored + " chunk(s).";
        input.value = "";
      } catch (e) {
        if (String(e.message) !== "unauthorized") docMsgEl.textContent = "Could not add the file: " + clip(e.message, 160);
      }
      btn.disabled = false;
      await refreshDocuments();
    } } }, "Upload");
    docInputEl = input;
    docHelpEl = h("p", { class: "sub" }, "Your AI employees answer from the documents listed here.");
    const urlInput = h("input", { type: "text", placeholder: "example.com/page", maxlength: 2048 });
    const urlBtn = h("button", { type: "button", on: { click: async () => {
      const raw = urlInput.value.trim();
      if (!raw) { docMsgEl.textContent = "Type a web address first."; return; }
      const full = /^https?:\/\//i.test(raw) ? raw : "https:" + "//" + raw;
      urlBtn.disabled = true;
      docMsgEl.textContent = "Fetching and indexing " + clip(full, 60) + " ...";
      try {
        const r = await api("POST", "/ingest/url", { url: full });
        docMsgEl.textContent = "Added " + clip(full, 60) + ": " + r.chunks_stored + " chunk(s).";
        urlInput.value = "";
      } catch (e) {
        if (String(e.message) !== "unauthorized") docMsgEl.textContent = "Could not add the page: " + clip(e.message, 160);
      }
      urlBtn.disabled = false;
      await refreshDocuments();
    } } }, "Add web page");
    docUrlParts = [urlInput, urlBtn];
    docUrlRow = h("div", { class: "row" });
    return h("section", { class: "card" }, h("h2", null, "Add a document"), docHelpEl,
      h("div", { class: "row" }, input, btn), docUrlRow, h("div", { class: "row" }, docMsgEl));
  };
  const docCard = (d) => {
    const isArmed = (id) => docConfirm.has(id) && Date.now() - docConfirm.get(id) < DOC_CONFIRM_MS;
    const del = h("button", { type: "button", class: "bad", on: { click: async () => {
      if (!isArmed(d.id)) { docConfirm.set(d.id, Date.now()); await refreshDocuments(); return; }
      docConfirm.delete(d.id);
      del.disabled = true;
      try {
        const r = await api("DELETE", "/documents/" + encodeURIComponent(d.id));
        docMsgEl.textContent = (r && r.deleted) ? "Deleted " + clip(d.filename, 60) + "." + (r.graph === "failed" ? " Warning: the knowledge graph could not be cleaned, so entity names from this document may remain." : "") : "The server did not confirm the delete.";
      } catch (e) {
        if (String(e.message) !== "unauthorized") docMsgEl.textContent = "Could not delete: " + clip(e.message, 160);
      }
      await refreshDocuments();
    } } }, isArmed(d.id) ? "Click again to delete" : "Delete");
    const folder = d.source && d.source.grant;
    return h("article", { class: "item" },
      h("div", { class: "row" }, h("strong", null, clip(d.filename, 120)), h("span", { class: "tag" }, d.chunks + " chunk(s)"),
        folder ? h("span", { class: "tag" }, "from folder " + clip(d.source.grant, 32)) : null,
        h("span", { class: "sub" }, "added " + ago(d.uploaded_at)),
        folder ? h("span", { class: "sub" }, "kept up to date by the folder") : del));
  };
  // ---- Folders granted for knowledge: read automatically; the owner approves the first read of each ----
  let docFolderEl = null;
  const mb = (b) => { const m = Number(b || 0) / 1048576; return (m >= 10 ? Math.round(m) : m.toFixed(1)) + " MB"; };
  const folderCard = (g) => {
    const name = clip(g.grant, 32);
    if (g.state === "waiting_approval") {
      const btn = h("button", { type: "button", on: { click: async () => {
        btn.disabled = true;
        try {
          await api("POST", "/folder-ingest/approve", { grant: g.grant });
          docMsgEl.textContent = "Started reading folder " + name + ". New documents appear in the list below.";
        } catch (e) {
          if (String(e.message) !== "unauthorized") docMsgEl.textContent = "Could not approve: " + clip(e.message, 160);
        }
        await refreshDocuments();
      } } }, "Approve and start");
      return h("article", { class: "item" },
        h("div", { class: "row" }, h("strong", null, name), h("span", { class: "tag" }, "waiting for you")),
        h("p", { class: "sub" }, num(g.new) + " file(s), " + mb(g.bytes_new) + ", will be read and embedded. With a hosted embedding provider this can cost money. Nothing is read until you approve."),
        h("div", { class: "row" }, btn));
    }
    const notes = { no_manifest: "waiting for the first copy", stale_manifest: "copy is out of date", error: "could not be checked" };
    const todo = Number(g.new || 0) + Number(g.changed || 0);
    const bits = [num(g.staged) + " file(s) copied", num(g.ingested) + " document(s) indexed"];
    if (todo > 0) bits.push(num(todo) + " waiting");
    if (g.failed) bits.push(num(g.failed) + " failed");
    if (g.unsupported) bits.push(num(g.unsupported) + " skipped (type not supported)");
    if (g.truncated) bits.push("large folder, copied in parts");
    return h("article", { class: "item" },
      h("div", { class: "row" }, h("strong", null, name), h("span", { class: "tag" }, notes[g.state] || (todo > 0 ? "catching up" : "up to date"))),
      notes[g.state] ? null : h("p", { class: "sub" }, bits.join(", ")));
  };
  const renderFolders = (fi) => {
    if (!fi || !fi.enabled) return [];
    const last = fi.last_run ? ago(new Date(fi.last_run * 1000).toISOString()) : "not yet";
    return [h("section", { class: "card" }, h("h2", null, "Folders"),
      h("p", { class: "sub" }, "Folders you granted for knowledge are read automatically every " + fi.interval_minutes + " min. Last check: " + last + "."),
      fi.schema_ready === false ? h("p", { class: "err" }, "The database needs db/schema.sql applied before folders can be read.") : null,
      fi.runner === "error" ? h("p", { class: "err" }, "The sandbox runner did not answer. Check that it is running with computer control on.") : null,
      fi.grants && fi.grants.length ? h("div", { class: "list" }, fi.grants.map(folderCard))
        : h("p", { class: "empty" }, "No folder copied yet. On the machine run: ragleap files add /path/to/folder --name docs --purpose knowledge"))];
  };
  const refreshDocuments = async () => {
    if (!docFormEl) { docFormEl = buildDocForm(); docListEl = h("div", null); docFolderEl = h("div", null); }
    if (view.firstChild !== docFormEl) view.replaceChildren(docFormEl, docFolderEl, docListEl);
    const info = await api("GET", "/documents");
    let fi = null;
    try { fi = await api("GET", "/folder-ingest"); } catch (e) { if (String(e.message) === "unauthorized") throw e; }
    docFolderEl.replaceChildren(...renderFolders(fi));
    const docs = info.documents;
    if (info.formats && docInputEl) {
      docInputEl.setAttribute("accept", info.formats.join(","));
      docHelpEl.textContent = "Supported: " + info.formats.join(" ") + " (up to " + (info.max_mb || 20) + " MB each). Your AI employees answer from the documents listed here.";
    }
    if (docUrlRow) docUrlRow.replaceChildren(...(info.url_ingest ? docUrlParts : []));
    docListEl.replaceChildren(docs.length ? h("div", { class: "list" }, docs.map(docCard)) : h("p", { class: "empty" }, "No documents yet. Add one above."));
  };

  // ---- Files tab: folders the owner granted to AI employees. Read-only: grants are made only on the machine ----
  const GRANT_NAME = /^[a-z0-9][a-z0-9_-]{0,31}$/;
  const grantCommand = (name) => "ragleap files add /path/to/folder --name " + name + " --mode read";
  const renderFiles = (info, log) => {
    const notes = [];
    if (!info.tools_enabled) notes.push("Agent file tools are off. To turn them on, set FILE_TOOLS_ENABLED=true in .env and restart the app.");
    if (info.runner === "not_configured") notes.push("The sandbox runner is not set up (no SANDBOX_TOKEN), so no folders can be granted.");
    else if (info.runner === "unreachable" || info.runner === "error") notes.push("The sandbox runner did not answer. Check that the sandbox service is running.");
    else if (!info.computer_control) notes.push("File access is off or paused on the machine. Turn it on with: ragleap files on (and COMPUTER_CONTROL=true).");
    const grants = info.grants || [];
    const requests = (log || []).filter((r) => r.action_type === "request_access" && r.approved && GRANT_NAME.test(String(r.target))).slice(0, 10);
    return h("div", { class: "list" },
      notes.map((n) => h("p", { class: "sub" }, n)),
      h("p", { class: "sub" }, "Folders are granted only by a command on this machine, never from this page. To stop all file access right now, run: ragleap files off"),
      grants.length ? grants.map((g) => h("article", { class: "item" },
        h("div", { class: "row" }, h("strong", null, g.name), h("span", { class: "tag" }, g.mode), h("span", { class: "tag" }, g.purpose),
          h("span", { class: "sub" }, g.expires ? "expires " + when(g.expires) : "no expiry"))))
        : h("p", { class: "empty" }, "No folders are granted. To grant one, run: " + grantCommand("NAME")),
      requests.length ? h("p", { class: "sub" }, "Approved access requests. Approving grants nothing; run the command to give access.") : null,
      requests.map((r) => h("article", { class: "item" },
        h("div", { class: "row" }, h("strong", null, r.target), r.role ? h("span", { class: "tag" }, r.role) : null, h("span", { class: "sub" }, when(r.created_at))),
        h("div", { class: "sub" }, "Reason: " + clip(r.content, 300)),
        h("pre", null, grantCommand(r.target)))));
  };

  // ---- Proactive tab: scheduled prompts that an AI employee runs on its own ----
  let proFormEl = null;
  let proListEl = null;
  let proMsgEl = null;
  const proConfirm = new Map();
  const PRO_CONFIRM_MS = 10000;
  const scheduleText = (t) => (t.cron ? "cron " + t.cron + " (" + t.timezone + ")" : "every " + t.schedule_minutes + " min");
  const buildProactiveForm = (roleNames) => {
    const name = h("input", { type: "text", placeholder: "Name, e.g. Morning sales summary", maxlength: 200 });
    const who = h("select", null, roleNames.map((r) => h("option", { value: r }, r)));
    const prompt = h("textarea", { rows: 3, placeholder: "What should the AI employee do each time? Up to 4000 characters.", maxlength: 4000 });
    const kind = h("select", { on: { change: () => {
      when_.setAttribute("placeholder", kind.value === "cron" ? "0 9 * * 1-5" : "60");
      tz.disabled = kind.value !== "cron";
    } } }, h("option", { value: "cron" }, "Cron schedule"), h("option", { value: "minutes" }, "Every N minutes"));
    const when_ = h("input", { type: "text", placeholder: "0 9 * * 1-5", maxlength: 100 });
    let zone = "UTC";
    try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (e) { zone = "UTC"; }
    const tz = h("input", { type: "text", placeholder: "Time zone", value: zone, maxlength: 64 });
    proMsgEl = h("span", { class: "sub" }, "");
    const go = h("button", { type: "button", class: "ok", on: { click: async () => {
      const body = { name: name.value.trim(), role: who.value, prompt: prompt.value.trim(), is_active: true };
      if (!body.name || !body.prompt) { proMsgEl.textContent = "Give the trigger a name and a prompt."; return; }
      if (kind.value === "cron") { body.cron = when_.value.trim(); body.timezone = tz.value.trim() || "UTC"; }
      else { body.schedule_minutes = parseInt(when_.value.trim(), 10); }
      go.disabled = true;
      try {
        await api("POST", "/proactive-triggers", body);
        name.value = "";
        prompt.value = "";
        proMsgEl.textContent = "Added.";
      } catch (e) {
        if (String(e.message) !== "unauthorized") proMsgEl.textContent = "Could not add: " + clip(e.message, 160);
      }
      go.disabled = false;
      await refreshProactive();
    } } }, "Add trigger");
    return h("section", { class: "card" }, h("h2", null, "Add a proactive trigger"),
      h("p", { class: "sub" }, "A trigger makes an AI employee run a prompt on a schedule. The answer reaches you through the approval gate, so look in the Approvals and Activity tabs. While autonomy mode is off, actions are skipped. Cron example: 0 9 * * 1-5 means 09:00 on weekdays in the time zone you give."),
      h("div", { class: "row" }, name, who), prompt, h("div", { class: "row" }, kind, when_, tz), h("div", { class: "row" }, go, proMsgEl));
  };
  const triggerCard = (t) => {
    const isArmed = () => proConfirm.has(t.id) && Date.now() - proConfirm.get(t.id) < PRO_CONFIRM_MS;
    const toggleBtn = h("button", { type: "button", class: "ghost", on: { click: async () => {
      toggleBtn.disabled = true;
      try {
        await api("PATCH", "/proactive-triggers/" + encodeURIComponent(t.id), { is_active: !t.is_active });
        proMsgEl.textContent = (t.is_active ? "Paused " : "Resumed ") + clip(t.name, 60) + ".";
      } catch (e) {
        if (String(e.message) !== "unauthorized") proMsgEl.textContent = "Could not change it: " + clip(e.message, 160);
      }
      await refreshProactive();
    } } }, t.is_active ? "Pause" : "Resume");
    const del = h("button", { type: "button", class: "bad", on: { click: async () => {
      if (!isArmed()) { proConfirm.set(t.id, Date.now()); await refreshProactive(); return; }
      proConfirm.delete(t.id);
      del.disabled = true;
      try {
        await api("DELETE", "/proactive-triggers/" + encodeURIComponent(t.id));
        proMsgEl.textContent = "Deleted " + clip(t.name, 60) + ".";
      } catch (e) {
        if (String(e.message) !== "unauthorized") proMsgEl.textContent = "Could not delete: " + clip(e.message, 160);
      }
      await refreshProactive();
    } } }, isArmed() ? "Click again to delete" : "Delete");
    return h("article", { class: "item" },
      h("div", { class: "row" }, h("strong", null, clip(t.name, 100)), h("span", { class: "tag" }, t.role),
        h("span", { class: "tag " + (t.is_active ? "ok" : "warn") }, t.is_active ? "active" : "paused"), toggleBtn, del),
      h("div", { class: "sub" }, scheduleText(t) + " | last run " + (t.last_run_at ? ago(t.last_run_at) : "never") + " | next run " + (t.is_active && t.next_run_at ? when(t.next_run_at) : "-")),
      h("pre", null, clip(t.prompt, 400)));
  };
  const refreshProactive = async () => {
    if (!proFormEl) {
      const roleNames = (await api("GET", "/employees?active_only=true")).roles.map((r) => r.role);
      proFormEl = buildProactiveForm(roleNames);
      proListEl = h("div", null);
    }
    if (view.firstChild !== proFormEl) view.replaceChildren(proFormEl, proListEl);
    const triggers = (await api("GET", "/proactive-triggers")).triggers;
    proListEl.replaceChildren(triggers.length
      ? h("div", { class: "list" }, triggers.map((t) => triggerCard(t)))
      : h("p", { class: "empty" }, "No proactive triggers yet. Add one above."));
  };

  // ---- Connectors tab: data sources that bring business data in as context ----
  let srcFormEl = null;
  let srcListEl = null;
  let srcMsgEl = null;
  const postForm = async (path, body) => {
    const res = await fetch(path, { method: "POST", cache: "no-store", headers: { "x-api-key": key }, body });
    if (res.status === 401) { lock("That key was not accepted."); throw new Error("unauthorized"); }
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok) throw new Error(clip(data.detail || ("request failed (" + res.status + ")"), 200));
    return data;
  };
  const buildSourceForm = () => {
    const name = h("input", { type: "text", placeholder: "Name, e.g. Customers", maxlength: 200 });
    const file = h("input", { type: "file", accept: ".csv" });
    const idField = h("input", { type: "text", placeholder: "Column that identifies each row", value: "user_id", maxlength: 100 });
    srcMsgEl = h("span", { class: "sub" }, "");
    const go = h("button", { type: "button", class: "ok", on: { click: async () => {
      const f = file.files && file.files[0];
      const nm = name.value.trim();
      if (!nm || !f) { srcMsgEl.textContent = "Give the source a name and choose a .csv file."; return; }
      const body = new FormData();
      body.append("name", nm);
      body.append("user_identifier_field", idField.value.trim() || "user_id");
      body.append("file", f);
      go.disabled = true;
      try {
        await postForm("/integrations/csv", body);
        srcMsgEl.textContent = "Added " + clip(nm, 60) + ". Use Sync now if it shows no records.";
        name.value = "";
        file.value = "";
      } catch (e) {
        if (String(e.message) !== "unauthorized") srcMsgEl.textContent = "Could not add the file: " + clip(e.message, 160);
      }
      go.disabled = false;
      await refreshSources();
    } } }, "Add CSV source");
    return h("section", { class: "card" }, h("h2", null, "Add a data source"),
      h("p", { class: "sub" }, "Connectors bring your business data in as context for the AI employees. Here you can add a CSV file (up to 10 MB, UTF-8), test a source and sync it. Database and API sources that need credentials are added through the API for now, because the dashboard does not store secrets for them yet."),
      h("div", { class: "row" }, name, idField), h("div", { class: "row" }, file, go), h("div", { class: "row" }, srcMsgEl));
  };
  const sourceCard = (s) => {
    const run = async (btn, path, label, okText) => {
      btn.disabled = true;
      srcMsgEl.textContent = label + " " + clip(s.name, 60) + " ...";
      try {
        const r = await api("POST", "/integrations/" + encodeURIComponent(s.id) + path);
        srcMsgEl.textContent = r.success ? okText(r) : (clip(s.name, 60) + ": failed. " + clip(r.error || r.message || "", 160));
      } catch (e) {
        if (String(e.message) !== "unauthorized") srcMsgEl.textContent = clip(s.name, 60) + ": failed. " + clip(e.message, 160);
      }
      btn.disabled = false;
      await refreshSources();
    };
    const testBtn = h("button", { type: "button", class: "ghost", on: { click: () => run(testBtn, "/test", "Testing", (r) => clip(s.name, 60) + ": connection OK. " + clip(r.message || "", 120)) } }, "Test");
    const syncBtn = h("button", { type: "button", on: { click: () => run(syncBtn, "/sync", "Syncing", (r) => clip(s.name, 60) + ": synced " + r.records_synced + " record(s).") } }, "Sync now");
    return h("article", { class: "item" },
      h("div", { class: "row" }, h("strong", null, clip(s.name, 100)), h("span", { class: "tag" }, s.source_type),
        s.last_sync_status ? h("span", { class: "tag " + (s.last_sync_status === "success" ? "ok" : "bad") }, s.last_sync_status) : h("span", { class: "tag warn" }, "never synced"),
        testBtn, syncBtn),
      h("div", { class: "sub" }, "last sync " + (s.last_sync_at ? ago(s.last_sync_at) : "never")
        + (s.last_sync_record_count !== null && s.last_sync_record_count !== undefined ? " | " + s.last_sync_record_count + " record(s)" : "")
        + (s.sync_interval_minutes ? " | auto every " + s.sync_interval_minutes + " min" : "")
        + (s.is_active === false ? " | inactive" : "")));
  };
  const refreshSources = async () => {
    if (!srcFormEl) { srcFormEl = buildSourceForm(); srcListEl = h("div", null); }
    if (view.firstChild !== srcFormEl) view.replaceChildren(srcFormEl, srcListEl);
    const sources = (await api("GET", "/integrations")).data_sources;
    srcListEl.replaceChildren(sources.length
      ? h("div", { class: "list" }, sources.map((s) => sourceCard(s)))
      : h("p", { class: "empty" }, "No data sources yet. Add a CSV file above."));
  };

  const SETTINGS_NOTE = "A value saved here wins over the .env file. Saved keys are encrypted and can never be read back.";
  const sourceTag = (it) => h("span", { class: "tag" }, it.source === "dashboard" ? "set here" : (it.source === "env" ? "from .env" : "default"));
  const llmFields = (p) => {
    const up = p.toUpperCase();
    if (p === "gemini") return [["GEMINI_CHAT_MODEL", "Model", "chat model name"], ["GEMINI_API_KEY", "API key", ""]];
    if (p === "anthropic") return [["ANTHROPIC_MODEL", "Model", "chat model name"], ["ANTHROPIC_API_KEY", "API key", ""]];
    const rows = [[up + "_MODEL", "Model", "chat model name"]];
    if (p !== "ollama") rows.push([up + "_API_KEY", "API key", ""]);
    rows.push([up + "_BASE_URL", p === "ollama" ? "Address" : "Address (optional)", "include the http prefix, e.g. host.docker.internal:11434/v1"]);
    return rows;
  };
  const embFields = (p) => {
    if (p === "gemini") return [["GEMINI_EMBEDDING_MODEL", "Model", "embedding model name"], ["GEMINI_API_KEY", "API key", ""]];
    const up = p.toUpperCase();
    const rows = [[up + "_EMBEDDING_MODEL", "Model", p === "ollama" ? "nomic-embed-text" : "embedding model name"]];
    if (p !== "ollama") rows.push([up + "_API_KEY", "API key", ""]);
    rows.push([up + "_BASE_URL", p === "ollama" ? "Address" : "Address (optional)", "include the http prefix, e.g. host.docker.internal:11434/v1"]);
    return rows;
  };
  let settingsEl = null;
  const buildSettings = (data, note) => {
    const items = {};
    data.settings.forEach((s) => { items[s.name] = s; });
    const stores = { llm: {}, emb: {}, common: {} };
    const msg = h("p", { class: "sub" }, note || "");
    const reload = async (text) => {
      try { const d = await api("GET", "/settings"); settingsEl = buildSettings(d, text); view.replaceChildren(settingsEl); }
      catch (e) { if (String(e.message) !== "unauthorized") msg.textContent = "Failed: " + clip(e.message, 120); }
    };
    const save = async (values, okText) => {
      if (!Object.keys(values).length) { msg.textContent = "Nothing to save."; return; }
      msg.textContent = "Saving...";
      try {
        const r = await api("PUT", "/settings", { values: values });
        let text = okText || "Saved.";
        const bad = (r.vector_status || []).filter((v) => v.status === "mismatch");
        if (bad.length) text += " Warning: stored documents use a different vector size, so nothing was resized. Re-ingest after clearing them, or switch back.";
        reload(text);
      } catch (e) { msg.textContent = "Failed: " + clip(e.message, 160); }
    };
    const field = (store, name, label, hint) => {
      const it = items[name];
      if (!it) return null;
      const el = h("input", { type: it.secret ? "password" : "text", placeholder: it.secret && it.is_set ? "leave blank to keep" : (hint || ""), autocomplete: "off", maxlength: 512 });
      if (!it.secret) el.value = it.value || "";
      store[name] = { el: el, secret: it.secret, orig: it.secret ? "" : (it.value || "") };
      let tag;
      if (it.secret) tag = h("span", { class: "tag " + (it.is_set ? "ok" : "") }, it.is_set ? (it.source === "dashboard" ? "saved, hidden" : "set in .env, hidden") : "not set");
      else tag = sourceTag(it);
      const rm = it.secret && it.source === "dashboard"
        ? h("button", { type: "button", class: "ghost", on: { click: () => save({ [name]: null }, "Removed.") } }, "Remove") : null;
      return h("div", { class: "row" }, h("label", { class: "sub" }, label), el, tag, rm);
    };
    const select = (list, current) => {
      const s = h("select", null, list.map((p) => h("option", { value: p }, p)));
      if (list.includes(current)) s.value = current;
      return s;
    };
    const origLlm = ((items.LLM_PROVIDER && items.LLM_PROVIDER.value) || "gemini").toLowerCase();
    const origEmb = ((items.EMBEDDING_PROVIDER && items.EMBEDDING_PROVIDER.value) || "gemini").toLowerCase();
    const llmSel = select(data.providers.llm, origLlm);
    const embSel = select(data.providers.embedding, origEmb);
    const llmBox = h("div", { class: "list" });
    const embBox = h("div", { class: "list" });
    const draw = (box, store, rows) => {
      Object.keys(store).forEach((k) => { delete store[k]; });
      box.replaceChildren(...rows.map((r) => field(store, r[0], r[1], r[2])).filter(Boolean));
    };
    const drawLlm = () => draw(llmBox, stores.llm, llmFields(llmSel.value));
    const drawEmb = () => draw(embBox, stores.emb, embFields(embSel.value));
    llmSel.addEventListener("change", drawLlm);
    embSel.addEventListener("change", drawEmb);
    drawLlm();
    drawEmb();
    const fallbackRow = field(stores.common, "LLM_FALLBACK_PROVIDERS", "Fallbacks", "comma separated, e.g. groq,gemini");
    const dimsRow = field(stores.common, "EMBEDDING_DIMENSIONS", "Vector size", "must match the model, e.g. 768 or 3072");
    const collect = () => {
      const out = {};
      Object.values(stores).forEach((store) => Object.keys(store).forEach((n) => {
        const f = store[n];
        const v = f.el.value.trim();
        if (f.secret) { if (v) out[n] = v; } else if (v !== f.orig) out[n] = v;
      }));
      if (llmSel.value !== origLlm) out.LLM_PROVIDER = llmSel.value;
      if (embSel.value !== origEmb) out.EMBEDDING_PROVIDER = embSel.value;
      return out;
    };
    const testBtn = (label, path) => h("button", { type: "button", on: { click: async () => {
      msg.textContent = "Testing the saved settings (save first if you changed something)...";
      try {
        const r = await api("POST", path);
        msg.textContent = r.ok ? ("Works: " + r.provider + " / " + r.model + (r.dimensions ? " (" + r.dimensions + " dims)" : "")) : ("Problem: " + (r.hint || r.error));
      } catch (e) { msg.textContent = "Failed: " + clip(e.message, 120); }
    } } }, label);
    return h("div", { class: "list" },
      h("p", { class: "sub" }, SETTINGS_NOTE),
      data.encryption_ready ? null : h("p", { class: "err" }, "Keys cannot be saved here: ADDON_ENCRYPTION_KEY is not set on the server."),
      h("section", { class: "dept" }, h("h2", null, "Chat AI"),
        h("div", { class: "row" }, h("label", { class: "sub" }, "Provider"), llmSel),
        llmBox, fallbackRow, h("div", { class: "row" }, testBtn("Test chat AI", "/settings/test/llm"))),
      h("section", { class: "dept" }, h("h2", null, "Embeddings (document search)"),
        h("p", { class: "sub" }, "Changing the embedding model after you have added documents means re-adding them."),
        h("div", { class: "row" }, h("label", { class: "sub" }, "Provider"), embSel),
        embBox, dimsRow, h("div", { class: "row" }, testBtn("Test embeddings", "/settings/test/embedding"))),
      h("div", { class: "row" }, h("button", { type: "button", class: "ok", on: { click: () => save(collect()) } }, "Save changes"), msg));
  };
  const refreshSettings = async () => {
    if (settingsEl && view.firstChild === settingsEl) return;
    settingsEl = buildSettings(await api("GET", "/settings"), "");
    view.replaceChildren(settingsEl);
  };

  const SETUP_TAG = { ok: "ok", todo: "bad", warn: "warn", info: "" };
  const SETUP_WORD = { ok: "done", todo: "needs setup", warn: "check this", info: "info" };
  const renderSetup = (s) => h("div", { class: "list" },
    h("div", { class: "card" + (s.ready ? "" : " warn") },
      h("div", { class: "lab" }, "Setup"),
      h("div", { class: "big" }, s.ready ? "Ready" : "Almost there"),
      h("div", { class: "sub" }, s.summary)),
    s.items.map((it) => h("article", { class: "item" },
      h("div", { class: "row" },
        h("strong", null, it.title),
        h("span", { class: "tag " + (SETUP_TAG[it.state] || "") }, SETUP_WORD[it.state] || it.state),
        it.required ? h("span", { class: "tag" }, "required") : null),
      h("div", { class: "sub" }, it.detail),
      it.action ? h("div", { class: "sub" }, "What to do: " + it.action) : null,
      it.fix === "settings" ? h("div", { class: "row" }, h("button", { type: "button", on: { click: () => { tab = "settings"; armed = false; refresh(); } } }, "Open Settings")) : null)));

  // ---- Audit (admin): who did what ----
  const renderAudit = (events) => (events.length ? h("div", { class: "list" }, events.map((e) => h("article", { class: "item" },
    h("div", { class: "row" }, h("strong", null, clip(e.actor, 60)), h("span", { class: "tag" }, e.role),
      h("span", { class: "tag " + (e.status < 400 ? "ok" : "bad") }, e.status === 403 ? "refused" : String(e.status)),
      h("span", { class: "sub" }, when(e.at))),
    h("div", { class: "sub" }, e.method + " " + clip(e.path, 160)))))
    : h("p", { class: "empty" }, "Nothing recorded yet. State-changing requests by signed-in people appear here."));

  // ---- People (owner): add a person, change a role, disable, make and revoke personal keys ----
  const ROLE_LIST = ["viewer", "operator", "admin", "owner"];
  const ROLE_HELP = "viewer: overview pages. operator: also decides on actions and uploads. admin: also changes settings. owner: everything, including people.";
  const roleSelect = (value, onChange, disabled) => {
    const s = h("select", { disabled: !!disabled }, ...ROLE_LIST.map((r) => h("option", { value: r }, r)));
    s.value = value;
    if (onChange) s.addEventListener("change", () => onChange(s.value));
    return s;
  };
  const userSay = (t) => { if (userMsgEl) userMsgEl.textContent = t; };
  const userDo = async (fn, done) => {
    try { await fn(); if (done) userSay(done); } catch (e) { if (String(e.message) !== "unauthorized") userSay("Could not do that: " + clip(e.message, 160)); }
    await refreshUsers();
  };
  const confirmButton = (label, kind, action) => {
    const b = h("button", { type: "button", class: kind }, label);
    let timeout = null;
    b.addEventListener("click", async () => {
      if (!b.dataset.armed) {
        b.dataset.armed = "1";
        armed = true;
        b.textContent = "Click again to confirm";
        timeout = setTimeout(() => { delete b.dataset.armed; b.textContent = label; armed = false; }, 6000);
        return;
      }
      clearTimeout(timeout);
      armed = false;
      b.disabled = true;
      await action();
    });
    return b;
  };
  const keyRow = (u, k) => h("div", { class: "row" }, h("span", { class: "sub" }, k.prefix + "...  " + (k.label || "no label") + "  made " + ago(k.created_at) + ", last used " + (k.last_used_at ? ago(k.last_used_at) : "never")),
    confirmButton("Revoke", "ghost", () => userDo(() => api("DELETE", "/users/" + u.id + "/keys/" + k.id), "Key revoked.")));
  const userCard = (u) => {
    const mine = !!(me && me.user_id && me.user_id === u.id);   // you cannot lock yourself out from here
    return h("article", { class: "item" },
      h("div", { class: "row" }, h("strong", null, clip(u.name, 80)), mine ? h("span", { class: "tag" }, "you") : null,
        roleSelect(u.role, (v) => userDo(() => api("PATCH", "/users/" + u.id, { role: v }), "Role changed."), mine),
        h("span", { class: "tag " + (u.status === "active" ? "ok" : "bad") }, u.status),
        h("span", { class: "sub" }, num(u.active_keys) + " key(s), last used " + (u.last_used_at ? ago(u.last_used_at) : "never"))),
      h("div", { class: "row" },
        u.status === "active" ? h("button", { type: "button", on: { click: () => userDo(async () => {
          const r = await api("POST", "/users/" + u.id + "/keys", { label: "made in the dashboard" });
          newKey = { name: u.name, key: r.key };
          renderKeyBanner();
        }) } }, "Make a key") : null,
        mine ? null : h("button", { type: "button", class: "ghost", on: { click: () => userDo(() => api("PATCH", "/users/" + u.id, { status: u.status === "active" ? "disabled" : "active" }),
          u.status === "active" ? "Person disabled." : "Person enabled.") } }, u.status === "active" ? "Disable" : "Enable")),
      (u.keys || []).map((k) => keyRow(u, k)));
  };
  const renderKeyBanner = () => {
    if (!userBannerEl) return;
    if (!newKey) { userBannerEl.replaceChildren(); return; }
    userBannerEl.replaceChildren(h("section", { class: "card" }, h("h2", null, "New key for " + clip(newKey.name, 60)),
      h("p", { class: "sub" }, "Copy it now and give it to that person. It is shown only once and cannot be shown again."),
      h("pre", null, newKey.key),
      h("div", { class: "row" },
        h("button", { type: "button", on: { click: async (ev) => {
          try { await navigator.clipboard.writeText(newKey.key); ev.target.textContent = "Copied"; } catch (e) { ev.target.textContent = "Select the key and copy it yourself"; }
        } } }, "Copy"),
        h("button", { type: "button", class: "ghost", on: { click: () => { newKey = null; renderKeyBanner(); } } }, "I have copied it"))));
  };
  const buildUserForm = () => {
    const name = h("input", { type: "text", placeholder: "Name", maxlength: 80 });
    const role = roleSelect("operator");
    userMsgEl = h("span", { class: "sub" }, "");
    const btn = h("button", { type: "button", on: { click: async () => {
      if (!name.value.trim()) { userSay("Type a name first."); return; }
      btn.disabled = true;
      try { await api("POST", "/users", { name: name.value.trim(), role: role.value }); name.value = ""; userSay("Person added. Now make a key for them."); }
      catch (e) { if (String(e.message) !== "unauthorized") userSay("Could not add: " + clip(e.message, 160)); }
      btn.disabled = false;
      await refreshUsers();
    } } }, "Add person");
    return h("section", { class: "card" }, h("h2", null, "People"),
      h("p", { class: "sub" }, "Each person signs in with their own key. The shared key keeps working and counts as the owner. " + ROLE_HELP),
      h("div", { class: "row" }, name, role, btn, userMsgEl));
  };
  const refreshUsers = async () => {
    if (!userFormEl) { userFormEl = buildUserForm(); userListEl = h("div", null); userBannerEl = h("div", null); renderKeyBanner(); }
    if (view.firstChild !== userBannerEl) view.replaceChildren(userBannerEl, userFormEl, userListEl);
    const d = await api("GET", "/users");
    userListEl.replaceChildren(d.users.length ? h("div", { class: "list" }, d.users.map(userCard))
      : h("p", { class: "empty" }, "No named people yet. The shared key still works as the owner. Add a person, then make a key for them."));
  };

  const refresh = async () => {
    if (armed || !view) return;
    try {
      const ov = await api("GET", "/overview");
      const n = ov.pending_approvals || 0;
      if (tabButtons.approvals) tabButtons.approvals.textContent = n ? "Approvals (" + n + ")" : "Approvals";
      Object.keys(tabButtons).forEach((id) => tabButtons[id].classList.toggle("on", id === tab));
      let content;
      if (tab === "overview") content = renderOverview(ov);
      else if (tab === "approvals") content = renderApprovals((await api("GET", "/autonomy/pending")).pending);
      else if (tab === "runs") content = renderRuns((await api("GET", "/agent-runs?limit=30")).runs);
      else if (tab === "employees") content = renderOrg(await api("GET", "/org"));
      else if (tab === "tasks") { await refreshTasks(); content = null; }
      else if (tab === "documents") { await refreshDocuments(); content = null; }
      else if (tab === "files") content = renderFiles(await api("GET", "/file-grants"), (await api("GET", "/autonomy/log?limit=200")).log);
      else if (tab === "proactive") { await refreshProactive(); content = null; }
      else if (tab === "sources") { await refreshSources(); content = null; }
      else if (tab === "settings") { await refreshSettings(); content = null; }
      else if (tab === "audit") content = renderAudit((await api("GET", "/audit?limit=200")).events);
      else if (tab === "users") { await refreshUsers(); content = null; }
      else if (tab === "setup") content = renderSetup(await api("GET", "/setup/status"));
      else content = renderLog((await api("GET", "/autonomy/log?limit=60")).log);
      if (content) view.replaceChildren(content);
      stamp.textContent = "updated " + new Date().toLocaleTimeString();
    } catch (e) {
      if (String(e.message) !== "unauthorized") stamp.textContent = "error: " + clip(e.message, 80);
    }
  };

  const buildShell = () => {
    root.replaceChildren();
    stamp = h("span", { class: "stamp" }, "");
    const nav = h("nav", null);
    TABS.filter(([id]) => canSee(id)).forEach(([id, label]) => {
      const b = h("button", { type: "button", class: "tab", on: { click: () => { tab = id; armed = false; refresh(); } } }, label);
      tabButtons[id] = b;
      nav.appendChild(b);
    });
    view = h("main", null);
    add(root, h("header", null, h("strong", null, "RagLeap AI Office"), nav, stamp,
      me ? h("span", { class: "sub" }, "Signed in as " + clip(me.name, 40) + " (" + me.role + ")") : null,
      h("button", { type: "button", class: "ghost", on: { click: () => lock("") } }, "Lock")));
    add(root, view);
  };

  const start = async () => {
    root.replaceChildren(h("p", { class: "empty" }, "Connecting..."));
    try { await api("GET", "/overview"); } catch (e) {
      if (String(e.message) !== "unauthorized") showLogin("Could not reach the server: " + clip(e.message, 100));
      return;
    }
    try { me = await api("GET", "/me"); } catch (e) { if (String(e.message) === "unauthorized") return; me = null; }
    if (!canSee(tab)) tab = "overview";
    buildShell();
    if (canSee("setup")) {
      try { const s = await api("GET", "/setup/status"); if (!s.ready) tab = "setup"; } catch (e) { /* the checklist is optional */ }
    }
    await refresh();
    if (timer) clearInterval(timer);
    timer = setInterval(refresh, 10000);
  };

  if (key) start(); else showLogin("");
})();
