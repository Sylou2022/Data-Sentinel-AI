/* DataSentinel AI — interface web (Vue d'ensemble, Import, Enquête, Rapport, Journal, Paramètres).
   Toutes les données proviennent de /ui/api : rien n'est inventé côté navigateur. */
"use strict";

const API = "/ui/api";
const CHECKS = [["quality", "Qualité des données"], ["volume", "Volume"], ["freshness", "Fraîcheur"], ["amounts", "Montants"], ["categories", "Catégories"]];
const PAGES = {
  "vue-ensemble": { key: "overview", title: "Vue d'ensemble", tall: false },
  "import": { key: "import", title: "Import et profilage", tall: false },
  "enquete": { key: "investigation", title: "Enquête des agents", tall: true },
  "rapport": { key: "report", title: "Rapport et validation", tall: true },
  "journal": { key: "audit", title: "Journal d'audit", tall: false },
  "parametres": { key: "settings", title: "Paramètres", tall: false },
};
const SEVERITY_CLASS = { critical: "b-critical", high: "b-high", medium: "b-medium", low: "b-low" };
const LEVELS = { observed: ["Observée", "b-observed"], hypothesis: ["Hypothèse", "b-hypothesis"],
                 confirmed: ["Confirmée", "b-confirmed"] };
const AGENT_BADGES = { done: ["Terminé", "b-done"], running: ["En cours", "b-running"], pending: ["En attente", "b-neutral"],
                       skipped: ["Non requis", "b-neutral"], error: ["Échec", "b-critical"] };
const ALERT_CLASS = { "Doublons possibles": "b-high", "Valeurs non conformes": "b-high", "Distribution à contrôler": "b-medium", "Aucune": "b-neutral" };

// ------------------------------------------------------------------ état local
const backend = area => ({
  get(k, d) { try { const v = area.getItem("ds." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { area.setItem("ds." + k, JSON.stringify(v)); },
});
const store = backend(localStorage);
// La source choisie (fichiers CSV, instantané SQL Server) ne vaut que pour la session : sans choix,
// la Vue d'ensemble revient à l'analyse de la base SQL Server reliée.
const session = backend(sessionStorage);
const S = {
  useDefault: session.get("useDefault", true),
  dbPath: session.get("dbPath", null), runId: session.get("runId", null), actor: store.get("actor", "analyste"),
  checks: store.get("checks", CHECKS.map(c => c[0])), sep: store.get("sep", "auto"),
  data: null, filter: "Toutes", profile: null, profileTable: null, expanded: false, openResults: {}, testResult: {},
  audit: null, auditQueries: false, auditAll: false, settings: null, poll: null, busy: false,
};
// Préférences enregistrées avant l'ajout des contrôles de qualité : on les active une fois.
if (!store.get("checks_v2", false)) {
  if (!S.checks.includes("quality")) S.checks.unshift("quality");
  store.set("checks_v2", true);
}
function save() {
  ["actor", "checks", "sep"].forEach(k => store.set(k, S[k]));
  session.set("useDefault", S.useDefault);
  session.set("dbPath", S.useDefault ? null : S.dbPath);
  session.set("runId", S.useDefault ? null : S.runId);
}

// ------------------------------------------------------------------ utilitaires
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const badge = (label, cls, sm) => `<span class="badge ${cls}${sm ? " sm" : ""}">${esc(label)}</span>`;
const $ = sel => document.querySelector(sel);

function toast(msg, error) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (error ? " error" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.className = "toast"; }, 4200);
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.json !== undefined) { init.body = JSON.stringify(opts.json); init.headers["Content-Type"] = "application/json"; }
  if (opts.form) init.body = opts.form;
  if (init.body && !opts.method) init.method = "POST";
  const r = await fetch(API + path, init);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Erreur " + r.status);
  return body;
}

function route() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [name, query] = raw.split("?");
  const page = PAGES[name] ? name : "vue-ensemble";
  return { page, ...PAGES[page], params: new URLSearchParams(query || "") };
}

