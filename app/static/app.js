"use strict";
/* Photothèque — interface (aucune dépendance, aucun build) */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const view = $("#view");
const FILTER_KEYS = ["person", "from", "to", "folder", "camera", "q", "before"];

/* ------------------------------------------------------------------ utilitaires */

class AuthError extends Error {}

async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.json !== undefined) {
    o.body = JSON.stringify(o.json);
    o.headers["Content-Type"] = "application/json";
    delete o.json;
  }
  const r = await fetch(path, o);
  if (r.status === 401 && !path.startsWith("/api/login")) { showLogin(); throw new AuthError(); }
  if (!r.ok) {
    let msg = `Erreur ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

const fmtMonth = (ym) => {
  const [y, m] = ym.split("-").map(Number);
  const s = new Date(y, m - 1, 1).toLocaleDateString("fr-FR", { month: "long", year: "numeric" });
  return s.charAt(0).toUpperCase() + s.slice(1);
};
const parseTaken = (t) => { const [d, h = "00:00:00"] = t.split(" "); const [y, m, dd] = d.split("-").map(Number); const [H, M] = h.split(":").map(Number); return new Date(y, m - 1, dd, H, M); };
const fmtLong = (t) => {
  const d = parseTaken(t);
  const s = d.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  return s.charAt(0).toUpperCase() + s.slice(1) + ", " + d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
};
const fmtShortDate = (s) => new Date(s + "T12:00:00").toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
const fmtSize = (b) => b > 1e6 ? (b / 1e6).toFixed(1).replace(".", ",") + " Mo" : Math.round(b / 1e3) + " Ko";
const plural = (n, one, many) => `${n.toLocaleString("fr-FR")} ${n > 1 ? many : one}`;

/* ------------------------------------------------------------------ routage */

function parseHash() {
  const h = location.hash.replace(/^#\/?/, "");
  const [path, qs = ""] = h.split("?");
  const parts = path.split("/").filter(Boolean);
  const params = Object.fromEntries(new URLSearchParams(qs));
  return { view: parts[0] || "photos", id: parts[1] ? Number(parts[1]) : null, params };
}

function hashFor(viewName, params = {}, id = null) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
  return `#/${viewName}${id ? "/" + id : ""}${qs ? "?" + qs : ""}`;
}

function setFilters(patch, { keepBefore = false } = {}) {
  const r = parseHash();
  const params = { ...r.params, ...patch };
  if (!keepBefore && !("before" in patch)) delete params.before;
  const viewName = r.view === "person" ? "person" : "photos";
  location.hash = hashFor(viewName, params, viewName === "person" ? r.id : null);
}

let current = null; // composant de vue actif (Gallery…)

