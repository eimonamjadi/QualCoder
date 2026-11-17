// QualCoder front end: plain JS, no build step.
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, init);
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(data?.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText);
  return data;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 3000);
}
function modal(html) { $("#modal-body").innerHTML = html; $("#modal").showModal(); }

const state = { codes: [], docs: [], currentDoc: null, settings: null };

// ------------------------------------------------------------------ tabs
$$("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  ({ review: renderReview, codebook: renderCodebook, prompts: renderPrompts, documents: renderDocuments, settings: renderSettings })[name]();
}

// ------------------------------------------------------------------ settings / provider
async function loadSettings() {
  state.settings = await api("/api/settings");
  const p = state.settings.providers.find((x) => x.name === state.settings.provider);
  const badge = $("#provider-badge");
  badge.textContent = `Model: ${p.name} · ${p.model}${p.configured ? "" : " (not configured)"}`;
  badge.classList.toggle("warn", !p.configured);
}
async function renderSettings() {
  await loadSettings();
  const s = state.settings;
  const notes = {
    claude: "Default. Needs ANTHROPIC_API_KEY.",
    openai: "Needs OPENAI_API_KEY and QUALCODE_OPENAI_MODEL.",
    ollama: "Local open model via Ollama; data stays on this machine.",
    mock: "Offline keyword matcher for demos. Not a real model.",
  };
  $("#provider-list").innerHTML = s.providers.map((p) => `
    <label><input type="radio" name="provider" value="${p.name}" ${p.name === s.provider ? "checked" : ""}>
      <b>${p.name}</b> · <code>${esc(p.model)}</code>
      ${p.configured ? '<span class="pill">ready</span>' : '<span class="pill flag">not configured</span>'}
      <div class="muted">${notes[p.name] || ""}</div></label>`).join("");
  $$("#provider-list input").forEach((r) => r.addEventListener("change", async () => {
    await api("/api/settings", { method: "PUT", json: { provider: r.value } });
    await loadSettings(); toast(`Using ${r.value}`);
  }));
  $("#learn-accepted").checked = s.learn_from_accepted;
}
$("#learn-accepted").addEventListener("change", (e) =>
  api("/api/settings", { method: "PUT", json: { learn_from_accepted: e.target.checked } }));

// ------------------------------------------------------------------ codebook
async function loadCodes() { state.codes = await api("/api/codes"); }
async function renderCodebook() {
  await loadCodes();
  $("#code-list").innerHTML = state.codes.length ? state.codes.map((c) => `
    <div class="list-item">
      <h4><span>${esc(c.name)} <span class="pill">CODE:${c.id}</span></span>
        <span class="actions"><button data-edit="${c.id}">Edit</button> <button data-del="${c.id}">Delete</button></span></h4>
      <div>${esc(c.definition)}</div>
      <div class="muted">${c.examples.filter((e) => e.polarity === "positive").length} positive,
        ${c.examples.filter((e) => e.polarity === "negative").length} negative examples
        (${c.examples.filter((e) => e.source !== "user").length} from review)</div>
    </div>`).join("") : '<p class="muted">No codes yet.</p>';
  $$("#code-list [data-edit]").forEach((b) => b.addEventListener("click", () => editCode(+b.dataset.edit)));
  $$("#code-list [data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this code, its examples and every place it was applied?")) return;
    await api(`/api/codes/${b.dataset.del}`, { method: "DELETE" }); resetCodeForm(); renderCodebook();
  }));
}
function editCode(id) {
  const c = state.codes.find((x) => x.id === id);
  const f = $("#code-form");
  for (const k of ["id", "name", "definition", "include_when", "exclude_when"]) f.elements[k].value = c[k];
  $("#code-form-title").textContent = `Edit ${c.name}`;
  $("#example-panel").classList.remove("hidden");
  $("#example-code-name").textContent = c.name;
  $("#example-list").innerHTML = c.examples.map((e) => `
    <div class="example"><div><span class="pill">EX:${e.id}</span> <span class="pill ${e.polarity === "negative" ? "flag" : ""}">${e.polarity}</span>
      <span class="pill">${e.source}</span> “${esc(e.quote)}” <span class="muted">${esc(e.note)}</span></div>
      <button data-delex="${e.id}">×</button></div>`).join("") || '<p class="muted">No examples yet.</p>';
  $$("#example-list [data-delex]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/examples/${b.dataset.delex}`, { method: "DELETE" }); await loadCodes(); editCode(id);
  }));
}
function resetCodeForm() {
  $("#code-form").reset(); $("#code-form").elements.id.value = "";
  $("#code-form-title").textContent = "New code"; $("#example-panel").classList.add("hidden");
}
$("#code-form-reset").addEventListener("click", resetCodeForm);
$("#code-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, body = Object.fromEntries(new FormData(f)); const id = body.id; delete body.id;
  try {
    if (id) await api(`/api/codes/${id}`, { method: "PUT", json: body });
    else { const r = await api("/api/codes", { json: body }); f.elements.id.value = r.id; }
    await renderCodebook(); editCode(+f.elements.id.value); toast("Saved");
  } catch (err) { toast(err.message); }
});
$("#example-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = +$("#code-form").elements.id.value;
  await api(`/api/codes/${id}/examples`, { json: Object.fromEntries(new FormData(e.target)) });
  e.target.reset(); await renderCodebook(); editCode(id);
});
$("#codebook-import").addEventListener("change", async (e) => {
  const fd = new FormData(); fd.append("file", e.target.files[0]);
  try { const r = await api("/api/codebook/import", { method: "POST", body: fd }); toast(`Imported ${r.imported} codes`); renderCodebook(); }
  catch (err) { toast(err.message); }
  e.target.value = "";
});

