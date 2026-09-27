/* Mail Agent — panneau Home Assistant.
 *
 * Vanilla JS, sans dépendance ni étape de build.
 * SÉCURITÉ : le contenu des mails (objet, expéditeur, extrait) est contrôlé par
 * n'importe qui sur Internet. Il est TOUJOURS inséré comme texte via el(),
 * jamais via innerHTML.
 */
"use strict";

const CATEGORIES = {
  important: { label: "Important", help: "Demande une action, vient d'une vraie personne ou d'un organisme qui compte." },
  personnel: { label: "Personnel", help: "Message personnel sans urgence." },
  transactionnel: { label: "Suivi / transaction", help: "Commande, livraison, reçu, dossier en cours." },
  notification: { label: "Notification", help: "Alertes automatiques, réseaux sociaux." },
  newsletter: { label: "Newsletter", help: "Contenu éditorial récurrent." },
  promo: { label: "Promo", help: "Publicité, soldes, offres commerciales." },
  spam: { label: "Spam", help: "Arnaque, phishing, indésirable." },
};
const NOISE = ["promo", "newsletter", "notification", "spam"];
const STATUS_LABELS = { running: "en cours", done: "terminée", error: "erreur", quota: "quota atteint" };
const TRIGGER_LABELS = { manual: "panneau", auto: "automatique", ha: "Home Assistant" };

const S = {
  tab: "home",
  status: null,
  important: [],
  runs: [],
  run: null,            // { run, decisions } affiché dans l'onglet Aperçu
  runId: null,
  filterCat: null,
  filterAccount: null,
  suggest: null,        // suggestion de règle après une correction
  settings: null,
  wasRunning: false,
};

/* ---------- Utilitaires ---------- */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "value") node.value = v;
    else if (k === "checked") node.checked = !!v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

async function api(path, opts = {}) {
  const init = { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const r = await fetch(path, init);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || r.statusText || "Erreur");
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), isError ? 6000 : 3000);
}

const nf = new Intl.NumberFormat("fr-FR");
const fmtInt = (n) => nf.format(Math.round(n || 0));
const fmtUsd = (n) => (n < 0.01 && n > 0 ? "< 0,01 $" : new Intl.NumberFormat("fr-FR", { style: "currency", currency: "USD" }).format(n || 0));
const rtf = new Intl.RelativeTimeFormat("fr", { numeric: "auto" });