async function render() {
  if (current && current.destroy) current.destroy();
  current = null;
  const r = parseHash();
  $$(".tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === (r.view === "person" ? "people" : r.view)));
  $("#searchInput").value = r.params.q || "";
  window.scrollTo(0, 0);
  try {
    if (r.view === "people") await renderPeople(r.params);
    else if (r.view === "folders") await renderFolders(r.params);
    else if (r.view === "person" && r.id) await renderPerson(r.id, r.params);
    else await renderPhotos(r.params);
  } catch (e) {
    if (!(e instanceof AuthError)) view.innerHTML = `<div class="empty"><h2>Impossible d'afficher cette page</h2><p>${esc(e.message)}</p></div>`;
  }
  renderChips(r);
  updateFilm(r);
}

/* ------------------------------------------------------------------ personnes (cache) */

const People = {
  list: null,
  async all(force = false) {
    if (!this.list || force) this.list = (await api("/api/persons?named=1&limit=5000")).items;
    return this.list;
  },
  byId(id) { return (this.list || []).find((p) => p.id === Number(id)); },
  invalidate() { this.list = null; },
};

/* ------------------------------------------------------------------ galerie justifiée */

class Gallery {
  constructor(container, params) {
    this.el = container;
    this.params = params;
    this.items = [];
    this.next = undefined;
    this.loading = false;
    this.sections = new Map();
    this.counts = {};
    this.tiles = new Map();
    this.sentinel = document.createElement("div");
    this.sentinel.id = "sentinel";
    this.el.append(this.sentinel);
    this.io = new IntersectionObserver((e) => e[0].isIntersecting && this.load(), { rootMargin: "1800px" });
    this.onResize = () => { clearTimeout(this._rt); this._rt = setTimeout(() => this.relayout(), 120); };
    window.addEventListener("resize", this.onResize);
  }

  query(extra = {}) {
    const p = new URLSearchParams();
    for (const k of FILTER_KEYS) if (this.params[k]) p.set(k, this.params[k]);
    for (const [k, v] of Object.entries(extra)) if (v) p.set(k, v);
    return p.toString();
  }

  async start() {
    const tl = await api("/api/timeline?" + this.query());
    tl.forEach((t) => (this.counts[t.ym] = t.n));
    await this.load();
    this.io.observe(this.sentinel);
    return this.items.length;
  }

  get hasMore() { return this.next !== null; }

  async load() {
    if (this.loading || this.next === null) return;
    this.loading = true;
    try {
      const data = await api("/api/photos?" + this.query({ limit: 240, cursor: this.next }));
      this.next = data.next;
      this.append(data.items);
    } finally { this.loading = false; }
  }

  append(items) {
    const touched = new Set();
    for (const it of items) {
      it.idx = this.items.length;
      this.items.push(it);
      const ym = it.t.slice(0, 7);
      let sec = this.sections.get(ym);
      if (!sec) {
        const el = document.createElement("section");
        el.className = "month";
        el.dataset.ym = ym;
        const n = this.counts[ym];
        el.innerHTML = `<h2 id="m-${ym}">${fmtMonth(ym)}${n ? `<small>${plural(n, "photo", "photos")}</small>` : ""}</h2><div class="rows"></div>`;
        this.el.insertBefore(el, this.sentinel);
        sec = { el, rows: $(".rows", el), items: [] };
        this.sections.set(ym, sec);
      }
      sec.items.push(it);
      touched.add(sec);
    }
    touched.forEach((s) => this.layout(s));
  }

  tile(it) {
    let t = this.tiles.get(it.id);
    if (!t) {
      t = document.createElement("button");
      t.className = "tile";
      t.setAttribute("aria-label", "Photo du " + fmtLong(it.t));
      const img = new Image();
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = "";
      img.onload = () => img.classList.add("ld");
      img.src = `/thumb/${it.id}`;
      t.append(img);
      t.onclick = () => Lightbox.open(this, it.idx);
      this.tiles.set(it.id, t);
    }
    return t;
  }

  layout(sec) {
    const width = sec.rows.clientWidth || this.el.clientWidth;
    const target = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--row")) || 210;
    const gap = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--gap")) || 4;
    const frag = document.createDocumentFragment();
    let row = [], sum = 0;
    const flush = (full) => {
      const avail = width - gap * (row.length - 1);
      const scale = full ? avail / sum : Math.min(1, avail / sum);
      const h = Math.round(target * scale);
      const div = document.createElement("div");
      div.className = "row";
      for (const r of row) {
        const t = this.tile(r.it);
        t.style.width = (r.w * scale).toFixed(2) + "px";
        t.style.height = h + "px";
        div.append(t);
      }
      frag.append(div);
      row = []; sum = 0;
    };
    for (const it of sec.items) {
      const w = (it.w / it.h) * target;
      row.push({ it, w }); sum += w;
      if (sum + gap * (row.length - 1) >= width) flush(true);
    }
    if (row.length) flush(false);
    sec.rows.replaceChildren(frag);
  }

  relayout() { this.sections.forEach((s) => this.layout(s)); }

  destroy() {
    this.io.disconnect();
    window.removeEventListener("resize", this.onResize);
  }
}

/* ------------------------------------------------------------------ vue Photos */

async function renderPhotos(params) {
  view.innerHTML = "";
  if (params.before) {
    const bar = document.createElement("div");
    bar.className = "jumpbar";
    bar.innerHTML = `<span>Photos jusqu'au ${fmtShortDate(params.before)}</span><button class="btn">Revenir aux plus récentes</button>`;
    $("button", bar).onclick = () => setFilters({ before: undefined });
    view.append(bar);
  }
  const g = new Gallery(view, params);
  current = g;
  const n = await g.start();
  if (!n) {
    const filtered = FILTER_KEYS.some((k) => params[k]);
    const st = Status.last || {};
    view.innerHTML = filtered
      ? `<div class="empty"><h2>Aucune photo ne correspond</h2><p>Retirez un filtre ou élargissez la période.</p><button class="btn" id="emptyClear">Effacer les filtres</button></div>`
      : `<div class="empty"><h2>Aucune photo pour l'instant</h2><p>${st.state && st.state !== "idle"
          ? "L'analyse de votre dossier est en cours. Les photos apparaîtront au fur et à mesure."
          : "Ajoutez un dossier de photos pour commencer."}</p><a class="btn primary" href="#/folders">Ajouter un dossier</a> <button class="btn" id="emptyScan">Lancer une analyse</button></div>`;
    $("#emptyClear")?.addEventListener("click", clearFilters);
    $("#emptyScan")?.addEventListener("click", () => Status.scan());
  }
}

/* ------------------------------------------------------------------ vue Personnes */

async function renderPeople(params) {
  const showHidden = params.hidden === "1";
  const [named, st] = await Promise.all([People.all(true), api("/api/status")]);
  view.innerHTML = "";
  const head = document.createElement("div");
  head.className = "page-head";
  head.innerHTML = `<h1>${showHidden ? "Groupes ignorés" : "Personnes"}</h1><p>${showHidden
    ? "Ces groupes n'apparaissent plus dans « Qui est-ce ? ». Ouvrez-en un pour ne plus l'ignorer."
    : plural(named.length, "personne nommée", "personnes nommées")}</p>`;
  view.append(head);

  if (!st.faces_enabled) {
    view.insertAdjacentHTML("beforeend", `<div class="empty"><h2>Reconnaissance des visages désactivée</h2><p>Activez-la avec <code>ENABLE_FACES=1</code> dans le fichier docker-compose, puis relancez le conteneur.</p></div>`);
    return;
  }

  if (!showHidden && named.length) {
    const grid = document.createElement("div");
    grid.className = "people";
    grid.innerHTML = named.map((p) => `<a class="person" href="#/person/${p.id}">
      <img class="avatar" loading="lazy" src="${p.cover ? `/face/${p.cover}` : ""}" alt="">
      <b>${esc(p.name)}</b><span>${plural(p.photos, "photo", "photos")}</span></a>`).join("");
    view.append(grid);
  }

  const title = document.createElement("div");
  title.innerHTML = showHidden ? "" : `<h2 class="section-title">Qui est-ce ?</h2>
    <p class="section-sub">Visages qui se ressemblent, regroupés automatiquement. Donnez-leur un nom pour pouvoir filtrer vos photos. Un nom déjà utilisé regroupe les deux.</p>`;
  view.append(title);
  const list = document.createElement("div");
  list.className = "unknown";
  view.append(list);
  const more = document.createElement("button");
  more.className = "btn more";
  more.textContent = "Afficher plus de visages";
  view.append(more);

  let offset = 0;
  const names = named.map((p) => `<option value="${esc(p.name)}">`).join("");
  view.insertAdjacentHTML("beforeend", `<datalist id="nameList">${names}</datalist>`);

  const loadMore = async () => {
    const data = await api(`/api/persons?named=0&hidden=${showHidden ? 1 : 0}&limit=60&offset=${offset}`);
    offset += data.items.length;
    for (const p of data.items) list.append(whoCard(p, showHidden));
    more.hidden = offset >= data.total;
    if (!offset) list.innerHTML = `<p class="section-sub">${showHidden ? "Aucun groupe ignoré." : st.state !== "idle" ? "L'analyse est en cours : de nouveaux visages arriveront." : "Tous les visages ont un nom."}</p>`;
  };
  more.onclick = loadMore;
  await loadMore();

  view.insertAdjacentHTML("beforeend", `<p style="margin-top:40px"><a class="back" href="${showHidden ? "#/people" : "#/people?hidden=1"}">${showHidden ? "← Retour aux personnes" : "Voir les groupes ignorés"}</a></p>`);
}

function whoCard(p, hiddenList) {
  const card = document.createElement("div");
  card.className = "who";
  card.innerHTML = `<img class="avatar" loading="lazy" src="/face/${p.cover}" alt="" title="Voir les photos">
    <span class="n">${plural(p.photos, "photo", "photos")}</span>
    <form><input name="n" placeholder="Son nom" list="nameList" autocomplete="off" aria-label="Nom"><button class="btn" type="submit">OK</button></form>
    <button class="ign" type="button">${hiddenList ? "Ne plus ignorer" : "Ignorer"}</button>`;
  $("img", card).onclick = () => (location.hash = `#/person/${p.id}`);
  $("form", card).onsubmit = async (e) => {
    e.preventDefault();
    const name = e.target.n.value.trim();
    if (!name) return;
    const res = await api(`/api/persons/${p.id}`, { method: "PATCH", json: { name } });
    People.invalidate();
    toast(res.id !== p.id ? `Regroupé avec ${res.name}` : `Nommé : ${res.name}`);
    card.remove();
  };
  $(".ign", card).onclick = async () => {
    await api(`/api/persons/${p.id}`, { method: "PATCH", json: { hidden: !hiddenList } });
    toast(hiddenList ? "Ce groupe réapparaîtra dans « Qui est-ce ? »" : "Groupe ignoré");
    card.remove();
  };
  return card;
}

/* ------------------------------------------------------------------ vue Personne */

async function renderPerson(id, params) {
  const p = await api(`/api/persons/${id}`);
  await People.all();
  view.innerHTML = `<a class="back" href="#/people">← Personnes</a>
    <div class="person-head">
      <img class="avatar" src="${p.cover ? `/face/${p.cover}` : ""}" alt="">
      <div><h1 id="pname" title="Cliquer pour renommer" style="cursor:text">${p.name ? esc(p.name) : "Sans nom"}</h1>
      <p>${plural(p.photos, "photo", "photos")}${p.hidden ? " · groupe ignoré" : ""}</p></div>
      <div class="actions">
        <button class="btn" id="pRename">${p.name ? "Renommer" : "Ajouter un nom"}</button>
        <button class="btn" id="pFaces">Vérifier les visages</button>
        <button class="btn" id="pMerge">Fusionner avec…</button>
        <button class="btn ghost" id="pHide">${p.hidden ? "Ne plus ignorer" : "Ignorer"}</button>
      </div>
    </div>`;
  const rename = () => {
    const h1 = $("#pname");
    h1.innerHTML = `<input value="${esc(p.name || "")}" placeholder="Nom" list="nameList2" aria-label="Nom"><datalist id="nameList2">${People.list.map((x) => `<option value="${esc(x.name)}">`).join("")}</datalist>`;
    const inp = $("input", h1);
    inp.focus(); inp.select();
    const save = async () => {
      const name = inp.value.trim();
      if (name === (p.name || "")) { render(); return; }
      const res = await api(`/api/persons/${id}`, { method: "PATCH", json: { name } });
      People.invalidate();
      toast(res.id !== id ? `Regroupé avec ${res.name}` : name ? `Renommé : ${name}` : "Nom retiré");
      location.hash = `#/person/${res.id}`;
      if (res.id === id) render();
    };
    inp.onkeydown = (e) => { if (e.key === "Enter") save(); if (e.key === "Escape") render(); };
    inp.onblur = save;
  };
  $("#pname").onclick = rename;
  $("#pRename").onclick = rename;
  $("#pHide").onclick = async () => {
    await api(`/api/persons/${id}`, { method: "PATCH", json: { hidden: !p.hidden } });
    toast(p.hidden ? "Ce groupe n'est plus ignoré" : "Groupe ignoré");
    People.invalidate();
    render();
  };
  $("#pFaces").onclick = () => reviewFaces(p);
  $("#pMerge").onclick = () => mergeDialog(p);

  const holder = document.createElement("div");
  view.append(holder);
  const g = new Gallery(holder, { ...params, person: [String(id), ...(params.person || "").split(",").filter((x) => x && x !== String(id))].join(",") });
  current = g;
  await g.start();
}

function openModal(html) {
  const m = $("#modal");
  m.innerHTML = `<div class="box">${html}</div>`;
  m.hidden = false;
  const close = () => { m.hidden = true; m.innerHTML = ""; document.removeEventListener("keydown", onKey); };
  const onKey = (e) => e.key === "Escape" && close();
  document.addEventListener("keydown", onKey);
  m.onclick = (e) => e.target === m && close();
  return { box: $(".box", m), close };
}

async function reviewFaces(p) {
  const { box, close } = openModal(`<h2>Visages de ${esc(p.name || "ce groupe")}</h2>
    <p>Retirez les visages qui ne sont pas ${p.name ? esc(p.name) : "cette personne"}. Ils ne lui seront plus attribués.</p>
    <div class="facegrid"></div><div class="modal-actions"><button class="btn" id="mMore" hidden>Afficher plus</button><button class="btn primary" id="mDone">Terminé</button></div>`);
  const grid = $(".facegrid", box);
  let offset = 0, changed = false;
  const load = async () => {
    const list = await api(`/api/persons/${p.id}/faces?limit=120&offset=${offset}`);
    offset += list.length;
    for (const f of list) {
      const fig = document.createElement("figure");
      fig.innerHTML = `<img src="/face/${f.id}" alt="" loading="lazy" title="Ouvrir la photo"><button title="Pas ${esc(p.name || "cette personne")}" aria-label="Retirer">×</button>`;
      $("button", fig).onclick = async () => {
        await api(`/api/faces/${f.id}`, { method: "POST", json: { remove: true } });
        fig.classList.add("gone");
        changed = true;
      };
      $("img", fig).onclick = () => Lightbox.openSingle(f.photo);
      grid.append(fig);
    }
    $("#mMore", box).hidden = list.length < 120;
  };
  $("#mMore", box).onclick = load;
  $("#mDone", box).onclick = () => { close(); if (changed) render(); };
  await load();
}

async function mergeDialog(p) {
  const people = (await People.all()).filter((x) => x.id !== p.id);
  const { box, close } = openModal(`<h2>Fusionner ${esc(p.name || "ce groupe")} avec…</h2>
    <p>Tous ses visages seront attribués à la personne choisie.</p>
    <input type="search" placeholder="Chercher un nom" class="msearch" style="width:100%;padding:8px 11px;border-radius:9px;border:1px solid var(--line);background:var(--table)">
    <div class="merge-list"></div>`);
  const listEl = $(".merge-list", box);
  const draw = (q = "") => {
    listEl.innerHTML = "";
    people.filter((x) => x.name.toLowerCase().includes(q.toLowerCase())).slice(0, 60).forEach((x) => {
      const b = document.createElement("button");
      b.className = "pick";
      b.innerHTML = `<img src="/face/${x.cover}" alt="">${esc(x.name)}`;
      b.onclick = async () => {
        const res = await api(`/api/persons/${p.id}/merge`, { method: "POST", json: { into: x.id } });
        People.invalidate(); close();
        toast(`Fusionné avec ${res.name}`);
        location.hash = `#/person/${res.id}`;
      };
      listEl.append(b);
    });
    if (!listEl.children.length) listEl.innerHTML = `<p>Aucune personne nommée ne correspond.</p>`;
  };
  $(".msearch", box).oninput = (e) => draw(e.target.value);
  draw();
  $(".msearch", box).focus();
}

/* ------------------------------------------------------------------ vue Dossiers */

async function renderFolders(params) {
  view.innerHTML = `<div class="page-head"><h1>Dossiers</h1>
    <p>Les dossiers du Pi que la photothèque analyse. Vos fichiers ne sont jamais modifiés.</p></div>
    <div class="sources"></div>
    <section class="browser" aria-label="Ajouter un dossier">
      <h2 class="section-title">Ajouter un dossier</h2>
      <p class="section-sub">Parcourez les disques du Pi (dossiers /mnt et /media) et choisissez le dossier qui contient vos photos. Ses sous-dossiers sont inclus.</p>
      <div class="bx"></div>
    </section>
    <details class="journal"><summary>Journal des erreurs</summary><pre></pre></details>`;
  $(".journal", view).addEventListener("toggle", async (e) => {
    if (!e.target.open) return;
    const lines = await api("/api/logs").catch(() => []);
    $("pre", e.target).textContent = lines.length ? lines.slice(-60).join("\n") : "Aucune erreur récente.";
  });
  await Promise.all([drawSources(), browseTo($(".bx", view), params.path || "/")]);
}

async function drawSources() {
  const list = $(".sources", view);
  if (!list) return;
  const [sources, st] = await Promise.all([api("/api/sources"), api("/api/status")]);
  list.replaceChildren();
  if (st.last_error) list.insertAdjacentHTML("beforeend", `<p class="tag warn" style="margin:0 0 4px">La dernière analyse s'est arrêtée sur une erreur : ${esc(st.last_error)}. Ouvrez le journal en bas de page pour le détail.</p>`);
  if (!sources.length) list.innerHTML = `<p class="section-sub">Aucun dossier pour l'instant. Ajoutez-en un ci-dessous.</p>`;
  for (const s of sources) {
    const row = document.createElement("div");
    row.className = "source";
    let state = "";
    if (s.state === "removing") state = `<span class="tag">Retrait en cours…</span>`;
    else if (s.error) state = `<span class="tag warn">Lecture impossible : ${esc(s.error)}</span>`;
    else if (!s.available) state = `<span class="tag warn">Introuvable : disque débranché ou partage réseau absent. Ses photos sont conservées.</span>`;
    else if (s.found > s.photos) state = `<span class="tag">Analyse en cours : ${s.photos.toLocaleString("fr-FR")} / ${s.found.toLocaleString("fr-FR")}</span>`;
    else if (!s.last_scan) state = `<span class="tag">En attente d'analyse</span>`;
    row.innerHTML = `<div class="src-main"><b>${esc(s.path)}</b>
        <span>${plural(s.photos, "photo", "photos")}</span>${state}</div>
      <div class="src-actions">
        <a class="btn ghost" href="${hashFor("photos", { folder: s.path })}">Voir</a>
        ${s.state === "removing" ? "" : `<button class="btn ghost danger" data-remove>Retirer</button>`}
      </div>`;
    $("[data-remove]", row)?.addEventListener("click", () => confirmRemove(s));
    list.append(row);
  }
  clearTimeout(drawSources._t);
  const busy = sources.some((s) => s.state === "removing" || !s.last_scan || (s.available && s.found > s.photos));
  if (busy) drawSources._t = setTimeout(() => { if (parseHash().view === "folders") drawSources().catch(() => {}); }, 5000);
}

async function browseTo(box, path) {
  box.dataset.path = path;
  box.innerHTML = `<p class="loading">Lecture du dossier…</p>`;
  let d;
  try { d = await api("/api/browse?path=" + encodeURIComponent(path)); }
  catch (e) { box.innerHTML = `<p class="section-sub">${esc(e.message)}</p><button class="btn" data-go="/">Revenir au début</button>`; $("[data-go]", box).onclick = () => browseTo(box, "/"); return; }
  const crumbs = [`<button class="crumb" data-go="/">Disques du Pi</button>`];
  const parts = d.path.split("/").filter(Boolean), shown = (d.label || d.path).split("/").filter(Boolean);
  let acc = "";
  parts.forEach((part, i) => {
    acc += "/" + part;
    crumbs.push(`<span aria-hidden="true">/</span><button class="crumb" data-go="${esc(acc)}">${esc(shown[i] || part)}</button>`);
  });
  let action = "";
  if (d.path !== "/") {
    if (d.in_library) action = `<p class="bx-note">Ce dossier fait déjà partie de la photothèque${d.in_library !== d.label ? ` (dans ${esc(d.in_library)})` : ""}.</p>`;
    else if (d.contains_library.length) action = `<p class="bx-note">Ce dossier contient ${esc(d.contains_library.join(", "))}, déjà dans la photothèque. Choisissez un autre dossier, ou retirez d'abord celui-ci.</p>`;
    else action = `<button class="btn primary" data-add>Ajouter ce dossier</button><span class="bx-count">${d.images ? plural(d.images, "image", "images") + " ici, plus les sous-dossiers" : "Images des sous-dossiers incluses"}</span>`;
  }
  box.innerHTML = `<nav class="crumbs" aria-label="Emplacement">${crumbs.join("")}</nav>
    <div class="bx-bar">${action}</div>
    <ul class="dirs">${d.dirs.map((x) => `<li><button data-go="${esc(x.path)}" class="${x.in_library ? "in" : ""}">
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M2.5 5.5a1.5 1.5 0 0 1 1.5-1.5h3.6l1.8 2h6.6a1.5 1.5 0 0 1 1.5 1.5v7a1.5 1.5 0 0 1-1.5 1.5H4a1.5 1.5 0 0 1-1.5-1.5z"/></svg>
        <span class="dn">${esc(x.name)}</span>
        <span class="dc">${x.in_library ? "dans la photothèque" : x.images ? plural(x.images, "image", "images") : ""}</span></button></li>`).join("")}</ul>
    ${d.dirs.length ? "" : `<p class="section-sub">${d.path === "/" ? "Aucun disque visible. Vérifiez les montages /mnt et /media dans docker-compose.yml." : "Pas de sous-dossier."}</p>`}`;
  $$("[data-go]", box).forEach((b) => (b.onclick = () => browseTo(box, b.dataset.go)));
  $("[data-add]", box)?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/sources", { method: "POST", json: { path: d.path } });
      toast(`${r.path} ajouté. L'analyse commence.`);
      Filters.loaded = false;
      setTimeout(() => Status.poll(), 1500);
      await Promise.all([drawSources(), browseTo(box, d.path)]);
    } catch (err) { e.target.disabled = false; toast(err.message, 5000); }
  });
}