// ------------------------------------------------------------------ prompts
async function renderPrompts() {
  const [prompts, base] = await Promise.all([api("/api/prompts"), api("/api/base-instructions")]);
  $("#base-instructions").textContent = base.text;
  $("#prompt-list").innerHTML = prompts.map((p) => `
    <div class="list-item"><h4><span>${esc(p.name)} <span class="pill">PROMPT:${p.id}</span>
      ${p.active ? '<span class="pill">active</span>' : '<span class="pill flag">off</span>'}</span>
      <span class="actions"><button data-edit="${p.id}">Edit</button> <button data-del="${p.id}">Delete</button></span></h4>
      <div>${esc(p.text)}</div></div>`).join("") || '<p class="muted">No prompts yet.</p>';
  $$("#prompt-list [data-edit]").forEach((b) => b.addEventListener("click", () => {
    const p = prompts.find((x) => x.id === +b.dataset.edit), f = $("#prompt-form");
    f.elements.id.value = p.id; f.elements.name.value = p.name; f.elements.text.value = p.text; f.elements.active.checked = !!p.active;
  }));
  $$("#prompt-list [data-del]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/prompts/${b.dataset.del}`, { method: "DELETE" }); renderPrompts();
  }));
}
$("#prompt-form-reset").addEventListener("click", () => { $("#prompt-form").reset(); $("#prompt-form").elements.id.value = ""; });
$("#prompt-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, body = { name: f.elements.name.value, text: f.elements.text.value, active: f.elements.active.checked };
  if (f.elements.id.value) await api(`/api/prompts/${f.elements.id.value}`, { method: "PUT", json: body });
  else await api("/api/prompts", { json: body });
  f.reset(); f.elements.id.value = ""; renderPrompts(); toast("Saved");
});

// ------------------------------------------------------------------ documents
async function loadDocs() { state.docs = await api("/api/documents"); }
async function renderDocuments() {
  await loadDocs();
  $("#doc-table tbody").innerHTML = state.docs.map((d) => `
    <tr><td><a href="#" data-open="${d.id}">${esc(d.name)}</a></td><td>${d.passages}</td><td>${d.pending}</td><td>${d.accepted}</td>
    <td><button data-del="${d.id}">Delete</button></td></tr>`).join("") || '<tr><td colspan="5" class="muted">None yet</td></tr>';
  $$("#doc-table [data-open]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); state.currentDoc = +a.dataset.open; showTab("review"); }));
  $$("#doc-table [data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this document and its coding?")) return;
    await api(`/api/documents/${b.dataset.del}`, { method: "DELETE" }); renderDocuments();
  }));
}
$("#upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { const r = await api("/api/documents/upload", { method: "POST", body: new FormData(e.target) });
    e.target.reset(); state.currentDoc = r.id; toast("Uploaded"); renderDocuments(); }
  catch (err) { toast(err.message); }
});
$("#paste-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await api("/api/documents/text", { json: Object.fromEntries(new FormData(e.target)) });
  e.target.reset(); state.currentDoc = r.id; toast("Added"); renderDocuments();
});
async function loadSamples() {
  const r = await api("/api/samples/load", { method: "POST" });
  if (r.document_id) state.currentDoc = r.document_id;
  toast(`Loaded ${r.codes_imported} sample codes${r.document_id ? " and a transcript" : ""}`);
  showTab("review");
}
$("#load-samples").addEventListener("click", loadSamples);
$("#load-samples-2").addEventListener("click", loadSamples);