function relTime(iso) {
  if (!iso) return "jamais";
  const diff = (new Date(iso) - new Date()) / 1000;
  const abs = Math.abs(diff);
  if (abs < 60) return rtf.format(Math.round(diff), "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  return rtf.format(Math.round(diff / 86400), "day");
}

function fmtDate(value) {
  const d = new Date(value);
  if (isNaN(d)) return value || "";
  return d.toLocaleString("fr-FR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

function senderName(sender) {
  const name = (sender || "").split("<")[0].trim().replace(/^"|"$/g, "");
  return name || sender || "(inconnu)";
}

function gmailLink(account, msgId) {
  return `https://mail.google.com/mail/u/?authuser=${encodeURIComponent(account)}#all/${encodeURIComponent(msgId)}`;
}

const catBadge = (cat) => el("span", { class: `badge cat cat-${cat}`, title: CATEGORIES[cat]?.help }, CATEGORIES[cat]?.label || cat);

function sourceBadge(source, confidence) {
  if (source === "rule") return el("span", { class: "badge", title: "Décidé par une de tes règles, sans appel au LLM" }, "règle");
  const pct = confidence == null ? "" : ` ${Math.round(confidence * 100)} %`;
  const low = confidence != null && confidence < 0.7;
  return el("span", {
    class: "badge" + (low ? " low" : ""),
    title: low ? "Confiance faible : le modèle hésitait, vérifie ce mail" : "Décidé par le LLM, avec son niveau de confiance",
  }, `LLM${pct}`);
}

const accountShort = (email) => (email || "").split("@")[0];

/* ---------- Navigation ---------- */

document.querySelectorAll(".tabs button").forEach((b) =>
  b.addEventListener("click", () => showTab(b.dataset.tab))
);

function showTab(tab) {
  S.tab = tab;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${tab}`));
  loadTab(tab);
}

async function loadTab(tab) {
  try {
    if (tab === "home") await loadHome();
    else if (tab === "preview") await loadPreview();
    else if (tab === "usage") await loadUsage();
    else if (tab === "settings") await loadSettings();
    else if (tab === "accounts") await loadAccounts();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------- Lancement des analyses ---------- */

document.querySelectorAll("[data-analyze]").forEach((b) =>
  b.addEventListener("click", async () => {
    const apply = b.dataset.analyze === "apply";
    try {
      const { run_id } = await api("api/analyze", { method: "POST", body: { apply } });
      S.runId = run_id;
      toast(apply ? "Analyse lancée : les labels seront posés à la fin." : "Analyse lancée (aperçu, rien ne sera modifié).");
      await refreshStatus();
    } catch (e) {
      toast(e.message, true);
    }
  })
);

/* ---------- Statut global (sondage) ---------- */

async function refreshStatus() {
  try {
    S.status = await api("api/status");
  } catch (e) {
    return;
  }
  const s = S.status;
  document.getElementById("running").hidden = !s.running;
  document.querySelectorAll("[data-analyze]").forEach((b) => (b.disabled = !!s.running));
  document.getElementById("auto-summary").textContent = s.auto_enabled
    ? `Analyse auto chaque jour à ${s.schedule_time} · prochaine ${relTime(s.next_run)}`
    : "Analyse auto désactivée · mode manuel";

  const alerts = document.getElementById("alerts");
  alerts.replaceChildren();
  for (const a of s.quota_alerts) {
    alerts.append(el("div", { class: "banner warn" }, el("span", { class: "icon" }, "!"), a));
  }
  const bad = s.accounts.filter((a) => a.status !== "connected");
  if (bad.length) {
    alerts.append(el("div", { class: "banner error" }, el("span", { class: "icon" }, "!"),
      `Compte(s) à connecter : ${bad.map((a) => a.email).join(", ")}. `,
      el("a", { href: "#", onclick: (e) => { e.preventDefault(); showTab("accounts"); } }, "Onglet Comptes")));
  }
  if (s.last_run && s.last_run.error && s.last_run.status !== "running") {
    alerts.append(el("div", { class: "banner warn" }, el("span", { class: "icon" }, "!"),
      `Dernière analyse : ${s.last_run.error}`));
  }

  // Fin d'une analyse : on recharge la vue courante
  if (S.wasRunning && !s.running) {
    toast("Analyse terminée.");
    S.runId = s.last_run?.id ?? S.runId;
    loadTab(S.tab);
  }
  S.wasRunning = !!s.running;
  if (S.tab === "home") renderHomeStats();
}

(function poll() {
  refreshStatus().finally(() => setTimeout(poll, S.status?.running ? 3000 : 20000));
})();

/* ---------- Accueil ---------- */

async function loadHome(refresh = false) {
  if (!S.status) await refreshStatus();
  S.important = await api(`api/important${refresh ? "?refresh=true" : ""}`);
  renderHome();
}

function renderHomeStats() {
  const s = S.status;
  const box = document.getElementById("home-stats");
  if (!s || !box) return;
  const last = s.last_run;
  box.replaceChildren(
    el("div", { class: "stat" },
      el("div", { class: "label" }, "Importants non lus"),
      el("div", { class: "value", style: s.important_unread ? "color: var(--c-important)" : "" }, s.important_unread),
      el("div", { class: "hint" }, "sur tous les comptes")),
    el("div", { class: "stat" },
      el("div", { class: "label" }, "Dernière analyse"),
      el("div", { class: "value", style: "font-size:1.15rem" }, last ? relTime(last.finished_at || last.started_at) : "jamais"),
      el("div", { class: "hint" }, last ? `${STATUS_LABELS[last.status]} · ${TRIGGER_LABELS[last.trigger]} · ${last.n_emails} mails${last.applied ? " · appliquée" : ""}` : "Lance ta première analyse")),
    el("div", { class: "stat" },
      el("div", { class: "label" }, "Tokens aujourd'hui"),
      el("div", { class: "value" }, fmtInt(s.tokens_today)),
      el("div", { class: "hint" }, `équivalent payant : ${fmtUsd(s.cost_today)}`)),
    el("div", { class: "stat" },
      el("div", { class: "label" }, "Analyse automatique"),
      el("label", { class: "switch", style: "margin-top:8px" },
        el("input", { type: "checkbox", checked: s.auto_enabled, onchange: (e) => toggleAuto(e.target.checked) }),
        s.auto_enabled ? `Active, à ${s.schedule_time}` : "Désactivée"),
      el("div", { class: "hint" }, s.auto_enabled ? `Prochaine : ${relTime(s.next_run)}` : "Labels + bruit marqué lu, puis récap"))
  );
}

async function toggleAuto(enabled) {
  try {
    await api("api/auto", { method: "POST", body: { enabled } });
    toast(enabled ? "Analyse automatique activée." : "Analyse automatique désactivée.");
    await refreshStatus();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderHome() {
  const root = document.getElementById("tab-home");
  const list = S.important.length
    ? el("ul", { class: "mail-list" }, S.important.map(importantItem))
    : el("p", { class: "empty" }, "Aucun mail important non lu. 🎉");
  root.replaceChildren(
    el("div", { class: "grid", id: "home-stats" }),
    el("div", { class: "card" },
      el("div", { class: "card-head" },
        el("h2", {}, "Mails importants non lus"),
        el("button", { class: "btn small", onclick: () => loadHome(true).then(() => toast("Liste actualisée depuis Gmail.")) }, "Actualiser")),
      el("p", { class: "muted small", style: "margin-top:0" },
        "Mails labellisés « Important » encore non lus dans Gmail, plus ceux du dernier aperçu pas encore appliqué. ",
        "La ligne en italique explique pourquoi le mail a été jugé important."),
      list)
  );
  renderHomeStats();
}

function importantItem(m) {
  return el("li", { class: "mail" },
    el("div", {},
      el("div", { class: "meta" },
        el("span", { class: "who" }, m.sender_name || senderName(m.sender)),
        el("span", { class: "badge", title: m.account }, accountShort(m.account)),
        m.pending && el("span", { class: "badge low", title: "Issu d'un aperçu : pas encore labellisé dans Gmail" }, "aperçu non appliqué")),
      el("div", { class: "subject" }, m.subject),
      el("div", { class: "snippet" }, m.snippet),
      el("div", { class: "why" },
        el("b", {}, "Pourquoi ? "),
        m.reason || "Labellisé Important dans Gmail (décision antérieure à l'historique ou label posé à la main).",
        m.source ? " · " : "", m.source ? sourceBadge(m.source, m.confidence) : null)),
    el("div", { class: "side" },
      el("span", { class: "muted small" }, fmtDate(m.date)),
      el("a", { class: "btn small", href: gmailLink(m.account, m.msg_id), target: "_blank", rel: "noopener" }, "Ouvrir dans Gmail"))
  );
}

/* ---------- Aperçu du tri ---------- */

async function loadPreview() {
  S.runs = await api("api/runs?limit=30");
  if (!S.runs.length) {
    document.getElementById("tab-preview").replaceChildren(
      el("div", { class: "card" }, el("h2", {}, "Aperçu du tri"),
        el("p", { class: "empty" }, "Aucune analyse pour l'instant. Clique sur « Analyser (aperçu) » en haut : rien ne sera modifié dans Gmail.")));
    return;
  }
  if (!S.runId || !S.runs.some((r) => r.id === S.runId)) S.runId = S.runs[0].id;
  S.run = await api(`api/runs/${S.runId}`);
  renderPreview();
}

function runLabel(r) {
  return `#${r.id} · ${fmtDate(r.started_at)} · ${TRIGGER_LABELS[r.trigger]} · ${r.n_emails} mails · ${STATUS_LABELS[r.status]}${r.applied ? " · appliquée" : ""}`;
}

function renderPreview() {
  const root = document.getElementById("tab-preview");
  const { run, decisions } = S.run;
  const accounts = [...new Set(decisions.map((d) => d.account))];
  const counts = {};
  decisions.forEach((d) => (counts[d.category] = (counts[d.category] || 0) + 1));
  const visible = decisions.filter((d) =>
    (!S.filterCat || d.category === S.filterCat) && (!S.filterAccount || d.account === S.filterAccount));
  const corrected = decisions.filter((d) => d.category !== d.original_category).length;
  const lowConf = decisions.filter((d) => d.source === "llm" && d.confidence != null && d.confidence < 0.7).length;

  const header = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("select", {
        style: "max-width: 100%; width: auto",
        onchange: (e) => { S.runId = Number(e.target.value); S.suggest = null; loadPreview(); },
      }, S.runs.map((r) => el("option", { value: r.id, selected: r.id === run.id }, runLabel(r)))),
      run.applied
        ? el("span", { class: "badge", style: "color:var(--ok);border-color:var(--ok)" }, `✓ appliquée ${relTime(run.applied_at)}`)
        : el("button", {
            class: "btn primary",
            disabled: run.status === "running" || !decisions.length,
            onclick: () => applyRun(run.id),
            title: "Pose les labels IA/… et marque le bruit (promo, newsletter, notification, spam) comme lu",
          }, "Appliquer ce tri dans Gmail")),
    el("div", { class: "legend" },
      el("div", {}, `${decisions.length} mails classés · ${fmtInt(run.input_tokens + run.output_tokens)} tokens (${run.requests} requête(s) au LLM, modèle ${run.model})`),
      el("div", {}, "Comment lire : ", el("span", { class: "badge" }, "règle"), " = décidé par une de tes règles (gratuit, sans LLM) · ",
        el("span", { class: "badge" }, "LLM 90 %"), " = décidé par Gemini avec sa confiance · ",
        el("span", { class: "badge low" }, "LLM 60 %"), " = confiance faible, à vérifier."),
      !run.applied && el("div", {}, "Rien n'est encore modifié dans Gmail. Corrige si besoin avec le menu à droite de chaque mail, puis clique sur « Appliquer »."),
      !!run.applied && el("div", {}, "Déjà appliquée : une correction met aussi à jour le label dans Gmail."),
      (corrected + lowConf > 0) && el("div", {}, `${corrected} correction(s) · ${lowConf} mail(s) à faible confiance.`)),
    run.error && el("div", { class: "banner warn", style: "margin:12px 0 0" }, el("span", { class: "icon" }, "!"), run.error)
  );

  const filters = el("div", { class: "filters" },
    el("button", { class: "chip" + (!S.filterCat ? " active" : ""), onclick: () => { S.filterCat = null; renderPreview(); } }, `Toutes (${decisions.length})`),
    Object.keys(CATEGORIES).filter((c) => counts[c]).map((c) =>
      el("button", { class: "chip" + (S.filterCat === c ? " active" : ""), onclick: () => { S.filterCat = c; renderPreview(); } },
        `${CATEGORIES[c].label} (${counts[c]})`)),
    accounts.length > 1 && el("select", {
      class: "correct", style: "margin-left:auto",
      onchange: (e) => { S.filterAccount = e.target.value || null; renderPreview(); },
    }, el("option", { value: "" }, "Tous les comptes"), accounts.map((a) => el("option", { value: a, selected: a === S.filterAccount }, a)))
  );

  const groups = Object.keys(CATEGORIES).map((cat) => {
    const items = visible.filter((d) => d.category === cat);
    if (!items.length) return null;
    return [
      el("div", { class: "group-title" }, catBadge(cat), el("span", { class: "count" }, `${items.length} · ${CATEGORIES[cat].help}`),
        NOISE.includes(cat) && el("span", { class: "muted small" }, "→ sera marqué lu")),
      el("ul", { class: "mail-list" }, items.map(decisionItem)),
    ];
  });

  const suggested = S.suggest && decisions.find((d) => d.id === S.suggest.decisionId);
  root.replaceChildren(header, el("div", { class: "card" }, suggested && suggestBox(suggested), filters, visible.length ? groups : el("p", { class: "empty" }, "Aucun mail pour ce filtre.")));
}

function decisionItem(d) {
  const wasCorrected = d.category !== d.original_category;
  const select = el("select", {
    class: "correct", title: "Corriger la catégorie",
    onchange: (e) => correctDecision(d, e.target.value),
  }, Object.keys(CATEGORIES).map((c) => el("option", { value: c, selected: c === d.category }, CATEGORIES[c].label)));

  const signals = [];
  if (d.has_unsubscribe) signals.push(el("span", { class: "badge", title: "Le mail contient un lien de désinscription (header List-Unsubscribe) : c'est un envoi de masse" }, "envoi de masse"));
  if (d.unread) signals.push(el("span", { class: "badge", title: "Non lu au moment de l'analyse" }, "non lu"));

  const item = el("li", { class: "mail" + (wasCorrected ? " corrected" : "") },
    el("div", {},
      el("div", { class: "meta" },
        el("span", { class: "who", title: d.sender }, senderName(d.sender)),
        el("span", { class: "badge", title: d.account }, accountShort(d.account)),
        sourceBadge(d.source, d.confidence), signals),
      el("div", { class: "subject" }, d.subject),
      el("div", { class: "snippet" }, d.snippet),
      el("div", { class: "why" },
        wasCorrected
          ? [el("b", {}, "Corrigé par toi. "), `Le tri initial était « ${CATEGORIES[d.original_category]?.label} » : ${d.reason}`]
          : [el("b", {}, "Pourquoi ? "), d.reason])),
    el("div", { class: "side" },
      el("span", { class: "muted small" }, fmtDate(d.date)),
      select,
      el("a", { class: "small", href: gmailLink(d.account, d.msg_id), target: "_blank", rel: "noopener" }, "Gmail ↗")),
  );
  return item;
}

function suggestBox(d) {
  const { category, suggestions } = S.suggest;
  return el("div", { class: "suggest", style: "margin:0 0 12px" },
    el("span", {}, el("b", {}, `${senderName(d.sender)} → ${CATEGORIES[category].label}. `),
      `Pour que ça ne se reproduise pas, toujours classer en « ${CATEGORIES[category].label} » :`),
    suggestions.map((s) => el("button", { class: "btn small", onclick: () => addRule(category, s.pattern) }, s.label)),
    el("button", { class: "btn small", onclick: () => { S.suggest = null; renderPreview(); } }, "Non merci"));
}

async function correctDecision(d, category) {
  try {
    const res = await api(`api/decisions/${d.id}/correct`, { method: "POST", body: { category } });
    d.category = category;
    S.suggest = { decisionId: d.id, category, suggestions: res.suggestions };
    renderPreview();
    toast(S.run.run.applied ? "Corrigé, label mis à jour dans Gmail." : "Corrigé. Sera pris en compte à l'application.");
  } catch (e) {
    toast(e.message, true);
  }
}

async function addRule(category, pattern) {
  try {
    await api("api/rules", { method: "POST", body: { category, pattern } });
    S.suggest = null;
    renderPreview();
    toast(`Règle ajoutée : « ${pattern} » → ${CATEGORIES[category].label}. Visible dans Réglages.`);
  } catch (e) {
    toast(e.message, true);
  }
}

async function applyRun(runId) {
  try {
    await api(`api/runs/${runId}/apply`, { method: "POST" });
    toast("Tri appliqué dans Gmail.");
    await loadPreview();
    refreshStatus();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------- Consommation ---------- */

async function loadUsage() {
  const u = await api("api/usage?days=30");
  const root = document.getElementById("tab-usage");
  const pin = u.price_input_per_m, pout = u.price_output_per_m;
  const cost = (r) => (r.input_tokens * pin + r.output_tokens * pout) / 1e6;
  const total = u.days.reduce((a, d) => a + d.input_tokens + d.output_tokens, 0);
  const totalCost = u.days.reduce((a, d) => a + cost(d), 0);
  const today = u.days[u.days.length - 1];
  const runsWithTokens = u.runs.filter((r) => r.requests > 0);
  const avg = runsWithTokens.length ? runsWithTokens.reduce((a, r) => a + r.input_tokens + r.output_tokens, 0) / runsWithTokens.length : 0;

  root.replaceChildren(
    el("div", { class: "grid" },
      stat("Aujourd'hui", fmtInt(today.input_tokens + today.output_tokens), `tokens · ${today.requests} requête(s)`),
      stat("30 derniers jours", fmtInt(total), "tokens"),
      stat("Coût équivalent 30 j", fmtUsd(totalCost), "si tu passais au palier payant"),
      stat("Par analyse", fmtInt(avg), "tokens en moyenne")),
    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("h2", {}, "Tokens par jour"),
        el("span", { class: "muted small" }, `Modèle ${u.model} · ${pin} $ / ${pout} $ par million (entrée / sortie)`)),
      usageChart(u.days, u.daily_token_alert),
      el("div", { class: "chart-legend" },
        el("span", {}, el("span", { class: "dot", style: "background:var(--accent)" }), "entrée (mails envoyés au LLM)"),
        el("span", {}, el("span", { class: "dot", style: "background:var(--c-newsletter)" }), "sortie (réponse + réflexion)"),
        u.daily_token_alert > 0 && el("span", {}, el("span", { class: "dot", style: "background:var(--warn)" }), `seuil d'alerte : ${fmtInt(u.daily_token_alert)} / jour (tracé s'il est proche)`),
        el("span", {}, el("span", { class: "dot", style: "background:var(--danger)" }), "jour avec quota atteint"))),
    el("div", { class: "card" },
      el("h2", {}, "Comprendre ces chiffres"),
      el("ul", { class: "legend", style: "padding-left:18px;margin:0" },
        el("li", {}, "Sur l'offre gratuite de Gemini, tu ne paies rien. Le « coût équivalent » est ce que tu paierais au palier payant, qui a l'avantage de ne pas utiliser tes mails pour entraîner les modèles de Google."),
        el("li", {}, "Les mails classés par une règle ne consomment aucun token, et un mail déjà appliqué n'est jamais renvoyé au LLM."),
        el("li", {}, "Si Gemini répond « quota dépassé », l'analyse s'arrête proprement et une alerte apparaît ici, dans HA et sur ton téléphone. Les limites exactes de ton compte sont visibles dans Google AI Studio."))),
    el("div", { class: "card" },
      el("h2", {}, "Historique des analyses"),
      el("div", { class: "table-wrap" },
        el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "Date"), el("th", {}, "Déclenchée par"), el("th", {}, "Statut"),
            el("th", { class: "num" }, "Mails"), el("th", { class: "num" }, "Requêtes"),
            el("th", { class: "num" }, "Entrée"), el("th", { class: "num" }, "Sortie"), el("th", { class: "num" }, "Coût équiv."))),
          el("tbody", {}, u.runs.map((r) => el("tr", {},
            el("td", {}, fmtDate(r.started_at)), el("td", {}, TRIGGER_LABELS[r.trigger]),
            el("td", { style: r.status === "quota" || r.status === "error" ? "color:var(--danger)" : "" }, STATUS_LABELS[r.status] + (r.applied ? " · appliquée" : "")),
            el("td", { class: "num" }, r.n_emails), el("td", { class: "num" }, r.requests),
            el("td", { class: "num" }, fmtInt(r.input_tokens)), el("td", { class: "num" }, fmtInt(r.output_tokens)),
            el("td", { class: "num" }, fmtUsd(cost(r)))))))))
  );
}