function confirmRemove(s) {
  const { box, close } = openModal(`<h2>Retirer ${esc(s.path)} ?</h2>
    <p>Ses ${plural(s.photos, "photo", "photos")} disparaîtront de la photothèque, ainsi que les visages reconnus dessus. Les fichiers restent sur le disque, intacts. Vous pourrez rajouter ce dossier plus tard, mais il faudra le réanalyser.</p>
    <div class="modal-actions"><button class="btn" data-no>Annuler</button><button class="btn primary danger-fill" data-yes>Retirer le dossier</button></div>`);
  $("[data-no]", box).onclick = close;
  $("[data-yes]", box).onclick = async () => {
    await api(`/api/sources/${s.id}`, { method: "DELETE" });
    close();
    toast(`${s.path} retiré de la photothèque`);
    People.invalidate();
    Filters.loaded = false;
    drawSources();
    const bx = $(".browser .bx");
    if (bx) browseTo(bx, bx.dataset.path || "/");
  };
  $("[data-no]", box).focus();
}

/* ------------------------------------------------------------------ visionneuse */

const Lightbox = {
  el: $("#lightbox"), img: $("#lbImg"), info: $("#lbInfo"), boxes: $("#lbFaces"),
  src: null, idx: 0, detail: null, showInfo: false, showFaces: false, opener: null, cache: new Map(),

  open(source, idx) {
    this.src = source;
    this.opener = document.activeElement;
    if (this.el.hidden) {
      this.el.hidden = false;
      document.body.style.overflow = "hidden";
      history.pushState({ lb: 1 }, "");
    }
    this.show(idx);
  },

  async openSingle(photoId) {
    const d = await api(`/api/photos/${photoId}`);
    const single = { items: [{ id: d.id, t: d.taken }], hasMore: false, load: async () => {} };
    $("#modal").hidden = true;
    this.open(single, 0);
  },

  close(fromPop = false) {
    if (this.el.hidden) return;
    this.el.hidden = true;
    document.body.style.overflow = "";
    this.img.removeAttribute("src");
    if (!fromPop && history.state && history.state.lb) history.back();
    const it = this.src?.items[this.idx];
    const tile = it && this.src.tiles?.get(it.id);
    (tile || this.opener)?.focus?.({ preventScroll: false });
    tile?.scrollIntoView?.({ block: "nearest" });
  },

  async show(idx) {
    const items = this.src.items;
    if (idx < 0 || idx >= items.length) return;
    this.idx = idx;
    const it = items[idx];
    this.boxes.innerHTML = "";
    $("#lbDate").textContent = fmtLong(it.t);
    $("#lbPlace").textContent = "";
    $(".lb-nav.prev", this.el).hidden = idx === 0;
    $(".lb-nav.next", this.el).hidden = idx >= items.length - 1 && !this.src.hasMore;
    $("#lbDownload").href = `/original/${it.id}?download=1`;
    // miniature immédiatement, puis l'aperçu grand format
    this.img.classList.add("loading");
    this.img.src = `/thumb/${it.id}`;
    const big = new Image();
    big.onload = () => { if (this.items_id() === it.id) { this.img.src = big.src; this.img.classList.remove("loading"); this.drawFaces(); } };
    big.onerror = () => { if (this.items_id() === it.id) { this.img.classList.remove("loading"); toast("Aperçu indisponible pour cette photo."); } };
    big.src = `/preview/${it.id}`;
    [idx + 1, idx - 1].forEach((j) => { if (items[j]) new Image().src = `/preview/${items[j].id}`; });
    if (idx >= items.length - 8 && this.src.hasMore) this.src.load().then(() => { $(".lb-nav.next", this.el).hidden = this.idx >= this.src.items.length - 1 && !this.src.hasMore; });
    const d = this.cache.get(it.id) || await api(`/api/photos/${it.id}`).catch(() => null);
    if (!d || this.items_id() !== it.id) return;
    this.cache.set(it.id, d);
    this.detail = d;
    const camera = [d.make, d.model].filter(Boolean).join(" ");
    $("#lbPlace").textContent = [d.folder || "Dossier racine", camera].filter(Boolean).join("  ·  ");
    if (this.showInfo) this.renderInfo();
    this.drawFaces();
  },

  items_id() { return this.src?.items[this.idx]?.id; },

  navigate(hash) {
    // ferme la visionneuse et remplace son entrée d'historique par la destination
    this.el.hidden = true;
    document.body.style.overflow = "";
    this.img.removeAttribute("src");
    if (history.state && history.state.lb) { history.replaceState(null, "", hash); render(); }
    else location.hash = hash;
  },

  toggleInfo() {
    this.showInfo = !this.showInfo;
    this.info.hidden = !this.showInfo;
    this.el.classList.toggle("info", this.showInfo);
    $('[data-act="info"]', this.el).classList.toggle("on", this.showInfo);
    if (this.showInfo) this.renderInfo();
    requestAnimationFrame(() => this.drawFaces());
  },

  toggleFaces() {
    this.showFaces = !this.showFaces;
    $('[data-act="faces"]', this.el).classList.toggle("on", this.showFaces);
    this.drawFaces();
  },

  drawFaces() {
    const d = this.detail;
    this.boxes.innerHTML = "";
    if (!this.showFaces || !d || d.id !== this.items_id() || this.img.classList.contains("loading")) return;
    // zone réellement occupée par l'image (object-fit: contain peut laisser des marges)
    const ir = this.img.getBoundingClientRect(), sr = $("#lbStage").getBoundingClientRect();
    const nw = this.img.naturalWidth || ir.width, nh = this.img.naturalHeight || ir.height;
    const k = Math.min(ir.width / nw, ir.height / nh), cw = nw * k, ch = nh * k;
    Object.assign(this.boxes.style, { left: ir.left - sr.left + (ir.width - cw) / 2 + "px", top: ir.top - sr.top + (ir.height - ch) / 2 + "px", width: cw + "px", height: ch + "px" });
    for (const f of d.faces) {
      const b = document.createElement("div");
      const known = f.name && !f.hidden;
      b.className = "fbox" + (known ? "" : " unknown");
      Object.assign(b.style, { left: f.x * 100 + "%", top: f.y * 100 + "%", width: f.w * 100 + "%", height: f.h * 100 + "%" });
      b.innerHTML = `<span>${known ? esc(f.name) : "Qui est-ce ?"}</span>`;
      b.onclick = () => { if (!this.showInfo) this.toggleInfo(); setTimeout(() => $(`[data-face="${f.id}"] input`, this.info)?.focus(), 50); };
      this.boxes.append(b);
    }
  },

  async renderInfo() {
    const d = this.detail;
    if (!d || d.id !== this.items_id()) { this.info.innerHTML = ""; return; }
    const camera = [d.make, d.model].filter(Boolean).join(" ");
    const mp = d.width && d.height ? ` (${((d.width * d.height) / 1e6).toFixed(1).replace(".", ",")} Mpx)` : "";
    this.info.innerHTML = `
      <h4>Prise de vue</h4>
      <dl>
        <dt>Date</dt><dd>${esc(fmtLong(d.taken))}</dd>
        ${camera ? `<dt>Appareil</dt><dd><button class="linkish" data-camera="${esc(d.model || "")}">${esc(camera)}</button></dd>` : ""}
        ${d.lat != null ? `<dt>Lieu</dt><dd><a href="https://www.openstreetmap.org/?mlat=${d.lat}&mlon=${d.lon}#map=15/${d.lat}/${d.lon}" target="_blank" rel="noopener">Voir sur la carte</a></dd>` : ""}
      </dl>
      <h4>Fichier</h4>
      <dl>
        <dt>Nom</dt><dd>${esc(d.filename)}</dd>
        <dt>Dossier</dt><dd><button class="linkish" data-folder="${esc(d.folder)}">${esc(d.folder || "Dossier racine")}</button></dd>
        <dt>Taille</dt><dd>${d.width}×${d.height}${mp}, ${fmtSize(d.size)}</dd>
      </dl>
      <h4>Personnes</h4>
      <div class="lb-people">${d.faces.length ? "" : `<p style="color:#9AA49F;font-size:14px;margin:0">Aucun visage détecté.</p>`}</div>`;
    const people = await People.all().catch(() => []);
    if (this.detail !== d) return;
    const dl = `<datalist id="lbNames">${people.map((p) => `<option value="${esc(p.name)}">`).join("")}</datalist>`;
    this.info.insertAdjacentHTML("beforeend", dl);
    const wrap = $(".lb-people", this.info);
    for (const f of d.faces) {
      const row = document.createElement("div");
      row.className = "lb-person";
      row.dataset.face = f.id;
      const known = f.name && !f.hidden;
      row.innerHTML = `<img src="/face/${f.id}" alt="">
        <div class="who-name">${known
          ? `<button class="linkish" data-person="${f.person_id}">${esc(f.name)}</button>`
          : `<form><input placeholder="Qui est-ce ?" list="lbNames" autocomplete="off" aria-label="Nom de la personne"></form><div class="sugg"></div>`}</div>
        ${known ? `<button class="x" data-edit title="Changer le nom">✎</button>` : ""}
        <button class="x" data-remove title="Ce n'est pas un visage, ou ne pas l'attribuer">×</button>`;
      const form = $("form", row);
      const assign = async (payload) => {
        await api(`/api/faces/${f.id}`, { method: "POST", json: payload });
        People.invalidate();
        this.cache.delete(d.id);
        const fresh = await api(`/api/photos/${d.id}`);
        this.cache.set(d.id, fresh);
        if (this.items_id() === d.id) { this.detail = fresh; this.renderInfo(); this.drawFaces(); }
      };
      if (form) {
        form.onsubmit = (e) => { e.preventDefault(); const v = $("input", form).value.trim(); if (v) assign({ name: v }).then(() => toast(`Visage attribué à ${v}`)); };
        api(`/api/faces/${f.id}/suggest`).then((s) => {
          const box = $(".sugg", row);
          s.forEach((p) => {
            const b = document.createElement("button");
            b.type = "button"; b.textContent = p.name;
            b.onclick = () => assign({ person_id: p.id }).then(() => toast(`Visage attribué à ${p.name}`));
            box.append(b);
          });
        }).catch(() => {});
      }
      $("[data-person]", row)?.addEventListener("click", () => this.navigate(`#/person/${f.person_id}`));
      $("[data-edit]", row)?.addEventListener("click", () => {
        $(".who-name", row).innerHTML = `<form><input value="${esc(f.name)}" list="lbNames" autocomplete="off"></form>`;
        const fm = $("form", row), inp = $("input", fm);
        inp.focus(); inp.select();
        fm.onsubmit = (e) => { e.preventDefault(); const v = inp.value.trim(); if (v && v !== f.name) assign({ name: v }).then(() => toast(`Visage attribué à ${v}`)); };
      });
      $("[data-remove]", row).onclick = () => assign({ remove: true }).then(() => toast("Visage retiré"));
      wrap.append(row);
    }
    $$("[data-folder]", this.info).forEach((b) => (b.onclick = () => this.navigate(hashFor("photos", { folder: b.dataset.folder || "" }))));
    $$("[data-camera]", this.info).forEach((b) => (b.onclick = () => this.navigate(hashFor("photos", { camera: b.dataset.camera }))));
  },
};

