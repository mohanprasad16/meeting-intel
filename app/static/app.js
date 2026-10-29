// Minutes UI. Plain DOM, no framework: every piece of user or transcript text goes
// through textContent (or escape()) so a "<script>" line in a meeting stays text.

const $ = (sel) => document.querySelector(sel);
const state = { projects: [], tasks: [], taskStatus: "", person: "", scope: "", meetingCache: new Map() };

const STARTERS = [
  "When is the pilot launching?",
  "What did we decide in the kickoff?",
  "Did Priya deliver what she promised?",
  "What's still pending?",
  "What are the budget concerns?",
  "Summarise the vendor sync",
];

// ---------- helpers ----------

function escape(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : document.createTextNode(c));
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error(body?.detail || `Request failed (${res.status})`);
  return body;
}

const fmtDate = (iso) => (iso ? new Date(iso + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "undated");
const today = new Date().toISOString().slice(0, 10);

// ---------- theme ----------

(function theme() {
  const saved = (() => { try { return localStorage.getItem("theme"); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch { /* private mode */ }
  });
})();

// ---------- projects & meetings ----------

async function loadProjects() {
  state.projects = await api("/api/projects");
  const nav = $("#projects");
  nav.replaceChildren();
  for (const p of state.projects) {
    nav.append(el("div", { class: "project" },
      el("div", { class: "project-name", text: p.name }),
      p.meetings.map((m) => el("button", { class: "meeting", onclick: () => openMeeting(m.id) },
        el("span", { class: "title", text: m.title }),
        el("span", { class: m.extraction === "failed" ? "badge warn" : "badge", text: m.extraction === "failed" ? "retry" : m.format }),
        el("span", { class: "meta", text: `${fmtDate(m.date)} · ${m.tasks} task${m.tasks === 1 ? "" : "s"}` }),
      )),
    ));
  }
  $("#side-empty").hidden = state.projects.length > 0;
  $("#project-list").replaceChildren(...state.projects.map((p) => el("option", { value: p.name })));
  renderScope();
}

function renderScope() {
  const select = $("#scope");
  const current = select.value || state.scope;
  select.replaceChildren(el("option", { value: "", text: "All meetings" }));
  for (const p of state.projects) {
    const group = el("optgroup", { label: p.name });
    group.append(el("option", { value: `p:${p.id}`, text: `Whole project: ${p.name}` }));
    for (const m of p.meetings) group.append(el("option", { value: `m:${p.id}:${m.id}`, text: m.title }));
    select.append(group);
  }
  select.value = [...select.options].some((o) => o.value === current) ? current : "";
}

function scopeBody() {
  const [kind, project, meeting] = ($("#scope").value || "").split(":");
  if (kind === "p") return { project_id: Number(project) };
  if (kind === "m") return { project_id: Number(project), meeting_id: Number(meeting) };
  return {};
}

// ---------- upload ----------

function logLine(name) {
  const li = el("li", { class: "busy" }, el("span", { text: `${name}: reading…` }));
  $("#upload-log").prepend(li);
  return {
    update(text) { li.firstChild.textContent = `${name}: ${text}`; },
    done(cls, text, details = []) {
      li.className = cls;
      li.replaceChildren(el("span", { text: `${name}: ${text}` }));
      if (details.length) li.append(el("details", {}, el("summary", { text: `${details.length} note${details.length > 1 ? "s" : ""}` }), el("ul", {}, details.map((d) => el("li", { text: d })))));
    },
  };
}

function describe(result) {
  const x = result.extraction;
  const notes = [...(result.warnings || [])];
  for (const s of result.skipped || []) notes.push(`Skipped line ${s.source_line}: "${s.text}" (${s.reason})`);
  if (x.status === "failed") notes.push("Couldn't extract tasks and decisions; search still works. Open the meeting to retry.");
  for (const d of x.dropped || []) notes.push(`Dropped an unverified ${d.kind.replace("_", " ")}: "${d.text}" (quote not found)`);
  const summary = `${result.turns} lines · ${x.decisions} decisions · ${x.action_items} tasks${x.status_updates ? ` · ${x.status_updates} updates to earlier tasks` : ""}`;
  return { cls: x.status === "failed" || result.warnings?.length ? "warn" : "ok", summary, notes };
}

async function uploadFile(file) {
  const line = logLine(file.name);
  const form = new FormData();
  form.append("file", file);
  const project = $("#project-input").value.trim();
  if (project) form.append("project", project);
  try {
    const isAudio = /\.(mp3|m4a|wav|ogg|webm)$/i.test(file.name);
    line.update(isAudio ? "listening to the recording…" : "reading and extracting…");
    const result = await api("/api/upload", { method: "POST", body: form });
    if (result.preview) {
      line.update("waiting for your check…");
      const confirmed = await showPreview(result.preview);
      if (!confirmed) return line.done("bad", "import cancelled");
      line.update("importing…");
      const imported = await api("/api/upload/confirm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(confirmed) });
      const d = describe(imported);
      line.done(d.cls, d.summary, d.notes);
    } else {
      const d = describe(result);
      line.done(d.cls, d.summary, d.notes);
    }
  } catch (e) {
    line.done("bad", e.message);
  }
  await refresh();
}

async function uploadMany(files) {
  for (const f of files) await uploadFile(f); // sequential: follow-ups need earlier meetings stored
}

const drop = $("#upload-form");
drop.addEventListener("click", (e) => { if (!e.target.closest("label")) $("#file-input").click(); });
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === drop) $("#file-input").click(); });
$("#file-input").addEventListener("change", (e) => { uploadMany([...e.target.files]); e.target.value = ""; });
for (const type of ["dragenter", "dragover"]) drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("over"); });
for (const type of ["dragleave", "drop"]) drop.addEventListener(type, () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); uploadMany([...e.dataTransfer.files]); });