function resultTable(rows) {
  if (!rows || !rows.length) return `<p class="note">Aucune ligne.</p>`;
  const cols = Object.keys(rows[0]);
  return `<div class="result-wrap"><table class="result"><thead><tr>${cols.map(c => `<th scope="col">${esc(c)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r => `<tr>${cols.map(c => `<td>${esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

// ------------------------------------------------------------------ chargement de l'état
async function loadState() {
  const r = route();
  const q = new URLSearchParams();
  if (!S.useDefault && S.runId) q.set("run_id", S.runId);
  if (!S.useDefault && S.dbPath) q.set("db_path", S.dbPath);
  if (r.params.get("i")) q.set("incident", r.params.get("i"));
  try {
    S.data = await api("/state?" + q);
  } catch (e) {
    if (!S.useDefault) { S.useDefault = true; S.runId = null; S.dbPath = null; save(); return loadState(); }
    throw e;
  }
  if (S.data.run_id) S.runId = S.data.run_id;
  if (S.data.db_path) S.dbPath = S.data.db_path;
  save();
  clearTimeout(S.poll);
  if (S.data.running) S.poll = setTimeout(refresh, 1200);
}

async function refresh() {
  try { await loadState(); } catch (e) { toast(e.message, true); }
  render();
}

// ------------------------------------------------------------------ rendu
function render() {
  const r = route();
  document.title = r.title;
  $("#app").classList.toggle("tall", r.tall);
  document.querySelectorAll(".sidebar a.nav").forEach(a => {
    if (a.dataset.page === r.key) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  const views = { overview: viewOverview, import: viewImport, investigation: viewInvestigation, report: viewReport,
                  audit: viewAudit, settings: viewSettings };
  const focused = document.activeElement && document.activeElement.id;
  $("#main").innerHTML = views[r.key](r);
  if (focused && document.getElementById(focused)) document.getElementById(focused).focus();
}

function pageHead(left, actions) {
  return `<div class="page-head"><div>${left}</div><div class="actions">${actions}</div></div>`;
}

// ================================================================== Écran 1 — Vue d'ensemble
function viewOverview() {
  const d = S.data || { kpis: [], incidents: [], pending: { count: 0 }, source: "" };
  const head = pageHead(`<h1>Vue d'ensemble</h1><div class="subtitle">${esc(d.source)}</div>`,
    `<button type="button" class="btn btn-secondary" data-action="change-source">Changer de source</button>
     <a class="btn btn-secondary" href="#/import">Importer un CSV</a>
     <button type="button" class="btn btn-primary" data-action="analyse" ${d.running ? "disabled" : ""}>
       ${d.running ? "Analyse en cours…" : "Lancer une analyse"}</button>`);
  const kpis = `<section class="kpis" aria-label="Indicateurs">${d.kpis.map(k => {
    const [tone, icon] = kpiTone(k);
    return `<div class="card kpi tone-${tone}"><div class="kpi-top"><span class="kpi-icon" aria-hidden="true">${icon}</span>
      <span class="kpi-label">${esc(k.label)}</span></div><div class="kpi-value">${esc(k.value)}</div>
      <div class="kpi-sub">${esc(k.sub)}</div></div>`;
  }).join("")}</section>`;

  const keep = i => ({ "Critiques": i.severity === "critical", "En cours": !i.resolved, "Résolues": i.resolved }[S.filter] ?? true);
  const rows = d.incidents.filter(keep);
  let body;
  if (d.running) body = `<tr class="empty-row"><td colspan="5">Analyse en cours : les anomalies s'affichent à la fin de l'enquête.</td></tr>`;
  else if (!d.incidents.length) body = `<tr class="empty-row"><td colspan="5">${d.run_id
      ? (d.skipped.length ? "Détection impossible : " + esc(d.skipped.join(" ")) : "Aucune anomalie détectée lors de la dernière analyse.")
      : "Aucune analyse pour cette source : lancez une analyse."}</td></tr>`;
  else if (!rows.length) body = `<tr class="empty-row"><td colspan="5">Aucune anomalie pour ce filtre.</td></tr>`;
  else body = rows.map((i, n) => {
    const [lvl, lcls] = LEVELS[i.level];
    const confirmed = i.level === "confirmed";
    const href = confirmed ? `#/rapport?i=${encodeURIComponent(i.id)}` : `#/enquete?i=${encodeURIComponent(i.id)}`;
    const cls = n === 0 ? "btn btn-primary" : "btn btn-outline";
    if (i.is_quality) return `<tr class="sev-${i.severity}"><td>${badge(i.severity_label, SEVERITY_CLASS[i.severity])}</td>
      <td><div class="title">${esc(i.title)}</div><div class="sub">${esc(i.subtitle)}</div>
        <div class="reco" title="${esc(i.recommendation)}"><strong>Recommandation :</strong> ${esc(i.recommendation)}</div></td>
      <td class="mono">${esc(i.table)}</td><td>${badge(i.status_label, i.status_label === "Erreur confirmée" ? "b-critical" : "b-hypothesis")}</td>
      <td><a class="${cls} btn-compact" href="#/enquete?i=${encodeURIComponent(i.id)}">Détail</a></td></tr>`;
    return `<tr class="sev-${i.severity}"><td>${badge(i.severity_label, SEVERITY_CLASS[i.severity])}</td>
      <td><div class="title">${esc(i.title)}</div><div class="sub">${esc(i.subtitle)}</div>
        <div class="reco" title="${esc(i.recommendation)}"><strong>Recommandation :</strong> ${esc(i.recommendation)}</div></td>
      <td class="mono">${esc(i.table)}</td><td>${badge(lvl, lcls)}</td>
      <td><a class="${cls} btn-compact" href="${href}">${confirmed ? "Rapport" : "Enquêter"}</a></td></tr>`;
  }).join("");
  const table = `<section class="card clip anomalies-card" aria-label="Anomalies détectées">
    <div class="card-head band"><h2><span class="band-count">${d.incidents.length}</span>Anomalies détectées</h2>
      <div class="pills" role="group" aria-label="Filtrer les anomalies">${["Toutes", "Critiques", "En cours", "Résolues"].map(f =>
        `<button type="button" class="pill" data-action="filter" data-value="${f}" aria-pressed="${S.filter === f}">${f}</button>`).join("")}</div>
    </div>
    <div class="table-wrap table-scroll"><table class="data anomalies"><thead><tr>
      <th scope="col">Gravité</th><th scope="col">Anomalie</th><th scope="col">Table</th><th scope="col">Statut</th><th scope="col">Action</th>
    </tr></thead><tbody>${body}</tbody></table></div></section>`;

  const p = d.pending;
  const pending = `<section class="card pad" aria-label="Validation en attente">
    <h2 style="margin-bottom:8px">Décision en attente</h2>
    ${p.count ? `<p class="card-text" style="margin-bottom:16px">${p.count === 1
        ? "Un plan de correction testé sur une copie attend votre validation avant toute modification."
        : `${p.count} plans de correction testés sur une copie attendent votre validation avant toute modification.`}</p>
      <a class="btn btn-dark" href="#/rapport?i=${encodeURIComponent(p.incident || "")}">Examiner le plan</a>`
      : `<p class="card-text" style="margin-bottom:0">Aucun plan de correction n'attend votre validation.</p>`}
  </section>`;
  const legend = `<section class="card pad" aria-label="Légende"><h2 style="margin-bottom:12px">Niveaux de preuve</h2>
    <div class="legend">
      <div>${badge("Observée", "b-observed", true)}<p>Fait mesuré dans les données.</p></div>
      <div>${badge("Hypothèse", "b-hypothesis", true)}<p>Cause plausible, pas encore prouvée.</p></div>
      <div>${badge("Confirmée", "b-confirmed", true)}<p>Cause démontrée par une requête et son résultat.</p></div>
    </div></section>`;
  const alert = d.error ? `<div class="card pad notice error" role="alert">Analyse impossible : ${esc(d.error)}</div>`
    : d.warning ? `<div class="card pad notice" role="status">${esc(d.warning)}</div>` : "";
  return head + alert + kpis + `<div class="split"><div class="col-main stack">${revenueCard(d)}${table}</div>
    <div class="col-side">${pending}${legend}</div></div>`;
}

// Couleur d'une carte indicateur : rouge/orange si une action est attendue, vert sinon.
function kpiTone(k) {
  const n = parseInt(k.value, 10) || 0;
  if (k.label === "Anomalies ouvertes") return n ? ["red", "⚠"] : ["green", "✓"];
  if (k.label === "Enquêtes en cours") return ["blue", "⟳"];
  if (k.label === "Corrections à valider") return n ? ["orange", "✎"] : ["green", "✓"];
  return ["teal", "◷"];
}

function revenueCard(d) {
  const rv = d.revenue;
  if (!rv) return d.running ? `<section class="card pad" aria-label="Chiffre d'affaires"><h2>Chiffre d'affaires</h2>
    <p class="card-text" style="margin:8px 0 0 0">Analyse en cours : l'évolution du chiffre d'affaires s'affiche à la fin de l'enquête.</p></section>` : "";
  const verdict = { drop: ["En baisse", "b-critical"], soft: ["Léger recul", "b-medium"], spike: ["Hausse anormale", "b-high"],
                    stable: ["Pas de baisse", "b-confirmed"] }[rv.verdict];
  const row = (g, kind) => `<div class="fact-row">
      ${badge(kind === "drop" ? "Baisse " + g.change : "Hausse " + g.change, kind === "drop" ? "b-critical" : "b-high")}
      <div class="text"><strong>${esc(g.days[0].toUpperCase() + g.days.slice(1))}</strong> — ${esc(g.detail)}
        ${g.causes.map(c => `<div class="cause">${badge(c.label, LEVELS[c.level][1], true)} ${esc(c.text)}</div>`).join("")}</div>
      ${g.incident ? `<a class="btn btn-outline btn-compact" href="#/enquete?i=${encodeURIComponent(g.incident)}">Enquêter</a>` : ""}</div>`;
  const rows = [...rv.drops.map(g => row(g, "drop")), ...rv.spikes.map(g => row(g, "spike"))];
  return `<section class="card article" aria-label="Chiffre d'affaires">
    <div class="row-between"><h2>${esc(rv.title)}</h2>${badge(verdict[0], verdict[1])}</div>
    <p class="article-text">${esc(rv.summary)}</p>
    ${revenueChart(rv)}
    ${rows.length ? `<div class="fact-rows">${rows.join("")}</div>` : ""}
    <div class="note" style="margin-top:12px;line-height:1.5">${esc(rv.note)}</div></section>`;
}

// Deux courbes : chiffre d'affaires observé et attendu (médiane des 4 mêmes jours précédents), jours anormaux marqués.
const eur = v => v == null ? "—" : Math.round(v).toLocaleString("fr-FR") + " €";
const dmy = day => `${day.slice(8, 10)}/${day.slice(5, 7)}/${day.slice(0, 4)}`;
function revenueChart(rv) {
  const pts = rv.series || [];
  if (pts.length < 2) return "";
  const W = 820, H = 230, L = 64, R = 92, T = 14, B = 28, slot = (W - L - R) / (pts.length - 1);
  const vals = pts.flatMap(p => [p.observed, p.expected]).filter(v => v != null);
  const top = Math.max(...vals) || 1, max = top * 1.08;
  const x = i => L + i * slot;
  const y = v => T + (H - T - B) * (1 - v / max);
  const path = key => {
    let d = "", pen = "M";
    pts.forEach((p, i) => { if (p[key] == null) { pen = "M"; return; } d += `${pen}${x(i).toFixed(1)},${y(p[key]).toFixed(1)} `; pen = "L"; });
    return d;
  };
  const kfmt = v => v >= 1000 ? Math.round(v / 1000).toLocaleString("fr-FR") + " k€" : Math.round(v) + " €";
  const grid = [0, .25, .5, .75, 1].map(f => top * f).map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="grid"/>
    <text x="${L - 8}" y="${y(v) + 4}" class="axis" text-anchor="end">${kfmt(v)}</text>`).join("");
  const step = Math.max(1, Math.ceil(pts.length / 8));
  const xl = pts.map((p, i) => i % step && i !== pts.length - 1 ? "" :
    `<text x="${x(i)}" y="${H - 8}" class="axis" text-anchor="middle">${p.day.slice(8, 10)}/${p.day.slice(5, 7)}</text>`).join("");
  const ws = pts.findIndex(p => p.day >= rv.window_start);
  const shade = ws > 0 ? `<rect x="${x(ws)}" y="${T}" width="${W - R - x(ws)}" height="${H - T - B}" class="window"/>
    <text x="${x(ws) + 6}" y="${T + 12}" class="axis">Période analysée</text>` : "";
  const marks = pts.map((p, i) => p.flag ? `<circle cx="${x(i)}" cy="${y(p.observed)}" r="6" class="flag-${p.flag}"/>` : "").join("");
  const last = pts[pts.length - 1];
  let ly1 = y(last.observed), ly2 = last.expected != null ? y(last.expected) : null;
  if (ly2 != null && Math.abs(ly1 - ly2) < 14) [ly1, ly2] = ly1 < ly2 ? [ly1 - 6, ly2 + 8] : [ly1 + 8, ly2 - 6];
  const labels = `<text x="${W - R + 8}" y="${ly1 + 4}" class="lbl">Observé</text>`
    + (ly2 != null ? `<text x="${W - R + 8}" y="${ly2 + 4}" class="lbl lbl-muted">Attendu</text>` : "");
  const hits = pts.map((p, i) => `<rect x="${x(i) - slot / 2}" y="${T}" width="${slot}" height="${H - T - B}" class="hit" data-i="${i}"/>`).join("");
  const table = `<details class="chart-table"><summary>Voir les données du graphique</summary>${resultTable(pts.map(p => ({
    "Jour": dmy(p.day), "Observé": eur(p.observed), "Attendu": eur(p.expected),
    "Écart": p.expected ? `${p.observed >= p.expected ? "+" : ""}${Math.round((p.observed - p.expected) / p.expected * 100)} %` : "—",
    "Signal": p.flag === "drop" ? "Baisse anormale" : p.flag === "spike" ? "Hausse anormale" : "" })))}</details>`;
  return `<div class="rev-chart" data-series="${esc(JSON.stringify(pts))}">
    <div class="chart-legend"><span><i class="key key-obs"></i>CA observé</span><span><i class="key key-exp"></i>CA attendu (référence)</span>
      <span><i class="dot flag-drop"></i>Baisse anormale</span><span><i class="dot flag-spike"></i>Hausse anormale</span></div>
    <div class="chart-box"><svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="Chiffre d'affaires quotidien observé et attendu">
      ${shade}${grid}${xl}<path d="${path("expected")}" class="line-exp"/><path d="${path("observed")}" class="line-obs"/>${marks}${labels}
      <line class="cross" x1="0" x2="0" y1="${T}" y2="${H - B}" visibility="hidden"/>${hits}</svg>
    <div class="chart-tip" hidden></div></div>${table}</div>`;
}

// Survol du graphique : ligne verticale et infobulle du jour pointé.
document.addEventListener("mouseover", ev => {
  const t = ev.target;
  const box = t.closest ? t.closest(".rev-chart") : null;
  document.querySelectorAll(".rev-chart").forEach(c => {
    if (c !== box || !t.classList.contains("hit")) { c.querySelector(".chart-tip").hidden = true; c.querySelector(".cross").setAttribute("visibility", "hidden"); }
  });
  if (!box || !t.classList.contains("hit")) return;
  const p = JSON.parse(box.dataset.series)[+t.dataset.i];
  const svg = box.querySelector("svg"), cross = box.querySelector(".cross");
  const cx = +t.getAttribute("x") + +t.getAttribute("width") / 2;
  cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
  const ecart = p.expected ? Math.round((p.observed - p.expected) / p.expected * 100) : null;
  const tip = box.querySelector(".chart-tip");
  tip.innerHTML = `<strong>${dmy(p.day)}</strong><br>Observé : ${eur(p.observed)}<br>Attendu : ${eur(p.expected)}`
    + (ecart != null ? `<br>Écart : ${ecart >= 0 ? "+" : ""}${ecart} %` : "")
    + (p.flag ? `<br><b>${p.flag === "drop" ? "Baisse anormale" : "Hausse anormale"}</b>` : "");
  tip.hidden = false;
  const scale = svg.clientWidth / svg.viewBox.baseVal.width;
  tip.style.left = Math.max(0, Math.min(cx * scale + 12, svg.clientWidth - 200)) + "px";
});

// ================================================================== Écran 2 — Import et profilage
function viewImport() {
  const kind = S.data && S.data.source_kind;
  const head = pageHead(`<h1>Importer et profiler des données</h1>
    <div class="subtitle">Étape 1 sur 5 : déposez un ou plusieurs fichiers CSV, quelle que soit leur structure ; DataSentinel AI détecte leur format et analyse leur qualité.</div>`, "");
  const drop = `<section class="import-zone" aria-label="Import">
    <div class="dropzone" id="dropzone">
      <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#0B6E7A" stroke-width="1.6" stroke-linecap="round" aria-hidden="true">
        <path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3"/></svg>
      <div class="drop-title">Glissez un fichier CSV ici</div>
      <div class="muted" style="font-size:14px">ou</div>
      <button type="button" class="btn btn-primary" data-action="pick-file">Choisir un fichier</button>
      <input type="file" id="file-input" accept=".csv,text/csv" multiple hidden>
      <div class="note">Les fichiers restent sur cette machine tant que le traitement local est actif.</div>
    </div>
    <div class="card demo">
      <h2>Options de lecture</h2>
      <p>Séparateur, encodage, format des dates et des décimales sont détectés automatiquement ; forcez le séparateur si besoin.</p>
      <label class="field" for="separator" style="margin:0">Séparateur</label>
      <select id="separator" data-change="sep">
        ${[["auto", "Détection automatique"], ["comma", "Virgule"], ["semicolon", "Point-virgule"]].map(([v, l]) =>
          `<option value="${v}" ${S.sep === v ? "selected" : ""}>${l}</option>`).join("")}
      </select>
    </div></section>`;

  const prof = S.profile;
  const tables = prof || [];
  const current = tables.find(t => t.table === S.profileTable) || tables.find(t => t.table === "orders") || tables[0];
  const what = current ? (kind === "sqlserver" ? `Profil de la table ${current.table}` : `Profil du fichier ${current.table}.csv`)
                       : "Profil du fichier";
  const rows = !S.dbPath ? `<tr class="empty-row"><td colspan="5">Aucune source : choisissez un fichier CSV.</td></tr>`
    : !current ? `<tr class="empty-row"><td colspan="5">Profilage en cours…</td></tr>`
    : current.columns.map(c => `<tr><td class="mono">${esc(c.name)}</td><td>${esc(c.type)}</td><td>${esc(c.missing)}</td>
        <td>${esc(c.distinct)}</td><td>${badge(c.alert, ALERT_CLASS[c.alert] || "b-neutral", true)}</td></tr>`).join("");
  const tablePills = tables.length > 1 ? `<div class="pills" role="group" aria-label="Table profilée" style="padding:12px 20px 0 20px">${tables.map(t =>
      `<button type="button" class="pill" data-action="profile-table" data-value="${esc(t.table)}" aria-pressed="${t === current}">${esc(t.table)}</button>`).join("")}</div>` : "";
  const profile = `<section class="card clip" aria-label="Profilage">
    <div class="card-head"><div><h2>${esc(what)}</h2>
      <div class="muted" style="font-size:14px;margin-top:2px">Aperçu du profilage : colonnes, valeurs manquantes, doublons${current ? ` · ${current.rows.toLocaleString("fr-FR")} lignes` : ""}</div></div>
      <div class="actions" style="display:flex;flex-wrap:wrap;gap:12px">
        <button type="button" class="btn btn-secondary" data-action="reprofile" ${S.dbPath ? "" : "disabled"}>Relancer le profilage</button>
        <a class="btn btn-primary" href="#/vue-ensemble" data-action="detect">Détecter les anomalies</a></div></div>
    ${tablePills}
    <div class="table-wrap"><table class="data profile"><thead><tr><th scope="col">Colonne</th><th scope="col">Type détecté</th>
      <th scope="col">Valeurs manquantes</th><th scope="col">Valeurs distinctes</th><th scope="col">Alerte</th></tr></thead>
      <tbody>${rows}</tbody></table></div></section>`;

  const checks = `<section class="card" style="padding:20px 24px" aria-label="Contrôles à exécuter">
    <h2 style="margin-bottom:12px">Contrôles à exécuter</h2>
    <div class="checks">${CHECKS.map(([k, l]) => `<label><input type="checkbox" data-change="check" value="${k}" ${S.checks.includes(k) ? "checked" : ""}>${l}</label>`).join("")}</div>
  </section>`;
  return head + drop + profile + importCard(current && current.import) + checks;
}

// Ce que l'import a détecté dans le fichier : format, ambiguïtés, lignes écartées, aperçu brut.
function importCard(imp) {
  if (!imp) return "";
  const facts = `Encodage ${esc(imp.encoding)} · séparateur ${esc(imp.delimiter)} · ${imp.rows.toLocaleString("fr-FR")} ligne(s) importée(s)`
    + (imp.bad_line_count ? ` · ${imp.bad_line_count} ligne(s) mal formée(s) écartée(s)` : "")
    + (imp.duplicate_rows ? ` · ${imp.duplicate_rows} ligne(s) en double` : "");
  const warnings = imp.warnings.length
    ? `<ul class="impact">${imp.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>`
    : `<p class="card-text">Aucune ambiguïté : le format du fichier a été reconnu sans hésitation.</p>`;
  const bad = imp.bad_lines.length ? `<h3 style="margin:14px 0 6px">Lignes mal formées</h3>${resultTable(imp.bad_lines.map(b =>
      ({ "Ligne": b.line, "Champs attendus": b.expected, "Champs trouvés": b.found, "Contenu": b.excerpt })))}` : "";
  const preview = !imp.preview.length ? "" : `<div class="btn-row"><button type="button" class="btn btn-secondary btn-compact"
      data-action="toggle-preview" aria-expanded="${!!S.showPreview}">${S.showPreview ? "Masquer" : "Afficher"} l'aperçu des premières lignes</button></div>`
    + (!S.showPreview ? "" : `<h3 style="margin:14px 0 6px">Aperçu des premières lignes (valeurs brutes du fichier)</h3>
    ${resultTable(imp.preview.map(r => Object.fromEntries(imp.preview_columns.map(c => [c === "_ligne_source" ? "Ligne" : c, r[c]]))))}`);
  return `<section class="card pad" aria-label="Diagnostic d'import"><h2 style="margin-bottom:6px">Diagnostic d'import : ${esc(imp.file)}</h2>
    <p class="card-text">${facts}</p>${warnings}${bad}${preview}</section>`;
}

// ================================================================== Écran 3 — Enquête des agents
function viewInvestigation() {
  const d = S.data;
  const inc = d && d.incident;
  const back = `<a class="back" href="#/vue-ensemble">← Retour aux anomalies</a>`;
  const actionsDisabled = !d || !d.run_id || d.running ? "disabled" : "";
  const heading = inc ? inc.heading : d && d.running ? "Enquête en cours" : "Enquête";
  const sub = inc ? inc.subtitle : d && d.run_id ? `${d.source} · ${d.status}` : "Aucune enquête : lancez une analyse depuis la vue d'ensemble ou l'import.";
  const head = pageHead(`${back}<h1 class="after-back">${esc(heading)}</h1><div class="subtitle">${esc(sub)}</div>`,
    `<button type="button" class="btn btn-secondary" data-action="pause" ${actionsDisabled}>${d && d.paused ? "Reprendre l'enquête" : "Mettre en pause"}</button>
     <button type="button" class="btn btn-secondary" data-action="add-hypothesis" ${inc ? "" : "disabled"}>Ajouter une hypothèse</button>
     <a class="btn btn-primary" href="#/rapport${inc ? "?i=" + encodeURIComponent(inc.id) : ""}">Générer le rapport</a>`);

  const agents = `<section class="agents" aria-label="Progression des agents">${(d ? d.agents : []).map(a => {
    const [lbl, cls] = AGENT_BADGES[a.status];
    return `<div class="card agent${a.status === "running" ? " active" : ""}"><div class="agent-n">Agent ${a.n}</div>
      <div class="agent-name">${esc(a.name)}</div>${badge(lbl, cls, true)}<div class="agent-desc">${esc(a.description)}</div></div>`;
  }).join("")}</section>`;

  if (inc && inc.quality) return pageHead(`${back}<h1 class="after-back">${esc(heading)}</h1><div class="subtitle">${esc(sub)}</div>`,
    `<a class="btn btn-primary" href="#/rapport?i=${encodeURIComponent(inc.id)}">Voir la correction et le rapport</a>`) + qualityDetail(inc);
  let left;
  if (!inc) {
    left = `<div class="card article"><p class="article-text" style="margin:0">${d && d.running
      ? "Les hypothèses et leurs preuves s'affichent lorsque les agents ont terminé."
      : d && d.run_id ? "Aucune anomalie à examiner pour cette analyse." : "Aucune enquête ouverte."}</p></div>`;
  } else {
    const cards = inc.hypotheses.map((h, n) => hypothesisCard(h, n));
    cards.splice(Math.min(1, cards.length), 0, factCard(inc.fact));
    left = cards.join("");
  }
  const im = inc && inc.impact;
  const impact = `<section class="card pad" aria-label="Impact métier"><h2 style="margin-bottom:10px">Impact métier</h2>
    ${im ? `<ul class="impact"><li>KPI affectés : ${esc(im.kpis)}</li><li>Période concernée : ${esc(im.period)}</li>
      <li>Décisions exposées : ${esc(im.decisions)}</li></ul>
      <div class="note" style="margin-top:10px;line-height:1.5">Estimé à partir des dépendances réellement disponibles.</div>
      <button type="button" class="btn btn-secondary btn-block" style="margin-top:14px" data-action="dashboards" aria-expanded="${!!S.openResults.dashboards}">Voir les tableaux de bord concernés</button>
      ${S.openResults.dashboards ? `<div class="inline-result">${im.dashboards.length ? `<ul class="impact">${im.dashboards.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "Aucun tableau de bord relié."}</div>` : ""}`
      : `<p class="card-text" style="margin:0">Disponible lorsque l'enquête est terminée.</p>`}</section>`;
  const journal = `<section class="card pad" aria-label="Journal de l'enquête"><h2 style="margin-bottom:12px">Journal de l'enquête</h2>
    <div class="journal">${d && d.journal.length ? d.journal.map(j => `<div><span class="time">${esc(j.time)}</span>${esc(j.text)}</div>`).join("")
      : `<div class="muted">Aucune étape pour l'instant.</div>`}</div>
    <button type="button" class="btn btn-secondary btn-block" style="margin-top:14px" data-action="export-journal" ${d && d.run_id ? "" : "disabled"}>Exporter le journal</button></section>`;
  return head + agents + `<div class="split"><section class="col-main stack" aria-label="Hypothèses et preuves">${left}</section>
    <div class="col-side w320">${impact}${journal}</div></div>`;
}

function hypothesisCard(h, n) {
  const letter = String.fromCharCode(65 + n);
  const [lbl, cls] = h.badge;
  const id = "h" + n;
  const ev = h.evidence;
  const sqlText = ev.length ? ev.map(e => (ev.length > 1 ? `-- ${e.description}\n` : "") + e.sql).join("\n\n") : (h.sql || "");
  const open = S.openResults[h.key];
  let buttons;
  if (h.dismissed) buttons = "";
  else if (ev.length) buttons = `<div class="btn-row">
      <button type="button" class="btn btn-secondary btn-compact" data-action="see-result" data-key="${esc(h.key)}" aria-expanded="${open === "result"}">Voir le résultat</button>
      <button type="button" class="btn btn-secondary btn-compact" data-action="copy" data-target="${id}">Copier la requête</button>
      <button type="button" class="btn btn-secondary btn-compact" data-action="rerun" data-key="${esc(h.key)}" data-n="${n}">Relancer le test</button></div>`;
  else buttons = `<div class="btn-row">
      <button type="button" class="btn btn-outline btn-compact" data-action="test-hypothesis" data-n="${n}">Tester cette hypothèse</button>
      ${h.sql ? `<button type="button" class="btn btn-secondary btn-compact" data-action="copy" data-target="${id}">Copier la requête</button>` : ""}
      <button type="button" class="btn btn-secondary btn-compact" data-action="dismiss" data-n="${n}">Écarter</button></div>`;
  let extra = "";
  if (open === "result") extra = ev.map(e => `<div class="inline-result"><strong>${esc(e.description)}</strong> — ${e.rows} ligne(s)${resultTable(e.result)}</div>`).join("");
  if (open && open.rerun) extra = open.rerun;
  if (S.testResult[h.key]) extra += S.testResult[h.key];
  else if (h.analyst_test) {
    const t = h.analyst_test;
    extra += `<div class="inline-result">Dernier test par l'analyste (${esc(t.time)}) : ${t.error ? "échec — " + esc(t.error) : `${t.rows} ligne(s)`}. Ce résultat reste une hypothèse.</div>`;
  }
  const dismissed = h.dismissed ? `<div class="inline-result">Écartée par ${esc(h.dismissed.by)} : ${esc(h.dismissed.reason)}</div>` : "";
  return `<article class="card article">
    <div class="row-between"><h2>Hypothèse ${letter} : ${esc(h.statement)}</h2>${badge(lbl, "b-" + cls)}</div>
    <p class="article-text">${esc(h.text)}</p>
    ${sqlText ? `<div class="code-label">${ev.length > 1 ? "Requêtes exécutées" : ev.length ? "Requête exécutée" : "Requête de test proposée"}</div>
      <pre class="code" id="${id}">${esc(sqlText)}</pre>` : ""}
    ${buttons}${extra}${dismissed}</article>`;
}

function factCard(f) {
  if (!f) return "";
  let chart = "";
  if (f.bars && f.bars.length) {
    const max = Math.max(...f.bars.map(b => b.value), 1);
    const step = Math.min(50, 580 / f.bars.length), w = Math.max(4, step * 0.6);
    const rects = f.bars.map((b, i) => {
      // un jour sans aucune ligne reste visible (trait de 2 px) quand il fait partie de la période concernée
      const h = b.value > 0 ? Math.max(1, (b.value / max) * 84) : (b.flagged ? 2 : 0);
      return `<rect x="${(20 + i * step).toFixed(1)}" y="${(110 - h).toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" fill="${b.flagged ? "#B4470F" : "#5A7FA6"}"><title>${esc(b.label)} : ${b.value.toLocaleString("fr-FR")} ${esc(f.unit)}</title></rect>`;
    }).join("");
    const first = f.bars[0].label, last = f.bars[f.bars.length - 1].label;
    chart = `<svg class="chart" viewBox="0 0 600 120" role="img" aria-label="Graphique : ${esc(f.title.replace("Fait observé : ", ""))} du ${first} au ${last}, période concernée en orange">
      <line x1="0" y1="110" x2="600" y2="110" stroke="#D8DEE6" stroke-width="1"/>${rects}</svg>
      <div class="chart-legend"><span>Bleu : période de référence</span><span>${esc(f.legend_flagged)}</span>
      <span>Du ${first} au ${last} · maximum ${max.toLocaleString("fr-FR")} ${esc(f.unit)}</span></div>`;
  }
  return `<article class="card article"><div class="row-between"><h2>${esc(f.title)}</h2>${badge("Observée", "b-observed")}</div>
    <p class="article-text">${esc(f.text)}</p>${chart}</article>`;
}

// ================================================================== Écran 4 — Rapport et validation
function viewReport() {
  const d = S.data;
  const inc = d && d.incident;
  const r = inc && inc.report;
  const head = pageHead(`<a class="back" href="#/enquete${inc ? "?i=" + encodeURIComponent(inc.id) : ""}">← Retour à l'enquête</a>
      <h1 class="after-back">Rapport de diagnostic</h1><div class="subtitle">${esc(r ? r.subtitle : "Aucune enquête terminée à présenter.")}</div>`,
    `<button type="button" class="btn btn-secondary" data-action="export" data-fmt="json" ${r ? "" : "disabled"}>Exporter en JSON</button>
     <button type="button" class="btn btn-secondary" data-action="export" data-fmt="html" ${r ? "" : "disabled"}>Exporter en HTML</button>
     <button type="button" class="btn btn-secondary" data-action="share" ${r ? "" : "disabled"}>Partager</button>`);
  if (!r) return head;

  const proofs = S.expanded ? r.proofs : r.proofs.slice(0, 1);
  const content = `<section class="col-main stack" aria-label="Contenu du rapport">
    <article class="card article"><h2 style="margin-bottom:12px">1. Résumé pour les décideurs</h2>
      <p style="font-size:15px;line-height:1.6;margin:0">${esc(r.summary)}</p>
      ${r.narrative ? `<p class="note" style="margin:10px 0 0 0;line-height:1.5">Synthèse rédigée (${esc(r.narrative.by)}) : ${esc(r.narrative.text)}</p>` : ""}</article>
    <article class="card article"><h2 style="margin-bottom:14px">2. Faits, hypothèses et causes</h2>
      <div class="fact-rows">${r.facts.map(f => `<div class="fact-row">${badge(f.badge[0], "b-" + f.badge[1])}<div class="text">${esc(f.text)}</div></div>`).join("")}</div></article>
    <article class="card article"><div class="row-between" style="margin-bottom:12px"><h2>3. Preuves : requêtes et résultats</h2>
      ${r.proofs.length > 1 || r.proofs.some(p => p.result.length) ? `<button type="button" class="btn btn-secondary btn-compact" data-action="expand" aria-expanded="${S.expanded}">${S.expanded ? "Tout replier" : "Tout déplier"}</button>` : ""}</div>
      ${proofs.length ? proofs.map((p, i) => `<div class="proof"><pre class="code">${esc(`-- Preuve ${i + 1} : ${p.description}\n${p.sql}`)}</pre>
        ${S.expanded ? `<div class="note" style="margin-top:8px">${p.rows} ligne(s) en résultat</div>${resultTable(p.result)}` : ""}</div>`).join("")
        : `<p class="note">Aucune requête de preuve.</p>`}
      <div class="note" style="margin-top:10px">Chaque requête est journalisée avec son résultat et la version du modèle utilisé.</div></article>
    <article class="card article"><h2 style="margin-bottom:12px">4. Plan de correction proposé</h2>
      ${r.plan ? `<ol class="plan">${r.plan.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>
        ${r.plan.executable ? changesView(r.plan.id) : ""}
        ${r.plan.sql ? `<h3 class="sub-h">Requête de correction à exécuter</h3>
          <p class="card-text" style="margin:0 0 8px 0">À exécuter par votre équipe data, dans une transaction, après sauvegarde de la table et après validation. L'application ne l'exécute jamais sur la source.</p>
          <pre class="code">${esc(r.plan.sql)}</pre>
          <div class="btn-row"><button type="button" class="btn btn-secondary btn-compact" data-action="copy-sql">Copier la requête</button></div>` : ""}
        <p class="note" style="margin:10px 0 0 0;line-height:1.5">Retour arrière : ${esc(r.plan.rollback)}</p>`
        : `<p class="card-text" style="margin:0">Aucun plan de correction : la cause n'est pas déterminée.</p>`}</article>
  </section>`;

  const test = S.testResult.plan;
  const testCard = `<section class="card pad" aria-label="Test sur une copie"><h2 style="margin-bottom:8px">Test sur une copie</h2>
    <p class="card-text">La correction est exécutée sur une copie isolée. La production n'est pas touchée.</p>
    <button type="button" class="btn btn-outline btn-block" data-action="test-copy" ${r.plan && r.plan.executable ? "" : "disabled"}>Tester sur une copie</button>
    <div class="result-box" aria-live="polite">${test || (r.plan && !r.plan.executable
      ? esc(r.plan.not_executable_reason || "Cette correction ne peut pas être exécutée sur une copie.")
      : "Résultat avant / après affiché ici après le test.")}</div></section>`;

  let state = "";
  if (r.decision) {
    const verb = { approve: "Approuvée", reject: "Refusée", modify: "Modifiée" }[r.decision.decision];
    state = `<div class="inline-result" style="margin:0 0 14px 0">${verb} par ${esc(r.decision.by)} le ${esc(r.decision.time)}${r.decision.comment ? " : " + esc(r.decision.comment) : ""}.</div>`;
  }
  if (!r.can_decide) {
    const why = !r.plan ? "Aucune correction à valider pour cet incident."
      : d.paused ? "Enquête en pause : reprenez-la depuis l'écran Enquêtes pour valider."
      : r.plan.state !== "open" ? "" : "Cette enquête n'attend pas de validation.";
    if (why) state += `<div class="inline-result" style="margin:0 0 14px 0">${esc(why)}</div>`;
  }
  const dis = r.can_decide ? "" : "disabled";
  const validation = `<section class="validation card" aria-label="Validation humaine"><h2 style="margin-bottom:8px">Validation humaine</h2>
    <p class="card-text">Toute modification sensible exige une décision explicite et un retour arrière possible.</p>${state}
    <label class="field" for="decision-comment">Commentaire de décision</label>
    <textarea id="decision-comment" rows="3" ${dis} aria-describedby="comment-error"></textarea>
    <div id="comment-error" class="field-error" role="alert"></div>
    <div class="btn-col">
      <button type="button" class="btn btn-primary btn-block" data-action="decide" data-decision="approve" ${dis}>Approuver la correction</button>
      <button type="button" class="btn btn-secondary btn-block" data-action="modify-plan" ${dis}>Modifier le plan</button>
      <button type="button" class="btn btn-danger btn-block" data-action="decide" data-decision="reject" ${dis}>Refuser</button>
    </div></section>`;
  const trace = `<section class="card pad" aria-label="Traçabilité"><h2 style="margin-bottom:8px">Traçabilité</h2>
    <p class="card-text" style="font-size:13px;margin-bottom:12px">Requêtes, résultats, versions de modèle et validations sont consignés.</p>
    <a class="link-box" href="#/journal">Ouvrir le journal d'audit</a></section>`;
  return head + `<div class="split">${content}<div class="col-side w320">${testCard}${validation}${trace}</div></div>`;
}

// Constat de qualité : diagnostic, lignes concernées, graphique, recommandation et correction proposée.
function qualityDetail(inc) {
  const q = inc.quality;
  const st = q.status === "Erreur confirmée" ? "b-critical" : "b-hypothesis";
  const diag = `<section class="card article" aria-label="Diagnostic"><div class="row-between"><h2>Diagnostic</h2>
      <div class="badges">${badge(q.status, st)} ${badge(inc.severity_label, SEVERITY_CLASS[inc.severity])}</div></div>
    <dl class="diag">
      <dt>Catégorie</dt><dd>${esc(q.category)}</dd>
      <dt>Règle</dt><dd>${esc(q.rule)}</dd>
      <dt>Méthode</dt><dd>${esc(q.method)}</dd>
      <dt>Lignes concernées</dt><dd>${q.count} sur ${q.total}</dd>
      <dt>Confiance</dt><dd>${esc(q.confidence)}</dd>
      <dt>Justification</dt><dd>${esc(q.justification)}</dd>
      <dt>Limites</dt><dd>${esc(q.limits)}</dd>
    </dl></section>`;
  const hasExp = q.rows.some(r => r.expected !== undefined);
  const rows = `<section class="card article" aria-label="Lignes concernées"><h2 style="margin-bottom:6px">Lignes concernées</h2>
    <p class="card-text" style="margin:0">${q.count > q.rows.length ? `${q.rows.length} premières lignes sur ${q.count}.` : `${q.count} ligne(s).`}
      La valeur observée est celle de la table analysée.</p>
    ${resultTable(q.rows.map(r => Object.assign({ [q.ident_label]: r.ident, "Colonne": r.column,
      "Valeur observée": r.value === null ? "(vide)" : r.value }, hasExp ? { "Valeur attendue": r.expected ?? "—" } : {})))}
    <details class="chart-table"><summary>Requête de preuve (rejouable)</summary><pre class="code">${esc(q.evidence.sql)}</pre></details></section>`;
  const chart = q.chart ? `<section class="card article" aria-label="Distribution"><h2 style="margin-bottom:6px">Distribution des valeurs</h2>
    <p class="card-text" style="margin:0">Barres : nombre de lignes par tranche de valeurs. En rouge : les valeurs extrêmes signalées.</p>
    ${histogram(q.chart)}</section>` : "";
  const corr = q.correction;
  const reco = `<section class="card pad reco-card" aria-label="Recommandation"><h2 style="margin-bottom:8px">Recommandation</h2>
    <p class="card-text" style="color:var(--ink);font-size:15px">${esc(q.recommendation)}</p>
    <h3 class="sub-h">Correction proposée</h3>
    ${corr && corr.sql ? `<p class="card-text" style="margin:0 0 8px 0">${esc(corr.description)} ${esc(corr.consequences || "")}</p>
      <a class="btn btn-primary btn-block" href="#/rapport?i=${encodeURIComponent(inc.id)}">Voir la correction et la valider</a>`
      : `<p class="card-text" style="margin:0">Aucune correction automatique : la valeur correcte ne peut pas être déduite des données.
        La correction se fait dans le système source.</p>`}
    ${q.decision ? `<div class="inline-result">Décision enregistrée : ${esc({ approve: "approuvée", reject: "refusée", modify: "modifiée" }[q.decision])}.</div>` : ""}
  </section>`;
  return `<div class="split"><section class="col-main stack" aria-label="Constat">${diag}${chart}${rows}</section>
    <div class="col-side w320">${reco}</div></div>`;
}

// Histogramme d'une colonne numérique ; les valeurs extrêmes signalées sont marquées en rouge.
function histogram(c) {
  const W = 640, H = 170, L = 44, R = 12, T = 10, B = 30;
  const n = c.counts.length, max = Math.max(...c.counts) || 1;
  const lo = c.edges[0], hi = c.edges[n];
  const x = v => L + (v - lo) / ((hi - lo) || 1) * (W - L - R);
  const y = v => T + (H - T - B) * (1 - v / max);
  const bw = (W - L - R) / n;
  const bars = c.counts.map((k, i) => k ? `<rect x="${(L + i * bw + 1).toFixed(1)}" y="${y(k).toFixed(1)}" width="${Math.max(bw - 2, 1).toFixed(1)}"
    height="${(H - B - y(k)).toFixed(1)}" rx="2" class="bar"><title>${c.edges[i].toLocaleString("fr-FR")} à ${c.edges[i + 1].toLocaleString("fr-FR")} : ${k} ligne(s)</title></rect>` : "").join("");
  const marks = c.flagged.map(v => `<line x1="${x(v)}" x2="${x(v)}" y1="${T}" y2="${H - B}" class="flag-line"/>
    <circle cx="${x(v)}" cy="${T + 6}" r="5" class="flag-dot"><title>Valeur signalée : ${v.toLocaleString("fr-FR")}</title></circle>`).join("");
  const med = `<line x1="${x(c.median)}" x2="${x(c.median)}" y1="${T}" y2="${H - B}" class="median"/>
    <text x="${x(c.median) + 4}" y="${T + 22}" class="axis">médiane</text>`;
  const ticks = [[lo, "start"], [(lo + hi) / 2, "middle"], [hi, "end"]].map(([v, a]) =>
    `<text x="${x(v)}" y="${H - 10}" class="axis" text-anchor="${a}">${Math.round(v).toLocaleString("fr-FR")}</text>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="chart hist" role="img" aria-label="Distribution des valeurs">
    <line x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}" class="grid"/>${bars}${med}${marks}${ticks}</svg>`;
}

// Enregistrements à corriger : identifiant, valeur actuelle -> valeur proposée (calculés sur une copie).
function changesView(planId) {
  const key = S.runId + "|" + planId;
  S.changes = S.changes || {};
  if (!(key in S.changes)) {
    S.changes[key] = null;
    api(`/runs/${S.runId}/plans/changes?plan_id=${encodeURIComponent(planId)}`)
      .then(c => { S.changes[key] = c; }).catch(e => { S.changes[key] = { error: e.message }; })
      .then(() => { if (route().key === "report") render(); });
  }
  const ch = S.changes[key];
  const head = `<h3 class="sub-h">Enregistrements à corriger</h3>`;
  if (ch === null) return head + `<p class="card-text">Calcul des lignes concernées sur une copie…</p>`;
  if (ch.error) return head + `<p class="card-text">${esc(ch.error)}</p>`;
  if (!ch.length) return head + `<p class="card-text">La correction ne modifie aucune ligne existante.</p>`;
  const fmt = v => v === null || v === undefined ? "(vide)" : String(v);
  return head + ch.map(t => {
    const keyLabel = t.key === "_ligne_source" ? "Ligne du fichier" : (t.key || "Ligne");
    const rows = t.changes.map(c => c.action === "modifiée"
      ? Object.entries(c.changes).map(([col, v]) => ({ [keyLabel]: c.key, "Action": "Modifier", "Colonne": col,
          "Valeur actuelle": fmt(v["actuelle"]), "Valeur proposée": fmt(v["proposée"]) }))
      : [{ [keyLabel]: c.key ?? "—", "Action": c.action === "supprimée" ? "Supprimer" : "Ajouter", "Colonne": "(ligne entière)",
          "Valeur actuelle": c.action === "supprimée" ? Object.entries(c.row).slice(0, 4).map(([k, v]) => `${k}=${fmt(v)}`).join(", ") : "—",
          "Valeur proposée": c.action === "ajoutée" ? Object.entries(c.row).slice(0, 4).map(([k, v]) => `${k}=${fmt(v)}`).join(", ") : "—" }]).flat();
    return `<p class="card-text" style="margin:6px 0 0 0">Table <span class="mono">${esc(t.table)}</span> : ${t.total} ligne(s) concernée(s)${t.total > t.changes.length ? `, ${t.changes.length} premières affichées` : ""}.</p>${resultTable(rows)}`;
  }).join("");
}

// ================================================================== Journal d'audit et Paramètres
function viewAudit() {
  if (S.audit === null && !S.auditLoading) loadAudit();
  const rows = S.audit || [];
  const head = pageHead(`<h1>Journal d'audit</h1><div class="subtitle">${S.auditAll || !S.runId ? "Toutes les enquêtes" : "Enquête " + esc(S.runId)}</div>`,
    `<button type="button" class="btn btn-secondary" data-action="export-journal">Exporter le journal</button>`);
  const filters = `<section class="card pad" aria-label="Filtres du journal"><div class="checks" style="grid-template-columns:repeat(2,minmax(0,1fr))">
    <label><input type="checkbox" data-change="audit-all" ${S.auditAll ? "checked" : ""}>Toutes les enquêtes</label>
    <label><input type="checkbox" data-change="audit-queries" ${S.auditQueries ? "checked" : ""}>Inclure les requêtes SQL</label></div></section>`;
  const counts = {};
  rows.forEach(e => { const c = auditCat(e.kind)[0]; counts[c] = (counts[c] || 0) + 1; });
  const tiles = `<section class="kpis" aria-label="Synthèse du journal">${AUDIT_CATS.map(([key, label, tone, icon]) =>
    `<div class="card kpi tone-${tone}"><div class="kpi-top"><span class="kpi-icon" aria-hidden="true">${icon}</span><span class="kpi-label">${label}</span></div>
      <div class="kpi-value">${counts[key] || 0}</div><div class="kpi-sub">événement(s)</div></div>`).join("")}</section>`;
  const table = `<section class="card clip audit-card" aria-label="Événements"><div class="card-head band"><h2>Événements</h2></div><div class="table-wrap"><table class="data profile audit"><thead><tr>
    <th scope="col">Horodatage</th><th scope="col">Enquête</th><th scope="col">Acteur</th><th scope="col">Type</th><th scope="col">Détail</th></tr></thead>
    <tbody>${S.audit === null ? `<tr class="empty-row"><td colspan="5">Chargement…</td></tr>` : rows.length ? rows.map(e => `<tr>
      <td class="mono">${esc(e.time)}</td><td class="mono">${esc(e.run_id)}</td><td>${esc(e.actor)}</td><td>${badge(auditCat(e.kind)[1], "b-" + auditCat(e.kind)[2], true)}</td>
      <td style="font-size:13px;word-break:break-word">${esc(e.detail)}</td></tr>`).join("") : `<tr class="empty-row"><td colspan="5">Aucun événement.</td></tr>`}</tbody>
    </table></div></section>`;
  return head + tiles + filters + table;
}

// Familles d'événements du journal : [clé, libellé, couleur de carte, icône] et badge de chaque ligne.
const AUDIT_CATS = [["analysis", "Analyses", "blue", "⟳"], ["decision", "Décisions", "green", "✓"],
                    ["test", "Tests et requêtes", "orange", "⚗"], ["other", "Exports et sources", "teal", "⇩"]];
const AUDIT_LABELS = {
  run_started: "Analyse lancée", agent_step: "Étape d'agent", narrative: "Synthèse rédigée", report_built: "Rapport produit",
  report_exported: "Rapport exporté", human_decision: "Décision humaine", hypothesis_dismissed: "Hypothèse écartée",
  analyst_hypothesis: "Hypothèse ajoutée", analyst_hypothesis_tested: "Hypothèse testée", run_paused: "Enquête en pause",
  run_resumed: "Enquête reprise", query: "Requête SQL", query_rerun: "Requête relancée", sandbox_test: "Test sur une copie",
  sqlserver_extract: "Extraction SQL Server",
};
function auditCat(kind) {
  const label = AUDIT_LABELS[kind] || kind.replace(/_/g, " ");
  if (/decision|review|approv|reject|dismiss|paused|resumed|^analyst_hypothesis$/.test(kind)) return ["decision", label, "confirmed"];
  if (/test|query|rerun|sandbox|verif/.test(kind)) return ["test", label, "high"];
  if (/^run_|detect|investig|impact|narrat|agent/.test(kind)) return ["analysis", label, "observed"];
  return ["other", label, "neutral"];
}

async function loadAudit() {
  S.audit = null;
  S.auditLoading = true;
  const q = new URLSearchParams({ queries: S.auditQueries });
  if (!S.auditAll && S.runId) q.set("run_id", S.runId);
  try { S.audit = await api("/audit?" + q); } catch (e) { S.audit = []; toast(e.message, true); }
  S.auditLoading = false;
  if (route().key === "audit") render();
}

function viewSettings() {
  if (!S.settings && !S.settingsLoading) S.settingsLoading = api("/settings").then(s => { S.settings = s; if (route().key === "settings") render(); }).catch(e => toast(e.message, true));
  const st = S.settings;
  const head = pageHead(`<h1>Paramètres</h1><div class="subtitle">Préférences locales, sources et rédaction des rapports.</div>`, "");
  const prefs = `<section class="card pad" aria-label="Préférences"><h2 style="margin-bottom:12px">Préférences</h2>
    <label class="field" for="actor">Votre nom (enregistré dans le journal d'audit)</label>
    <input type="text" id="actor" value="${esc(S.actor)}" data-change="actor">
  </section>`;
  const sql = `<section class="card pad" aria-label="SQL Server"><h2 style="margin-bottom:8px">SQL Server (lecture seule)</h2>
    <p class="card-text">${st ? `Serveur ${esc(st.sqlserver.server)} · base ${esc(st.sqlserver.database)} · pilote ${esc(st.sqlserver.driver)} · ${esc(st.sqlserver.auth)}. Modifiable dans le fichier .env.` : "Chargement…"}</p>
    <div class="btn-row" style="margin-top:0"><button type="button" class="btn btn-secondary" data-action="sql-test">Tester la connexion</button>
    <button type="button" class="btn btn-outline" data-action="sql-extract">Extraire un instantané et l'utiliser</button></div>
    <div class="result-box" id="sql-result" aria-live="polite">${S.testResult.sql || "Aucun test effectué."}</div></section>`;
  const llm = `<section class="card pad" aria-label="Rédaction"><h2 style="margin-bottom:8px">Rédaction des rapports</h2>
    <p class="card-text">Rédaction actuelle : ${st ? esc(st.llm) : "…"}. Le modèle ne fait que reformuler des faits établis : aucune ligne de données n'est envoyée, aucun statut n'est décidé par le modèle.</p>
    <button type="button" class="btn btn-secondary" data-action="llm-check">Tester la connexion à Claude</button>
    <div class="result-box" aria-live="polite">${S.testResult.llm || "Aucun test effectué."}</div></section>`;
  return head + prefs + sql + llm;
}

// ------------------------------------------------------------------ actions
async function loadProfile() {
  if (!S.dbPath) { S.profile = null; return; }
  S.profile = null;
  render();
  try { S.profile = await api("/profile?db_path=" + encodeURIComponent(S.dbPath)); } catch (e) { S.profile = []; toast(e.message, true); }
  if (route().key === "import") render();
}

async function useDefaultSource(silent) {
  S.useDefault = true; S.dbPath = null; S.runId = null; S.profile = null; S.profileTable = null; S.openResults = {}; S.testResult = {};
  save();
  if (!silent) { await refresh(); toast("Retour à la base SQL Server reliée."); }
}

async function useSource(res) {
  S.useDefault = false;
  S.dbPath = res.db_path; S.runId = null; S.profile = null; S.profileTable = null; S.openResults = {}; S.testResult = {};
  save();
  await loadState();
  toast("Source chargée : " + res.source.replace("Source active : ", "") + ".");
  if (route().key === "import") await loadProfile(); else render();
}

async function startAnalysis(goTo) {
  if (!S.checks.length) { toast("Cochez au moins un contrôle.", true); location.hash = "#/import"; return; }
  try {
    // base SQL Server reliée : db_path absent, le serveur extrait un instantané frais avant l'analyse
    const r = await api("/runs", { json: { db_path: S.useDefault ? null : S.dbPath, checks: S.checks, actor: S.actor } });
    S.runId = r.run_id; S.openResults = {}; S.testResult = {}; save();
    if (goTo && location.hash !== goTo) location.hash = goTo; else await refresh();
  } catch (e) { toast(e.message, true); }
}

async function uploadFiles(files) {
  if (!files || !files.length) return;
  const form = new FormData();
  [...files].forEach(f => form.append("files", f));
  form.append("sep", S.sep);
  try { await useSource(await api("/sources/upload", { form })); } catch (e) { toast(e.message, true); }
}

function openDialog(html, onSubmit) {
  const dlg = $("#dialog");
  dlg.innerHTML = `<form method="dialog">${html}</form>`;
  const form = dlg.querySelector("form");
  form.addEventListener("submit", async ev => {
    const action = ev.submitter && ev.submitter.value;
    if (action !== "ok") return;
    ev.preventDefault();
    const err = form.querySelector(".field-error");
    try { if (err) err.textContent = ""; await onSubmit(new FormData(form), ev.submitter); dlg.close(); }
    catch (e) { if (err) err.textContent = e.message; else toast(e.message, true); }
  });
  dlg.showModal();
  const first = form.querySelector("textarea, input, select, button");
  if (first) first.focus();
}

const dialogButtons = (ok, cls = "btn-primary") => `<div class="field-error" role="alert"></div><div class="dialog-actions">
  <button type="submit" class="btn btn-secondary" value="cancel" formnovalidate>Annuler</button>
  <button type="submit" class="btn ${cls}" value="ok">${ok}</button></div>`;

const actions = {
  filter: el => { S.filter = el.dataset.value; render(); },
  analyse: () => startAnalysis(null),
  detect: (el, ev) => { ev.preventDefault(); startAnalysis("#/vue-ensemble"); },
  "change-source": () => openDialog(`<h2 id="dialog-title">Changer de source</h2>
      <p>${esc(S.data ? S.data.source : "")}</p>
      <div class="btn-col" style="margin-top:0">
        <button type="submit" class="btn btn-outline btn-block" value="ok" data-choice="default">Revenir à la base SQL Server reliée</button>
        <button type="submit" class="btn btn-secondary btn-block" value="ok" data-choice="sql">Extraire un instantané frais de la base SQL Server et l'analyser</button>
        <button type="submit" class="btn btn-secondary btn-block" value="ok" data-choice="csv">Importer des fichiers CSV</button>
      </div><div class="field-error" role="alert"></div>
      <div class="dialog-actions"><button type="submit" class="btn btn-secondary" value="cancel" formnovalidate>Fermer</button></div>`,
    async (_, submitter) => {
      const choice = submitter.dataset.choice;
      if (choice === "csv") { location.hash = "#/import"; return; }
      if (choice === "default") await useDefaultSource();
      if (choice === "sql") { await useDefaultSource(true); await startAnalysis(null); }
    }),
  "pick-file": () => $("#file-input").click(),
  "copy-sql": async () => {
    const r = S.data && S.data.incident && S.data.incident.report;
    try { await navigator.clipboard.writeText(r.plan.sql); toast("Requête copiée."); }
    catch (e) { toast("Copie impossible : sélectionnez la requête à la main.", true); }
  },
  reprofile: () => loadProfile(),
  "toggle-preview": () => { S.showPreview = !S.showPreview; render(); },
  "profile-table": el => { S.profileTable = el.dataset.value; render(); },
  pause: async () => {
    try { await api(`/runs/${S.runId}/pause`, { json: { paused: !S.data.paused, actor: S.actor } });
      toast(S.data.paused ? "Enquête reprise." : "Enquête en pause : la validation est bloquée.");
      await refresh(); } catch (e) { toast(e.message, true); }
  },
  "add-hypothesis": () => {
    const inc = S.data.incident;
    openDialog(`<h2 id="dialog-title">Ajouter une hypothèse</h2>
      <p>Elle reste au niveau « Hypothèse » : seule une correction vérifiée sur une copie confirme une cause.</p>
      <div><label class="field" for="hyp-statement">Énoncé</label><input type="text" id="hyp-statement" name="statement" required></div>
      <div><label class="field" for="hyp-sql">Requête de test (lecture seule, facultative)</label>
      <textarea id="hyp-sql" name="sql" rows="5" class="code-edit"></textarea></div>${dialogButtons("Ajouter l'hypothèse")}`,
      async fd => {
        await api(`/runs/${S.runId}/hypotheses`, { json: { case_id: inc.case_ids[0], statement: fd.get("statement"), sql: fd.get("sql"), actor: S.actor } });
        toast("Hypothèse ajoutée.");
        await refresh();
      });
  },
  "see-result": el => { const k = el.dataset.key; S.openResults[k] = S.openResults[k] === "result" ? null : "result"; render(); },
  copy: async el => {
    const text = document.getElementById(el.dataset.target).textContent;
    try { await navigator.clipboard.writeText(text); toast("Requête copiée."); } catch { toast("Copie impossible dans ce navigateur.", true); }
  },
  rerun: async el => {
    const h = S.data.incident.hypotheses[+el.dataset.n];
    const parts = [];
    for (const e of h.evidence) {
      if (!e.rerunnable) { parts.push(`<div class="inline-result"><strong>${esc(e.description)}</strong> — requête paramétrée : ses paramètres ne sont pas conservés, relancez l'analyse complète.</div>`); continue; }
      try {
        const r = await api(`/runs/${S.runId}/rerun`, { json: { sql: e.sql, actor: S.actor } });
        parts.push(`<div class="inline-result"><strong>${esc(e.description)}</strong> — ${r.rows} ligne(s), initialement ${e.rows} (${r.rows === e.rows ? "identique" : "différent"})${resultTable(r.result)}</div>`);
      } catch (err) { parts.push(`<div class="inline-result">Échec : ${esc(err.message)}</div>`); }
    }
    S.openResults[h.key] = { rerun: parts.join("") };
    render();
  },
  "test-hypothesis": el => {
    const h = S.data.incident.hypotheses[+el.dataset.n];
    if (h.kind === "analyst" && h.sql) return runAnalystTest(h);
    openDialog(`<h2 id="dialog-title">Tester cette hypothèse</h2><p>${esc(h.statement)}</p>
      <div><label class="field" for="test-sql">Requête de test (lecture seule)</label>
      <textarea id="test-sql" name="sql" rows="6" class="code-edit" required>${esc(h.sql || "")}</textarea></div>
      <p>Le résultat est journalisé. Il ne suffit pas à confirmer une cause.</p>${dialogButtons("Exécuter le test")}`,
      async fd => {
        const r = await api(`/runs/${S.runId}/hypotheses`, { json: { case_id: h.case_id, statement: h.statement, sql: fd.get("sql"),
          about: h.kind === "agent" ? h.key : null, run_test: true, actor: S.actor } });
        S.testResult[h.key] = testHtml(r.test);
        await refresh();
      });
  },
  dismiss: el => {
    const h = S.data.incident.hypotheses[+el.dataset.n];
    openDialog(`<h2 id="dialog-title">Écarter l'hypothèse</h2><p>${esc(h.statement)}</p>
      <div><label class="field" for="dismiss-reason">Motif (obligatoire)</label><textarea id="dismiss-reason" name="reason" rows="3" required></textarea></div>
      ${dialogButtons("Écarter")}`,
      async fd => { await api(`/runs/${S.runId}/dismiss`, { json: { key: h.key, reason: fd.get("reason"), actor: S.actor } }); toast("Hypothèse écartée."); await refresh(); });
  },
  dashboards: () => { S.openResults.dashboards = !S.openResults.dashboards; render(); },
  "export-journal": () => {
    const q = new URLSearchParams();
    if (S.runId && !(route().key === "audit" && S.auditAll)) q.set("run_id", S.runId);
    location.href = `${API}/audit/export?${q}`;
  },
  export: el => {
    const r = S.data.incident.report;
    if (!r.report_ready) { toast("Le rapport complet est disponible lorsque tous les plans de correction ont été approuvés ou refusés.", true); return; }
    location.href = `${API}/runs/${S.runId}/report.${el.dataset.fmt}`;
  },
  share: async () => {
    const inc = S.data.incident, r = inc.report;
    const text = [`DataSentinel AI — ${inc.title}`, r.subtitle, "", r.summary, "",
      ...r.facts.map(f => `[${f.badge[0]}] ${f.text}`), "", `Enquête ${S.runId}`].join("\n");
    try { await navigator.clipboard.writeText(text); toast("Résumé copié dans le presse-papiers. Rien n'est envoyé à l'extérieur."); }
    catch { toast("Copie impossible dans ce navigateur.", true); }
  },
  expand: () => { S.expanded = !S.expanded; render(); },
  "test-copy": async () => {
    const r = S.data.incident.report;
    S.testResult.plan = "Test en cours sur une copie…"; render();
    try {
      const t = await api(`/runs/${S.runId}/plans/test`, { json: { plan_id: r.plan.id, actor: S.actor } });
      S.testResult.plan = !t.executed ? `Non exécuté : ${esc(t.reason || "")}` :
        `<strong>Avant :</strong> ${esc(r.before)}<br><strong>Après, sur la copie :</strong>
        <ul>${t.checks.map(c => `<li>${badge(c.passed ? "Réussi" : "Échoué", c.passed ? "b-confirmed" : "b-critical", true)} ${esc(c.name)} — observé : ${esc(c.observed)}</li>`).join("")}</ul>
        ${t.passed ? "Tous les contrôles passent." : "Au moins un contrôle échoue."}`;
    } catch (e) { S.testResult.plan = "Échec : " + esc(e.message); }
    render();
  },
  decide: async el => {
    const comment = $("#decision-comment").value.trim();
    if (!comment) { $("#comment-error").textContent = "Le commentaire est obligatoire pour approuver ou refuser."; $("#decision-comment").focus(); return; }
    await submitDecision(el.dataset.decision, comment, null);
  },
  "modify-plan": () => {
    const r = S.data.incident.report;
    const comment = $("#decision-comment").value;
    openDialog(`<h2 id="dialog-title">Modifier le plan</h2>
      <p>La correction modifiée est retestée sur une copie, puis soumise à une nouvelle validation.</p>
      <div><label class="field" for="plan-sql">Correction (SQL exécuté uniquement sur une copie)</label>
      <textarea id="plan-sql" name="sql" rows="8" class="code-edit">${esc(r.plan.sql)}</textarea></div>
      <div><label class="field" for="plan-comment">Commentaire de décision</label><textarea id="plan-comment" name="comment" rows="3">${esc(comment)}</textarea></div>
      ${dialogButtons("Soumettre le plan modifié")}`,
      async fd => { await submitDecision("modify", fd.get("comment"), fd.get("sql"), true); });
  },
  "sql-test": async () => {
    S.testResult.sql = "Test en cours…"; render();
    try { const i = await api("/sources/sqlserver/test");
      S.testResult.sql = `Connecté à ${esc(i.server_name)} / ${esc(i.database_name)} en tant que ${esc(i.login_name)} (SQL Server ${esc(i.version)}).${i.warning ? "<br>" + esc(i.warning) : ""}`;
    } catch (e) { S.testResult.sql = esc(e.message); }
    render();
  },
  "sql-extract": async () => { await useDefaultSource(true); await startAnalysis(null); location.hash = "#/vue-ensemble"; },
  "llm-check": async () => {
    try { const r = await api("/llm/check", { method: "POST" }); S.testResult.llm = esc(r.message); } catch (e) { S.testResult.llm = esc(e.message); }
    render();
  },
};

function testHtml(t) {
  if (!t) return "";
  return `<div class="inline-result">Test exécuté : ${t.rows} ligne(s). Ce résultat reste une hypothèse.${resultTable(t.result)}</div>`;
}

async function runAnalystTest(h) {
  try {
    const t = await api(`/runs/${S.runId}/hypotheses/${h.id}/test`, { json: { actor: S.actor } });
    S.testResult[h.key] = testHtml(t);
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function submitDecision(decision, comment, sql, fromDialog) {
  const r = S.data.incident.report;
  try {
    const res = await api(`/runs/${S.runId}/plans/decide`, { json: { plan_id: r.plan.id, decision, comment: comment || "", sql, actor: S.actor } });
    S.testResult.plan = null;
    toast({ approve: "Correction approuvée.", reject: "Correction refusée.", modify: "Plan modifié : retesté sur une copie, nouvelle validation requise." }[decision]
      + (res.status === "completed" ? " Le rapport complet est prêt à l'export." : ""));
    await refresh();
  } catch (e) {
    if (fromDialog) throw e;
    $("#comment-error").textContent = e.message;
  }
}

const changes = {
  sep: el => { S.sep = el.value; save(); },
  check: () => { S.checks = [...document.querySelectorAll('input[data-change="check"]:checked')].map(i => i.value); save(); },
  actor: el => { S.actor = el.value.trim() || "analyste"; save(); },
  "audit-all": el => { S.auditAll = el.checked; loadAudit(); },
  "audit-queries": el => { S.auditQueries = el.checked; loadAudit(); },
};

// ------------------------------------------------------------------ câblage
document.addEventListener("click", ev => {
  const el = ev.target.closest("[data-action]");
  if (!el || el.disabled) return;
  const fn = actions[el.dataset.action];
  if (fn) { if (el.tagName === "BUTTON") ev.preventDefault(); fn(el, ev); }
});
document.addEventListener("change", ev => {
  const el = ev.target.closest("[data-change]");
  if (el && changes[el.dataset.change]) changes[el.dataset.change](el);
  if (ev.target.id === "file-input") uploadFiles(ev.target.files);
});
document.addEventListener("dragover", ev => {
  const z = ev.target.closest && ev.target.closest("#dropzone");
  if (z) { ev.preventDefault(); z.classList.add("over"); }
});
document.addEventListener("dragleave", ev => { const z = ev.target.closest && ev.target.closest("#dropzone"); if (z) z.classList.remove("over"); });
document.addEventListener("drop", ev => {
  const z = ev.target.closest && ev.target.closest("#dropzone");
  if (z) { ev.preventDefault(); z.classList.remove("over"); uploadFiles(ev.dataTransfer.files); }
});

window.addEventListener("hashchange", async () => {
  const r = route();
  S.openResults = {}; S.expanded = false;
  if (r.key !== "report") S.testResult.plan = null;
  if (r.key === "audit") S.audit = null;
  window.scrollTo(0, 0);
  await refresh();
  if (r.key === "import" && S.dbPath && !S.profile) loadProfile();
});

(async function init() {
  await refresh();
  if (route().key === "import") loadProfile();
})();