function stat(label, value, hint) {
  return el("div", { class: "stat" }, el("div", { class: "label" }, label), el("div", { class: "value" }, value), el("div", { class: "hint" }, hint));
}

function usageChart(days, threshold) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = (tag, attrs = {}, ...kids) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    kids.forEach((k) => n.append(k instanceof Node ? k : document.createTextNode(k)));
    return n;
  };
  const W = 800, H = 220, L = 50, B = 24, T = 10;
  const dataMax = Math.max(1, ...days.map((d) => d.input_tokens + d.output_tokens));
  // Le seuil n'agrandit l'échelle que s'il est du même ordre que la consommation réelle
  const showThreshold = threshold > 0 && threshold <= dataMax * 3;
  const max = Math.max(dataMax, showThreshold ? threshold : 0) * 1.1;
  const bw = (W - L) / days.length;
  const y = (v) => H - B - (v / max) * (H - B - T);
  const root = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img", "aria-label": "Tokens consommés par jour sur 30 jours" });
  for (let i = 0; i <= 4; i++) {
    const v = (max / 1.1) * (i / 4), yy = y(v);
    root.append(svg("line", { class: "grid-line", x1: L, x2: W, y1: yy, y2: yy }),
      svg("text", { class: "axis", x: L - 6, y: yy + 3, "text-anchor": "end" }, v >= 1000 ? `${Math.round(v / 1000)}k` : String(Math.round(v))));
  }
  days.forEach((d, i) => {
    const x = L + i * bw + 2, w = Math.max(1, bw - 4);
    const yin = y(d.input_tokens), yout = y(d.input_tokens + d.output_tokens);
    const g = svg("g", {}, svg("title", {}, `${d.day} : ${fmtInt(d.input_tokens)} entrée, ${fmtInt(d.output_tokens)} sortie, ${d.runs} analyse(s)`));
    g.append(svg("rect", { class: "in", x, y: yin, width: w, height: H - B - yin, rx: 2 }));
    g.append(svg("rect", { class: "out", x, y: yout, width: w, height: yin - yout, rx: 2 }));
    if (d.quota_errors) g.append(svg("rect", { class: "quota", x, y: H - B + 2, width: w, height: 4 }));
    root.append(g);
    if (i % 5 === 0 || i === days.length - 1)
      root.append(svg("text", { class: "axis", x: x + w / 2, y: H - 6, "text-anchor": "middle" }, d.day.slice(8, 10) + "/" + d.day.slice(5, 7)));
  });
  if (showThreshold) root.append(svg("line", { class: "threshold", x1: L, x2: W, y1: y(threshold), y2: y(threshold) }));
  return root;
}