$("#load-samples").addEventListener("click", async (e) => {
  e.target.disabled = true;
  const names = await api("/api/samples");
  for (const name of names) {
    const line = logLine(name);
    line.update("loading sample…");
    try {
      const r = await api(`/api/samples/${encodeURIComponent(name)}`, { method: "POST" });
      if (r.already_loaded) line.done("ok", "already loaded");
      else { const d = describe(r); line.done(d.cls, d.summary, d.notes); }
    } catch (err) { line.done("bad", err.message); }
    await refresh();
  }
  e.target.disabled = false;
});

// ---------- preview dialog (unknown formats) ----------

function showPreview(p) {
  const dialog = $("#preview");
  $("#pv-title").value = p.title || "";
  $("#pv-date").value = p.date || "";
  $("#pv-project").value = p.project || $("#project-input").value || "";
  const audio = p.source === "audio";
  $("#preview-title").textContent = audio ? "Check the transcript" : "We didn't recognise this format";
  $("#preview-intro").textContent = audio
    ? "AI listened to the recording and wrote this transcript. Names are its guesses and words can be misheard: check them before importing. Nothing is saved yet."
    : "AI suggested a layout rule; code applied it to every line. Check the reading before importing. Nothing is saved yet.";
  $("#pv-stats").textContent = audio
    ? `Heard ${p.total_turns} speaker turns from ${p.speakers.length} voice${p.speakers.length === 1 ? "" : "s"}. First 10 shown.`
    : `Read ${p.total_turns} spoken lines (${Math.round(p.match_ratio * 100)}% of the file) after ${p.attempts} attempt${p.attempts > 1 ? "s" : ""}. First 10 shown.`;
  $("#pv-pattern-wrap").hidden = audio;
  $("#pv-pattern").textContent = p.pattern || "";
  $("#pv-speakers-wrap").hidden = !audio;
  $("#pv-speakers").replaceChildren(...(audio ? p.speakers : []).map((name) =>
    el("label", { class: "small" }, el("span", { class: "muted", text: name }), el("input", { value: name, maxlength: "60", "data-speaker": name }))));
  $("#pv-rows").replaceChildren(...p.turns.map((t) => el("tr", {}, el("td", { text: t.time ?? "—" }), el("td", { text: t.speaker }), el("td", { text: t.text }))));
  $("#pv-skipped-wrap").hidden = !p.skipped.length;
  $("#pv-skipped-label").textContent = `${p.skipped.length} line(s) didn't fit and will be skipped`;
  $("#pv-skipped").replaceChildren(...p.skipped.map((s) => el("li", { text: `L${s.line}: ${s.text}` })));
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      if (dialog.returnValue !== "import") return resolve(null);
      const speakers = Object.fromEntries([...document.querySelectorAll("#pv-speakers input")]
        .filter((i) => i.value.trim() && i.value.trim() !== i.dataset.speaker)
        .map((i) => [i.dataset.speaker, i.value.trim()]));
      resolve({ token: p.token, title: $("#pv-title").value.trim() || p.title, date: $("#pv-date").value || null, project: $("#pv-project").value.trim() || null, speakers });
    }, { once: true });
  });
}