Lightbox.el.addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "close") Lightbox.close();
  else if (act === "prev") Lightbox.show(Lightbox.idx - 1);
  else if (act === "next") Lightbox.show(Lightbox.idx + 1);
  else if (act === "info") Lightbox.toggleInfo();
  else if (act === "faces") Lightbox.toggleFaces();
  else if (e.target.id === "lbStage") Lightbox.close();
});
document.addEventListener("keydown", (e) => {
  if (Lightbox.el.hidden || e.target.matches("input, select, textarea")) return;
  if (e.key === "ArrowRight") Lightbox.show(Lightbox.idx + 1);
  else if (e.key === "ArrowLeft") Lightbox.show(Lightbox.idx - 1);
  else if (e.key === "Escape") Lightbox.close();
  else if (e.key === "i") Lightbox.toggleInfo();
  else if (e.key === "v") Lightbox.toggleFaces();
});
window.addEventListener("popstate", () => { if (!Lightbox.el.hidden) Lightbox.close(true); });
window.addEventListener("resize", () => Lightbox.drawFaces());
(() => {
  let x0 = null, y0 = 0;
  const st = $("#lbStage");
  st.addEventListener("touchstart", (e) => { if (e.touches.length === 1) { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; } }, { passive: true });
  st.addEventListener("touchend", (e) => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
    x0 = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) Lightbox.show(Lightbox.idx + (dx < 0 ? 1 : -1));
    else if (dy > 90 && Math.abs(dy) > Math.abs(dx)) Lightbox.close();
  });
})();

