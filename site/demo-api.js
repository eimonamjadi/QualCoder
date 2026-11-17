// QualCoder browser demo backend.
//
// The real app is a FastAPI server (qualcode/main.py). GitHub Pages can only serve
// static files, so this script answers the UI's /api/* requests in the browser
// instead: same routes, same JSON shapes, data kept in localStorage. Only the
// offline "mock" coder runs here; it is a port of MockProvider in
// qualcode/providers.py and is NOT a language model. Coding logic mirrors
// qualcode/coding.py, retrieval.py and segment.py; keep them in step.
(() => {
  "use strict";
  const STORE_KEY = "qualcoder-demo-v1";
  const BASE = new URL(".", document.currentScript.src).href;

  // ---------------------------------------------------------------- storage
  const TABLES = ["codes", "examples", "prompts", "documents", "passages", "runs", "applications"];
  function emptyDb() {
    const db = { seq: {}, settings: { provider: "mock" } };
    for (const t of TABLES) { db[t] = []; db.seq[t] = 0; }
    return db;
  }
  let db;
  try { db = JSON.parse(localStorage.getItem(STORE_KEY)) || emptyDb(); } catch { db = emptyDb(); }
  const save = () => { try { localStorage.setItem(STORE_KEY, JSON.stringify(db)); } catch { /* private mode */ } };
  const now = () => new Date().toISOString().slice(0, 19).replace("T", " ");
  function insert(table, row) {
    const id = ++db.seq[table];
    db[table].push({ id, ...row });
    return id;
  }
  const byId = (table, id) => db[table].find((r) => r.id === id) || null;
  const update = (table, id, fields) => Object.assign(byId(table, id), fields);
  const copy = (x) => JSON.parse(JSON.stringify(x));

  function deleteExample(id) {
    db.examples = db.examples.filter((e) => e.id !== id);
    for (const a of db.applications) if (a.learned_example_id === id) a.learned_example_id = null;
  }
  function deleteApplications(pred) { db.applications = db.applications.filter((a) => !pred(a)); }
  function deleteCode(id) {
    for (const e of db.examples.filter((e) => e.code_id === id)) deleteExample(e.id);
    deleteApplications((a) => a.code_id === id);
    db.codes = db.codes.filter((c) => c.id !== id);
  }
  function deleteDocument(id) {
    const pids = new Set(db.passages.filter((p) => p.document_id === id).map((p) => p.id));
    db.runs = db.runs.filter((r) => !pids.has(r.passage_id));
    deleteApplications((a) => pids.has(a.passage_id));
    db.passages = db.passages.filter((p) => !pids.has(p.id));
    db.documents = db.documents.filter((d) => d.id !== id);
  }

  // ---------------------------------------------------------------- retrieval.py
  const STOPWORDS = new Set(`a an and are as at be been but by for from had has have he her his i if in into is it
    its me my no not of on or our she so than that the their them then there these they this
    to too us was we were what when which who will with would you your just like really very
    um uh yeah okay ok kind sort`.split(/\s+/));
  function tokenize(text) {
    const out = [];
    for (let tok of String(text).toLowerCase().match(/[a-z0-9']+/g) || []) {
      tok = tok.replace(/^'+|'+$/g, "");
      if (tok.length < 2 || STOPWORDS.has(tok)) continue;
      for (const suffix of ["ing", "ed", "es", "s"]) {
        if (tok.length > suffix.length + 3 && tok.endsWith(suffix)) { tok = tok.slice(0, -suffix.length); break; }
      }
      out.push(tok);
    }
    return out;
  }
  const counter = (toks) => { const m = new Map(); for (const t of toks) m.set(t, (m.get(t) || 0) + 1); return m; };
  function bm25Top(items, query, k, k1 = 1.5, b = 0.75) {
    const docs = items.map((i) => counter(tokenize(i.text)));
    const lengths = docs.map((d) => [...d.values()].reduce((x, y) => x + y, 0));
    const avg = items.length ? lengths.reduce((x, y) => x + y, 0) / items.length : 0;
    const df = new Map();
    for (const d of docs) for (const t of d.keys()) df.set(t, (df.get(t) || 0) + 1);
    const n = items.length, idf = new Map();
    for (const [t, f] of df) idf.set(t, Math.log(1 + (n - f + 0.5) / (f + 0.5)));
    const q = tokenize(query);
    const scored = items.map((item, i) => {
      let s = 0;
      for (const t of q) {
        const tf = docs[i].get(t);
        if (!tf) continue;
        s += idf.get(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * lengths[i] / (avg || 1)));
      }
      return [s, item];
    });
    scored.sort((x, y) => y[0] - x[0]);
    return scored.slice(0, k).filter(([s]) => s > 0).map(([, item]) => item);
  }

  // ---------------------------------------------------------------- segment.py
  const SPEAKER_RE = /^\s*[A-Z][\w .'\-]{0,40}:\s/;
  function segment(text, maxChars = 1200) {
    const units = [];
    let pos = 0;
    for (const block of text.split(/(\n\s*\n)/)) {
      if (!block.trim() || /^\n\s*\n$/.test(block)) { pos += block.length; continue; }
      let lineStart = pos, curStart = null, curEnd = pos;
      for (const line of block.match(/[^\n]*\n|[^\n]+$/g) || []) {
        if (line.trim()) {
          const s = lineStart + (line.length - line.trimStart().length);
          const e = lineStart + line.trimEnd().length;
          if (curStart !== null && SPEAKER_RE.test(line)) { units.push([curStart, curEnd]); curStart = null; }
          if (curStart === null) curStart = s;
          curEnd = e;
        }
        lineStart += line.length;
      }
      if (curStart !== null) units.push([curStart, curEnd]);
      pos += block.length;
    }
    const passages = [];
    for (const [start, end] of units) {
      const chunk = text.slice(start, end);
      if (chunk.length <= maxChars) { passages.push({ text: chunk, start, end }); continue; }
      let pieceStart = start, cursor = start;
      const ends = [...chunk.matchAll(/(?<=[.!?])\s+/g)].map((m) => m.index).concat([chunk.length]);
      for (const se of ends) {
        const absEnd = start + se;
        if (absEnd - pieceStart > maxChars && cursor > pieceStart) {
          passages.push({ text: text.slice(pieceStart, cursor).trim(), start: pieceStart, end: cursor });
          pieceStart = cursor;
          while (pieceStart < end && /\s/.test(text[pieceStart])) pieceStart++;
        }
        cursor = absEnd;
      }
      if (cursor > pieceStart) passages.push({ text: text.slice(pieceStart, cursor).trim(), start: pieceStart, end: cursor });
    }
    return passages.filter((p) => p.text);
  }

  // ---------------------------------------------------------------- coding.py
  let BASE_INSTRUCTIONS = "";
  const MAX_ALL_CODES = 40, TOP_CODES = 15, TOP_EXAMPLES = 8;

  const codeLine = (c) => {
    const parts = [`[CODE:${c.id}] ${c.name} (code_id=${c.id}): ${c.definition.trim()}`];
    if (c.include_when.trim()) parts.push(`  Include when: ${c.include_when.trim()}`);
    if (c.exclude_when.trim()) parts.push(`  Exclude when: ${c.exclude_when.trim()}`);
    return parts.join("\n");
  };
  const exampleLine = (e, names) => {
    const label = e.polarity === "positive" ? "positive" : "NEGATIVE";
    let line = `[EX:${e.id}] (${label}, code: ${names[e.code_id] ?? "?"}, from ${e.source}) "${e.quote.trim()}"`;
    if (e.note.trim()) line += ` — ${e.note.trim()}`;
    return line;
  };

  function buildRequest(passage) {
    const codes = db.codes, examples = db.examples, prompts = db.prompts.filter((p) => p.active);
    const names = Object.fromEntries(codes.map((c) => [c.id, c.name]));
    const prev = db.passages.find((p) => p.document_id === passage.document_id && p.idx === passage.idx - 1);
    let shownCodes = codes, codesInSystem = true;
    if (codes.length > MAX_ALL_CODES) {
      shownCodes = bm25Top(codes.map((c) => ({ text: `${c.name} ${c.definition} ${c.include_when}`, payload: c })), passage.text, TOP_CODES).map((i) => i.payload);
      codesInSystem = false;
    }
    const shownExamples = bm25Top(
      examples.map((e) => ({ text: `${e.quote} ${e.note} ${names[e.code_id] ?? ""}`, payload: e })), passage.text, TOP_EXAMPLES,
    ).map((i) => i.payload);

    const system = [BASE_INSTRUCTIONS];
    if (prompts.length) system.push("## Researcher instructions\n" + prompts.map((p) => `[PROMPT:${p.id}] ${p.name}: ${p.text.trim()}`).join("\n"));
    const codebook = "## Codebook\n" + (shownCodes.map(codeLine).join("\n") || "(empty)");
    if (codesInSystem) system.push(codebook);
    const user = [];
    if (!codesInSystem) user.push(codebook.replace("## Codebook", "## Codebook entries most relevant to this passage"));
    if (shownExamples.length) user.push("## Retrieved examples and past corrections\n" + shownExamples.map((e) => exampleLine(e, names)).join("\n"));
    if (prev) user.push(`## Preceding passage (context only, do not code)\n${prev.text}`);
    user.push(`## Passage to code\n<passage>\n${passage.text}\n</passage>`);
    return {
      system_prompt: system.join("\n\n"),
      user_prompt: user.join("\n\n"),
      passage: passage.text,
      codes: shownCodes,
      examples: shownExamples,
      context_refs: [...shownCodes.map((c) => `CODE:${c.id}`), ...shownExamples.map((e) => `EX:${e.id}`), ...prompts.map((p) => `PROMPT:${p.id}`)],
    };
  }

  const normalize = (s) => String(s).replace(/\s+/g, " ").trim().toLowerCase().replace(/^["'“”‘’ ]+|["'“”‘’ ]+$/g, "");
  function validate(app, req, validIds) {
    const flags = [];
    if (!validIds.has(app.code_id)) flags.push(`code_id ${app.code_id} is not in the codebook`);
    const quote = normalize(app.evidence_quote || "");
    if (!quote) flags.push("no evidence quote");
    else if (!normalize(req.passage).includes(quote)) flags.push("evidence quote is not verbatim in the passage");
    const shown = new Set(req.context_refs);
    const bad = (app.cited_refs || []).filter((r) => !shown.has(r));
    if (bad.length) flags.push("cited refs the model was not shown: " + bad.join(", "));
    if (!(app.cited_refs || []).length) flags.push("no codebook reference cited");
    return flags;
  }

  // Python json.dumps formatting, so the "raw reply" looks like the real app's.
  const pyJson = (v, ascii = true) => {
    const s = JSON.stringify(v, null, 0)
      .replace(/("(?:[^"\\]|\\.)*")|([,:])/g, (m, str, sep) => str || (sep === "," ? ", " : ": "));
    return ascii ? s.replace(/[\u007f-￿]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0")) : s;
  };

  // MockProvider from providers.py: a keyword matcher, not a model.
  function mockCode(req) {
    const passageTokens = new Set(tokenize(req.passage));
    const sentences = req.passage.split(/(?<=[.!?])\s+/).filter((s) => s.trim());
    if (!sentences.length) sentences.push(req.passage);
    const overlap = (a, b) => [...a].filter((t) => b.has(t)).length;
    const apps = [];
    for (const code of req.codes) {
      const codeTerms = new Set(tokenize(`${code.name} ${code.definition} ${code.include_when}`));
      const pos = req.examples.filter((e) => e.code_id === code.id && e.polarity === "positive");
      const neg = req.examples.filter((e) => e.code_id === code.id && e.polarity === "negative");
      const exTerms = new Set(pos.flatMap((e) => tokenize(e.quote)));
      const hits = new Set([...passageTokens].filter((t) => codeTerms.has(t) || exTerms.has(t)));
      if (hits.size < 3) continue;
      if (neg.some((e) => {
        const q = new Set(tokenize(e.quote));
        const union = new Set([...passageTokens, ...q]);
        return passageTokens.size && q.size && overlap(passageTokens, q) / union.size > 0.6;
      })) continue;
      const maxBy = (arr, f) => arr.reduce((best, x) => (f(x) > f(best) ? x : best));
      const best = maxBy(sentences, (s) => overlap(new Set(tokenize(s)), hits));
      const refs = [`CODE:${code.id}`];
      if (pos.length) refs.push(`EX:${maxBy(pos, (e) => overlap(new Set(tokenize(e.quote)), passageTokens)).id}`);
      apps.push({
        code_id: code.id,
        evidence_quote: best.trim(),
        justification: `[mock] Shares the terms ${[...hits].sort().slice(0, 6).join(", ")} with the definition of '${code.name}' and its examples.`,
        cited_refs: refs,
        confidence: hits.size >= 5 ? "high" : "medium",
      });
    }
    const data = { applications: apps };
    return { data, raw: pyJson(data), model: "mock-keyword-matcher" };
  }

  function codePassage(passageId) {
    const passage = byId("passages", passageId);
    if (!passage) throw new HttpError(404, "Passage not found");
    const req = buildRequest(passage);
    const result = mockCode(req);
    const runId = insert("runs", {
      passage_id: passageId, provider: "mock", model: result.model,
      system_prompt: req.system_prompt, user_prompt: req.user_prompt, context_refs: req.context_refs,
      raw_response: result.raw, error: null, created_at: now(),
    });
    deleteApplications((a) => a.passage_id === passageId && a.status === "pending" && a.origin === "model");
    const reviewed = new Set(db.applications.filter((a) => a.passage_id === passageId && a.status !== "pending").map((a) => a.code_id));
    const validIds = new Set(db.codes.map((c) => c.id));
    const created = [];
    for (const app of result.data.applications) {
      if (reviewed.has(app.code_id)) continue;
      const flags = validate(app, req, validIds);
      if (!validIds.has(app.code_id)) continue;
      created.push(newApplication({
        passage_id: passageId, code_id: app.code_id, run_id: runId, origin: "model",
        evidence_quote: app.evidence_quote, justification: app.justification, cited_refs: app.cited_refs,
        confidence: app.confidence, flags, status: "pending",
      }));
    }
    return { run_id: runId, error: null, applications: created };
  }

  function newApplication(fields) {
    return insert("applications", {
      run_id: null, origin: "model", evidence_quote: "", justification: "", cited_refs: [], confidence: "medium",
      flags: [], status: "pending", reviewer_note: "", learned_example_id: null, created_at: now(), ...fields,
    });
  }

  function resolveRef(ref) {
    const [kind, raw] = ref.split(":");
    if (!/^\d+$/.test(raw || "")) return null;
    const i = +raw;
    if (kind === "CODE") {
      const c = byId("codes", i);
      return c && { ref, type: "Codebook entry", title: c.name, text: c.definition, include_when: c.include_when, exclude_when: c.exclude_when };
    }
    if (kind === "EX") {
      const e = byId("examples", i);
      const title = e && byId("codes", e.code_id)?.name;
      return e && { ref, type: `${e.polarity[0].toUpperCase()}${e.polarity.slice(1)} example (${e.source})`, title, text: e.quote, note: e.note };
    }
    if (kind === "PROMPT") {
      const p = byId("prompts", i);
      return p && { ref, type: "Researcher prompt", title: p.name, text: p.text };
    }
    return null;
  }

  function exportTraining() {
    const pids = new Set(db.applications.filter((a) => a.status !== "pending").map((a) => a.passage_id));
    const passages = db.passages.filter((p) => pids.has(p.id)).sort((a, b) => a.document_id - b.document_id || a.idx - b.idx);
    const lines = passages.map((p) => {
      const req = buildRequest(p);
      const accepted = db.applications.filter((a) => a.passage_id === p.id && a.status === "accepted");
      const target = { applications: accepted.map((a) => ({ code_id: a.code_id, evidence_quote: a.evidence_quote, justification: a.justification, cited_refs: a.cited_refs, confidence: a.confidence })) };
      return pyJson({ messages: [
        { role: "system", content: req.system_prompt },
        { role: "user", content: req.user_prompt },
        { role: "assistant", content: pyJson(target, false) },
      ] }, false);
    });
    return lines.join("\n") + (lines.length ? "\n" : "");
  }

  function exportSegments() {
    const rows = [];
    const docs = [...db.documents].sort((a, b) => a.id - b.id);
    for (const d of docs) {
      const passages = db.passages.filter((p) => p.document_id === d.id).sort((a, b) => a.idx - b.idx);
      for (const p of passages) {
        for (const a of db.applications.filter((x) => x.passage_id === p.id).sort((x, y) => x.id - y.id)) {
          rows.push({
            document: d.name, passage: p.idx, start_char: p.start_char, end_char: p.end_char, code: byId("codes", a.code_id)?.name,
            status: a.status, origin: a.origin, evidence_quote: a.evidence_quote, justification: a.justification,
            cited_refs: a.cited_refs.join(" "), confidence: a.confidence, flags: a.flags.join("; "),
            reviewer_note: a.reviewer_note, model: a.run_id ? byId("runs", a.run_id)?.model ?? "" : "",
          });
        }
      }
    }
    if (!rows.length) return "";
    const cell = (v) => { const s = String(v ?? ""); return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    const cols = Object.keys(rows[0]);
    return [cols, ...rows.map((r) => cols.map((c) => r[c]))].map((r) => r.map(cell).join(",")).join("\r\n") + "\r\n";
  }

  // ---------------------------------------------------------------- main.py routes
  class HttpError extends Error { constructor(status, detail) { super(detail); this.status = status; } }
  const need = (row, what = "Not found") => { if (!row) throw new HttpError(404, what); return row; };
  const learn = (code_id, quote, note, polarity, source) => insert("examples", { code_id, quote, note, polarity, source, created_at: now() });

  function storeDocument(name, text) {
    const id = insert("documents", { name, text, created_at: now() });
    segment(text).forEach((p, i) => insert("passages", { document_id: id, idx: i, text: p.text, start_char: p.start, end_char: p.end }));
    return id;
  }
  function importCodebook(data) {
    let n = 0;
    for (const c of data.codes || []) {
      if (db.codes.some((x) => x.name === c.name)) continue;
      const id = insert("codes", { name: c.name, definition: c.definition || "", include_when: c.include_when || "", exclude_when: c.exclude_when || "" });
      for (const e of c.examples || []) learn(id, e.quote, e.note || "", e.polarity || "positive", "user");
      n++;
    }
    return n;
  }
  function addHumanCode(passageId, { code_id, evidence_quote = "", note = "" }) {
    const passage = need(byId("passages", passageId));
    need(byId("codes", code_id), "Unknown code");
    const quote = evidence_quote || passage.text;
    const n = note || "Applied by the researcher.";
    const exId = learn(code_id, quote, n, "positive", "correction");
    return newApplication({
      passage_id: passageId, code_id, origin: "human", evidence_quote: quote, justification: n,
      confidence: "high", status: "accepted", reviewer_note: note, learned_example_id: exId,
    });
  }
  const PROVIDERS = [
    { name: "claude", model: "claude-opus-5-5", configured: false },
    { name: "openai", model: "(set QUALCODE_OPENAI_MODEL)", configured: false },
    { name: "ollama", model: "llama3.1", configured: false },
    { name: "mock", model: "mock-keyword-matcher", configured: true },
  ];
  const settings = () => ({
    provider: db.settings.provider,
    learn_from_accepted: (db.settings.learn_from_accepted ?? "1") === "1",
    providers: PROVIDERS,
  });
  const nameRequired = (b) => { if (!b.name || !String(b.name).length) throw new HttpError(422, "name is required"); };

  const routes = [
    ["GET", /^\/api\/codes$/, () => db.codes.map((c) => ({ ...c, examples: db.examples.filter((e) => e.code_id === c.id) }))],
    ["POST", /^\/api\/codes$/, (m, b) => {
      nameRequired(b);
      if (db.codes.some((c) => c.name === b.name)) throw new HttpError(409, "A code with that name exists");
      return { id: insert("codes", { name: b.name, definition: b.definition || "", include_when: b.include_when || "", exclude_when: b.exclude_when || "" }) };
    }],
    ["PUT", /^\/api\/codes\/(\d+)$/, (m, b) => {
      nameRequired(b); need(byId("codes", +m[1]));
      update("codes", +m[1], { name: b.name, definition: b.definition || "", include_when: b.include_when || "", exclude_when: b.exclude_when || "" });
      return { ok: true };
    }],
    ["DELETE", /^\/api\/codes\/(\d+)$/, (m) => { deleteCode(+m[1]); return { ok: true }; }],
    ["POST", /^\/api\/codes\/(\d+)\/examples$/, (m, b) => {
      need(byId("codes", +m[1]));
      if (!b.quote) throw new HttpError(422, "quote is required");
      return { id: learn(+m[1], b.quote, b.note || "", b.polarity === "negative" ? "negative" : "positive", "user") };
    }],
    ["DELETE", /^\/api\/examples\/(\d+)$/, (m) => { deleteExample(+m[1]); return { ok: true }; }],
    ["GET", /^\/api\/codebook\/export$/, () => download("codebook.json", "application/json", JSON.stringify({
      codes: db.codes.map((c) => ({
        name: c.name, definition: c.definition, include_when: c.include_when, exclude_when: c.exclude_when,
        examples: db.examples.filter((e) => e.code_id === c.id && e.source === "user").map((e) => ({ quote: e.quote, note: e.note, polarity: e.polarity })),
      })),
    }, null, 2))],
    ["POST", /^\/api\/codebook\/import$/, async (m, b, form) => {
      let data;
      try { data = JSON.parse(await form.get("file").text()); } catch { throw new HttpError(400, "Codebook must be JSON"); }
      return { imported: importCodebook(data) };
    }],
    ["GET", /^\/api\/prompts$/, () => db.prompts],
    ["POST", /^\/api\/prompts$/, (m, b) => { nameRequired(b); return { id: insert("prompts", { name: b.name, text: b.text || "", active: b.active === false ? 0 : 1 }) }; }],
    ["PUT", /^\/api\/prompts\/(\d+)$/, (m, b) => {
      nameRequired(b); need(byId("prompts", +m[1]));
      update("prompts", +m[1], { name: b.name, text: b.text || "", active: b.active === false ? 0 : 1 });
      return { ok: true };
    }],
    ["DELETE", /^\/api\/prompts\/(\d+)$/, (m) => { db.prompts = db.prompts.filter((p) => p.id !== +m[1]); return { ok: true }; }],
    ["GET", /^\/api\/base-instructions$/, () => ({ text: BASE_INSTRUCTIONS })],
    ["GET", /^\/api\/documents$/, () => db.documents.map((d) => {
      const pids = new Set(db.passages.filter((p) => p.document_id === d.id).map((p) => p.id));
      const apps = db.applications.filter((a) => pids.has(a.passage_id));
      return { id: d.id, name: d.name, created_at: d.created_at, passages: pids.size,
        pending: apps.filter((a) => a.status === "pending").length, accepted: apps.filter((a) => a.status === "accepted").length };
    })],
    ["POST", /^\/api\/documents\/upload$/, async (m, b, form) => {
      const file = form.get("file");
      const fname = file?.name || "upload.txt";
      if (/\.(docx|pdf)$/i.test(fname)) throw new HttpError(400, "The browser demo reads .txt and .md only. Run QualCoder locally for .docx and .pdf.");
      if (!/\.(txt|md|text)$/i.test(fname)) throw new HttpError(400, "Unsupported file type. Use .txt, .md, .docx or .pdf");
      const text = await file.text();
      if (!text.trim()) throw new HttpError(400, "No text found in that file");
      return { id: storeDocument(form.get("name") || fname || "Untitled", text) };
    }],
    ["POST", /^\/api\/documents\/text$/, (m, b) => {
      nameRequired(b);
      if (!b.text) throw new HttpError(422, "text is required");
      return { id: storeDocument(b.name, b.text) };
    }],
    ["GET", /^\/api\/documents\/(\d+)$/, (m) => {
      const d = need(byId("documents", +m[1]));
      const passages = db.passages.filter((p) => p.document_id === d.id).sort((a, b) => a.idx - b.idx);
      return {
        id: d.id, name: d.name, created_at: d.created_at,
        passages: passages.map((p) => {
          const runs = db.runs.filter((r) => r.passage_id === p.id);
          const last = runs.length ? runs.reduce((x, y) => (y.id > x.id ? y : x)) : null;
          return {
            ...p,
            applications: db.applications.filter((a) => a.passage_id === p.id).sort((x, y) => x.id - y.id).map((a) => {
              const run = a.run_id ? byId("runs", a.run_id) : null;
              return { ...a, code_name: byId("codes", a.code_id)?.name, provider: run?.provider ?? null, model: run?.model ?? null };
            }),
            last_run: last && { passage_id: p.id, id: last.id, error: last.error },
          };
        }),
      };
    }],
    ["DELETE", /^\/api\/documents\/(\d+)$/, (m) => { deleteDocument(+m[1]); return { ok: true }; }],
    ["POST", /^\/api\/passages\/(\d+)\/code$/, (m) => {
      if (db.settings.provider !== "mock") throw new HttpError(400, `The ${db.settings.provider} provider is not configured. See the README.`);
      return codePassage(+m[1]);
    }],
    ["GET", /^\/api\/passages\/(\d+)\/preview$/, (m) => {
      const r = buildRequest(need(byId("passages", +m[1])));
      return { system_prompt: r.system_prompt, user_prompt: r.user_prompt, context_refs: r.context_refs };
    }],
    ["GET", /^\/api\/runs\/(\d+)$/, (m) => need(byId("runs", +m[1]))],
    ["GET", /^\/api\/refs\/(.+)$/, (m) => need(resolveRef(decodeURIComponent(m[1])), "That reference no longer exists")],
    ["POST", /^\/api\/applications\/(\d+)\/review$/, (m, b) => {
      const id = +m[1], a = need(byId("applications", id)), passage = byId("passages", a.passage_id);
      if (b.action === "undo") {
        if (a.learned_example_id) deleteExample(a.learned_example_id);
        if (a.origin === "human") deleteApplications((x) => x.id === id);
        else update("applications", id, { status: "pending", reviewer_note: "", learned_example_id: null });
        return { ok: true };
      }
      if (a.status !== "pending") throw new HttpError(409, "Already reviewed; undo first");
      const note = b.note || "", quote = a.evidence_quote || passage.text;
      if (b.action === "accept") {
        const exId = (db.settings.learn_from_accepted ?? "1") === "1" ? learn(a.code_id, quote, note || a.justification, "positive", "accepted") : null;
        update("applications", id, { status: "accepted", reviewer_note: note, learned_example_id: exId });
        return { ok: true };
      }
      if (b.action === "recode" && (b.new_code_id == null || !byId("codes", b.new_code_id))) throw new HttpError(400, "new_code_id must be an existing code");
      const exId = learn(a.code_id, passage.text, note || "Researcher rejected this code for this passage.", "negative", "correction");
      update("applications", id, { status: "rejected", reviewer_note: note, learned_example_id: exId });
      if (b.action === "recode") addHumanCode(a.passage_id, { code_id: b.new_code_id, evidence_quote: quote, note });
      return { ok: true };
    }],
    ["POST", /^\/api\/passages\/(\d+)\/applications$/, (m, b) => ({ id: addHumanCode(+m[1], b) })],
    ["GET", /^\/api\/settings$/, settings],
    ["PUT", /^\/api\/settings$/, (m, b) => {
      if (b.provider != null) {
        if (!PROVIDERS.some((p) => p.name === b.provider)) throw new HttpError(400, `Unknown provider '${b.provider}'`);
        db.settings.provider = b.provider;
      }
      if (b.learn_from_accepted != null) db.settings.learn_from_accepted = b.learn_from_accepted ? "1" : "0";
      return settings();
    }],
    ["GET", /^\/api\/export\/segments\.csv$/, () => download("coded_segments.csv", "text/csv", exportSegments())],
    ["GET", /^\/api\/export\/training\.jsonl$/, () => download("training_data.jsonl", "application/jsonl", exportTraining())],
    ["POST", /^\/api\/samples\/load$/, async () => {
      const [data, transcript] = await Promise.all([
        fetch(BASE + "samples/codebook.json").then((r) => r.json()),
        fetch(BASE + "samples/interview_transcript.txt").then((r) => r.text()),
      ]);
      const n = importCodebook(data);
      for (const p of data.prompts || []) if (!db.prompts.some((x) => x.name === p.name)) insert("prompts", { name: p.name, text: p.text, active: 1 });
      let docId = null;
      if (!db.documents.some((d) => d.name === "interview_transcript.txt")) docId = storeDocument("interview_transcript.txt", transcript);
      return { codes_imported: n, document_id: docId };
    }],
  ];

  class Download { constructor(name, type, body) { Object.assign(this, { name, type, body }); } }
  const download = (name, type, body) => new Download(name, type, body);

  async function handle(method, path, body, form) {
    for (const [m, re, fn] of routes) {
      const match = m === method && path.match(re);
      if (match) {
        const result = await fn(match, body || {}, form);
        if (method !== "GET") save();
        return result;
      }
    }
    throw new HttpError(404, "Not Found");
  }

  // ---------------------------------------------------------------- fetch shim
  const realFetch = window.fetch.bind(window);
  const ready = realFetch(BASE + "base_instructions.json").then((r) => r.json()).then((j) => { BASE_INSTRUCTIONS = j.text; });

  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    if (!url.startsWith("/api/")) return realFetch(input, init);
    await ready;
    const method = (init.method || "GET").toUpperCase();
    let body = null, form = null;
    if (init.body instanceof FormData) form = init.body;
    else if (typeof init.body === "string") body = JSON.parse(init.body || "{}");
    try {
      const out = await handle(method, url.split("?")[0], body, form);
      if (out instanceof Download) return new Response(out.body, { headers: { "content-type": out.type } });
      return new Response(JSON.stringify(out), { headers: { "content-type": "application/json" } });
    } catch (err) {
      if (!(err instanceof HttpError)) console.error(err);
      return new Response(JSON.stringify({ detail: err.message }), { status: err.status || 500, statusText: err.message, headers: { "content-type": "application/json" } });
    }
  };

  // Export links are plain <a href="/api/..."> in the UI; turn them into downloads.
  document.addEventListener("click", async (e) => {
    const a = e.target.closest?.('a[href^="/api/"]');
    if (!a) return;
    e.preventDefault();
    try {
      await ready;
      const out = await handle("GET", a.getAttribute("href"), null, null);
      const blob = new Blob([out.body], { type: out.type });
      const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: out.name });
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    } catch (err) { alert(err.message); }
  }, true);

  // Demo banner with a reset button.
  window.qualcoderDemoReset = () => { try { localStorage.removeItem(STORE_KEY); } catch { /* ignore */ } location.reload(); };
  document.addEventListener("DOMContentLoaded", () => {
    const bar = document.createElement("div");
    bar.className = "demo-banner";
    bar.innerHTML = `<b>Browser demo.</b> Coding here uses the offline <code>mock</code> keyword matcher, not an AI model.
      Your data stays in this browser. <a href="../">About QualCoder</a> ·
      <a href="https://github.com/eimonamjadi/QualCoder#run-it">Run the real app</a> ·
      <button class="link" onclick="qualcoderDemoReset()">Reset demo</button>`;
    document.body.prepend(bar);
  });
})();
