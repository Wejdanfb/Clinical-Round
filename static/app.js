/* Clinical Round - single-user front end.
 * Data lives on the server (database); nothing is kept in localStorage,
 * sessionStorage or IndexedDB. Photos chosen in the editor are held in
 * page memory only until they are uploaded.
 */
(function () {
  "use strict";

  const MAX_PHOTO_MB = Number(document.body.dataset.maxPhotoMb || 15);
  const MAX_PHOTOS = Number(document.body.dataset.maxPhotos || 12);
  const OK_EXT = /\.(jpe?g|png)$/i;
  const OK_TYPES = ["image/jpeg", "image/png", "image/pjpeg", ""];

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtDate = (iso, withTime) => {
    if (!iso) return "";
    const d = new Date(iso.length <= 10 ? iso + "T00:00:00" : iso);
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric",
      ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}) });
  };

  const S = {
    csrf: "",
    authRequired: false,
    round: null,         // current round state from the server
    metaDirty: false,
    ed: null,            // editor state
    busy: false,
  };

  // ------------------------------------------------------------------ API
  class ApiErr extends Error { constructor(msg, status) { super(msg); this.status = status; } }

  async function api(method, url, payload) {
    const opts = { method, headers: { "X-CSRF-Token": S.csrf }, credentials: "same-origin" };
    if (payload instanceof FormData) opts.body = payload;
    else if (payload !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(payload); }
    let res;
    try { res = await fetch(url, opts); }
    catch (_) { throw new ApiErr("Cannot reach the application. Check your connection and try again.", 0); }
    if (res.status === 401 && url !== "/api/login") { showLogin(); throw new ApiErr("Please sign in.", 401); }
    if (!res.ok) {
      let msg = "Something went wrong. Please try again.";
      try { const j = await res.json(); if (j.error) msg = j.error; } catch (_) { /* ignore */ }
      if (res.status === 403 && /session has expired/i.test(msg)) await refreshSession();
      throw new ApiErr(msg, res.status);
    }
    const ct = res.headers.get("Content-Type") || "";
    return ct.includes("application/json") ? res.json() : res.blob();
  }

  async function refreshSession() {
    const r = await fetch("/api/session", { credentials: "same-origin" });
    const j = await r.json();
    S.csrf = j.csrf; S.authRequired = j.auth_required;
    $("#btn-logout").hidden = !j.auth_required || !j.signed_in;
    return j;
  }

  // ------------------------------------------------------------------ alerts
  function alertMsg(kind, html, box = $("#alert")) {
    box.className = "alert " + (kind === "ok" ? "alert-ok" : "alert-error");
    box.innerHTML = html;
    box.hidden = false;
    if (box.id === "alert") box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function clearAlert(box = $("#alert")) { box.hidden = true; box.innerHTML = ""; }
  function fail(e, box) { alertMsg("error", esc(e.message || String(e)), box); }

  // ------------------------------------------------------------------ views
  function show(view) {
    ["login", "home", "round"].forEach((v) => { $("#view-" + v).hidden = v !== view; });
    window.scrollTo(0, 0);
  }

  function showLogin() {
    closeEditor(true);
    $("#btn-logout").hidden = true;
    show("login");
    $("#login-form [name=password]").focus();
  }

  async function route() {
    clearAlert();
    const h = location.hash || "#/";
    if (S.metaDirty && !confirm("You have unsaved round details. Leave without saving?")) return;
    S.metaDirty = false;
    try {
      if (h.startsWith("#/round/")) await openRound(h.slice(8));
      else if (h === "#/new") newRound();
      else await loadHome();
    } catch (e) { if (e.status !== 401) fail(e); }
  }

  // ------------------------------------------------------------------ home
  async function loadHome() {
    const data = await api("GET", "/api/rounds");
    show("home");
    const ret = data.retention;
    $("#home-retention").textContent = `Completed rounds are deleted automatically ${ret.completed_days} days after completion; rounds left in progress are deleted after ${ret.inactive_days} days without changes. Word reports are never stored.`;
    const list = $("#rounds-list");
    list.innerHTML = "";
    $("#rounds-empty").hidden = data.rounds.length > 0;
    data.rounds.forEach((r) => {
      const el = document.createElement("div");
      el.className = "round-item" + (r.status === "completed" ? " completed" : "");
      el.innerHTML = `
        <div class="round-main">
          <h3><span dir="auto">${esc(r.department)}</span> · <span dir="auto">${esc(r.facility)}</span></h3>
          <div class="round-meta">
            <span>${esc(fmtDate(r.round_date))}</span>
            <span>${r.observations} observation${r.observations === 1 ? "" : "s"} · ${r.photos} photo${r.photos === 1 ? "" : "s"}</span>
            <span class="status-badge ${r.status}">${r.status === "completed" ? "Completed" : "In progress"}</span>
            <span>Auto-delete: ${esc(fmtDate(r.delete_due))}</span>
          </div>
        </div>
        <div class="round-actions">
          <a class="btn btn-primary" href="#/round/${esc(r.id)}">Open</a>
          <button type="button" class="btn btn-danger" data-del="${esc(r.id)}">Delete</button>
        </div>`;
      list.appendChild(el);
    });
  }

  $("#rounds-list").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-del]");
    if (!b) return;
    if (!confirm("Permanently delete this round, all its observations and photos?")) return;
    try { await api("DELETE", "/api/rounds/" + b.dataset.del); await loadHome(); }
    catch (err) { fail(err); }
  });

  // ------------------------------------------------------------------ round
  const metaForm = $("#meta-form");
  const META_FIELDS = ["facility", "department", "round_date", "nurses", "patients", "supportTeam", "hospitalAction"];

  function newRound() {
    S.round = null;
    show("round");
    metaForm.reset();
    setMetaDisabled(false);
    $("#meta-title").innerHTML = 'New round <span class="ar" lang="ar">جولة جديدة</span>';
    $("#btn-save-meta").textContent = "Create round";
    $("#round-status").hidden = true;
    $("#meta-saved").textContent = "";
    $("#round-body").hidden = true;
    metaForm.elements.facility.focus();
  }

  async function openRound(id) {
    const st = await api("GET", "/api/rounds/" + encodeURIComponent(id));
    S.round = st;
    show("round");
    fillMeta();
    renderRound();
  }

  function fillMeta() {
    const r = S.round.round;
    META_FIELDS.forEach((f) => { metaForm.elements[f].value = r[f] || ""; });
    metaForm.elements.repeatHeader.checked = !!r.repeatHeader;
    $("#meta-title").innerHTML = `<span dir="auto">${esc(r.department)}</span> · <span dir="auto">${esc(r.facility)}</span>`;
    $("#btn-save-meta").textContent = "Save details";
    $("#meta-saved").textContent = "";
    S.metaDirty = false;
  }

  function setMetaDisabled(dis) {
    $$("input, textarea", metaForm).forEach((el) => { el.disabled = dis; });
    $("#btn-save-meta").hidden = dis;
  }

  function renderRound() {
    const st = S.round, r = st.round, open = r.status === "open";
    $("#round-body").hidden = false;
    const badge = $("#round-status");
    badge.hidden = false;
    badge.className = "status-badge " + r.status;
    badge.textContent = open ? "In progress" : "Completed";
    setMetaDisabled(!open);
    $$("[data-open-only]").forEach((b) => { b.hidden = !open; });
    $("#btn-complete").hidden = !open;
    $("#btn-reopen").hidden = open;
    $("#retention-line").textContent = open
      ? `In progress. If nothing changes, this round is deleted automatically on ${fmtDate(r.delete_due, true)}. When you have your final report, mark it completed: it is then kept for ${st.retention.completed_days} more days.`
      : `Completed on ${fmtDate(r.completed_at, true)}. Read-only. This round and all its photos will be permanently deleted on ${fmtDate(r.delete_due, true)}. You can still generate the report until then, or reopen the round to edit it.`;
    $("#obs-count").textContent = st.observations.length;
    $("#obs-empty").hidden = st.observations.length > 0;

    const tb = $("#obs-tbody");
    tb.innerHTML = "";
    const n = st.observations.length;
    st.observations.forEach((o, i) => {
      const tr = document.createElement("tr");
      tr.dataset.id = o.id;
      if (o.wide) tr.className = "wide-row";
      const photos = o.photos.length
        ? `<div class="cell-photos">${o.photos.map((p, k) => `<img loading="lazy" src="/api/photos/${esc(p.id)}?v=thumb" alt="Photo ${k + 1} of observation ${o.no}">`).join("")}</div>`
        : `<span class="none">لايوجد</span>`;
      const textCell = `<td class="c-text" dir="auto" data-label="Observation · الملاحظات"${o.wide ? ' colspan="2"' : ""}>${esc(o.text)}</td>`;
      tr.innerHTML = `
        <td class="c-no">${o.no}</td>
        ${textCell}
        ${o.wide ? "" : `<td class="c-photos" data-label="Photos · توثيقها">${photos}</td>`}
        <td dir="auto" data-label="Recommendations · التوصيات">${esc(o.recommendations) || '<span class="none">—</span>'}</td>
        <td class="c-rep" data-label="Repeated? · هل الملاحظات مكررة">${o.repeated === "yes" ? "نعم" : "لا"}</td>
        <td dir="auto" data-label="Corrective plan · الخطة التصحيحية">${esc(o.correctivePlan) || '<span class="none">—</span>'}</td>
        <td dir="auto" data-label="Documentation · التوثيق">${esc(o.documentation) || '<span class="none">—</span>'}</td>
        <td class="c-act">${open ? `<div class="row-actions">
          <button type="button" class="icon-btn" data-row="up" title="Move up" aria-label="Move observation ${o.no} up" ${i === 0 ? "disabled" : ""}>↑</button>
          <button type="button" class="icon-btn" data-row="down" title="Move down" aria-label="Move observation ${o.no} down" ${i === n - 1 ? "disabled" : ""}>↓</button>
          <button type="button" class="icon-btn" data-row="edit" title="Edit" aria-label="Edit observation ${o.no}">✎</button>
          <button type="button" class="icon-btn danger" data-row="delete" title="Delete" aria-label="Delete observation ${o.no}">✕</button>
        </div>` : ""}</td>`;
      tb.appendChild(tr);
    });
  }

  metaForm.addEventListener("input", () => { S.metaDirty = true; $("#meta-saved").textContent = "Unsaved changes"; });

  metaForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearAlert();
    const f = metaForm.elements;
    const payload = {};
    META_FIELDS.forEach((k) => { payload[k] = f[k].value; });
    payload.repeatHeader = f.repeatHeader.checked;
    const missing = [];
    if (!payload.facility.trim()) missing.push("Facility Name");
    if (!payload.department.trim()) missing.push("Department / Unit");
    if (!payload.round_date) missing.push("Clinical Round Date");
    if (missing.length) { alertMsg("error", "Please fill in: " + esc(missing.join(", ")) + "."); return; }
    const btn = $("#btn-save-meta");
    btn.disabled = true;
    try {
      if (!S.round) {
        const st = await api("POST", "/api/rounds", payload);
        S.metaDirty = false;
        location.hash = "#/round/" + st.round.id;
      } else {
        S.round = await api("PATCH", "/api/rounds/" + S.round.round.id, payload);
        fillMeta();
        renderRound();
        $("#meta-saved").textContent = "Saved ✓";
      }
    } catch (err) { fail(err); }
    finally { btn.disabled = false; }
  });

  $("#obs-tbody").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-row]");
    if (!b || S.busy) return;
    const id = b.closest("tr").dataset.id;
    const list = S.round.observations;
    const idx = list.findIndex((o) => o.id === id);
    const act = b.dataset.row;
    clearAlert();
    try {
      if (act === "edit") openEditor(list[idx]);
      else if (act === "delete") {
        if (!confirm(`Delete observation ${idx + 1} and its photos?`)) return;
        S.round = (await api("DELETE", "/api/observations/" + id)).state;
        renderRound();
      } else if (act === "up" || act === "down") {
        const order = list.map((o) => o.id);
        const j = act === "up" ? idx - 1 : idx + 1;
        if (j < 0 || j >= order.length) return;
        [order[idx], order[j]] = [order[j], order[idx]];
        S.busy = true;
        S.round = await api("POST", `/api/rounds/${S.round.round.id}/reorder`, { order });
        renderRound();
        $(`#obs-tbody tr[data-id="${id}"] [data-row="${act}"]`)?.focus();
      }
    } catch (err) { fail(err); if (err.status === 404 || err.status === 409) reloadRound(); }
    finally { S.busy = false; }
  });

  async function reloadRound() {
    if (!S.round) return;
    try { S.round = await api("GET", "/api/rounds/" + S.round.round.id); renderRound(); } catch (_) { /* shown already */ }
  }

  // ------------------------------------------------------------------ editor
  const editor = $("#editor");

  function openEditor(obs) {
    clearAlert($("#editor-alert"));
    S.ed = {
      id: obs ? obs.id : null,
      version: obs ? obs.version : 0,
      existing: obs ? obs.photos.map((p) => ({ ...p })) : [],
      pending: [],
      dirty: false,
    };
    $("#editor-title").textContent = obs ? `Edit observation ${obs.no}` : "New observation";
    ["text", "recommendations", "correctivePlan", "documentation"].forEach((f) => {
      $(`[data-f="${f}"]`, editor).value = obs ? obs[f] || "" : "";
    });
    $$('[data-f="repeated"]', editor).forEach((r) => { r.checked = !!obs && obs.repeated === r.value; });
    $('[data-f="wide"]', editor).checked = !!(obs && obs.wide);
    $("#editor-progress").textContent = "";
    $$(".invalid", editor).forEach((el) => el.classList.remove("invalid"));
    renderEditorPhotos();
    editor.hidden = false;
    document.body.classList.add("modal-open");
    $('[data-f="text"]', editor).focus();
  }

  function closeEditor(force) {
    if (!S.ed) { editor.hidden = true; return; }
    if (!force && (S.ed.dirty || S.ed.pending.length) &&
        !confirm("Discard the changes to this observation?")) return;
    S.ed.pending.forEach((p) => URL.revokeObjectURL(p.url));
    S.ed = null;
    editor.hidden = true;
    document.body.classList.remove("modal-open");
  }

  function renderEditorPhotos() {
    const ed = S.ed, box = $("#ed-thumbs");
    box.innerHTML = "";
    const all = [...ed.existing.map((p) => ({ kind: "saved", p })), ...ed.pending.map((p) => ({ kind: "new", p }))];
    all.forEach((item, i) => {
      const t = document.createElement("div");
      t.className = "thumb" + (item.kind === "new" ? " pending" : "");
      t.dataset.kind = item.kind;
      t.dataset.id = item.p.id;
      const firstOfKind = item.kind === "saved" ? i === 0 : i === ed.existing.length;
      const lastOfKind = item.kind === "saved" ? i === ed.existing.length - 1 : i === all.length - 1;
      t.innerHTML = `<img alt="Photo ${i + 1}"><span class="num">${i + 1}</span>
        ${item.kind === "new" ? '<span class="pend">not saved</span>' : ""}
        <div class="name"></div>
        <div class="tbar">
          <button type="button" data-ph="left" title="Move earlier" aria-label="Move photo earlier" ${firstOfKind ? "disabled" : ""}>◀</button>
          <button type="button" data-ph="right" title="Move later" aria-label="Move photo later" ${lastOfKind ? "disabled" : ""}>▶</button>
          <button type="button" class="rm" data-ph="remove" title="Remove photo" aria-label="Remove photo">✕</button>
        </div>`;
      $("img", t).src = item.kind === "saved" ? `/api/photos/${item.p.id}?v=thumb` : item.p.url;
      $(".name", t).textContent = item.kind === "saved" ? item.p.filename : item.p.file.name;
      box.appendChild(t);
    });
    const hasPhotos = all.length > 0;
    const wide = $('[data-f="wide"]', editor);
    wide.disabled = hasPhotos;
    if (hasPhotos) wide.checked = false;
    $("#ed-wide-wrap").classList.toggle("disabled", hasPhotos);
    $("#ed-nophoto").hidden = hasPhotos || wide.checked;
  }

  editor.addEventListener("input", (e) => {
    if (!S.ed) return;
    S.ed.dirty = true;
    e.target.classList.remove("invalid");
    if (e.target.dataset.f === "repeated") $(".seg", editor).classList.remove("invalid");
    if (e.target.dataset.f === "wide") $("#ed-nophoto").hidden = e.target.checked;
  });

  function decodable(file) {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => resolve(url);
      img.onerror = () => { URL.revokeObjectURL(url); resolve(null); };
      img.src = url;
    });
  }

  $("#ed-file").addEventListener("change", async (e) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    const errs = [];
    for (const f of files) {
      if (S.ed.existing.length + S.ed.pending.length >= MAX_PHOTOS) { errs.push(`Only ${MAX_PHOTOS} photos are allowed per observation.`); break; }
      if (!OK_EXT.test(f.name) || !OK_TYPES.includes(f.type)) { errs.push(`“${f.name}” is not a JPEG or PNG image.`); continue; }
      if (f.size > MAX_PHOTO_MB * 1024 * 1024) { errs.push(`“${f.name}” is larger than ${MAX_PHOTO_MB} MB.`); continue; }
      const url = await decodable(f);
      if (!url) { errs.push(`“${f.name}” is damaged or cannot be opened as an image.`); continue; }
      S.ed.pending.push({ id: "n" + Math.random().toString(36).slice(2), file: f, url });
    }
    S.ed.dirty = true;
    renderEditorPhotos();
    if (errs.length) alertMsg("error", "<strong>Some photos were not added:</strong><ul>" + errs.map((x) => `<li>${esc(x)}</li>`).join("") + "</ul>", $("#editor-alert"));
  });

  $("#ed-thumbs").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-ph]");
    if (!b || S.busy) return;
    const t = b.closest(".thumb");
    const ed = S.ed;
    const arr = t.dataset.kind === "saved" ? ed.existing : ed.pending;
    const i = arr.findIndex((p) => p.id === t.dataset.id);
    const act = b.dataset.ph;
    clearAlert($("#editor-alert"));
    if (t.dataset.kind === "new") {
      if (act === "remove") { URL.revokeObjectURL(arr[i].url); arr.splice(i, 1); }
      else if (act === "left" && i > 0) [arr[i - 1], arr[i]] = [arr[i], arr[i - 1]];
      else if (act === "right" && i < arr.length - 1) [arr[i + 1], arr[i]] = [arr[i], arr[i + 1]];
      renderEditorPhotos();
      return;
    }
    // saved photo: change on the server right away
    S.busy = true;
    try {
      if (act === "remove") {
        if (!confirm("Remove this saved photo from the observation?")) return;
        const r = await api("DELETE", "/api/photos/" + arr[i].id);
        arr.splice(i, 1);
        ed.version = r.version;
      } else {
        const j = act === "left" ? i - 1 : i + 1;
        if (j < 0 || j >= arr.length) return;
        [arr[i], arr[j]] = [arr[j], arr[i]];
        const r = await api("POST", `/api/observations/${ed.id}/photos/order`, { order: arr.map((p) => p.id) });
        ed.version = r.version;
      }
      renderEditorPhotos();
      reloadRound();
    } catch (err) { fail(err, $("#editor-alert")); }
    finally { S.busy = false; }
  });

  async function saveObservation() {
    const ed = S.ed;
    if (!ed || S.busy) return;
    clearAlert($("#editor-alert"));
    const val = (f) => $(`[data-f="${f}"]`, editor).value;
    const rep = $$('[data-f="repeated"]', editor).find((r) => r.checked);
    const problems = [];
    if (!val("text").trim()) { problems.push("The observation text is required."); $('[data-f="text"]', editor).classList.add("invalid"); }
    if (!rep) { problems.push("Choose Yes or No for “Is the observation repeated?”."); $(".seg", editor).classList.add("invalid"); }
    if (problems.length) { alertMsg("error", problems.map(esc).join("<br>"), $("#editor-alert")); return; }
    const payload = {
      text: val("text"), recommendations: val("recommendations"), repeated: rep.value,
      correctivePlan: val("correctivePlan"), documentation: val("documentation"),
      wide: $('[data-f="wide"]', editor).checked,
    };
    const btn = $("#btn-save-obs");
    const prog = $("#editor-progress");
    S.busy = true; btn.disabled = true;
    try {
      prog.textContent = "Saving…";
      if (!ed.id) {
        const r = await api("POST", `/api/rounds/${S.round.round.id}/observations`, payload);
        ed.id = r.id; ed.version = r.version;
        S.round = r.state;
      } else if (ed.dirty) {
        payload.version = ed.version;
        const r = await api("PATCH", "/api/observations/" + ed.id, payload);
        ed.version = r.version;
        S.round = r.state;
      }
      ed.dirty = false;
      let k = 0;
      while (ed.pending.length) {
        const p = ed.pending[0];
        k += 1;
        prog.textContent = `Uploading photo ${k}…`;
        const fd = new FormData();
        fd.append("photo", p.file, p.file.name);
        const r = await api("POST", `/api/observations/${ed.id}/photos`, fd);
        ed.version = r.version;
        URL.revokeObjectURL(p.url);
        ed.pending.shift();
        ed.existing.push({ id: r.id, filename: p.file.name });
      }
      await reloadRound();
      closeEditor(true);
    } catch (err) {
      prog.textContent = "";
      // observation text may already be saved; keep editor open with what is left
      if (ed.id) $("#editor-title").textContent = "Edit observation";
      renderEditorPhotos();
      fail(err, $("#editor-alert"));
      reloadRound();
    } finally {
      S.busy = false; btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------ actions
  async function generate() {
    const btn = $("#btn-generate");
    const st = $("#gen-status");
    btn.disabled = true; btn.textContent = "Generating…";
    clearAlert();
    try {
      const blob = await api("GET", `/api/rounds/${S.round.round.id}/report.docx`);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = S.round.round.filename; a.rel = "noopener";
      document.body.appendChild(a); a.click();
      setTimeout(() => { a.remove(); URL.revokeObjectURL(url); }, 2000);
      st.textContent = `Downloaded ${S.round.round.filename} (${(blob.size / 1048576).toFixed(1)} MB). Generate again at any time after making changes.`;
    } catch (err) { fail(err); }
    finally { btn.disabled = false; btn.textContent = "Generate Word Report"; }
  }

  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-action]");
    if (!b) return;
    const act = b.dataset.action;
    if (b.tagName === "A") e.preventDefault();
    try {
      switch (act) {
        case "home": closeEditor(); if (!S.ed) { location.hash = "#/"; if (location.hash === "#/") route(); } break;
        case "new-round": location.hash = "#/new"; break;
        case "add-obs": openEditor(null); break;
        case "close-editor": closeEditor(); break;
        case "save-obs": await saveObservation(); break;
        case "generate": await generate(); break;
        case "complete":
          if (!confirm(`Mark this round completed? It becomes read-only and is permanently deleted after ${S.round.retention.completed_days} days. Generate your report first.`)) return;
          S.round = await api("POST", `/api/rounds/${S.round.round.id}/complete`); fillMeta(); renderRound(); break;
        case "reopen":
          S.round = await api("POST", `/api/rounds/${S.round.round.id}/reopen`); fillMeta(); renderRound(); break;
        case "delete-round":
          if (!confirm("Permanently delete this round, all its observations and photos now? This cannot be undone.")) return;
          await api("DELETE", `/api/rounds/${S.round.round.id}`); S.round = null; location.hash = "#/"; break;
        case "logout":
          if (S.ed && !confirm("Sign out and discard the open observation?")) return;
          await api("POST", "/api/logout"); await refreshSession(); showLogin(); break;
      }
    } catch (err) { if (err.status !== 401) fail(err); }
  });

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearAlert();
    const pw = e.target.elements.password;
    try {
      await api("POST", "/api/login", { password: pw.value });
      pw.value = "";
      await refreshSession();
      route();
    } catch (err) { fail(err); pw.select(); }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !editor.hidden) closeEditor();
  });

  window.addEventListener("hashchange", route);
  window.addEventListener("beforeunload", (e) => {
    if ((S.ed && (S.ed.dirty || S.ed.pending.length)) || S.metaDirty) { e.preventDefault(); e.returnValue = ""; }
  });

  // ------------------------------------------------------------------ start
  (async function init() {
    try {
      const s = await refreshSession();
      if (s.auth_required && !s.signed_in) showLogin();
      else route();
    } catch (_) {
      alertMsg("error", "The application could not be reached. Please reload the page.");
    }
  })();
})();