// ---------- chat ----------

function renderStarters() {
  $("#starters").replaceChildren(...STARTERS.map((q) => el("button", { class: "starter", text: q, onclick: () => ask(q) })));
}

const KIND = { c: "Transcript", a: "Task", u: "Task update", d: "Decision", s: "Summary" };

function citationLabel(c) {
  const lines = c.start_line ? ` · L${c.start_line}${c.end_line && c.end_line !== c.start_line ? "–" + c.end_line : ""}` : "";
  return `${c.meeting}${c.time ? " · " + c.time.replace(/^00:/, "") : ""}${lines}`;
}

// Citations render as small numbered markers in the text plus one Sources list below,
// so a well-cited answer stays readable.
function renderAnswer(container, text, citations) {
  const byId = Object.fromEntries(citations.map((c) => [c.id, c]));
  const order = [];
  const num = (id) => (order.includes(id) ? order.indexOf(id) : order.push(id) - 1) + 1;
  const inline = (s) => escape(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(?:\s*,?\s*\[[a-z]\d+\])+/g, (group) => {
      const ids = [...new Set(group.match(/[a-z]\d+/g))].filter((id) => byId[id]);
      return ids.map((id) => `<button class="cite" data-id="${id}">${num(id)}</button>`).join("");
    });
  let html = "", list = false;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const bullet = line.match(/^[*-]\s+(.*)$/);
    if (bullet) { if (!list) { html += "<ul>"; list = true; } html += `<li>${inline(bullet[1])}</li>`; continue; }
    if (list) { html += "</ul>"; list = false; }
    html += `<p>${inline(line)}</p>`;
  }
  if (list) html += "</ul>";
  container.innerHTML = html; // safe: built only from escape()d text and our own tags

  const open = (c) => openMeeting(c.meeting_id, c.start_line, c.end_line);
  container.querySelectorAll(".cite").forEach((b) => {
    const c = byId[b.dataset.id];
    b.title = `${KIND[c.id[0]]}: ${citationLabel(c)}`;
    b.setAttribute("aria-label", `Source ${b.textContent}: ${b.title}`);
    b.addEventListener("click", () => open(c));
  });
  if (order.length) {
    container.append(el("ol", { class: "sources" }, order.map((id, i) => el("li", {},
      el("button", { class: "source", onclick: () => open(byId[id]) },
        el("span", { class: "n", text: i + 1 }),
        el("span", { class: "kind", text: KIND[id[0]] }),
        el("span", { text: citationLabel(byId[id]) })),
    ))));
  }
}

function row(label, ...content) {
  return el("div", { class: "how-row" }, el("span", { text: label }), el("div", {}, ...content));
}