// ------------------------------------------------------------------ review
let docData = null;
async function renderReview() {
  await Promise.all([loadDocs(), loadCodes(), loadSettings()]);
  const sel = $("#review-doc");
  $("#empty-hint").classList.toggle("hidden", state.docs.length > 0);
  sel.innerHTML = state.docs.map((d) => `<option value="${d.id}">${esc(d.name)}</option>`).join("");
  if (!state.docs.length) { $("#passages").innerHTML = ""; return; }
  if (!state.docs.some((d) => d.id === state.currentDoc)) state.currentDoc = state.docs[state.docs.length - 1].id;
  sel.value = state.currentDoc;
  docData = await api(`/api/documents/${state.currentDoc}`);
  drawPassages();
}
$("#review-doc").addEventListener("change", (e) => { state.currentDoc = +e.target.value; renderReview(); });
$("#filter-pending").addEventListener("change", drawPassages);

function highlight(text, quotes) {
  // mark each evidence quote inside the passage (case-insensitive, whitespace tolerant)
  let html = esc(text);
  for (const q of quotes) {
    if (!q) continue;
    const pattern = esc(q).trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
    try { html = html.replace(new RegExp(pattern, "i"), (m) => `<mark>${m}</mark>`); } catch { /* ignore */ }
  }
  return html;
}

function codeOptions(selected) {
  return state.codes.map((c) => `<option value="${c.id}" ${c.id === selected ? "selected" : ""}>${esc(c.name)}</option>`).join("");
}

function drawPassages() {
  if (!docData) return;
  const onlyPending = $("#filter-pending").checked;
  const visible = docData.passages.filter((p) => !onlyPending || p.applications.some((a) => a.status === "pending"));
  $("#passages").innerHTML = visible.map((p) => {
    const live = p.applications.filter((a) => a.status !== "rejected");
    return `
    <div class="passage" id="p-${p.id}">
      <div class="passage-head"><span>Passage ${p.idx + 1} · chars ${p.start_char}–${p.end_char}</span>
        <span>${p.last_run ? `<button class="link" data-run="${p.last_run.id}">view prompt &amp; raw reply</button>` : "not coded yet"}</span></div>
      <div class="passage-text">${highlight(p.text, live.map((a) => a.evidence_quote))}</div>
      ${p.last_run?.error ? `<div class="error">Last run failed: ${esc(p.last_run.error)}</div>` : ""}
      <div class="apps">${p.applications.map(appHtml).join("")}</div>
      <div class="passage-actions">
        <button data-code="${p.id}">${p.last_run ? "Re-code" : "Code"} this passage</button>
        <button class="link" data-preview="${p.id}">preview prompt</button>
        <span class="muted">Add a code the model missed:</span>
        <select data-addsel="${p.id}"><option value="">choose…</option>${codeOptions()}</select>
        <button data-add="${p.id}">Add</button>
      </div>
    </div>`;
  }).join("") || '<p class="muted">Nothing to show.</p>';
  bindPassageEvents();
}

function appHtml(a) {
  const refs = a.cited_refs.map((r) => `<button class="pill ref" data-ref="${esc(r)}" data-app="${a.id}">${esc(r)}</button>`).join(" ");
  const flags = a.flags.map((f) => `<span class="pill flag" title="${esc(f)}">⚠ ${esc(f)}</span>`).join(" ");
  const who = a.origin === "human" ? "added by you" : `${esc(a.model || a.provider || "model")} · ${a.confidence}`;
  const actions = a.status === "pending" ? `
      <button data-accept="${a.id}">Accept</button>
      <button data-reject="${a.id}">Reject</button>
      <select data-recodesel="${a.id}">${codeOptions(a.code_id)}</select><button data-recode="${a.id}">Change code</button>
      <input data-note="${a.id}" placeholder="Note / reason (optional, becomes part of the example)" style="flex:1;min-width:200px;margin:0">`
    : `<span class="muted">${a.status}${a.reviewer_note ? `: ${esc(a.reviewer_note)}` : ""}</span> <button data-undo="${a.id}">Undo</button>`;
  return `
    <div class="app ${a.status}">
      <div class="app-head"><span class="code-name">${esc(a.code_name)}</span><span class="pill">${who}</span>${flags}</div>
      ${a.evidence_quote ? `<div class="app-body">“${esc(a.evidence_quote)}”</div>` : ""}
      ${a.justification ? `<div class="app-body muted">${esc(a.justification)}</div>` : ""}
      ${refs ? `<div class="app-body">Relied on: ${refs}</div><div data-refbox="${a.id}"></div>` : ""}
      <div class="app-actions">${actions}</div>
    </div>`;
}