/* ---------- Réglages ---------- */

async function loadSettings() {
  S.settings = await api("api/settings");
  renderSettings();
}

function renderSettings() {
  const { config, categories, models, notify_services, in_addon } = S.settings;
  const c = config;
  const field = (label, input, help) => el("label", { class: "field" }, el("span", {}, label), input, help && el("small", {}, help));
  const lines = (arr) => (arr || []).join("\n");

  const notifySelect = el("select", { id: "s-notify" },
    el("option", { value: "" }, "— Pas de récap —"),
    [...new Set([...notify_services, c.notifications.service].filter(Boolean))].map((s) =>
      el("option", { value: s, selected: s === c.notifications.service }, `notify.${s}`)));

  const modelSelect = el("select", { id: "s-model" },
    [...new Set([...models, c.llm.model])].map((m) => el("option", { value: m, selected: m === c.llm.model }, m)));

  const accountsBox = el("div", { id: "s-accounts" }, c.accounts.map(accountEditor));

  const ruleKeys = ["private", ...categories];
  const rules = el("div", { class: "row" }, ruleKeys.map((cat) =>
    field(cat === "private" ? "Privés (important, jamais envoyés au LLM)" : `→ ${CATEGORIES[cat].label}`,
      el("textarea", { dataset: { rule: `${cat}_senders` }, placeholder: "@domaine.fr\nadresse@exacte.fr\nname:Nom affiché", value: lines(c.rules[`${cat}_senders`]) }))));

  const root = document.getElementById("tab-settings");
  root.replaceChildren(
    el("div", { class: "card" },
      el("h2", {}, "Analyse automatique"),
      el("div", { class: "row" },
        field("Activation", el("label", { class: "switch" }, el("input", { type: "checkbox", id: "s-auto", checked: c.schedule.enabled }), "Analyser chaque jour"),
          "Pose les labels IA/… et marque le bruit (promo, newsletter, notification, spam) comme lu."),
        field("Heure", el("input", { type: "time", id: "s-time", value: c.schedule.time })),
        field("Récap sur le téléphone", notifySelect,
          in_addon ? "Envoyé après chaque analyse automatique, seulement s'il y a des mails importants." : "Disponible uniquement dans Home Assistant."))),
    el("div", { class: "card" },
      el("h2", {}, "Modèle et consommation"),
      el("div", { class: "row" },
        field("Modèle Gemini", modelSelect, "Flash-Lite : rapide et gros quota gratuit. Flash : plus fin, quota plus petit."),
        field("Mails par requête", el("input", { type: "number", id: "s-batch", min: 1, max: 50, value: c.llm.batch_size || 15 }), "Plus c'est grand, moins il y a de requêtes (économise le quota)."),
        field("Seuil d'alerte (tokens / jour)", el("input", { type: "number", id: "s-threshold", min: 0, step: 10000, value: c.usage.daily_token_alert || 0 }), "0 = pas d'alerte."))),
    el("div", { class: "card" },
      el("h2", {}, "Profil par défaut"),
      el("p", { class: "muted small", style: "margin-top:0" }, "Décris en langage naturel ce qui compte pour toi. Le LLM lit ce texte pour décider. Chaque compte peut avoir le sien (plus bas)."),
      el("textarea", { class: "prose", id: "s-profile", value: c.profile })),
    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("h2", {}, "Comptes"),
        el("button", { class: "btn small", onclick: () => accountsBox.append(accountEditor({ email: "" })) }, "+ Ajouter un compte")),
      accountsBox),
    el("div", { class: "card" },
      el("h2", {}, "Règles"),
      el("p", { class: "muted small", style: "margin-top:0" },
        "Appliquées avant le LLM : gratuites, instantanées et sans hésitation. Un motif par ligne : ",
        el("code", {}, "@domaine.fr"), " (et ses sous-domaines), ", el("code", {}, "adresse@exacte.fr"), " ou ",
        el("code", {}, "name:Nom affiché"), ". Les corrections de l'Aperçu peuvent en ajouter automatiquement."),
      rules),
    el("div", { class: "card" },
      el("h2", {}, "Valeurs par défaut Gmail"),
      el("div", { class: "row" },
        field("Requête Gmail", el("input", { type: "text", id: "s-query", value: c.gmail?.query || "" }), "Syntaxe de la recherche Gmail, ex. in:inbox newer_than:3d"),
        field("Mails max par compte", el("input", { type: "number", id: "s-max", min: 1, max: 500, value: c.gmail?.max_messages || 100 })),
        field("Préfixe des labels", el("input", { type: "text", id: "s-prefix", value: c.gmail?.label_prefix || "IA" })))),
    el("div", { class: "card" },
      el("details", { ontoggle: (e) => e.target.open && loadYaml() },
        el("summary", {}, "Avancé : éditer le YAML complet"),
        el("p", { class: "muted small" }, "Pour importer ton profile.yaml existant d'un coup : colle-le ici puis enregistre."),
        el("textarea", { id: "s-yaml", style: "min-height:360px" }),
        el("div", { style: "margin-top:8px" }, el("button", { class: "btn", onclick: saveYaml }, "Enregistrer le YAML")))),
    el("div", { class: "sticky-save" },
      el("button", { class: "btn", onclick: loadSettings }, "Annuler"),
      el("button", { class: "btn primary", onclick: saveSettings }, "Enregistrer les réglages"))
  );
}