/* ------------------------------------------------------------------ filtres */

const Filters = {
  panel: $("#filterPanel"), loaded: false, folders: [],
  async toggle(force) {
    const open = force ?? this.panel.hidden;
    this.panel.hidden = !open;
    $("#filterBtn").setAttribute("aria-expanded", open);
    if (open) await this.fill();
  },
  async fill() {
    const r = parseHash().params;
    const [people, folders, cams, tl] = await Promise.all([
      People.all().catch(() => []),
      this.loaded ? this.folders : api("/api/folders"),
      this.loaded ? null : api("/api/cameras"),
      api("/api/timeline"),
    ]);
    if (!this.loaded) {
      this.folders = folders;
      $("#folderList").innerHTML = folders.map((f) => `<option value="${esc(f.folder)}">${f.n}</option>`).join("");
      $("#cameraSelect").innerHTML = `<option value="">Tous les appareils</option>` + cams.map((c) => `<option value="${esc(c.model)}">${esc(c.model)} (${c.n})</option>`).join("");
      this.loaded = true;
    }
    const sel = new Set((r.person || "").split(",").filter(Boolean).map(Number));
    const draw = (q = "") => {
      $("#personPicks").innerHTML = people.filter((p) => !q || p.name.toLowerCase().includes(q.toLowerCase())).slice(0, 80)
        .map((p) => `<button class="pick${sel.has(p.id) ? " on" : ""}" data-id="${p.id}" aria-pressed="${sel.has(p.id)}"><img src="/face/${p.cover}" alt="">${esc(p.name)}</button>`).join("")
        || `<p class="section-sub" style="margin:0">Nommez des visages dans l'onglet Personnes pour filtrer par personne.</p>`;
    };
    draw($("#personFilterSearch").value);
    $("#personFilterSearch").oninput = (e) => draw(e.target.value);
    const years = [...new Set(tl.map((t) => t.ym.slice(0, 4)))];
    $("#yearPicks").innerHTML = years.map((y) => `<button data-year="${y}">${y}</button>`).join("");
    $("#fFrom").value = r.from || "";
    $("#fTo").value = r.to || "";
    $("#folderSearch").value = r.folder || "";
    $("#cameraSelect").value = r.camera || "";
  },
};

