/* Cable Ring Studio: browse plans, watch the solver live, run batches. Talks to web_app.py. */
"use strict";
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const NS = "http://www.w3.org/2000/svg";
  const HUB = ["#d1495b", "#2e86ab", "#3c9d5d", "#edae49", "#8e6c8a", "#1b998b", "#c3423f", "#5c6bc0"];
  const hubColour = (i) => HUB[i % HUB.length];
  const STAGES = [
    ["plan", "Floor plan"], ["devices", "Devices"], ["grid", "Grid + door links"], ["prep", "Distance tables"],
    ["zones", "Zones + hubs"], ["rings", "Ring routing"], ["cleanup", "Clean-up"], ["check", "Check"],
  ];
  const TYPE_LABEL = { living: "Living", bedroom: "Bedroom", kitchen: "Kitchen", storage: "Storage", bathroom: "Bathroom", stair: "Stair" };

  const S = {
    rows: [], rowById: new Map(), listLimit: 300, current: null, data: null,
    geo: null, links: null, nodes: null, gridLines: null, out: null, outSource: null,
    live: { rings: [] }, attempt: null, zoneOf: {}, hubs: [], hubsCandidate: false,
    es: null, speed: "watch", runZone: "any", savedKey: null,
    view: null, base: null, scenes: {}, fs: 0.3, cpus: 8, outDir: "resplan_output",
  };

  // ------------------------------------------------------------------ helpers
  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (v, d = 2) => (v == null || Number.isNaN(+v) ? "—" : (+v).toFixed(d));
  const fmtInt = (v) => (v == null ? "—" : Number(v).toLocaleString());
  const planId = (id) => "PLAN_" + String(id).padStart(6, "0");
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;
  const outName = () => String(S.outDir).split(/[\\/]/).filter(Boolean).pop() || "resplan_output";
  const P = (x, y) => `${+x.toFixed(4)},${+(-y).toFixed(4)}`;
  const pathOf = (polys) => {
    let d = "";
    for (const poly of polys || []) for (const ring of poly) {
      ring.forEach(([x, y], i) => { d += (i ? "L" : "M") + P(x, y); });
      d += "Z";
    }
    return d;
  };
  const lineOf = (pts) => pts.map(([x, y], i) => (i ? "L" : "M") + P(x, y)).join("");
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok && !d.msg && !d.error) throw new Error(`${r.status} ${r.statusText}`);
    return d;
  }
  let toastTimer = null;
  function toast(msg, ms = 3600) {
    const t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, ms);
  }
  function badge(status) {
    if (!status) return `<span class="badge">not run</span>`;
    return `<span class="badge ${esc(status)}">${esc(status)}</span>`;
  }

  // ------------------------------------------------------------------ status + plan list
  async function boot() {
    buildStages();
    renderMetrics(null);
    for (const svg of [$("#svgIn"), $("#svgOut")]) wireCanvas(svg);
    let st;
    for (;;) {
      try { st = await getJSON("/api/status"); } catch (e) { setStatus("Cannot reach web_app.py: is it still running?", "bad"); await sleep(1500); continue; }
      if (st.ready) break;
      setStatus("Loading ResPlan.pkl (17,000 plans)…");
      await sleep(700);
    }
    if (st.error) { setStatus("Dataset error: " + st.error, "bad"); return; }
    S.cpus = st.cpus || 8; S.outDir = st.out || S.outDir;
    $("#bWorkers").value = Math.max(1, S.cpus - 2);
    $("#outDir").textContent = S.outDir;
    $("#zone").value = st.zone_default || "any";
    await loadRows();
    renderList();
    const m = location.hash.match(/plan=(\d+)/);
    const first = m ? +m[1] : (filteredRows()[0] || S.rows[0] || [null])[0];
    if (first != null) openPlan(first);
    if (st.batch_running) bFollow();
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  function setStatus(text, cls) {
    const p = $("#serverStatus");
    p.textContent = text; p.className = "status-pill" + (cls ? " " + cls : "");
  }
  async function loadRows() {
    const d = await getJSON("/api/plans");
    S.rows = d.plans || [];
    S.rowById = new Map(S.rows.map((r) => [r[0], r]));
    const solved = S.rows.filter((r) => r[2]).length;
    setStatus(`${fmtInt(S.rows.length)} plans · ${fmtInt(solved)} with stored results`, "ok");
  }
  function filteredRows() {
    const split = $("#fSplit").value, st = $("#fStatus").value, q = $("#fSearch").value.replace(/\D/g, "");
    return S.rows.filter((r) => (!split || r[1] === split)
      && (!st || (st === "none" ? !r[2] : st === "failed" ? ["failed", "error", "skipped"].includes(r[2]) : r[2] === st))
      && (!q || String(r[0]).includes(q)));
  }
  function renderList() {
    const rows = filteredRows();
    const ol = $("#planList");
    const shown = rows.slice(0, S.listLimit);
    $("#listCount").textContent = rows.length > shown.length ? `${fmtInt(shown.length)} of ${fmtInt(rows.length)} plans` : `${fmtInt(rows.length)} plans`;
    const html = shown.map((r) => {
      const [id, split, status, hubs, J] = r;
      const bits = [split || "—", hubs != null ? `${hubs} hub${hubs === 1 ? "" : "s"}` : null, J != null ? `J ${fmt(J)}` : null].filter(Boolean);
      return `<li><button type="button" data-id="${id}"${id === S.current ? ' aria-current="true"' : ""}>`
        + `<span class="pid">${planId(id)}</span>${badge(status)}<span class="sub">${bits.map(esc).join(" · ")}</span></button></li>`;
    });
    if (rows.length > shown.length) html.push(`<li class="more"><button type="button" class="ghost small" data-more="1">Show 300 more</button></li>`);
    ol.innerHTML = html.join("");
  }
  function markCurrentInList() {
    for (const b of $$("#planList button[data-id]")) {
      if (+b.dataset.id === S.current) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
    }
  }
  function updateRow(out) {
    const id = out.resplan_id;
    const r = S.rowById.get(id);
    if (!r) return;
    const rm = out.reported_metrics || {};
    r[2] = out.status; r[3] = rm.hub_count ?? null; r[4] = rm.cost_J ?? null;
    r[5] = out.benchmark ? !!out.benchmark.eligible : null; r[6] = rm.hub_lower_bound ?? null;
  }

  // ------------------------------------------------------------------ open a plan
  async function openPlan(id) {
    stopRun(true);
    S.current = id;
    history.replaceState(null, "", "#plan=" + id);
    markCurrentInList();
    $("#planTitle").textContent = planId(id);
    $("#planMeta").textContent = "loading…";
    let d;
    try { d = await getJSON(`/api/plan?id=${id}`); } catch (e) { $("#planMeta").textContent = "Could not load: " + e.message; return; }
    if (S.current !== id) return;
    S.data = d;
    clearLog();
    resetStages();
    $("#emptyOut").classList.remove("pill");
    if (!d.geometry) {
      $("#planMeta").textContent = d.error || "Plan not available";
      S.geo = null; $("#emptyIn").hidden = false; $("#emptyOut").hidden = false;
      $("#btnRun").disabled = true;
      return;
    }
    S.geo = d.geometry; S.links = d.links; S.gridLines = null; S.nodes = null; S.out = null;
    S.live = { rings: [] }; S.attempt = null; S.zoneOf = {}; S.hubs = [];
    const g = S.geo, elig = g.rooms.filter((r) => r.has_device).length;
    $("#planMeta").innerHTML = [
      d.split ? `<span class="badge">${esc(d.split)} split</span>` : "",
      `${fmt(g.area_m2, 1)} m²`, `${g.rooms.length} rooms, ${elig} get a device`,
      `scale ${fmt(g.scale_units_per_m, 1)} units/m`, `wall ${fmt(g.wall_depth_m * 100, 0)} cm`,
      d.png ? `<a href="${esc(d.png)}" target="_blank" rel="noopener">overlay PNG</a>` : "",
    ].filter(Boolean).join('<span class="muted">·</span>');
    buildScenes();
    $("#btnRun").disabled = false;
    $("#btnSave").disabled = true;
    if (d.stored) {
      showOutput(d.stored, "stored");
      log(`Stored result loaded from ${outName()}/json/${planId(id)}.json`, "dim");
    } else {
      $("#emptyOut").hidden = false;
      $("#emptyOut").classList.add("pill");
      $("#emptyOut").textContent = "Not solved yet: press Run solver";
      $("#outCaption").textContent = "";
      renderMetrics(null); renderHubTable(null); renderIssues(null); legendOut();
      $("#btnJson").disabled = true;
    }
  }

  // ------------------------------------------------------------------ SVG scenes
  function planBounds(g) {
    let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    const eat = (polys) => { for (const poly of polys || []) for (const ring of poly) for (const [x, y] of ring) { if (x < x1) x1 = x; if (x > x2) x2 = x; if (y < y1) y1 = y; if (y > y2) y2 = y; } };
    eat(g.walls); eat(g.inner); eat(g.windows); for (const r of g.rooms) eat(r.polys);
    if (!Number.isFinite(x1)) [x1, y1, x2, y2] = g.bounds;
    return [x1, y1, x2, y2];
  }
  function buildScenes() {
    const g = S.geo;
    const [x1, y1, x2, y2] = planBounds(g);
    const w = x2 - x1, h = y2 - y1, pad = Math.max(w, h) * 0.04 + 0.3;
    S.fs = Math.min(0.42, Math.max(0.2, Math.max(w, h) * 0.021));
    S.base = { x: x1 - pad, y: -y2 - pad, w: w + 2 * pad, h: h + 2 * pad };
    S.view = { ...S.base };
    S.scenes.in = buildScene($("#svgIn"), "in");
    S.scenes.out = buildScene($("#svgOut"), "out");
    $("#emptyIn").hidden = true;
    $("#emptyOut").hidden = true;
    applyView();
    drawLabels(); drawGrid(); drawLinks(); legendIn(); legendOut();
  }
  function buildScene(svg, mode) {
    const g = S.geo;
    svg.replaceChildren();
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    const defs = el("defs", null, svg);
    const cid = svg.id + "-clip";
    el("path", { d: pathOf(g.inner) }, el("clipPath", { id: cid }, defs));
    const hatch = el("pattern", { id: svg.id + "-hatch", width: 0.22, height: 0.22, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, defs);
    el("rect", { width: 0.22, height: 0.22, style: "fill: var(--noelig)" }, hatch);
    el("line", { x1: 0, y1: 0, x2: 0, y2: 0.22, style: "stroke: var(--hatch); stroke-width: 0.05" }, hatch);
    const L = {};
    for (const n of ["floor", "rooms", "zones", "grid", "struct", "links", "labels", "attempt", "rings", "hubs", "devices", "order"]) L[n] = el("g", { class: "ly-" + n }, svg);
    L.grid.setAttribute("clip-path", `url(#${cid})`);
    el("path", { d: pathOf(g.inner), class: "inner" }, L.floor);
    for (const r of g.rooms) {
      const a = { d: pathOf(r.polys), "data-room": r.room_id };
      if (mode === "in") a.class = `room t-${r.type}`;
      else if (r.has_device) a.class = "room elig";
      else { a.class = "room noelig"; a.style = `fill: url(#${svg.id}-hatch)`; }
      el("path", a, L.rooms);
    }
    el("path", { d: pathOf(g.walls), class: "wall", "fill-rule": "evenodd" }, L.struct);
    el("path", { d: pathOf(g.windows), class: "window", "fill-rule": "evenodd" }, L.struct);
    el("path", { d: pathOf(g.doors), class: "door" }, L.struct);
    if (g.front_door) {
      if (g.front_door.polys.length) el("path", { d: pathOf(g.front_door.polys), class: "front" }, L.struct);
      for (const ln of g.front_door.lines) el("path", { d: lineOf(ln), class: "front-line" }, L.struct);
    }
    return { svg, L, mode };
  }
  function drawLabels() {
    const on = $("#lyLabels").checked, fs = S.fs;
    for (const sc of Object.values(S.scenes)) {
      sc.L.labels.replaceChildren();
      if (!on || !S.geo) continue;
      for (const r of S.geo.rooms) {
        const [x, y] = r.label;
        const t = el("text", { x, y: -y, class: "rlabel", "font-size": fs * 0.78 }, sc.L.labels);
        const t1 = el("tspan", { x, dy: 0, class: "t" }, t);
        t1.textContent = `${r.room_id} ${TYPE_LABEL[r.type] || r.type}`;
        const t2 = el("tspan", { x, dy: fs * 0.95 }, t);
        t2.textContent = `${fmt(r.area_m2, 1)} m²` + (sc.mode === "out" && !r.has_device ? " · no device" : "");
      }
    }
  }
  function gridLines() {
    if (S.gridLines) return S.gridLines;
    // same rule as hub_routing.Grid: a line every 0.1 m, plus one through every device
    const [x1, y1, x2, y2] = S.geo.bounds, h = 0.1;
    const reg = (lo, hi) => { const a = []; for (let k = Math.floor(lo / h); k <= Math.ceil(hi / h); k++) a.push(+(k * h).toFixed(4)); return a; };
    return { xs: reg(x1, x2), ys: reg(y1, y2), devx: (S.nodes || []).map((n) => n.x), devy: (S.nodes || []).map((n) => n.y), approx: true };
  }
  function drawGrid() {
    const on = $("#lyGrid").checked;
    for (const sc of Object.values(S.scenes)) {
      sc.L.grid.replaceChildren();
      if (!on || !S.geo) continue;
      const g = gridLines(), [x1, y1, x2, y2] = S.geo.bounds;
      const dx = new Set((S.nodes || []).map((n) => +n.x.toFixed(4))), dy = new Set((S.nodes || []).map((n) => +n.y.toFixed(4)));
      let reg = "", dev = "";
      for (const x of g.xs) { const s = `M${P(x, y1)}L${P(x, y2)}`; if (dx.has(+(+x).toFixed(4))) dev += s; else reg += s; }
      for (const y of g.ys) { const s = `M${P(x1, y)}L${P(x2, y)}`; if (dy.has(+(+y).toFixed(4))) dev += s; else reg += s; }
      if (g.approx) {
        for (const x of g.devx || []) dev += `M${P(x, y1)}L${P(x, y2)}`;
        for (const y of g.devy || []) dev += `M${P(x1, y)}L${P(x2, y)}`;
      }
      el("path", { d: reg, class: "gridline" }, sc.L.grid);
      if (dev) el("path", { d: dev, class: "gridline dev" }, sc.L.grid);
    }
  }
  function drawLinks() {
    const sc = S.scenes.in;
    if (!sc) return;
    sc.L.links.replaceChildren();
    if (!$("#lyLinks").checked || !S.geo || !S.links) return;
    const at = Object.fromEntries(S.geo.rooms.map((r) => [r.room_id, r.label]));
    for (const lk of S.links) {
      const [a, b] = lk.rooms;
      if (!at[a] || !at[b]) continue;
      el("path", { d: lineOf([at[a], at[b]]), class: "link" + (lk.type === "opening" ? " opening" : "") }, sc.L.links);
    }
    for (const r of S.geo.rooms) el("circle", { cx: r.label[0], cy: -r.label[1] - S.fs * 0.9, r: S.fs * 0.22, class: "link-node" }, sc.L.links);
  }
  function nodePos() {
    return Object.fromEntries((S.nodes || []).map((n) => [n.node_id, [n.x, n.y]]));
  }
  function drawDevices() {
    const sc = S.scenes.out;
    if (!sc) return;
    sc.L.devices.replaceChildren();
    const fs = S.fs;
    for (const n of S.nodes || []) {
      const g = el("g", { class: "node", "data-node": n.node_id }, sc.L.devices);
      el("circle", { cx: n.x, cy: -n.y, r: fs * 0.62 }, g);
      const t = el("text", { x: n.x, y: -n.y, "font-size": fs * 0.56 }, g);
      t.textContent = n.node_id;
    }
  }
  function drawZones(groups) {
    const sc = S.scenes.out;
    if (!sc) return;
    sc.L.zones.replaceChildren();
    S.zoneOf = {};
    const polys = Object.fromEntries(S.geo.rooms.map((r) => [r.room_id, r.polys]));
    (groups || []).forEach((grp, gi) => {
      for (const rid of grp) {
        S.zoneOf[rid] = gi;
        if (polys[rid]) el("path", { d: pathOf(polys[rid]), fill: hubColour(gi), "fill-opacity": 0.2, "data-room": rid, class: "zone" }, sc.L.zones);
      }
    });
  }
  function drawHubs(hubs, candidate) {
    S.hubs = hubs || []; S.hubsCandidate = !!candidate;
    const sc = S.scenes.out;
    if (!sc) return;
    sc.L.hubs.replaceChildren();
    const s = S.fs * 1.2;
    S.hubs.forEach((h, i) => {
      if (!h) return;
      const [x, y] = h;
      const g = el("g", { class: "hub" + (candidate ? " candidate" : ""), "data-hub": i }, sc.L.hubs);
      el("rect", { x: x - s / 2, y: -y - s / 2, width: s, height: s, rx: s * 0.12, fill: hubColour(i) }, g);
      const t = el("text", { x: x - s * 0.62, y: -y + s * 1.25, "font-size": S.fs * 0.78, fill: hubColour(i), "text-anchor": "end" }, g);
      t.textContent = "H" + (i + 1);
    });
  }
  function arrowFor(pts, colour, parent) {
    let best = -1, bl = 0;
    for (let i = 0; i + 1 < pts.length; i++) {
      const l = Math.abs(pts[i + 1][0] - pts[i][0]) + Math.abs(pts[i + 1][1] - pts[i][1]);
      if (l > bl) { bl = l; best = i; }
    }
    if (best < 0 || bl < 0.4) return;
    const [xa, ya] = pts[best], [xb, yb] = pts[best + 1];
    const ux = (xb - xa) / bl, uy = (yb - ya) / bl, mx = (xa + xb) / 2, my = (ya + yb) / 2, a = S.fs * 0.42;
    const tip = [mx + ux * a, my + uy * a], l = [mx - ux * a * 0.7 - uy * a * 0.6, my - uy * a * 0.7 + ux * a * 0.6], r = [mx - ux * a * 0.7 + uy * a * 0.6, my - uy * a * 0.7 - ux * a * 0.6];
    el("path", { d: `M${P(...tip)}L${P(...l)}L${P(...r)}Z`, fill: colour, class: "arrow" }, parent);
  }
  function drawRings(flash) {
    const sc = S.scenes.out;
    if (!sc) return;
    sc.L.rings.replaceChildren();
    sc.L.order.replaceChildren();
    const pos = nodePos(), fs = S.fs;
    S.live.rings.forEach((ring, ri) => {
      if (!ring) return;
      const c = hubColour(ri);
      ring.legs.forEach((leg, li) => {
        el("path", { d: lineOf(leg), class: "ring" + (flash ? " flash" : "") + (ring.bad && ring.bad.has(li) ? " bad" : ""), stroke: c, "data-hub": ri }, sc.L.rings);
        arrowFor(leg, c, sc.L.rings);
      });
      if ($("#lyOrder").checked) {
        (ring.order || []).forEach((lab, k) => {
          const p = pos[lab];
          if (!p) return;
          const g = el("g", { class: "orderdot" }, sc.L.order);
          el("circle", { cx: p[0] + fs * 0.78, cy: -(p[1] + fs * 0.78), r: fs * 0.4, fill: c }, g);
          const t = el("text", { x: p[0] + fs * 0.78, y: -(p[1] + fs * 0.78), "font-size": fs * 0.5 }, g);
          t.textContent = k + 1;
        });
      }
    });
  }
  function drawAttempt() {
    const sc = S.scenes.out;
    if (!sc) return;
    sc.L.attempt.replaceChildren();
    if (!S.attempt) return;
    const c = hubColour(S.attempt.ring);
    for (const leg of S.attempt.legs) el("path", { d: lineOf(leg), class: "ring try", stroke: c }, sc.L.attempt);
  }

  // ------------------------------------------------------------------ zoom + pan (both panels share one view)
  function applyView() {
    if (!S.view) return;
    const v = S.view, vb = `${v.x} ${v.y} ${v.w} ${v.h}`;
    for (const sc of Object.values(S.scenes)) sc.svg.setAttribute("viewBox", vb);
  }
  function svgPoint(svg, e) {
    const m = svg.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    return new DOMPoint(e.clientX, e.clientY).matrixTransform(m.inverse());
  }
  function wireCanvas(svg) {
    svg.addEventListener("wheel", (e) => {
      if (!S.view) return;
      e.preventDefault();
      const p = svgPoint(svg, e), v = S.view;
      const f = Math.exp(Math.max(-0.5, Math.min(0.5, e.deltaY * 0.0016)));
      const nw = Math.min(S.base.w * 3, Math.max(S.base.w / 60, v.w * f)), k = nw / v.w;
      v.x = p.x - (p.x - v.x) * k; v.y = p.y - (p.y - v.y) * k; v.w *= k; v.h *= k;
      applyView();
    }, { passive: false });
    let drag = null;
    svg.addEventListener("pointerdown", (e) => {
      if (!S.view || e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, id: e.pointerId };
      svg.setPointerCapture(e.pointerId);
      svg.classList.add("dragging");
    });
    svg.addEventListener("pointermove", (e) => {
      if (drag && e.pointerId === drag.id) {
        const m = svg.getScreenCTM();
        if (m) { S.view.x -= (e.clientX - drag.x) / m.a; S.view.y -= (e.clientY - drag.y) / m.d; applyView(); }
        drag.x = e.clientX; drag.y = e.clientY;
        hideTip();
        return;
      }
      showTip(e);
    });
    const end = () => { drag = null; svg.classList.remove("dragging"); };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    svg.addEventListener("pointerleave", hideTip);
    svg.addEventListener("dblclick", () => { if (S.base) { S.view = { ...S.base }; applyView(); } });
  }

  // ------------------------------------------------------------------ tooltip
  function showTip(e) {
    const tip = $("#tip");
    const t = e.target;
    let html = "";
    const nodeEl = t.closest && t.closest("[data-node]");
    const hubEl = t.closest && t.closest("[data-hub]");
    const roomEl = t.closest && t.closest("[data-room]");
    if (nodeEl) {
      const n = (S.nodes || []).find((x) => x.node_id === nodeEl.dataset.node);
      if (n) html = `<b>${esc(n.node_id)}</b> device in ${esc(n.room_id)}<br>(${fmt(n.x)}, ${fmt(n.y)}) m` + (S.zoneOf[n.room_id] != null ? `<br>served by H${S.zoneOf[n.room_id] + 1}` : "");
    } else if (hubEl) {
      const i = +hubEl.dataset.hub, h = S.hubs[i];
      const ho = S.out && S.out.hubs && S.out.hubs[i];
      html = `<b>H${i + 1}</b>` + (h ? ` at (${fmt(h[0])}, ${fmt(h[1])}) m` : "") + (ho ? `<br>${esc(ho.closed_sequence.join(" → "))}<br>ring ${fmt(ho.ring_length_m)} m, ${ho.bends} bends` : (S.hubsCandidate ? "<br>hub point being tried" : ""));
    } else if (roomEl && S.geo) {
      const r = S.geo.rooms.find((x) => x.room_id === roomEl.dataset.room);
      if (r) {
        const n = (S.nodes || []).find((x) => x.room_id === r.room_id);
        html = `<b>${esc(r.room_id)}</b> ${esc(TYPE_LABEL[r.type] || r.type)} · ${fmt(r.area_m2, 1)} m²`
          + (r.has_device ? (n ? `<br>device ${esc(n.node_id)}` : "<br>gets one device") : "<br>no device (bathroom / stair)")
          + (S.zoneOf[r.room_id] != null ? `<br>zone of H${S.zoneOf[r.room_id] + 1}` : "");
      }
    }
    if (!html) return hideTip();
    tip.innerHTML = html;
    tip.hidden = false;
    const x = Math.min(window.innerWidth - tip.offsetWidth - 8, e.clientX + 14), y = Math.min(window.innerHeight - tip.offsetHeight - 8, e.clientY + 14);
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  function hideTip() { $("#tip").hidden = true; }

  // ------------------------------------------------------------------ legends
  function legendIn() {
    if (!S.geo) return;
    const types = [...new Set(S.geo.rooms.map((r) => r.type))];
    $("#legendIn").innerHTML = types.map((t) => `<span><i style="background: var(--t-${t})"></i>${esc(TYPE_LABEL[t] || t)}</span>`).join("")
      + `<span><i style="background: var(--wall)"></i>Wall</span><span><i style="background: var(--door)"></i>Door</span>`
      + `<span><i style="background: var(--window)"></i>Window</span><span><i style="background: var(--front)"></i>Front door</span>`
      + ($("#lyLinks").checked ? `<span><i class="line" style="background: var(--accent)"></i>door link</span><span><i class="dash" style="border-color: var(--accent)"></i>open passage</span>` : "");
  }
  function legendOut() {
    const n = Math.max(S.live.rings.length, S.hubs.length, S.out && S.out.hubs ? S.out.hubs.length : 0);
    let h = "";
    for (let i = 0; i < n; i++) h += `<span><i style="background: ${hubColour(i)}"></i>H${i + 1} zone + ring</span>`;
    h += `<span><i class="circle"></i>Device</span><span><i class="dash"></i>Order being tried</span>`
      + `<span><i style="background-color: var(--noelig); background-image: repeating-linear-gradient(45deg, var(--hatch) 0 1px, transparent 1px 4px)"></i>No device</span>`;
    $("#legendOut").innerHTML = h;
  }

  // ------------------------------------------------------------------ results
  function showOutput(out, source) {
    S.out = out; S.outSource = source;
    $("#emptyOut").hidden = true;
    S.nodes = (out.nodes || []).map((n) => ({ node_id: n.node_id, room_id: n.room_id, x: n.x, y: n.y }));
    const hubs = out.hubs || [];
    drawZones(hubs.map((h) => h.assigned_rooms));
    drawHubs(hubs.map((h) => [h.location.x, h.location.y]), false);
    S.live = { rings: hubs.map((h) => ({ legs: h.route_segments.map((s) => s.polyline), order: h.closed_sequence.slice(1, -1) })) };
    S.attempt = null;
    drawAttempt(); drawDevices(); drawRings(); drawGrid(); legendOut();
    renderMetrics(out); renderHubTable(out); renderIssues(out);
    const zone = out.solver && out.solver.zone_mode;
    $("#outCaption").textContent = source === "stored"
      ? `stored result · ${outName()}/json/${out.plan_id}.json` + (zone ? ` · rule 3 ${zone}` : "")
      : `live result · rule 3 ${zone || S.runZone} · not saved yet`;
    $("#btnJson").disabled = false;
    if (!hubs.length) {
      $("#emptyOut").hidden = false;
      $("#emptyOut").classList.add("pill");
      $("#emptyOut").textContent = out.reason ? `No design: ${out.reason}` : "No design";
    }
  }
  function metric(k, v, s, cls, title) {
    return `<div class="metric ${cls || ""}"${title ? ` title="${esc(title)}"` : ""}><div class="k"><span>${esc(k)}</span></div><div class="v" data-k="${esc(k)}">${v}</div><div class="s">${s || "&nbsp;"}</div></div>`;
  }
  function renderMetrics(out, live) {
    const m = (out && out.reported_metrics) || {};
    const ok = out && out.status;
    const statusCol = { valid: "var(--ok)", invalid: "var(--warn)", failed: "var(--bad)", error: "var(--bad)" }[ok] || "var(--muted)";
    const bench = out && out.benchmark ? (out.benchmark.eligible ? "inside the benchmark" : "outside the benchmark") : "";
    $("#metrics").innerHTML = [
      metric("Status", out ? `<span style="color:${statusCol}">${esc(ok || "—")}</span>` : (live ? `<span style="color:var(--accent)">running</span>` : "—"), esc(bench), "", out && out.benchmark && out.benchmark.reasons && out.benchmark.reasons.join("; ")),
      metric("Hubs", m.hub_count ?? "—", m.hub_lower_bound != null ? `lower bound ${m.hub_lower_bound}` : "fewest wins"),
      metric("Cost J", fmt(m.cost_J), m.lower_bound_J != null ? `lower bound ${fmt(m.lower_bound_J)}` : "L + 5X + O + 0.5B", "hero"),
      metric("Length L", m.total_length_m != null ? fmt(m.total_length_m) + " m" : "—", "returns included"),
      metric("Crossings X", m.crossings ?? "—", "5 each"),
      metric("Overlap O", m.overlap_m != null ? fmt(m.overlap_m) + " m" : "—", "1 per m"),
      metric("Bends B", m.bends ?? "—", "0.5 each"),
      metric("Runtime", out && out.solver && out.solver.runtime_s != null ? fmt(out.solver.runtime_s, 1) + " s" : "—",
        out && out.solver && out.solver.grid_points ? `${fmtInt(out.solver.grid_points)} grid points` : ""),
    ].join("");
  }
  function setLiveJ(J, sub) {
    const v = $('#metrics .v[data-k="Cost J"]');
    if (!v) return;
    if (J != null) { v.textContent = fmt(J); v.classList.remove("tick"); void v.offsetWidth; v.classList.add("tick"); }
    if (sub) v.nextElementSibling.textContent = sub;
  }
  function renderHubTable(out) {
    const t = $("#hubTable");
    if (!out || !out.hubs || !out.hubs.length) { t.innerHTML = `<tbody><tr><td class="muted">${out ? "No hubs." : "Run the solver to see hubs and rings."}</td></tr></tbody>`; return; }
    t.innerHTML = `<thead><tr><th>Hub</th><th>In room</th><th>Zone rooms</th><th class="num">Area</th><th>Visiting order</th><th class="num">Ring</th><th class="num">Bends</th></tr></thead><tbody>`
      + out.hubs.map((h, i) => `<tr><td><span class="swatch" style="background:${hubColour(i)}"></span><b>${esc(h.hub_id)}</b></td>`
        + `<td>${esc(h.room_id)}</td><td>${esc(h.assigned_rooms.join(", "))}</td><td class="num">${fmt(h.zone_area_m2, 1)} m²</td>`
        + `<td class="seq">${esc(h.closed_sequence.join(" → "))}</td><td class="num">${fmt(h.ring_length_m)} m</td><td class="num">${h.bends ?? "—"}</td></tr>`).join("")
      + "</tbody>";
  }
  function renderIssues(out) {
    const box = $("#issues");
    if (!out) { box.innerHTML = ""; return; }
    const items = [];
    if (out.reason && out.status !== "valid") items.push(["bad", out.reason]);
    for (const v of out.violations || []) items.push(["bad", "Violation: " + v]);
    for (const v of out.solver_issues || []) items.push(["bad", "Grid check: " + v]);
    for (const w of (out.warnings || []).slice(0, 6)) items.push(["warn", w]);
    if ((out.warnings || []).length > 6) items.push(["warn", `+ ${out.warnings.length - 6} more warnings (see the JSON)`]);
    if (out.check && out.check.agrees_with_solver === false) items.push(["bad", "The independent polyline check disagrees with the solver"]);
    box.innerHTML = items.map(([c, m]) => `<div class="issue ${c}">${esc(m)}</div>`).join("");
  }

  // ------------------------------------------------------------------ log + stages
  function log(msg, cls, t) {
    const box = $("#log");
    const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
    const d = document.createElement("div");
    if (cls) d.className = cls;
    d.innerHTML = (t != null ? `<span class="t">${fmt(t, 2)}s</span>` : "") + esc(msg);
    box.appendChild(d);
    while (box.childElementCount > 800) box.firstElementChild.remove();
    if (atEnd) box.scrollTop = box.scrollHeight;
  }
  function clearLog() { $("#log").replaceChildren(); }
  function buildStages() {
    $("#stages").innerHTML = STAGES.map(([k, name]) => `<li data-stage="${k}"><i></i><span>${name}</span><small></small></li>`).join("");
  }
  function resetStages() { for (const li of $$("#stages li")) { li.className = ""; li.querySelector("small").textContent = ""; } }
  function setStage(name, t) {
    const idx = STAGES.findIndex(([k]) => k === name);
    if (idx < 0) return;
    $$("#stages li").forEach((li, i) => {
      if (i < idx) { if (li.className !== "done") li.className = "done"; }
      else if (i === idx) {
        if (li.className !== "active") { li.className = "active"; if (t != null) li.querySelector("small").textContent = fmt(t, 2) + "s"; }
      }
    });
  }
  function stagesDone(t) {
    $$("#stages li").forEach((li) => { li.className = "done"; });
    const last = $$("#stages li").pop();
    if (last && t != null) last.querySelector("small").textContent = fmt(t, 2) + "s";
  }

  // ------------------------------------------------------------------ live run
  function setRunning(on) {
    $("#btnRun").disabled = on || S.current == null || !S.geo;
    $("#btnStop").disabled = !on;
    $("#liveBadge").hidden = !on;
    $("#zone").disabled = on;
  }
  function stopRun(silent) {
    if (S.es) {
      S.es.close(); S.es = null;
      if (!silent) {
        log("Stopped.", "bad");
        const act = $("#stages li.active");
        if (act) act.className = "";
      }
    }
    setRunning(false);
  }
  function showLoops(loops, groups, flash) {
    S.live.rings = loops.map((l) => ({ legs: l.legs, order: l.order }));
    if (groups) drawZones(groups);
    drawHubs(loops.map((l) => l.hub), false);
    S.attempt = null;
    drawAttempt(); drawRings(flash); legendOut();
  }
  const groupsText = (groups) => groups.map((g, i) => `H${i + 1} {${g.join(", ")}}`).join("  ");

  function startRun() {
    if (S.current == null || !S.geo) return;
    stopRun(true);
    const zone = $("#zone").value, id = S.current;
    S.runZone = zone; S.runSpeed = S.speed;
    S.live = { rings: [] }; S.attempt = null; S.out = null; S.gridLines = null;
    drawZones([]); drawHubs([]); drawRings(); drawAttempt();
    S.nodes = null; drawDevices(); drawGrid();
    $("#emptyOut").hidden = true;
    $("#outCaption").textContent = `live run · rule 3 ${zone} · ${S.speed} speed`;
    $("#btnSave").disabled = true; $("#btnJson").disabled = true;
    renderMetrics(null, true); renderHubTable(null); renderIssues(null);
    clearLog(); resetStages();
    const es = new EventSource(`/api/solve?id=${id}&zone=${encodeURIComponent(zone)}&speed=${S.speed}`);
    S.es = es;
    setRunning(true);
    const on = (k, f) => es.addEventListener(k, (e) => {
      if (S.es !== es) return;
      let d;
      try { d = JSON.parse(e.data); } catch { return; }
      try { f(d); } catch (err) { console.error(k, err); }
    });
    on("start", (d) => log(`Solving ${d.plan_id} · rule 3: ${d.zone} · speed: ${d.speed}`, "stage"));
    on("stage", (d) => { setStage(d.stage, d.t); log(d.msg, "stage", d.t); });
    on("plan", (d) => { if (!S.geo) { S.geo = d; buildScenes(); } });
    on("devices", (d) => {
      S.nodes = d.nodes; drawDevices(); drawGrid();
      log(`${d.nodes.length} devices placed: ` + d.nodes.map((n) => `${n.node_id} in ${n.room_id}`).join(", "), null, d.t);
      for (const w of d.warnings || []) log("warning: " + w, "dim", d.t);
    });
    on("grid", (d) => {
      S.gridLines = { xs: d.xs, ys: d.ys };
      drawGrid();
      log(`Grid: ${d.xs.length} × ${d.ys.length} lines (every ${d.h} m + one through each device), ${fmtInt(d.points)} points, ${fmtInt(d.free_points)} in free space, ${fmtInt(d.edges)} usable segments`, null, d.t);
      if (!$("#lyGrid").checked) log("Tick \"Grid lines\" below the panels to see the grid.", "dim");
    });
    on("links", (d) => {
      S.links = d.links; drawLinks();
      const doors = d.links.filter((l) => l.type === "door").length;
      log(`Rule 3 room links: ${doors} through doors, ${d.links.length - doors} through open passages`, null, d.t);
    });
    on("benchmark", (d) => log(d.eligible ? "Plan is inside the organisers' benchmark filters" : "Outside the benchmark: " + d.reasons.join("; "), d.eligible ? "good" : "dim", d.t));
    on("blocks", (d) => { setStage("zones", d.t); log(`Stage 1: ${plural(d.count, "possible zone")} (≤ 5 devices, ≤ 100 m², rooms linked by doors); at least ${plural(d.lower_bound_hubs, "hub")} needed`, null, d.t); });
    on("groupings", (d) => {
      if (!d.count) { log(`${plural(d.k, "hub")}: no split of the rooms fits the rules, trying one more hub`, "dim", d.t); return; }
      log(`Stage 2: ${d.count === 1 ? "1 way" : d.count + " ways"} to split the rooms between ${plural(d.k, "hub")}; best hub point found for every zone (lowest bound J ${fmt(d.top[0] && d.top[0].lb)})`, null, d.t);
      d.top.slice(0, 4).forEach((g, i) => log(`  #${i + 1}  lower bound ${fmt(g.lb)}  ${groupsText(g.groups)}`, "dim"));
      if (d.top[0]) { drawZones(d.top[0].groups); drawHubs(d.top[0].hubs, true); legendOut(); }
    });
    on("grouping", (d) => {
      setStage("rings", d.t);
      S.live.rings = []; S.attempt = null;
      drawZones(d.groups); drawHubs(d.hubs, true); drawRings(); drawAttempt(); legendOut();
      log(`Routing split ${d.index + 1} of ${d.of}: ${groupsText(d.groups)} · lower bound ${fmt(d.lb)}`, "stage", d.t);
      setLiveJ(null, `lower bound ${fmt(d.lb)}`);
    });
    on("leg", (d) => { S.attempt = { ring: d.ring, legs: d.legs }; drawAttempt(); });
    on("attempt", (d) => {
      S.attempt = { ring: d.ring, legs: d.legs }; drawAttempt();
      log(`H${d.ring + 1} order ${d.order.join("→")}: J so far ${fmt(d.J)}${d.best ? "  ★ best" : ""}`, d.best ? "good" : "dim", d.t);
    });
    on("ring", (d) => {
      S.live.rings[d.ring] = { legs: d.legs, order: d.order };
      S.attempt = null; drawAttempt(); drawRings(true);
      log(`H${d.ring + 1} ring fixed: ${["H" + (d.ring + 1), ...d.order, "H" + (d.ring + 1)].join(" → ")}`, null, d.t);
    });
    on("routed", (d) => {
      if (!d.ok) { log(`Split ${d.index + 1} could not be routed`, "bad", d.t); return; }
      showLoops(d.loops, d.groups);
      log(`Split ${d.index + 1}: J ${fmt(d.J)}  (L ${fmt(d.metrics.L)} m, X ${d.metrics.X}, O ${fmt(d.metrics.O)} m, B ${d.metrics.B})`, "good", d.t);
      setLiveJ(d.best_J, "best so far");
    });
    on("log", (d) => log(d.msg, "dim", d.t));
    on("improving", (d) => {
      setStage("cleanup", d.t);
      showLoops(d.loops, d.groups);
      log(`Clean-up of design #${d.rank + 1} (J ${fmt(d.J)}): re-routing each leg against all the other wires`, "stage", d.t);
    });
    on("improve", (d) => { showLoops(d.loops, null, true); setLiveJ(d.J, "clean-up"); });
    on("improved", (d) => {
      const better = d.J < d.J_before - 1e-9;
      log(better ? `Clean-up: J ${fmt(d.J_before)} → ${fmt(d.J)}` : `Clean-up: no better routing (J ${fmt(d.J)})`, better ? "good" : "dim", d.t);
    });
    on("chosen", (d) => { showLoops(d.loops, d.groups); log(`Chosen design: J ${fmt(d.J)}`, "good", d.t); setLiveJ(d.J, "chosen"); });
    on("result", (d) => {
      es.close(); S.es = null; setRunning(false);
      stagesDone(d.t);
      showOutput(d.out, "live");
      updateRow(d.out); renderList();
      S.savedKey = { id, zone };
      $("#btnSave").disabled = false;
      const rm = d.out.reported_metrics || {};
      log(`Done in ${fmt(d.t, 1)} s: ${d.out.status}` + (rm.hub_count != null ? `, ${plural(rm.hub_count, "hub")}, J ${fmt(rm.cost_J)}` : "") + (d.out.reason ? ` (${d.out.reason})` : ""), d.out.status === "valid" ? "good" : "bad", d.t);
      if (S.runSpeed !== "instant") {
        const v = $('#metrics .v[data-k="Runtime"]');
        if (v) v.nextElementSibling.textContent = "incl. pauses for watching";
      }
    });
    on("fail", (d) => {
      log("Error: " + d.msg, "bad");
      if (d.trace) console.error(d.trace);
      toast(d.msg);
      stopRun(true);
    });
    es.onerror = () => {
      if (S.es !== es) return;
      log("Connection to the solver closed.", "bad");
      stopRun(true);
    };
  }

  async function savePlan() {
    if (!S.savedKey) return;
    $("#btnSave").disabled = true;
    try {
      const d = await getJSON(`/api/save?id=${S.savedKey.id}&zone=${encodeURIComponent(S.savedKey.zone)}`);
      toast(d.msg || (d.ok ? "Saved" : "Not saved"));
      if (d.ok) { $("#outCaption").textContent = `live result · saved to ${outName()}`; log(d.msg, "good"); }
      else $("#btnSave").disabled = false;
    } catch (e) { toast("Save failed: " + e.message); $("#btnSave").disabled = false; }
  }
  function downloadJSON() {
    if (!S.out) return;
    const blob = new Blob([JSON.stringify(S.out, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${S.out.plan_id || "plan"}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  // ------------------------------------------------------------------ batch
  const B = { es: null, run: null, rows: 0, total: 0, cached: 0, toSolve: 0, solved: 0, ts0: null, tsLast: null, counts: {}, hubs: {}, Js: [], raf: 0, label: "" };
  function bReset() {
    clearTimeout(B.raf);
    Object.assign(B, { run: null, rows: 0, total: 0, cached: 0, toSolve: 0, solved: 0, ts0: null, tsLast: null, counts: {}, hubs: {}, Js: [], label: "", done: null, raf: 0 });
    $("#bTable tbody").replaceChildren();
    bRender();
  }
  function bModeFields() {
    const mode = $("#bMode").value;
    for (const l of $$(".form-grid label[data-for]")) l.hidden = !l.dataset.for.split(" ").includes(mode);
  }
  async function bStart() {
    const q = new URLSearchParams({
      mode: $("#bMode").value, n: $("#bN").value, seed: $("#bSeed").value, split: $("#bSplit").value, ids: $("#bIds").value,
      workers: $("#bWorkers").value, zone: $("#bZone").value, png: $("#bPng").checked ? "1" : "0", force: $("#bForce").checked ? "1" : "0",
    });
    $("#bStart").disabled = true;
    try {
      const d = await getJSON("/api/batch/start?" + q);
      $("#bMsg").textContent = d.msg || "";
      if (d.ok) bFollow(); else { toast(d.msg); $("#bStart").disabled = false; }
    } catch (e) { toast("Could not start: " + e.message); $("#bStart").disabled = false; }
  }
  function bFollow() {
    if (B.es) B.es.close();
    bReset();
    const es = new EventSource("/api/batch/events?from=0");
    B.es = es;
    const on = (k, f) => es.addEventListener(k, (e) => { if (B.es !== es) return; try { f(JSON.parse(e.data)); } catch (err) { console.error(err); } });
    on("start", (d) => {
      B.run = d.run; B.total = d.total; B.cached = d.cached; B.toSolve = d.to_solve; B.ts0 = d.ts; B.label = d.label;
      $("#bStop").disabled = false; $("#bStart").disabled = true; $("#batchDot").hidden = false;
      $("#bMsg").textContent = `${d.label}: ${fmtInt(d.total)} plans, ${fmtInt(d.cached)} already done, ${fmtInt(d.to_solve)} to solve with ${d.workers} workers`;
      bRender();
    });
    on("row", (d) => { bAddRow(d); });
    on("fail", (d) => { toast("Batch error: " + d.msg); console.error(d.trace); });
    on("done", (d) => {
      es.close(); B.es = null;
      clearTimeout(B.raf); B.raf = 0;
      $("#bStop").disabled = true; $("#bStart").disabled = false; $("#batchDot").hidden = true;
      B.done = d;
      if (d.stopped) $("#bMsg").textContent = `Stopped: ${plural(d.solved, "plan")} solved; plans still in progress were cancelled (re-running picks them up).`;
      else $("#bMsg").textContent = `Finished: ${plural(d.solved, "plan")} solved and written to ${outName()}.`;
      bRender();
      loadRows().then(renderList);
    });
    on("idle", () => { es.close(); B.es = null; $("#bStart").disabled = false; $("#bStop").disabled = true; });
    es.onerror = () => { if (B.es === es) { es.close(); B.es = null; $("#bStart").disabled = false; $("#batchDot").hidden = true; } };
  }
  function bAddRow(d) {
    B.rows++;
    if (!d.cached) { B.solved++; B.tsLast = d.ts; }
    const st = d.status || "none";
    B.counts[st] = (B.counts[st] || 0) + 1;
    if (d.hubs) B.hubs[d.hubs] = (B.hubs[d.hubs] || 0) + 1;
    if (d.J != null && d.J !== "") B.Js.push(+d.J);
    const id = d.id ?? +String(d.plan_id).split("_")[1];
    const r = S.rowById.get(id);
    if (r) { r[2] = d.status; r[3] = d.hubs ? +d.hubs : null; r[4] = d.J ? +d.J : null; }
    const tb = $("#bTable tbody");
    const tr = document.createElement("tr");
    tr.className = "click" + (d.cached ? "" : " fresh");
    tr.dataset.id = id;
    const lbh = d.hub_lower_bound ? +d.hub_lower_bound : null, hubs = d.hubs ? +d.hubs : null;
    const benchTxt = d.in_benchmark === true || d.in_benchmark === "True" ? "yes" : d.in_benchmark === false || d.in_benchmark === "False" ? "no" : "—";
    tr.innerHTML = `<td class="seq">${esc(d.plan_id)}</td><td>${badge(d.status)}${d.cached ? ' <span class="muted">done before</span>' : ""}</td>`
      + `<td>${benchTxt}</td><td class="num">${hubs ?? "—"}${hubs && lbh && hubs > lbh ? ` <span class="muted">(min ${lbh})</span>` : ""}</td>`
      + `<td class="num">${fmt(d.J)}</td><td class="num">${fmt(d.lower_bound_J)}</td><td class="num">${d.runtime_s ? fmt(d.runtime_s, 1) + " s" : "—"}</td>`
      + `<td>${d.png ? `<a href="${esc(d.png)}" target="_blank" rel="noopener">PNG</a>` : ""}</td>`;
    tb.prepend(tr);
    while (tb.childElementCount > 1500) tb.lastElementChild.remove();
    if (!B.raf) B.raf = setTimeout(() => { B.raf = 0; bRender(); }, 120);
  }
  function bRender() {
    const done = B.rows, total = B.total || 0, pct = total ? (100 * done) / total : 0;
    $("#bBar").setAttribute("aria-valuenow", pct.toFixed(0));
    $("#bBar span").style.width = pct + "%";
    const running = !!B.es && !B.done;
    $("#bTitle").textContent = !total ? (B.done ? "Finished" : "No batch running")
      : `${B.label ? B.label + ": " : ""}${fmtInt(done)} / ${fmtInt(total)} plans` + (B.done ? (B.done.stopped ? " · stopped" : " · finished") : "");
    let rate = "";
    if (B.solved && B.ts0 && B.tsLast > B.ts0) {
      const per = (B.tsLast - B.ts0) / B.solved, left = Math.max(0, B.toSolve - B.solved) * per;
      rate = `${fmt(1 / per, 1)} plans/s · ${fmt(per, 2)} s per plan` + (running && left > 0 ? ` · about ${left > 90 ? fmt(left / 60, 0) + " min" : fmt(left, 0) + " s"} left` : "");
    }
    if (B.done) rate = `${fmtInt(B.done.solved)} solved in ${fmt(B.done.elapsed_s / 60, 1)} min`;
    $("#bRate").textContent = rate;
    const c = B.counts, bad = (c.failed || 0) + (c.error || 0) + (c.skipped || 0);
    const med = B.Js.length ? [...B.Js].sort((a, b) => a - b)[Math.floor(B.Js.length / 2)] : null;
    $("#bCounters").innerHTML = [
      ["", fmtInt(done), "done"], ["valid", fmtInt(c.valid || 0), "valid"], ["invalid", fmtInt(c.invalid || 0), "invalid"],
      ["failed", fmtInt(bad), "failed / skipped"], ["", fmt(med), "median J"],
    ].map(([k, v, l]) => `<div class="counter ${k}"><b>${v}</b><span>${l}</span></div>`).join("");
    const hk = Object.keys(B.hubs).map(Number).sort((a, b) => a - b), hmax = Math.max(1, ...Object.values(B.hubs));
    $("#bHubs").innerHTML = hk.length ? hk.map((k) => `<div class="bar"><span>${k} hub${k === 1 ? "" : "s"}</span><div style="width:${(100 * B.hubs[k]) / hmax}%"></div><span>${B.hubs[k]}</span></div>`).join("") : `<span class="muted">—</span>`;
    bSpark();
  }
  function bSpark() {
    const box = $("#bJ");
    const Js = B.Js.slice(-1500);
    if (!Js.length) { box.innerHTML = `<span class="muted">—</span>`; return; }
    const lo = Math.min(...Js), hi = Math.max(...Js), W = 600, H = 96, pad = 18;
    const sy = (v) => H - 14 - ((v - lo) / Math.max(1e-9, hi - lo)) * (H - 24);
    const sx = (i) => pad + (i / Math.max(1, Js.length - 1)) * (W - pad - 4);
    let pts = "";
    Js.forEach((v, i) => { pts += `<circle class="pt" cx="${sx(i).toFixed(1)}" cy="${sy(v).toFixed(1)}" r="1.6"/>`; });
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="J per plan in arrival order">`
      + `<line class="axis" x1="${pad}" y1="${H - 12}" x2="${W}" y2="${H - 12}"/>${pts}`
      + `<text x="0" y="10">${fmt(hi, 0)}</text><text x="0" y="${H - 14}">${fmt(lo, 0)}</text></svg>`;
  }

  // ------------------------------------------------------------------ events
  function selectTab(name) {
    for (const b of $$(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
    $("#tab-viewer").hidden = name !== "viewer";
    $("#tab-batch").hidden = name !== "batch";
    if (name === "batch" && !B.es) bFollow();
  }
  function wire() {
    for (const b of $$(".tabs button")) b.addEventListener("click", () => selectTab(b.dataset.tab));
    let t = null;
    const relist = () => { S.listLimit = 300; renderList(); };
    $("#fSplit").addEventListener("change", relist);
    $("#fStatus").addEventListener("change", relist);
    $("#fSearch").addEventListener("input", () => { clearTimeout(t); t = setTimeout(relist, 120); });
    $("#fSearch").addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const id = +$("#fSearch").value.replace(/\D/g, "");
      if ($("#fSearch").value.trim() && S.rowById.has(id)) { openPlan(id); return; }
      const r = filteredRows()[0];
      if (r) openPlan(r[0]);
    });
    $("#planList").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.more) { S.listLimit += 300; renderList(); return; }
      openPlan(+b.dataset.id);
    });
    $("#planList").addEventListener("keydown", (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const bs = $$("#planList button[data-id]"), i = bs.indexOf(document.activeElement);
      if (i < 0) return;
      e.preventDefault();
      const nb = bs[Math.max(0, Math.min(bs.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
      nb.focus(); openPlan(+nb.dataset.id);
    });
    $("#btnRandom").addEventListener("click", () => {
      const rows = filteredRows();
      if (rows.length) openPlan(rows[Math.floor(Math.random() * rows.length)][0]);
    });
    for (const b of $$("#speed button")) b.addEventListener("click", () => {
      S.speed = b.dataset.speed;
      for (const x of $$("#speed button")) x.setAttribute("aria-pressed", String(x === b));
    });
    $("#btnRun").addEventListener("click", startRun);
    $("#btnStop").addEventListener("click", () => stopRun(false));
    $("#btnSave").addEventListener("click", savePlan);
    $("#btnJson").addEventListener("click", downloadJSON);
    $("#btnClearLog").addEventListener("click", clearLog);
    $("#lyLabels").addEventListener("change", drawLabels);
    $("#lyGrid").addEventListener("change", drawGrid);
    $("#lyLinks").addEventListener("change", () => { drawLinks(); legendIn(); });
    $("#lyOrder").addEventListener("change", () => drawRings());
    for (const svg of [$("#svgIn"), $("#svgOut")]) {
      svg.addEventListener("pointerover", (e) => {
        const r = e.target.closest && e.target.closest(".room[data-room]");
        $$(".room.hover").forEach((x) => x.classList.remove("hover"));
        if (r) for (const x of $$(`.room[data-room="${CSS.escape(r.dataset.room)}"]`)) x.classList.add("hover");
      });
    }
    $("#bMode").addEventListener("change", bModeFields);
    $("#bStart").addEventListener("click", bStart);
    $("#bStop").addEventListener("click", async () => { $("#bStop").disabled = true; await getJSON("/api/batch/stop").catch(() => {}); $("#bMsg").textContent = "Stopping…"; });
    $("#bTable").addEventListener("click", (e) => {
      if (e.target.closest("a")) return;
      const tr = e.target.closest("tr[data-id]");
      if (!tr) return;
      selectTab("viewer");
      openPlan(+tr.dataset.id);
    });
    document.addEventListener("keydown", (e) => {
      if (e.target.matches("input, select, textarea")) return;
      if (e.key === "r" && !e.ctrlKey && !e.metaKey && !$("#btnRun").disabled && !$("#tab-viewer").hidden) startRun();
      if (e.key === "Escape" && S.es) stopRun(false);
    });
    bModeFields();
  }

  wire();
  boot();
})();