function accountEditor(a) {
  const box = el("div", { class: "card", style: "background:var(--surface-2);box-shadow:none" },
    el("div", { class: "row" },
      el("label", { class: "field" }, el("span", {}, "Adresse Gmail"), el("input", { type: "text", "data-k": "email", value: a.email || "", placeholder: "prenom.nom@gmail.com" })),
      el("label", { class: "field" }, el("span", {}, "Requête Gmail (facultatif)"), el("input", { type: "text", "data-k": "query", value: a.query || "", placeholder: "par défaut" })),
      el("label", { class: "field" }, el("span", {}, "Mails max (facultatif)"), el("input", { type: "number", "data-k": "max_messages", value: a.max_messages || "", placeholder: "par défaut" }))),
    el("label", { class: "field" }, el("span", {}, "Profil de ce compte (facultatif)"),
      el("textarea", { class: "prose", "data-k": "profile", value: a.profile || "", placeholder: "Vide = profil par défaut. Ex. : cette adresse me sert uniquement pour les achats en ligne…" })),
    el("button", { class: "btn small danger", onclick: () => box.remove() }, "Retirer ce compte"));
  box.dataset.account = "1";
  return box;
}

function collectSettings() {
  const c = structuredClone(S.settings.config);
  const v = (id) => document.getElementById(id).value;
  c.schedule = { enabled: document.getElementById("s-auto").checked, time: v("s-time") || "07:30" };
  c.notifications = { ...(c.notifications || {}), service: v("s-notify") };
  c.llm = { ...c.llm, model: v("s-model"), batch_size: Number(v("s-batch")) || 15 };
  c.usage = { ...(c.usage || {}), daily_token_alert: Number(v("s-threshold")) || 0 };
  c.profile = v("s-profile");
  c.gmail = { ...(c.gmail || {}), query: v("s-query"), max_messages: Number(v("s-max")) || 100, label_prefix: v("s-prefix") || "IA" };
  c.accounts = [...document.querySelectorAll("#s-accounts [data-account]")].map((box) => {
    const get = (k) => box.querySelector(`[data-k="${k}"]`).value.trim();
    const acc = { email: get("email") };
    if (get("query")) acc.query = get("query");
    if (get("max_messages")) acc.max_messages = Number(get("max_messages"));
    if (get("profile")) acc.profile = get("profile") + "\n";
    return acc;
  }).filter((a) => a.email);
  document.querySelectorAll("[data-rule]").forEach((t) => {
    c.rules[t.dataset.rule] = t.value.split("\n").map((s) => s.trim()).filter(Boolean);
  });
  return c;
}