$("#filterBtn").onclick = () => Filters.toggle();
$("#personPicks").addEventListener("click", (e) => {
  const b = e.target.closest(".pick");
  if (!b) return;
  const r = parseHash();
  const ids = new Set((r.params.person || "").split(",").filter(Boolean));
  ids.has(b.dataset.id) ? ids.delete(b.dataset.id) : ids.add(b.dataset.id);
  b.classList.toggle("on");
  if (r.view === "person") location.hash = hashFor("photos", { ...r.params, person: [...ids].join(",") });
  else setFilters({ person: [...ids].join(",") });
});
$("#yearPicks").addEventListener("click", (e) => {
  const y = e.target.dataset.year;
  if (y) setFilters({ from: `${y}-01-01`, to: `${y}-12-31` });
});
$("#fFrom").onchange = (e) => setFilters({ from: e.target.value });
$("#fTo").onchange = (e) => setFilters({ to: e.target.value });
$("#cameraSelect").onchange = (e) => setFilters({ camera: e.target.value });
$("#folderSearch").onchange = (e) => {
  const v = e.target.value.trim().replace(/^\/+|\/+$/g, "");
  if (!v || Filters.folders.some((f) => f.folder === v || f.folder.startsWith(v + "/"))) setFilters({ folder: v });
  else toast("Aucun dossier ne porte ce nom.");
};
function clearFilters() {
  location.hash = parseHash().view === "person" ? `#/person/${parseHash().id}` : "#/photos";
  Filters.panel.hidden || Filters.fill();
}
$("#clearFilters").onclick = clearFilters;
$("#searchForm").onsubmit = (e) => {
  e.preventDefault();
  const q = $("#searchInput").value.trim();
  const r = parseHash();
  if (r.view === "people" || r.view === "folders") location.hash = hashFor("photos", { q });
  else setFilters({ q });
};