function renderTrace(body, t) {
  const r = t.route;
  const filters = [r.person && `person: ${r.person}`, r.meeting_id && `meeting #${r.meeting_id}`, r.status !== "any" && `status: ${r.status}`].filter(Boolean);
  body.append(row("Route", el("span", { class: "pill", text: r.kind }), el("span", { class: "muted", text: ` decided by ${r.by}` }), filters.length ? el("div", { class: "muted", text: `Filters: ${filters.join(", ")}` }) : null));
  if (t.retrieval) {
    const kw = new Set(t.retrieval.keyword), mn = new Set(t.retrieval.meaning);
    body.append(row("Search", el("div", { class: "muted", text: `keyword found ${kw.size}, meaning found ${mn.size}${t.retrieval.meaning_error ? " (meaning search unavailable)" : ""}` })));
  }
  if (t.sent) {
    const cited = new Set(t.guard?.cited || []);
    body.append(row("Evidence", t.sent.map((id) => el("span", { class: cited.has(id) ? "pill ok" : "pill", title: cited.has(id) ? "cited in the answer" : "sent, not cited", text: id }))));
  }
  const g = t.guard || {};
  const dropped = [...(g.dropped_fake_citation || []).map((s) => `fake citation: ${s}`), ...(g.dropped_uncited || []).map((s) => `uncited claim: ${s}`)];
  body.append(row("Guard", el("span", { class: dropped.length ? "pill bad" : "pill ok", text: dropped.length ? `removed ${dropped.length}` : t.status === "answered" ? "all citations verified" : t.status }),
    g.reason ? el("span", { class: "muted", text: ` ${g.reason}` }) : null,
    dropped.length ? el("ul", {}, dropped.map((d) => el("li", { class: "muted", text: d }))) : null));
  const tm = t.timings || {};
  const total = tm.total || 0.001;
  body.append(row("Time", el("span", { text: `${total.toFixed(2)}s` }), el("span", { class: "muted", text: `  route ${(tm.route || 0).toFixed(2)} · retrieve ${(tm.retrieve || 0).toFixed(2)} · answer ${(tm.answer || 0).toFixed(2)}` }),
    el("div", { class: "timing" }, ["route", "retrieve", "answer"].map((k) => el("span", { class: k, style: `width:${((tm[k] || 0) / total) * 100}%` })))));
  const u = t.usage || {};
  body.append(row("AI usage", el("span", { class: "muted", text: `${u.calls} call${u.calls === 1 ? "" : "s"} · ${u.input_tokens} in / ${u.output_tokens} out tokens${u.models?.length ? " · " + [...new Set(u.models)].join(", ") : ""}` })));
}

async function ask(question) {
  question = question.trim();
  if (!question) return;
  $("#chat-empty").hidden = true;
  const box = $("#messages");
  box.append(el("article", { class: "msg user" }, el("div", { class: "bubble", text: question })));
  const pending = el("article", { class: "msg bot" }, el("div", { class: "bubble" }, el("div", { class: "thinking", text: "Searching your meetings…" })));
  box.append(pending);
  box.scrollTop = box.scrollHeight;
  $("#ask-btn").disabled = true;
  try {
    const res = await api("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, ...scopeBody() }) });
    const node = $("#tpl-answer").content.firstElementChild.cloneNode(true);
    node.classList.add(res.status);
    renderAnswer(node.querySelector(".answer"), res.answer, res.citations);
    renderTrace(node.querySelector(".how-body"), res.trace);
    pending.replaceWith(node);
  } catch (e) {
    pending.classList.add("error");
    pending.querySelector(".bubble").textContent = e.message;
  }
  $("#ask-btn").disabled = false;
  box.scrollTop = box.scrollHeight;
}

const q = $("#question");
$("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); const v = q.value; q.value = ""; autosize(); ask(v); });
q.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ask-form").requestSubmit(); } });
function autosize() { q.style.height = "auto"; q.style.height = Math.min(q.scrollHeight, 160) + "px"; }
q.addEventListener("input", autosize);
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) { e.preventDefault(); q.focus(); }
  if (e.key === "Escape") closeDrawer();
});

// ---------- tasks ----------

async function loadTasks() {
  state.tasks = await api("/api/action-items" + (state.taskStatus ? `?status=${state.taskStatus}` : ""));
  const people = [...new Set(state.tasks.map((t) => t.owner || "Unassigned"))].sort();
  const sel = $("#person-filter");
  const keep = sel.value;
  sel.replaceChildren(el("option", { value: "", text: "Everyone" }), ...people.map((p) => el("option", { value: p, text: p })));
  sel.value = people.includes(keep) ? keep : "";
  renderTasks();
}

const STATE_LABEL = { open: "Open", updated: "In progress", done: "Done", cancelled: "Cancelled" };