async function refreshDoc() { docData = await api(`/api/documents/${state.currentDoc}`); drawPassages(); }

function bindPassageEvents() {
  const on = (attr, fn) => $$(`[data-${attr}]`, $("#passages")).forEach((el) => el.addEventListener("click", () => fn(el.dataset[attr], el)));
  on("code", async (id, el) => { el.disabled = true; el.textContent = "Coding…"; await codeOne(+id); await refreshDoc(); });
  on("preview", async (id) => {
    const r = await api(`/api/passages/${id}/preview`);
    modal(`<h3>Prompt that would be sent</h3><p class="muted">Refs shown to the model: ${r.context_refs.join(", ")}</p>
      <h4>System</h4><pre class="prompt">${esc(r.system_prompt)}</pre><h4>User</h4><pre class="prompt">${esc(r.user_prompt)}</pre>`);
  });
  on("run", async (id) => {
    const r = await api(`/api/runs/${id}`);
    modal(`<h3>Run ${r.id} · ${esc(r.provider)} · ${esc(r.model)}</h3><p class="muted">${r.created_at} UTC</p>
      ${r.error ? `<p class="error">${esc(r.error)}</p>` : ""}
      <h4>System prompt</h4><pre class="prompt">${esc(r.system_prompt)}</pre>
      <h4>User prompt</h4><pre class="prompt">${esc(r.user_prompt)}</pre>
      <h4>Raw reply</h4><pre class="prompt">${esc(r.raw_response)}</pre>`);
  });
  on("ref", async (ref, el) => {
    const box = $(`[data-refbox="${el.dataset.app}"]`);
    if (box.dataset.open === ref) { box.innerHTML = ""; box.dataset.open = ""; return; }
    try {
      const r = await api(`/api/refs/${encodeURIComponent(ref)}`);
      box.dataset.open = ref;
      box.innerHTML = `<div class="ref-detail"><b>${esc(r.ref)}</b> · ${esc(r.type)} · <b>${esc(r.title)}</b><div>${esc(r.text)}</div>
        ${r.include_when ? `<div class="muted">Include when: ${esc(r.include_when)}</div>` : ""}
        ${r.exclude_when ? `<div class="muted">Exclude when: ${esc(r.exclude_when)}</div>` : ""}
        ${r.note ? `<div class="muted">Note: ${esc(r.note)}</div>` : ""}</div>`;
    } catch (err) { box.innerHTML = `<div class="ref-detail error">${esc(err.message)}</div>`; }
  });
  const note = (id) => $(`[data-note="${id}"]`)?.value || "";
  const review = async (id, body) => { try { await api(`/api/applications/${id}/review`, { json: body }); await refreshDoc(); } catch (err) { toast(err.message); } };
  on("accept", (id) => review(id, { action: "accept", note: note(id) }));
  on("reject", (id) => review(id, { action: "reject", note: note(id) }));
  on("undo", (id) => review(id, { action: "undo" }));
  on("recode", (id) => review(id, { action: "recode", new_code_id: +$(`[data-recodesel="${id}"]`).value, note: note(id) }));
  on("add", async (pid) => {
    const code_id = +$(`[data-addsel="${pid}"]`).value;
    if (!code_id) return toast("Choose a code first");
    await api(`/api/passages/${pid}/applications`, { json: { code_id } }); await refreshDoc();
  });
}

async function codeOne(passageId) {
  try {
    const r = await api(`/api/passages/${passageId}/code`, { json: {} });
    if (r.error) toast(r.error);
    return r;
  } catch (err) { toast(err.message); return { error: err.message }; }
}

async function codeMany(onlyUncoded) {
  if (!docData) return;
  const targets = docData.passages.filter((p) => !onlyUncoded || !p.last_run);
  const buttons = [$("#code-all"), $("#code-uncoded")];
  buttons.forEach((b) => (b.disabled = true));
  let failures = 0;
  for (let i = 0; i < targets.length; i++) {
    $("#progress").textContent = `Coding passage ${i + 1} of ${targets.length}…`;
    const r = await codeOne(targets[i].id);
    if (r.error) { failures++; if (failures >= 3 && i < 3) { toast("Stopping: the first passages all failed. Check Settings."); break; } }
  }
  $("#progress").textContent = failures ? `Done, ${failures} failed` : "Done";
  buttons.forEach((b) => (b.disabled = false));
  await refreshDoc();
}
$("#code-all").addEventListener("click", () => codeMany(false));
$("#code-uncoded").addEventListener("click", () => codeMany(true));

renderReview();