function renderChips(r) {
  const box = $("#chips");
  box.innerHTML = "";
  let count = 0;
  if (r.view === "people" || r.view === "folders") { $("#filterCount").hidden = true; return; }
  const add = (label, remove, img) => {
    const c = document.createElement("span");
    c.className = "chip";
    c.innerHTML = `${img ? `<img src="${img}" alt="">` : ""}${esc(label)}<button aria-label="Retirer ce filtre">×</button>`;
    $("button", c).onclick = remove;
    box.append(c);
    count++;
  };
  const p = r.params;
  const ids = (p.person || "").split(",").filter(Boolean);
  const own = r.view === "person" ? String(r.id) : null;
  const drawPeople = () => ids.filter((id) => id !== own).forEach((id) => {
    const person = People.byId(id);
    add(person ? person.name : "Personne", () => setFilters({ person: ids.filter((x) => x !== id).join(",") }), person?.cover ? `/face/${person.cover}` : null);
  });
  if (ids.length) {
    if (People.list) drawPeople(); else People.all().then(() => renderChips(parseHash())).catch(() => {});
  }
  if (p.from || p.to) add(p.from && p.to ? `Du ${fmtShortDate(p.from)} au ${fmtShortDate(p.to)}` : p.from ? `Depuis le ${fmtShortDate(p.from)}` : `Jusqu'au ${fmtShortDate(p.to)}`, () => setFilters({ from: "", to: "" }));
  if (p.folder) add(`Dossier : ${p.folder}`, () => setFilters({ folder: "" }));
  if (p.camera) add(p.camera, () => setFilters({ camera: "" }));
  if (p.q) add(`« ${p.q} »`, () => setFilters({ q: "" }));
  $("#filterCount").textContent = count;
  $("#filterCount").hidden = !count;
}