async function saveSettings() {
  try {
    await api("api/settings", { method: "PUT", body: collectSettings() });
    toast("Réglages enregistrés.");
    await loadSettings();
    refreshStatus();
  } catch (e) {
    toast(e.message, true);
  }
}

async function loadYaml() {
  document.getElementById("s-yaml").value = await api("api/settings/yaml");
}

async function saveYaml() {
  try {
    await api("api/settings/yaml", { method: "PUT", body: document.getElementById("s-yaml").value, headers: { "Content-Type": "text/plain" } });
    toast("YAML enregistré.");
    await loadSettings();
    refreshStatus();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------- Comptes ---------- */

async function loadAccounts() {
  const data = await api("api/accounts");
  const root = document.getElementById("tab-accounts");
  const STATUS = {
    connected: ["✓ connecté", "var(--ok)"],
    disconnected: ["non connecté", "var(--danger)"],
    expired: ["jeton expiré ou révoqué", "var(--danger)"],
  };

  const credCard = el("div", { class: "card" },
    el("div", { class: "card-head" }, el("h2", {}, "1. Identifiant OAuth Google"),
      el("span", { class: "badge", style: `color:${data.credentials_present ? "var(--ok)" : "var(--danger)"};border-color:currentColor` },
        data.credentials_present ? "✓ présent" : "absent")),
    el("p", { class: "muted small", style: "margin-top:0" },
      "Le fichier ", el("code", {}, "credentials.json"), " (type « Desktop app ») téléchargé depuis Google Cloud. ",
      data.credentials_present ? "Tu peux le remplacer si besoin." : "Choisis-le ou colle son contenu :"),
    el("input", { type: "file", accept: ".json,application/json", onchange: async (e) => {
      const f = e.target.files[0];
      if (f) document.getElementById("cred-text").value = await f.text();
    } }),
    el("textarea", { id: "cred-text", placeholder: '{"installed": {"client_id": "…", …}}', style: "margin-top:8px" }),
    el("div", { style: "margin-top:8px" }, el("button", { class: "btn primary", onclick: async () => {
      try {
        await api("api/credentials", { method: "POST", body: { text: document.getElementById("cred-text").value } });
        toast("Identifiant enregistré.");
        loadAccounts();
      } catch (e) { toast(e.message, true); }
    } }, "Enregistrer")));

  const accountCards = data.accounts.map((a) => {
    const [label, color] = STATUS[a.status] || [a.status, "var(--muted)"];
    const flow = el("div", { hidden: true });
    const enc = encodeURIComponent(a.email);
    return el("div", { class: "card" },
      el("div", { class: "card-head" }, el("h3", {}, a.email),
        el("span", { class: "badge", style: `color:${color};border-color:currentColor` }, label)),
      el("div", { class: "actions" },
        el("button", { class: "btn" + (a.status === "connected" ? "" : " primary"), onclick: async () => {
          try {
            const { url } = await api(`api/accounts/${enc}/auth-url`, { method: "POST" });
            flow.hidden = false;
            flow.replaceChildren(
              el("ol", { class: "steps", style: "margin-top:12px" },
                el("li", {}, el("a", { href: url, target: "_blank", rel: "noopener" }, "Ouvre ce lien d'autorisation Google"), ` et choisis le compte ${a.email}.`),
                el("li", {}, "Écran « application non validée » : clique sur Continuer (c'est ta propre app), puis autorise."),
                el("li", {}, "Google t'envoie vers une page ", el("code", {}, "http://localhost:8765/?…"), " qui ne s'affiche pas : c'est normal. Copie l'adresse complète depuis la barre du navigateur."),
                el("li", {}, "Colle-la ici :")),
              el("input", { type: "text", id: `code-${enc}`, placeholder: "http://localhost:8765/?state=…&code=…&scope=…" }),
              el("div", { style: "margin-top:8px" }, el("button", { class: "btn primary", onclick: async () => {
                try {
                  await api(`api/accounts/${enc}/auth-code`, { method: "POST", body: { text: document.getElementById(`code-${enc}`).value } });
                  toast(`${a.email} connecté.`);
                  loadAccounts();
                  refreshStatus();
                } catch (e) { toast(e.message, true); }
              } }, "Valider")));
          } catch (e) { toast(e.message, true); }
        } }, a.status === "connected" ? "Reconnecter" : "Connecter")),
      flow,
      el("details", { style: "margin-top:12px" },
        el("summary", { class: "small" }, "Alternative : importer un jeton existant"),
        el("p", { class: "muted small" }, "Colle le contenu du fichier ", el("code", {}, `tokens/${a.email}.json`), " obtenu en local sur ton ordinateur."),
        el("textarea", { id: `tok-${enc}` }),
        el("div", { style: "margin-top:8px" }, el("button", { class: "btn", onclick: async () => {
          try {
            await api(`api/accounts/${enc}/token`, { method: "POST", body: { text: document.getElementById(`tok-${enc}`).value } });
            toast("Jeton importé.");
            loadAccounts();
            refreshStatus();
          } catch (e) { toast(e.message, true); }
        } }, "Importer"))));
  });

  root.replaceChildren(
    credCard,
    el("h2", { style: "margin:24px 0 12px" }, "2. Comptes Gmail"),
    ...(data.accounts.length ? accountCards : [el("p", { class: "empty" }, "Aucun compte : ajoute-les dans l'onglet Réglages.")]),
    el("div", { class: "card" },
      el("h3", {}, "Bon à savoir"),
      el("ul", { class: "legend", style: "padding-left:18px;margin:8px 0 0" },
        el("li", {}, "Pour que l'add-on tourne sans interruption, passe ton app OAuth en « In production » dans Google Cloud (écran de consentement). En mode « Testing », les jetons expirent au bout de 7 jours."),
        el("li", {}, "Les jetons restent sur le Pi, dans le dossier privé de l'add-on. La permission demandée ne permet ni d'envoyer ni de supprimer définitivement des mails.")))
  );
}

/* ---------- Démarrage ---------- */
loadTab("home");