function renderTasks() {
  const person = $("#person-filter").value;
  const tasks = state.tasks.filter((t) => !person || (t.owner || "Unassigned") === person);
  $("#tasks-empty").hidden = tasks.length > 0;
  $("#task-list").replaceChildren(...tasks.map((t) => {
    const overdue = t.due_date && t.due_date < today && !["done", "cancelled"].includes(t.status);
    const due = t.due_date ? `Due ${fmtDate(t.due_date)}${t.due_text ? ` ("${t.due_text}")` : ""}` : t.due_text ? `Due: ${t.due_text}` : "No due date";
    return el("li", { class: `task ${t.status}` },
      el("div", { class: "task-top" }, el("span", { class: "task-name", text: t.task }), el("span", { class: `state ${t.status}`, text: STATE_LABEL[t.status] })),
      el("div", { class: "task-meta" },
        el("span", { text: (t.owner || "Unassigned") + (t.owner_in_meeting ? "" : " ⚠ not in meeting") }),
        el("span", { class: overdue ? "overdue" : "", text: due }),
      ),
      el("ul", { class: "timeline" },
        el("li", {}, el("button", { class: "link", text: `Promised in ${t.meeting}`, onclick: () => openMeeting(t.meeting_id, t.line_no, t.line_no) })),
        t.history.map((h) => el("li", { class: h.status },
          el("button", { class: "link", text: `${STATE_LABEL[h.status]} in ${h.meeting}`, onclick: () => openMeeting(h.meeting_id, h.line_no, h.line_no) }),
          h.note ? el("span", { class: "muted", text: ` · ${h.note}` }) : null)),
      ),
    );
  }));
}

$("#task-filter").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  document.querySelectorAll("#task-filter button").forEach((x) => x.setAttribute("aria-selected", x === b));
  state.taskStatus = b.dataset.status;
  loadTasks();
});
$("#person-filter").addEventListener("change", renderTasks);

// ---------- meeting drawer ----------

const speakerColor = (name) => {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `hsl(${h} 55% 45%)`;
};

async function openMeeting(id, from, to) {
  const m = await api(`/api/meetings/${id}`);
  $("#drawer-title").textContent = m.title;
  $("#drawer-meta").textContent = `${m.project} · ${fmtDate(m.date)} · ${m.format.toUpperCase()}`;

  const warn = $("#drawer-warnings");
  warn.replaceChildren();
  if (m.extraction === "failed") {
    warn.append(el("div", { class: "callout bad" }, "Couldn't extract tasks and decisions for this meeting. ",
      el("button", { class: "link", text: "Retry", onclick: async (e) => { e.target.disabled = true; await api(`/api/meetings/${id}/extract`, { method: "POST" }); await refresh(); openMeeting(id); } })));
  }
  if (m.warnings.length) warn.append(el("div", { class: "callout warn" }, el("ul", { class: "decisions" }, m.warnings.map((w) => el("li", { text: w })))));

  $("#drawer-summary").replaceChildren(...(m.summary ? [el("h3", { class: "section-title", text: "Summary" }), el("p", { text: m.summary })] : []));
  $("#drawer-decisions").replaceChildren(...(m.decisions.length ? [
    el("h3", { class: "section-title", text: "Decisions" }),
    el("ul", { class: "decisions" }, m.decisions.map((d) => el("li", {}, d.text, " ", el("button", { class: "link", text: `L${d.line_no}`, onclick: () => highlight(d.line_no, d.line_no) })))),
  ] : []));

  $("#transcript").replaceChildren(...m.turns.map((t) => el("li", { class: `turn${t.flagged ? " flagged" : ""}`, id: `L${t.line}` },
    el("span", { class: "ln", text: `L${t.line}` }),
    el("div", {},
      el("div", { class: "who", style: `color:${speakerColor(t.speaker)}` }, t.speaker, el("time", { text: t.time ? (t.approx ? "≈ " : "") + t.time : "" })),
      el("div", { class: "said", text: t.text })),
  )));

  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawer-backdrop").hidden = false;
  $("#drawer-close").focus();
  if (from) highlight(from, to ?? from);
}

function highlight(from, to) {
  document.querySelectorAll(".turn.hl").forEach((n) => n.classList.remove("hl"));
  for (let n = from; n <= to; n++) document.getElementById(`L${n}`)?.classList.add("hl");
  document.getElementById(`L${from}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawer-backdrop").hidden = true;
}
$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);

// ---------- boot ----------

async function refresh() {
  await Promise.all([loadProjects(), loadTasks()]);
}

renderStarters();
api("/api/health").then((h) => {
  const dot = $("#status-dot");
  dot.classList.add(h.ai_configured ? "ok" : "bad");
  dot.title = h.ai_configured ? "AI connected" : "No GEMINI_API_KEY set";
}).catch(() => $("#status-dot").classList.add("bad"));
refresh();