/* ------------------------------------------------------------------ frise des années */

async function updateFilm(r) {
  const film = $("#film");
  if (r.view === "people" || r.view === "folders") { film.innerHTML = ""; return; }
  const p = { ...r.params };
  if (r.view === "person") p.person = [String(r.id), ...(p.person || "").split(",").filter((x) => x && x !== String(r.id))].join(",");
  delete p.before;
  const qs = new URLSearchParams(Object.entries(p).filter(([k, v]) => v && FILTER_KEYS.includes(k))).toString();
  const tl = await api("/api/timeline?" + qs).catch(() => []);
  const years = [...new Set(tl.map((t) => t.ym.slice(0, 4)))];
  film.innerHTML = years.map((y) => `<button data-year="${y}" title="Aller à ${y}">${y}</button>`).join("");
  markFilm();
}
$("#film").addEventListener("click", (e) => {
  const y = e.target.closest("[data-year]")?.dataset.year;
  if (!y) return;
  const sec = $(`.month[data-ym^="${y}-"]`);
  if (sec) sec.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  else setFilters({ before: `${y}-12-31` }, { keepBefore: true });
});
function markFilm() {
  const secs = $$(".month");
  const top = 90;
  const sec = secs.find((s) => s.getBoundingClientRect().bottom > top);
  const y = sec?.dataset.ym.slice(0, 4);
  $$("#film button").forEach((b) => b.classList.toggle("on", b.dataset.year === y));
}
let _scrollRaf = 0;
window.addEventListener("scroll", () => { cancelAnimationFrame(_scrollRaf); _scrollRaf = requestAnimationFrame(markFilm); }, { passive: true });

/* ------------------------------------------------------------------ état de l'analyse */

const Status = {
  last: null,
  async poll() {
    try {
      const s = await api("/api/status");
      this.last = s;
      const t = $("#statusText"), bar = $("#progress");
      if (s.state === "processing" && s.total) {
        const helpers = (s.workers || []).map((w) => `${w.name} ${w.per_min.toLocaleString("fr-FR")}/min`).join(", ");
        t.textContent = `Analyse : ${s.done.toLocaleString("fr-FR")} / ${s.total.toLocaleString("fr-FR")}` + (helpers ? ` · renfort ${helpers}` : "");
        bar.hidden = false;
        $("i", bar).style.width = (100 * s.done / s.total).toFixed(1) + "%";
      } else if (s.state === "indexing") {
        t.textContent = s.message || "Recherche des photos…";
        bar.hidden = true;
      } else {
        t.textContent = plural(s.photos, "photo", "photos");
        bar.hidden = true;
      }
      $("#statusBtn").title = s.last_scan
        ? `Dernière analyse : ${new Date(s.last_scan * 1000).toLocaleString("fr-FR")}. Cliquer pour relancer.`
        : "Lancer une analyse";
      clearTimeout(this._t);
      this._t = setTimeout(() => this.poll(), s.state === "idle" ? 30000 : 4000);
    } catch (e) {
      if (!(e instanceof AuthError)) { clearTimeout(this._t); this._t = setTimeout(() => this.poll(), 15000); }
    }
  },
  async scan() {
    await api("/api/scan", { method: "POST" });
    toast("Analyse lancée. Les nouvelles photos apparaîtront au fur et à mesure.");
    setTimeout(() => this.poll(), 1500);
  },
};
$("#statusBtn").onclick = () => Status.scan();

/* ------------------------------------------------------------------ connexion */

function showLogin() {
  if ($(".login")) return;
  $$(".top > :not(.brand), #chips, #film, #filterPanel").forEach((e) => (e.style.visibility = "hidden"));
  view.innerHTML = `<form class="login"><h1>Photothèque</h1><p style="margin:0;color:var(--muted)">Saisissez le mot de passe de la photothèque.</p>
    <input type="password" name="pw" autocomplete="current-password" aria-label="Mot de passe" autofocus>
    <button class="btn primary" type="submit">Se connecter</button><p class="err"></p></form>`;
  $(".login").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/login", { method: "POST", json: { password: e.target.pw.value } });
      $$(".top > *, #chips, #film, #filterPanel").forEach((el) => (el.style.visibility = ""));
      start();
    } catch (err) { $(".login .err").textContent = err.message; }
  };
}

/* ------------------------------------------------------------------ démarrage */

async function start() {
  const a = await fetch("/api/auth").then((r) => r.json()).catch(() => ({ ok: true }));
  if (a.required && !a.ok) return showLogin();
  if (!location.hash) history.replaceState(null, "", "#/photos");
  Status.poll();
  render();
}
window.addEventListener("hashchange", () => {
  if (!Lightbox.el.hidden) Lightbox.close(true);
  if (!$("#modal").hidden) { $("#modal").hidden = true; }
  render();
});
start();
