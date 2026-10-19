(() => {
  const BRAND = "API AI generator";   // change the name here
  const $ = (id) => document.getElementById(id);
  const fmtUsd = (v) => "$" + (Number(v) || 0).toFixed(2);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  let state = null, presets = {}, kind = "image", manualPrompt = false, tableOpen = false, feedMode = "list";
  const settings = {aspect: "9:16", resolution: "1K", count: 1, duration: 10, vres: "480p", vaspect: "9:16"};
  const filter = {kind: "all", starred: false, rejected: false, q: ""};
  const jobs = new Map(), polling = new Set(), refSel = [], cmpSel = [];
  let source = null, parentId = null;
  let vmode = "first", endSource = null, refVideo = null;   // video mode: first | first-last | reference
  const castSel = [];                                         // selected character ids
  const viewer = {list: [], i: 0};
  try { feedMode = localStorage.getItem("feedMode") || "list"; } catch {}

  const P = (d) => `<svg class="i" viewBox="0 0 24 24">${d}</svg>`;
  const ICON = {
    save: P('<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>'),
    star: P('<path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.6l1-5.8L3.5 9.7l5.9-.9z"/>'),
    x: P('<path d="M6 6l12 12M18 6L6 18"/>'),
    dots: P('<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>'),
    rerun: P('<path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6"/>'),
    play: P('<path d="M7 4.5v15l12-7.5z"/>'),
    spark: P('<path d="M12 3l2 5.5L19.5 10 14 12l-2 5.5L10 12 4.5 10 10 8.5z"/>'),
    hd: P('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 9v6M7 12h3M10 9v6M14 9v6h1.5a2.5 2.5 0 0 0 0-5H14"/>'),
    ratio: P('<rect x="6" y="3" width="12" height="18" rx="2"/>'),
    res: P('<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/>'),
    stack: P('<rect x="4" y="8" width="12" height="12" rx="2"/><path d="M8 4h10a2 2 0 0 1 2 2v10"/>'),
    clock: P('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>'),
    plus: P('<path d="M12 5v14M5 12h14"/>'),
    left: P('<path d="M15 5l-7 7 7 7"/>'),
    right: P('<path d="M9 5l7 7-7 7"/>'),
    edit: P('<path d="M4 20h4L19 9l-4-4L4 16z"/>'),
  };
  const MODEL_LABEL = {"nano-banana-2": "Nano Banana 2", "gpt-image-2": "GPT Image 2", "2.5": "Seedance 2.5", "2.5-final": "Seedance 2.5 · 1080p final", "pro": "Seedance 1.0 Pro", "lite": "Seedance 1.0 Lite"};

  $("brandName").textContent = BRAND; $("logo").textContent = BRAND.trim()[0] || "R"; document.title = BRAND;

  async function api(path, body) {
    const r = await fetch(path, body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {cache: "no-store"});
    const text = await r.text();
    try { return JSON.parse(text); }
    catch { return {ok: false, error: r.status === 413 ? "The upload is too large for the server (4.5 MB limit)." : `HTTP ${r.status}: ${text.slice(0, 80)}`}; }
  }
  function toast(msg, ms = 2600) { const t = $("toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", ms); }
  const refOf = (g) => state && state.mode === "local" && g.path ? g.path : g.url;
  const isVideoItem = (g) => g.kind === "video";
  const byId = (id) => state && state.generations.find(g => g.id === id);

  // ---------- navigation ----------
  function showView(name) {
    document.querySelectorAll(".tab").forEach(b => b.classList.toggle("on", b.dataset.view === name));
    ["create", "cast", "library", "spend"].forEach(v => $("view-" + v).classList.toggle("hidden", v !== name));
    if (name === "spend" && state) renderChart(state.daily, state.spend);
    try { localStorage.setItem("view", name); } catch {}
  }
  document.querySelectorAll(".tab").forEach(b => b.onclick = () => showView(b.dataset.view));

  // ---------- state loop ----------
  async function load() {
    try {
      state = await api("/api/state");
      if (!state.ok) throw new Error(state.error || "state failed");
      (state.jobs || []).forEach(j => jobs.set(j.id, {...(jobs.get(j.id) || {}), ...j}));
      render();
    } catch (e) { $("creditsPill").textContent = "API not reachable: " + e.message; }
    jobs.forEach(j => { if (j.status === "running") pollJob(j.id); });
    clearTimeout(load._t);
    load._t = setTimeout(() => { if (document.visibilityState === "visible") load(); else load._due = true; }, 60000);
  }
  // Refresh when the tab comes back instead of polling in the background (keeps storage operations low).
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && load._due) { load._due = false; load(); } });
  async function pollJob(id) {
    if (polling.has(id)) return;
    polling.add(id);
    try {
      const r = await api("/api/tasks?id=" + encodeURIComponent(id));
      if (r.ok && r.job) { jobs.set(id, r.job); renderFeed(); if (r.job.status !== "running") { setTimeout(load, 800); return; } }
      else if (r.gone) { jobs.delete(id); renderFeed(); setTimeout(load, 800); return; }
    } catch {} finally { polling.delete(id); }
    const j = jobs.get(id);
    if (j && j.status === "running") setTimeout(() => pollJob(id), state && state.mode === "vercel" ? 12000 : 4000);
  }
  function render() {
    const s = state;
    $("dir").textContent = s.mode === "vercel" ? "Outputs are stored in Vercel Blob." : "Outputs folder: " + (s.generations_dir || "");
    renderBanner(s); renderCredits(s); renderTiles(s);
    if (!$("view-spend").classList.contains("hidden")) renderChart(s.daily, s.spend);
    renderPresetCard(); renderRefs(); renderCastPick(); renderCast(); renderPills(); renderFeed(); renderBoard(); estimate();
  }
  function renderBanner(s) {
    const b = $("banner");
    if (s.key_present && !s.setup_error) { b.classList.add("hidden"); return; }
    b.classList.remove("hidden");
    b.innerHTML = !s.key_present
      ? (s.mode === "vercel" ? `<b>No API key.</b> Add <code>KIE_API_KEY</code> in the Vercel project's Environment Variables, then redeploy.` : `<b>No API key.</b> Copy <code>.env.example</code> to <code>.env</code>, paste your kie.ai key, restart the dashboard.`)
      : `<b>Setup problem.</b> ${esc(s.setup_error)}`;
  }
  function renderCredits(s) {
    const c = s.credits || {};
    $("creditsPill").innerHTML = c.value != null
      ? `${ICON.spark}<b>${Number(c.value).toLocaleString()}</b> <span class="muted">credits ≈ ${fmtUsd(c.usd)}</span> <a href="https://kie.ai/billing" target="_blank" rel="noopener">Top up</a>`
      : `<span class="muted">${esc(c.error || "checking credits…")}</span>`;
  }

  // ---------- spend ----------
  function meter(used, cap) {
    const pct = cap > 0 ? Math.min(100, used / cap * 100) : 0, r = cap > 0 ? used / cap : 0;
    const cls = r >= 1 ? "over" : r >= 0.8 ? "warn" : "", word = cls === "over" ? "Over cap" : cls === "warn" ? "Near cap" : "Within cap";
    return `<div class="meter ${cls}"><div style="width:${pct.toFixed(1)}%"></div></div><span class="status ${cls}">${word}, ${pct.toFixed(0)}% of ${fmtUsd(cap)}</span>`;
  }
  function renderTiles(s) {
    const c = s.credits || {}, sp = s.spend, starred = s.generations.filter(g => g.rating === "star").length;
    $("tiles").innerHTML = `
      <div class="card hero"><div class="tile-k">Credits remaining</div><div class="tile-v">${c.value != null ? Number(c.value).toLocaleString() : "–"}</div><div class="tile-n">${c.value != null ? "about " + fmtUsd(c.usd) + " on kie.ai" : esc(c.error || "")} <button class="linkbtn" id="refreshCredits">refresh</button></div></div>
      <div class="card"><div class="tile-k">Spent today</div><div class="tile-v">${fmtUsd(sp.today_usd)}</div>${meter(sp.today_usd, sp.session_cap_usd)}</div>
      <div class="card"><div class="tile-k">Spent this month</div><div class="tile-v">${fmtUsd(sp.month_usd)}</div>${meter(sp.month_usd, sp.monthly_cap_usd)}</div>
      <div class="card"><div class="tile-k">Outputs</div><div class="tile-v">${s.generations.length}</div><div class="tile-n">${starred} starred · ${sp.month_runs} runs this month</div></div>`;
    $("refreshCredits").onclick = async () => { await api("/api/refresh-credits", {}); setTimeout(load, 1200); };
  }
  function niceMax(v) { if (v <= 0) return 1; const p = Math.pow(10, Math.floor(Math.log10(v))); for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p; return 10 * p; }
  function renderChart(daily, spend) {
    const host = $("chart"); const W = host.clientWidth || 600, H = 220, padL = 44, padR = 12, padT = 18, padB = 26;
    const maxV = niceMax(Math.max(0, ...daily.map(d => d.usd)));
    const n = daily.length, band = (W - padL - padR) / n, bw = Math.min(24, band * 0.6);
    const y = v => padT + (H - padT - padB) * (1 - v / maxV), base = H - padB;
    const maxIdx = daily.reduce((m, d, i) => d.usd > daily[m].usd ? i : m, 0);
    let g = "", hits = "", bars = "";
    for (let t = 0; t <= 4; t++) { const v = maxV * t / 4, yy = y(v); g += `<line class="${t ? "gridl" : "base"}" x1="${padL}" x2="${W - padR}" y1="${yy}" y2="${yy}"/><text x="${padL - 6}" y="${yy + 4}" text-anchor="end">$${v.toFixed(v >= 1 && v % 1 === 0 ? 0 : 2)}</text>`; }
    daily.forEach((d, i) => {
      const x = padL + band * i + (band - bw) / 2, top = y(d.usd), h = Math.max(0, base - top), r = Math.min(4, h);
      hits += `<rect class="hit" x="${padL + band * i}" y="${padT}" width="${band}" height="${H - padT - padB}" data-i="${i}"/>`;
      bars += `<path class="bar" d="${h > 0 ? `M${x},${base} V${top + r} q0,-${r} ${r},-${r} H${x + bw - r} q${r},0 ${r},${r} V${base} Z` : ""}"/>`;
      const day = +d.date.slice(8);
      if (n <= 16 || day === 1 || day % 5 === 0) bars += `<text x="${padL + band * i + band / 2}" y="${H - 8}" text-anchor="middle">${day}</text>`;
      if (i === maxIdx && d.usd > 0) bars += `<text class="lbl" x="${x + bw / 2}" y="${top - 5}" text-anchor="middle">${fmtUsd(d.usd)}</text>`;
    });
    host.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Daily spend this month">${g}${hits}${bars}</svg>`;
    const tip = $("tip");
    host.querySelectorAll(".hit").forEach(el => {
      el.addEventListener("mousemove", ev => { const d = daily[+el.dataset.i]; tip.innerHTML = `<b>${d.date}</b><span>${fmtUsd(d.usd)} · ${d.runs} run${d.runs === 1 ? "" : "s"}</span>`; tip.style.display = "block"; const rect = host.parentElement.getBoundingClientRect(); tip.style.left = Math.min(ev.clientX - rect.left + 12, rect.width - 150) + "px"; tip.style.top = (ev.clientY - rect.top - 10) + "px"; });
      el.addEventListener("mouseleave", () => tip.style.display = "none");
    });
    $("chartTable").innerHTML = `<table><thead><tr><th>Day</th><th class="num">Runs</th><th class="num">USD</th></tr></thead><tbody>` + daily.map(d => `<tr><td>${d.date}</td><td class="num">${d.runs}</td><td class="num">${fmtUsd(d.usd)}</td></tr>`).join("") + `<tr><td><b>Month</b></td><td class="num"><b>${spend.month_runs}</b></td><td class="num"><b>${fmtUsd(spend.month_usd)}</b></td></tr></tbody></table>`;
  }
  $("tableToggle").onclick = () => { tableOpen = !tableOpen; $("chart").classList.toggle("hidden", tableOpen); $("chartTable").classList.toggle("hidden", !tableOpen); $("tableToggle").textContent = tableOpen ? "Chart view" : "Table view"; };
  window.addEventListener("resize", () => state && !$("view-spend").classList.contains("hidden") && renderChart(state.daily, state.spend));

  // ---------- popover menu ----------
  const pop = $("pop");
  function openMenu(anchor, items, onPick) {
    pop.innerHTML = items.map((it, i) => `<button type="button" data-i="${i}" class="${it.on ? "on" : ""}"><span>${it.label}</span>${it.sub ? `<span class="s">${it.sub}</span>` : ""}</button>`).join("");
    pop.style.display = "block";
    const r = anchor.getBoundingClientRect(), pw = pop.offsetWidth, ph = pop.offsetHeight;
    let left = Math.min(r.left, window.innerWidth - pw - 8), top = r.bottom + 6;
    if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
    pop.style.left = Math.max(8, left) + "px"; pop.style.top = top + "px";
    pop.querySelectorAll("button").forEach(b => b.onclick = (e) => { e.stopPropagation(); closeMenu(); onPick(items[+b.dataset.i]); });
    setTimeout(() => document.addEventListener("click", closeMenu, {once: true}), 0);
  }
  function closeMenu() { pop.style.display = "none"; }

  // ---------- pills ----------
  function rate25() { const pr = state && state.pricing && state.pricing.video && state.pricing.video["2.5"]; return pr ? pr.credits_per_second : {"480p": 28, "720p": 63, "1080p": 158}; }
  function renderPills() {
    $("pillAspect").innerHTML = ICON.ratio + `<span>${settings.aspect}</span>`;
    $("pillRes").innerHTML = ICON.res + `<span>${settings.resolution}</span>`;
    $("pillCount").innerHTML = ICON.stack + `<span>${settings.count} ${settings.count > 1 ? "stills" : "still"}</span>`;
    const draft = settings.vres === "480p";
    $("pillDur").innerHTML = ICON.clock + `<span>${settings.duration}s</span>`;
    $("pillQual").innerHTML = ICON.hd + `<span>${draft ? "Draft 480p" : settings.vres}</span>`;
    $("pillDrafts").innerHTML = ICON.stack + `<span>${draft ? settings.count + (settings.count > 1 ? " drafts" : " draft") : "1 clip"}</span>`;
    $("pillDrafts").disabled = !draft;
    $("pillVAspect").innerHTML = ICON.ratio + `<span>${settings.vaspect}</span>`;
    $("pillAudio").textContent = $("audio").checked ? "Audio on" : "Audio off";
    $("pillAudio").classList.toggle("on", $("audio").checked);
    $("pillCam").classList.toggle("on", $("cameraFixed").checked);
    const r = rate25();
    $("vresNote").textContent = draft
      ? `Drafts are cheap previews. Finalize the keeper at 1080p with the same motion for about ${fmtUsd(r["1080p"] * 0.005)}/s. Drafts stay valid 7 days.`
      : `Direct render at ${settings.vres}. No cheap preview first.`;
  }
  const menuFor = {
    pillAspect: () => ["9:16", "16:9", "1:1", "4:5", "auto"].map(v => ({v, label: v, sub: {"9:16": "Shorts, Reels", "16:9": "YouTube", "1:1": "Feed", "4:5": "Portrait feed", "auto": "Model decides"}[v], on: settings.aspect === v, key: "aspect"})),
    pillRes: () => { const m = state && state.pricing && state.pricing.image[$("imgModel").value]; return ["1K", "2K", "4K"].map(v => ({v, label: v, sub: m ? `${m.credits[v]} cr` : "", on: settings.resolution === v, key: "resolution"})); },
    pillCount: () => [1, 2, 3, 4].map(v => ({v, label: `${v} ${v > 1 ? "stills" : "still"}`, on: settings.count === v, key: "count"})),
    pillDur: () => [4, 5, 6, 8, 10, 12, 15].map(v => ({v, label: `${v} seconds`, on: settings.duration === v, key: "duration"})),
    pillQual: () => { const r = rate25(); return [{v: "480p", label: "Draft 480p", sub: `${r["480p"]} cr/s`}, {v: "720p", label: "720p direct", sub: `${r["720p"]} cr/s`}, {v: "1080p", label: "1080p direct", sub: `${r["1080p"]} cr/s`}].map(o => ({...o, on: settings.vres === o.v, key: "vres"})); },
    pillDrafts: () => [1, 2, 3, 4].map(v => ({v, label: `${v} ${v > 1 ? "drafts" : "draft"}`, on: settings.count === v, key: "count"})),
    pillVAspect: () => ["9:16", "16:9", "1:1", "4:3", "3:4", "21:9"].map(v => ({v, label: v, on: settings.vaspect === v, key: "vaspect"})),
  };
  Object.keys(menuFor).forEach(id => $(id).onclick = (e) => { e.stopPropagation(); openMenu($(id), menuFor[id](), it => { settings[it.key] = it.v; if (it.key === "vres" && it.v !== "480p") settings.count = 1; renderPills(); estimate(); }); });
  $("pillAudio").onclick = () => { $("audio").checked = !$("audio").checked; renderPills(); };
  $("pillCam").onclick = () => { $("cameraFixed").checked = !$("cameraFixed").checked; renderPills(); };
  $("imgModel").onchange = () => { renderRefs(); estimate(); };

  // ---------- cost ----------
  function estimate() {
    if (!state) return;
    const pr = state.pricing || {}, per = state.usd_per_credit || 0.005;
    let credits = 0, label = "";
    if (kind === "image") {
      const m = pr.image && pr.image[$("imgModel").value];
      credits = m ? (m.credits[settings.resolution] || 0) * settings.count : 0;
      label = `${settings.count} still${settings.count > 1 ? "s" : ""} · ${settings.resolution} · ${settings.aspect}`;
      $("goLabel").textContent = settings.count > 1 ? `Generate ${settings.count}` : "Generate";
    } else {
      const r = rate25(), draft = settings.vres === "480p", n = draft ? settings.count : 1;
      const refS = vmode === "reference" && refVideo ? refVideo.seconds || 0 : 0;
      const pr25 = state.pricing && state.pricing.video && state.pricing.video["2.5"], rv = (pr25 && pr25.credits_per_second_with_video) || {"480p": 17, "720p": 38, "1080p": 95};
      // with a reference clip kie.ai bills input + output seconds at a lower rate
      credits = Math.round(refS ? (rv[settings.vres] || 0) * (settings.duration + refS) : (r[settings.vres] || 0) * settings.duration) * n;
      label = (draft ? `${n} draft${n > 1 ? "s" : ""} · ${settings.duration}s · 480p` : `${settings.duration}s · ${settings.vres}`) + (refS ? ` · +${Math.round(refS)}s clip in` : "");
      $("goLabel").textContent = draft ? (n > 1 ? `Make ${n} drafts` : "Make draft") : `Render ${settings.vres}`;
    }
    const left = state.spend ? Math.min(state.spend.remaining_today_usd, state.spend.remaining_month_usd) : null;
    $("cost").innerHTML = `${ICON.spark}${credits} · ${fmtUsd(credits * per)}`;
    $("costLabel").textContent = label + (left != null ? ` · ${fmtUsd(left)} left under cap` : "");
  }

  // ---------- presets and prompt helper ----------
  async function loadPresets() {
    try { presets = await (await fetch("/presets.json", {cache: "no-store"})).json(); } catch { presets = {}; }
    $("preset").innerHTML = `<option value="">No preset</option>` + Object.entries(presets).map(([k, p]) => `<option value="${esc(k)}">${esc(p.label || k)}</option>`).join("");
  }
  function preset() { return presets[$("preset").value] || null; }
  function renderPresetCard() {
    const p = preset();
    $("presetName").textContent = p ? p.label : (kind === "image" ? "Free prompt" : "Free motion");
    const last = state && state.generations.find(g => g.kind === "image" && g.url && g.rating !== "reject");
    $("presetBg").style.backgroundImage = last ? `url("${last.url}")` : "";
  }
  $("presetChange").onclick = (e) => {
    e.stopPropagation();
    const items = [{v: "", label: "No preset", sub: "free prompt", on: !$("preset").value}].concat(Object.entries(presets).map(([k, p]) => ({v: k, label: p.label || k, sub: p.aspect || "", on: $("preset").value === k})));
    openMenu($("presetChange"), items, it => { $("preset").value = it.v; applyPreset(); });
  };
  function applyPreset() {
    const p = preset();
    $("presetFrag").textContent = p ? "Preset adds: " + (kind === "image" ? p.still : p.motion) : "";
    renderPresetCard();
    if (!p) { compose(); return; }
    if (p.aspect) settings.aspect = p.aspect;
    if (p.image_model) $("imgModel").value = p.image_model;
    if (p.duration) settings.duration = p.duration;
    $("cameraFixed").checked = !!p.camera_fixed;
    if (p.negative && !$("negative").value) $("negative").value = p.negative;
    renderPills(); manualPrompt = false; compose(); estimate();
  }
  const join = (parts) => parts.map(s => (s || "").trim()).filter(Boolean).join(", ");
  function compose(force) {
    if (manualPrompt && !force) return;
    const p = preset();
    const text = kind === "image"
      ? join([$("f_subject").value, $("f_setting").value, p && p.still, $("f_camera").value, $("f_light").value, $("f_mood").value, $("f_details").value])
      : join([$("m_camera").value, $("m_subject").value, $("m_atmo").value, p && p.motion, $("m_keep").value ? "keep: " + $("m_keep").value : ""]);
    if (text || force) $("prompt").value = text;
  }
  ["f_subject", "f_setting", "f_camera", "f_light", "f_mood", "f_details", "m_camera", "m_subject", "m_atmo", "m_keep"].forEach(id => $(id).addEventListener("input", () => { manualPrompt = false; compose(); }));
  $("prompt").addEventListener("input", () => { manualPrompt = true; });
  $("rebuild").onclick = () => { manualPrompt = false; compose(true); };
  $("helperBtn").onclick = () => { const h = $("helper"); h.classList.toggle("hidden"); $("helperBtn").classList.toggle("on", !h.classList.contains("hidden")); };
  function fields() {
    return kind === "image"
      ? {subject: $("f_subject").value, setting: $("f_setting").value, camera: $("f_camera").value, lighting: $("f_light").value, mood: $("f_mood").value, details: $("f_details").value}
      : {camera: $("m_camera").value, subject: $("m_subject").value, atmosphere: $("m_atmo").value, keep: $("m_keep").value};
  }
  function setFields(f) {
    const map = kind === "image" ? {subject: "f_subject", setting: "f_setting", camera: "f_camera", lighting: "f_light", mood: "f_mood", details: "f_details"} : {camera: "m_camera", subject: "m_subject", atmosphere: "m_atmo", keep: "m_keep"};
    Object.entries(map).forEach(([k, id]) => $(id).value = (f && f[k]) || "");
  }
  function setKind(k) {
    if (k !== kind) settings.count = 1;
    kind = k;
    $("kindImage").classList.toggle("on", k === "image"); $("kindVideo").classList.toggle("on", k === "video");
    ["imgModel", "imageOptions", "imageSettings", "imageBuilder"].forEach(id => $(id).classList.toggle("hidden", k !== "image"));
    ["vidModel", "videoSettings", "videoBuilder", "approveWrap", "vresNote", "pillAudio", "pillCam", "vmodes"].forEach(id => $(id).classList.toggle("hidden", k !== "video"));
    applyVMode();
    $("prompt").placeholder = k === "image"
      ? "Describe the shot. Example: a Roman senator waking up in a marble villa at dawn, first-person POV, warm light, no text"
      : "Describe the motion and sound. Example: slow push-in, curtains move in the breeze, distant seagulls, keep the face in frame";
    manualPrompt = false; applyPreset(); renderSource(); renderPills(); updateGo(); estimate();
  }
  // ---------- video modes ----------
  // Seedance 2.5 takes a start frame, a start + end frame, or references (characters, images, a clip). Never a mix.
  function applyVMode() {
    const v = kind === "video";
    $("vmodes").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.m === vmode));
    $("videoOptions").classList.toggle("hidden", !v || vmode === "reference");
    $("endOptions").classList.toggle("hidden", !v || vmode !== "first-last");
    $("refVideoBlock").classList.toggle("hidden", !v || vmode !== "reference");
    $("imageOptions").classList.toggle("hidden", v && vmode !== "reference");
    $("pillVAspect").classList.toggle("hidden", !v || vmode !== "reference");
    $("pillCam").classList.toggle("hidden", !v || vmode === "reference");
    renderCastPick(); renderEnd(); renderRefVideo(); renderRefs(); estimate();
  }
  $("vmodes").querySelectorAll("button").forEach(b => b.onclick = () => { vmode = b.dataset.m; applyVMode(); });

  function updateGo() { $("goBtn").disabled = kind === "video" && !$("approve").checked; }
  $("kindImage").onclick = () => setKind("image"); $("kindVideo").onclick = () => setKind("video");
  $("approve").onchange = updateGo;
  $("clearBtn").onclick = () => { $("form").reset(); $("audio").checked = true; refSel.length = 0; castSel.length = 0; endSource = null; refVideo = null; source = null; parentId = null; settings.count = 1; manualPrompt = false; vmode = "first"; applyPreset(); renderSource(); applyVMode(); renderPills(); $("formMsg").textContent = ""; updateGo(); estimate(); };

  // ---------- references and uploads ----------
  function renderRefs() {
    const host = $("refs"); if (!state) return;
    const lib = [...(state.refs || [])];
    const known = new Set(lib.map(r => refOf(r)));
    refSel.forEach(r => { if (!known.has(r.ref)) lib.unshift({name: r.name, url: r.url, path: r.path}); });
    host.innerHTML = lib.slice(0, 11).map(r => { const ref = refOf(r), on = refSel.some(s => s.ref === ref); return `<div class="ref ${on ? "on" : ""}" data-ref="${esc(ref)}" data-url="${esc(r.url)}" data-name="${esc(r.name)}" data-path="${esc(r.path || "")}" title="${esc(r.name)}"><img src="${esc(r.url)}" loading="lazy" alt=""></div>`; }).join("")
      + `<div class="ref add" id="refAdd" title="Upload images">${ICON.plus}</div>` + (lib.length ? "" : `<div class="hint">Drop faces, products or styles here. Up to 3 per still.</div>`);
    host.querySelectorAll(".ref:not(.add)").forEach(el => el.onclick = () => toggleRef({ref: el.dataset.ref, url: el.dataset.url, name: el.dataset.name, path: el.dataset.path || null}));
    $("refAdd").onclick = () => $("refFile").click();
    $("refHint").textContent = kind === "video" || $("imgModel").value === "nano-banana-2" ? `${refSel.length}/3 selected` : "not used by GPT Image 2";
  }
  function toggleRef(r) {
    const i = refSel.findIndex(s => s.ref === r.ref);
    if (i >= 0) refSel.splice(i, 1); else { if (refSel.length >= 3) { toast("Up to 3 reference images."); return; } refSel.push(r); if (kind === "image" && $("imgModel").value !== "nano-banana-2") { $("imgModel").value = "nano-banana-2"; toast("Switched to Nano Banana 2, which accepts references."); } }
    renderRefs();
  }
  const UPLOAD_LIMIT = 3.0 * 1024 * 1024;
  function fileToDataUrl(file) { return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = () => rej(new Error("could not read the file")); r.readAsDataURL(file); }); }
  function loadImage(dataUrl) { return new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = () => rej(new Error("the browser cannot decode this image (HEIC and RAW are not supported; export it as JPEG or PNG)")); i.src = dataUrl; }); }
  async function downscale(dataUrl, max = 2048, quality = 0.9) {
    const img = await loadImage(dataUrl);
    const scale = Math.min(1, max / Math.max(img.width, img.height));
    const c = document.createElement("canvas"); c.width = Math.max(1, Math.round(img.width * scale)); c.height = Math.max(1, Math.round(img.height * scale));
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    return c.toDataURL("image/jpeg", quality);
  }
  async function fitForUpload(dataUrl, keepPng) {
    const img = await loadImage(dataUrl), longSide = Math.max(img.width, img.height);
    if (keepPng && dataUrl.length <= UPLOAD_LIMIT && longSide <= 2048) return dataUrl;
    let max = Math.min(2048, longSide), q = 0.9;
    for (let i = 0; i < 8; i++) { const out = await downscale(dataUrl, max, q); if (out.length <= UPLOAD_LIMIT) return out; q -= 0.1; if (q < 0.6) { q = 0.85; max = Math.round(max * 0.75); } }
    throw new Error("the image is too large even after downscaling");
  }
  async function uploadImage(file, name, kindOfUpload) {
    if (/hei[cf]/i.test(file.type) || /\.hei[cf]$/i.test(file.name)) throw new Error("HEIC photos are not supported by the browser. Export as JPEG or PNG first.");
    if (!/^image\//.test(file.type)) throw new Error("not an image file");
    const r = await api("/api/upload", {name, data: await fitForUpload(await fileToDataUrl(file), /^image\/(png|jpeg)/.test(file.type)), kind: kindOfUpload});
    if (!r.ok) throw new Error(r.error || "upload failed");
    return r;
  }
  async function uploadDataUrl(dataUrl, name, kindOfUpload) {
    const r = await api("/api/upload", {name, data: await fitForUpload(dataUrl, true), kind: kindOfUpload});
    if (!r.ok) throw new Error(r.error || "upload failed");
    return r;
  }
  async function addRefFiles(files) {
    for (const f of files) {
      try { toast(`Uploading ${f.name}…`, 8000); const up = await uploadImage(f, f.name.replace(/\.[^.]+$/, ""), "ref"); state.refs = state.refs || []; state.refs.unshift({name: up.name, url: up.url, path: up.path}); toggleRef({ref: refOf(up), url: up.url, name: up.name, path: up.path || null}); toast(`Uploaded ${up.name}`); }
      catch (err) { toast("Upload failed: " + err.message, 6000); }
    }
  }
  $("refFile").onchange = async (ev) => { await addRefFiles([...ev.target.files]); ev.target.value = ""; };
  function dropZone(el, onFiles) {
    el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("over"); });
    el.addEventListener("dragleave", () => el.classList.remove("over"));
    el.addEventListener("drop", (e) => { e.preventDefault(); el.classList.remove("over"); onFiles([...(e.dataTransfer.files || [])]); });
  }
  dropZone($("refs"), addRefFiles);

  // ---------- start frame (video) ----------
  function renderSource() {
    const host = $("source");
    if (!source) { host.innerHTML = `<div class="hint">Press <b>Animate</b> on an image in History, drop an image here, or paste a URL below.</div>`; return; }
    host.innerHTML = `<div class="srcrow"><img src="${esc(source.url)}" alt=""><div class="grow"><div class="ellip">${esc(source.name)}</div><div class="muted ellip">${esc(source.prompt || "")}</div></div><button type="button" class="btn ic" id="sourceClear" title="Remove">${ICON.x}</button></div>`;
    $("still").value = refOf(source);
    $("sourceClear").onclick = () => { source = null; $("still").value = ""; renderSource(); };
  }
  $("still").addEventListener("input", () => { if (source) { source = null; renderSource(); } });
  dropZone($("source"), async (files) => {
    const f = files[0]; if (!f) return;
    try { toast(`Uploading ${f.name}…`, 8000); const up = await uploadImage(f, f.name.replace(/\.[^.]+$/, ""), "still"); source = {name: up.name, url: up.url, path: up.path, prompt: ""}; renderSource(); toast("Start frame ready."); setTimeout(load, 500); }
    catch (err) { toast("Upload failed: " + err.message, 6000); }
  });

  // ---------- end frame and reference clip (video) ----------
  function renderEnd() {
    const host = $("endSource");
    if (!endSource) { host.innerHTML = `<div class="hint">Drop the final frame here, or use <b>Use as end frame</b> on an image. The clip moves from the start frame to this one.</div>`; return; }
    host.innerHTML = `<div class="srcrow"><img src="${esc(endSource.url)}" alt=""><div class="grow"><div class="ellip">${esc(endSource.name)}</div><div class="muted ellip">${esc(endSource.prompt || "")}</div></div><button type="button" class="btn ic" id="endClear" title="Remove">${ICON.x}</button></div>`;
    $("endStill").value = refOf(endSource);
    $("endClear").onclick = () => { endSource = null; $("endStill").value = ""; renderEnd(); };
  }
  $("endStill").addEventListener("input", () => { if (endSource) { endSource = null; renderEnd(); } });
  dropZone($("endSource"), async (files) => {
    const f = files[0]; if (!f) return;
    try { toast(`Uploading ${f.name}…`, 8000); const up = await uploadImage(f, f.name.replace(/\.[^.]+$/, ""), "still"); endSource = {name: up.name, url: up.url, path: up.path, prompt: ""}; renderEnd(); toast("End frame ready."); setTimeout(load, 500); }
    catch (err) { toast("Upload failed: " + err.message, 6000); }
  });
  function renderRefVideo() {
    const host = $("refVideo");
    if (!refVideo) { host.innerHTML = `<div class="hint">Optional. Use <b>Extend with cast</b> on a clip to continue it with the same people. Clip seconds are billed at a lower rate.</div>`; return; }
    host.innerHTML = `<div class="srcrow"><video src="${esc(refVideo.url)}" muted playsinline preload="metadata" style="width:44px;height:60px;object-fit:cover;border-radius:6px"></video><div class="grow"><div class="ellip">${esc(refVideo.name)}</div><div class="muted">${refVideo.seconds ? refVideo.seconds.toFixed(1) + "s · @Video1" : "@Video1"}</div></div><button type="button" class="btn ic" id="rvClear" title="Remove">${ICON.x}</button></div>`;
    $("rvClear").onclick = () => { refVideo = null; renderRefVideo(); estimate(); };
  }

  // ---------- cast (characters) ----------
  const chars = () => (state && state.characters) || [];
  const charById = (id) => chars().find(c => c.id === id);
  const avatar = (c) => (c.image_urls || [])[0] || "";
  function renderCastPick() {
    const host = $("castPick"); if (!state) return;
    const locked = kind === "video" && vmode !== "reference";
    $("castBlock").classList.toggle("hidden", locked && !chars().length);
    host.innerHTML = locked ? "" : chars().length
      ? chars().map(c => `<button type="button" class="cchip ${castSel.includes(c.id) ? "on" : ""}" data-id="${esc(c.id)}"><img src="${esc(avatar(c))}" alt="">${esc(c.name)} <span class="t">@${esc(c.tag)}</span></button>`).join("")
      : `<div class="hint muted">No characters yet. Open <b>Cast</b>, or press <b>Add to character</b> on any image.</div>`;
    host.querySelectorAll(".cchip").forEach(b => b.onclick = () => toggleCast(b.dataset.id));
    $("castHint").textContent = !locked && castSel.length ? `${castSel.length} in shot` : "";
    $("castNote").textContent = locked
      ? "Seedance can't take characters with a start frame. Put the cast into the start and end stills (Create Image), or switch to Cast."
      : castSel.length ? `Write @${(charById(castSel[0]) || {}).tag || "tag"} in the prompt to place them. Their photos are sent first.` : "";
  }
  function toggleCast(id) {
    const i = castSel.indexOf(id);
    if (i >= 0) castSel.splice(i, 1); else castSel.push(id);
    if (castSel.length && kind === "image" && $("imgModel").value !== "nano-banana-2") { $("imgModel").value = "nano-banana-2"; toast("Switched to Nano Banana 2, which takes character photos."); }
    renderCastPick();
  }
  const editor = {id: null, images: []};
  function openEditor(c, extraImages) {
    editor.id = c ? c.id : null;
    editor.images = c ? (c.images || []).map((ref, k) => ({ref, url: (c.image_urls || [])[k] || ref})) : [];
    (extraImages || []).forEach(x => { if (!editor.images.some(e => e.ref === x.ref)) editor.images.push(x); });
    $("ceName").value = c ? c.name : ""; $("ceDesc").value = c ? c.description || "" : "";
    $("ceDelete").classList.toggle("hidden", !c); $("castEditor").classList.remove("hidden");
    renderEditor(); showView("cast"); $("ceName").focus();
  }
  function renderEditor() {
    const tag = editor.id ? (charById(editor.id) || {}).tag : ($("ceName").value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24).replace(/-+$/, "") || "…");
    $("ceTag").textContent = "@" + tag;
    $("ceImgs").innerHTML = editor.images.map((im, k) => `<div class="ref" title="${k ? "" : "main photo"}"><img src="${esc(im.url)}" alt=""><button type="button" class="rm" data-k="${k}">${ICON.x}</button></div>`).join("")
      + `<div class="ref add" id="ceAdd" title="Upload photos">${ICON.plus}</div>`;
    $("ceImgs").querySelectorAll(".rm").forEach(b => b.onclick = () => { editor.images.splice(+b.dataset.k, 1); renderEditor(); });
    $("ceAdd").onclick = () => $("ceFile").click();
  }
  async function addEditorFiles(files) {
    for (const f of files) {
      try { toast(`Uploading ${f.name}…`, 8000); const up = await uploadImage(f, f.name.replace(/\.[^.]+$/, ""), "ref"); editor.images.push({ref: refOf(up), url: up.url}); renderEditor(); toast(`Added ${up.name}`); }
      catch (err) { toast("Upload failed: " + err.message, 6000); }
    }
  }
  $("ceFile").onchange = async (ev) => { await addEditorFiles([...ev.target.files]); ev.target.value = ""; };
  dropZone($("ceImgs"), addEditorFiles);
  $("ceName").addEventListener("input", renderEditor);
  $("castNew").onclick = () => openEditor(null);
  $("ceCancel").onclick = () => $("castEditor").classList.add("hidden");
  $("ceSave").onclick = async () => {
    const r = await api("/api/character", {id: editor.id, name: $("ceName").value, description: $("ceDesc").value, images: editor.images.map(i => i.ref)});
    if (!r.ok) { toast(r.error || "could not save", 5000); return; }
    $("castEditor").classList.add("hidden"); toast(`Saved @${r.character.tag}`); await load();
  };
  $("ceDelete").onclick = async () => {
    if (!editor.id || !confirm("Delete this character? Past generations stay.")) return;
    const r = await api("/api/character", {id: editor.id, delete: true});
    if (!r.ok) { toast(r.error || "could not delete", 5000); return; }
    const i = castSel.indexOf(editor.id); if (i >= 0) castSel.splice(i, 1);
    $("castEditor").classList.add("hidden"); await load();
  };
  function renderCast() {
    const host = $("castGrid"); if (!state) return;
    const list = chars();
    $("castCount").textContent = `${list.length} character${list.length === 1 ? "" : "s"}`;
    if (!list.length) { host.innerHTML = `<div class="empty" style="grid-column:1/-1"><b>No characters yet</b>Start with Reiks: four clean photos and one line about him.</div>`; return; }
    host.innerHTML = list.map(c => {
      const used = state.generations.filter(g => (g.characters || []).includes(c.id)).length;
      const imgs = (c.image_urls || []).slice(0, 5);
      return `<div class="ccard" data-id="${esc(c.id)}"><div class="strip">${imgs.map(u => `<img src="${esc(u)}" alt="">`).join("")}</div>
        <div class="body"><div class="nm">${esc(c.name)}</div><div class="tg">@${esc(c.tag)}</div><div class="sub">${esc(c.description || "No identity line yet.")}</div>
        <div class="muted">${(c.images || []).length} photo${(c.images || []).length === 1 ? "" : "s"} · used in ${used}</div>
        <div class="actions"><button type="button" class="btn primary" data-c="still">Still</button><button type="button" class="btn" data-c="clip">Clip</button><button type="button" class="btn" data-c="edit">Edit</button></div></div></div>`;
    }).join("");
    host.querySelectorAll(".ccard [data-c]").forEach(b => b.onclick = () => {
      const c = charById(b.closest(".ccard").dataset.id); if (!c) return;
      if (b.dataset.c === "edit") return openEditor(c);
      if (!castSel.includes(c.id)) castSel.push(c.id);
      if (b.dataset.c === "still") { setKind("image"); $("imgModel").value = "nano-banana-2"; renderRefs(); estimate(); } else { setKind("video"); vmode = "reference"; applyVMode(); }
      manualPrompt = true; if (!$("prompt").value.includes("@" + c.tag)) $("prompt").value = `@${c.tag} ` + $("prompt").value;
      renderCastPick(); toCreate(); $("prompt").focus();
    });
  }
  function addToCharacter(g, el) {
    const im = {ref: refOf(g), url: g.url};
    const items = [{v: "", label: "New character", sub: "starts with this image"}].concat(chars().map(c => ({v: c.id, label: c.name, sub: `@${c.tag} · ${(c.images || []).length} photos`})));
    openMenu(el, items, async it => {
      if (!it.v) { closeViewer(); openEditor(null, [im]); return; }
      const c = charById(it.v);
      if ((c.images || []).includes(im.ref)) { toast("Already in this character."); return; }
      if ((c.images || []).length >= 8) { toast(`@${c.tag} already has 8 photos. Remove one in Cast first.`, 5000); return; }
      const r = await api("/api/character", {id: c.id, name: c.name, description: c.description, images: (c.images || []).concat([im.ref])});
      if (r.ok) { toast(`Added to @${c.tag}`); load(); } else toast(r.error || "could not save", 5000);
    });
  }

  // ---------- generate ----------
  function payload() {
    const base = {kind, prompt: $("prompt").value, negative: $("negative").value, fields: fields(), preset: $("preset").value || null, override_cap: $("overrideCap").checked, parent_id: parentId};
    if (kind === "image") return {...base, model: $("imgModel").value, aspect: settings.aspect, resolution: settings.resolution, count: settings.count, name: $("imgName").value || null, refs: refSel.map(r => r.ref), characters: [...castSel]};
    const draft = settings.vres === "480p";
    const modeFields = vmode === "reference"
      ? {characters: [...castSel], refs: refSel.map(r => r.ref), ref_video: refVideo ? refVideo.ref : null, ref_video_s: refVideo ? refVideo.seconds : null, aspect: settings.vaspect}
      : {image: $("still").value, end_image: vmode === "first-last" ? $("endStill").value : null};
    return {...base, mode: vmode, ...modeFields, model: "2.5", duration: settings.duration, resolution: settings.vres, draft, count: draft ? settings.count : 1, audio: $("audio").checked, seed: $("seed").value, camera_fixed: $("cameraFixed").checked, name: $("vidName").value || null, approve: $("approve").checked};
  }
  async function submit(p) {
    const j = await api("/api/generate", p);
    if (j.ok && j.jobs) { j.jobs.forEach(job => { jobs.set(job.id, job); pollJob(job.id); }); renderFeed(); return j.jobs; }
    throw new Error(j.error || "failed");
  }
  $("form").onsubmit = async (ev) => {
    ev.preventDefault();
    const p = payload();
    if (!p.prompt.trim()) { $("formMsg").textContent = "Write a prompt first."; $("prompt").focus(); return; }
    if (kind === "video" && vmode !== "reference" && !(p.image || "").trim()) { $("formMsg").textContent = "Add a start frame first."; return; }
    if (kind === "video" && vmode === "first-last" && !(p.end_image || "").trim()) { $("formMsg").textContent = "Add an end frame first."; return; }
    if (kind === "video" && vmode === "reference" && !p.characters.length && !p.refs.length && !p.ref_video) { $("formMsg").textContent = "Pick a character, a reference image or a clip."; return; }
    if (kind === "video" && vmode === "reference" && p.ref_video && !(p.ref_video_s > 0) && state.mode === "vercel") { $("formMsg").textContent = "The reference clip's length is unknown, so it can't be priced. Remove it or pick another clip."; return; }
    $("formMsg").textContent = "Submitting…"; $("goBtn").disabled = true;
    try { const js = await submit(p); $("formMsg").textContent = js.length > 1 ? `${js.length} started.` : "Started."; $("approve").checked = false; parentId = null; showView("create"); }
    catch (e) { $("formMsg").textContent = e.message; }
    updateGo();
  };

  // ---------- item helpers ----------
  const RATIOS = [[9, 16], [16, 9], [1, 1], [4, 5], [3, 4], [4, 3], [21, 9], [2, 3], [3, 2]];
  function ratioOf(g) {
    if (g.params && g.params.aspect_ratio && g.params.aspect_ratio !== "adaptive") return g.params.aspect_ratio;
    const m = g.media; if (!m || !m.width) return "";
    const r = m.width / m.height; let best = RATIOS[0], d = 9;
    RATIOS.forEach(x => { const dd = Math.abs(x[0] / x[1] - r); if (dd < d) { d = dd; best = x; } });
    return best.join(":");
  }
  function fmtDate(s) { if (!s) return ""; const d = new Date(s); return isNaN(d) ? s.slice(0, 16) : d.toLocaleDateString(undefined, {month: "short", day: "numeric", year: "numeric"}) + " · " + d.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit"}); }
  function thumbUrl(ref) {
    if (!ref) return null;
    if (/^https?:/.test(ref)) return ref;
    const hit = (state.generations || []).concat(state.refs || []).find(x => x.path === ref || x.name === ref);
    if (hit) return hit.url;
    const clip = (state.generations || []).find(x => x.last_frame_path && x.last_frame_path === ref);
    return clip ? clip.last_frame_url : null;
  }
  function restoreCast(g) { castSel.length = 0; (g.characters || []).forEach(id => { if (charById(id)) castSel.push(id); }); }
  function restoreRefs(g) {
    // older hosted records stored the character photos in refs too; keep only the user's own
    const charImgs = new Set((g.characters || []).flatMap(id => (charById(id) || {}).images || []));
    refSel.length = 0;
    (g.refs || []).filter(r => !charImgs.has(r)).slice(0, 3).forEach(r => refSel.push({ref: r, url: thumbUrl(r) || r, name: String(r).split(/[\\/]/).pop()}));
  }
  const isDraft = (g) => isVideoItem(g) && g.params && g.params.draft;
  const finalOf = (g) => state.generations.find(x => x.parent_id === g.id && x.model === "2.5-final");
  function finalCost(g) { const pr = state.pricing && state.pricing.video["2.5-final"]; return fmtUsd(((pr && pr.credits_per_second) || 158) * ((g.params && g.params.duration_s) || 0) * (state.usd_per_credit || 0.005)); }
  function daysLeft(g) { return g.created_at ? Math.max(0, Math.ceil((new Date(g.created_at).getTime() + 7 * 864e5 - Date.now()) / 864e5)) : null; }
  function chipsOf(g) {
    const c = [], res = g.params && g.params.resolution, dur = (g.media && g.media.duration_s) || (g.params && g.params.duration_s);
    c.push(`<span class="chip acc">${esc(MODEL_LABEL[g.model] || g.model || g.kind)}</span>`);
    const mode = g.params && g.params.mode;
    if (mode === "first-last") c.push(`<span class="chip">start → end</span>`);
    if (mode === "reference") c.push(`<span class="chip">${g.ref_video ? "extended" : "cast"}</span>`);
    (g.characters || []).forEach(id => { const ch = state && charById(id); c.push(`<span class="chip">@${esc(ch ? ch.tag : id)}</span>`); });
    if (res) c.push(`<span class="chip">${esc(res)}</span>`);
    if (dur) c.push(`<span class="chip">${Number(dur).toFixed(1)}s</span>`);
    const r = ratioOf(g); if (r) c.push(`<span class="chip">${r}</span>`);
    if (g.usd != null) c.push(`<span class="chip">${fmtUsd(g.usd)}</span>`);
    if (isDraft(g)) { const dl = daysLeft(g); c.push(`<span class="chip">draft${dl != null ? " · " + dl + "d left" : ""}</span>`); }
    return c.join("");
  }
  function mediaHtml(g, grid) {
    if (isVideoItem(g)) return grid ? `<video src="${esc(g.url)}" muted loop playsinline preload="metadata" class="hoverplay"></video>` : `<video src="${esc(g.url)}" controls playsinline preload="metadata"></video>`;
    return `<img src="${esc(g.url)}" loading="lazy" alt="${esc(g.prompt || g.name)}">`;
  }
  function primaryAction(g) {
    if (g.kind === "image") return `<button class="btn primary act" data-a="animate">${ICON.play}Animate</button>`;
    const f = isDraft(g) ? finalOf(g) : null;
    if (isDraft(g) && !f && daysLeft(g) > 0) return `<button class="btn primary act" data-a="finalize">${ICON.hd}Finalize 1080p · ${finalCost(g)}</button>`;
    if (f) return `<button class="btn act" data-a="showfinal">${ICON.hd}1080p ready</button>`;
    return "";
  }
  function iconActions(g) {
    return `<button class="btn ic act star ${g.rating === "star" ? "on" : ""}" data-a="star" title="Star">${ICON.star}</button>`
      + `<button class="btn ic act rej ${g.rating === "reject" ? "on" : ""}" data-a="reject" title="Reject">${ICON.x}</button>`
      + `<button class="btn ic act" data-a="download" title="Save">${ICON.save}</button>`
      + `<button class="btn ic act" data-a="more" title="More">${ICON.dots}</button>`;
  }
  function moreItems(g) {
    const it = [];
    if (g.kind === "image") it.push({a: "endframe", label: "Use as end frame"}, {a: "makeend", label: "Make an end frame from this"}, {a: "tochar", label: "Add to character"}, {a: "tweak", label: "Rerun with edits"}, {a: "ref", label: "Use as reference"}, {a: "reframe169", label: "Make a 16:9 version"}, {a: "reframe11", label: "Make a 1:1 version"}, {a: "hires", label: `Make a ${((g.params && g.params.resolution) || "1K") === "1K" ? "2K" : "4K"} version`});
    else it.push({a: "redo", label: "Rerun with edits"}, {a: "extend", label: "Extend from last frame"}, {a: "extendcast", label: "Extend with cast (keeps faces)"});
    it.push({a: "note", label: g.note ? "Edit note" : "Add note"}, {a: "cmp", label: cmpSel.includes(g.id) ? "Remove from compare" : "Select to compare"});
    if (g.path) it.push({a: "copy", label: "Copy file path"});
    it.push({a: "open", label: "Open original"});
    return it.map(x => ({...x, sub: ""}));
  }
  function rowHtml(g) {
    const refs = (g.refs || []).concat(g.input_image ? [g.input_image] : [], g.end_image ? [g.end_image] : []).map(thumbUrl).filter(Boolean).slice(0, 4);
    return `<div class="grow-row" data-id="${esc(g.id)}">
      <div class="rowmedia act" data-a="view">${g.variant ? `<span class="badge">v${g.variant}</span>` : ""}${g.model === "2.5-final" ? `<span class="badge r">1080p final</span>` : isDraft(g) ? `<span class="badge r">draft 480p</span>` : ""}${mediaHtml(g, false)}</div>
      <div class="rowinfo">
        ${refs.length ? `<div class="thumbs">${refs.map(u => `<img src="${esc(u)}" alt="">`).join("")}</div>` : ""}
        <div class="chips">${chipsOf(g)}</div>
        <div class="muted">${esc(fmtDate(g.created_at || g.modified))}</div>
        <div class="prompt" title="${esc(g.prompt || "")}">${esc(g.prompt || g.name)}</div>
        ${g.note ? `<div class="inote">${esc(g.note)}</div>` : ""}
        <div class="actions"><button class="btn act" data-a="${g.kind === "image" ? "tweak" : "redo"}">${ICON.rerun}Rerun</button>${primaryAction(g)}</div>
        <div class="actions" style="margin-top:0">${iconActions(g)}</div>
      </div></div>`;
  }
  function setRowHtml(gs, all) {
    const g0 = gs[0], picked = all.find(x => x.rating === "star"), noun = isVideoItem(g0) ? "drafts" : "stills";
    return `<div class="grow-row">
      <div class="rowmedia multi">${gs.map(g => `<div class="tile ${g.rating === "star" ? "starred" : ""} ${g.rating === "reject" ? "rejected" : ""}" data-id="${esc(g.id)}"><span class="badge">v${g.variant || ""}</span><div class="act" data-a="view" style="width:100%;height:100%">${mediaHtml(g, true)}</div><div class="tbar"><button class="btn ic act" data-a="pick" title="Pick this one">${ICON.star}</button><button class="btn ic act" data-a="download" title="Save">${ICON.save}</button><button class="btn ic act" data-a="more" title="More">${ICON.dots}</button></div></div>`).join("")}</div>
      <div class="rowinfo" data-id="${esc(g0.id)}">
        <div class="chips">${chipsOf(g0)}<span class="chip">${all.length} ${noun}</span></div>
        <div class="muted">${esc(fmtDate(g0.created_at || g0.modified))}</div>
        <div class="prompt">${esc(g0.prompt || "")}</div>
        <div class="note">${picked ? `Picked v${picked.variant || ""}. ` : ""}Hover a ${noun.slice(0, -1)} and press ★ to pick it; the others are rejected.</div>
        <div class="actions"><button class="btn act" data-a="${g0.kind === "image" ? "tweak" : "redo"}">${ICON.rerun}Rerun</button>${picked ? primaryAction(picked).replace('class="btn', `data-id="${esc(picked.id)}" class="btn`) : ""}</div>
      </div></div>`;
  }
  function gridCard(g) {
    return `<div class="gcard ${g.rating === "star" ? "starred" : ""} ${g.rating === "reject" ? "rejected" : ""}" data-id="${esc(g.id)}">
      ${g.variant ? `<span class="badge">v${g.variant}</span>` : ""}${g.model === "2.5-final" ? `<span class="badge r">1080p</span>` : isDraft(g) ? `<span class="badge r">draft</span>` : ""}
      <div class="act" data-a="view">${mediaHtml(g, true)}</div>
      <div class="ov"><div class="p">${esc(g.prompt || g.name)}</div><div class="actions">${primaryAction(g)}${iconActions(g)}</div></div></div>`;
  }
  function attach(host) {
    host.querySelectorAll(".act").forEach(el => el.onclick = (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      const holder = el.dataset.id ? el : el.closest("[data-id]");
      action(el.dataset.a, holder && byId(holder.dataset.id), el, host);
    });
    host.querySelectorAll("video.hoverplay").forEach(v => { v.onmouseenter = () => v.play().catch(() => {}); v.onmouseleave = () => { v.pause(); v.currentTime = 0; }; });
  }
  function groupsOf(items) { const m = new Map(); items.forEach(g => { const k = g.group || "_" + g.id; if (!m.has(k)) m.set(k, []); m.get(k).push(g); }); return m; }
  let feedList = [];

  // ---------- history feed ----------
  function renderFeed() {
    const host = $("feed"); if (!state) return;
    let html = "";
    const running = [...jobs.values()].filter(j => j.status === "running").sort((a, b) => b.started_at - a.started_at);
    const byGroup = new Map(); running.forEach(j => { const k = j.group || j.id; if (!byGroup.has(k)) byGroup.set(k, []); byGroup.get(k).push(j); });
    byGroup.forEach(js => { html += `<div class="grow-row"><div class="rowmedia multi">${js.map(j => `<div class="tile skel" style="min-height:280px">${esc(j.kie_state || "running")} · ${Math.round(Date.now() / 1000 - j.started_at)}s</div>`).join("")}</div><div class="rowinfo"><div class="chips"><span class="chip acc">${esc(MODEL_LABEL[js[0].model] || js[0].model || js[0].kind)}</span><span class="chip">${js.length} running</span></div><div class="prompt">${esc(js[0].prompt || "")}</div><div class="note">Results appear here when ready. You can keep working.</div></div></div>`; });
    [...jobs.values()].filter(j => j.status !== "running" && j.status !== "done" && Date.now() / 1000 - (j.finished_at || j.started_at) < 600).forEach(j => {
      const r = j.result || {};
      const msg = j.status === "approval_required" ? `Needs approval: ${fmtUsd(r.usd_est)}. Tick "I approve this paid run" and try again.` : (r.error || j.message || "failed");
      html += `<div class="err"><b>${esc(j.status.replace("_", " "))}</b> · ${esc((j.prompt || "").slice(0, 90))}<div class="muted">${esc(msg)}</div></div>`;
    });
    const items = state.generations.filter(g => !(g.rating === "reject" || g.hidden)).slice(0, 40);
    feedList = items;
    if (!items.length && !html) { host.innerHTML = `<div class="empty"><b>Nothing here yet</b>Write a prompt on the left and press Generate. Stills cost about 4 cents; a 10-second video draft about $1.40.</div>`; return; }
    if (feedMode === "grid") html += `<div class="masonry">${items.map(gridCard).join("")}</div>`;
    else groupsOf(items).forEach((gs, k) => {
      const all = state.generations.filter(x => x.group === k);
      html += (!k.startsWith("_") && all.length > 1) ? setRowHtml(gs, all) : gs.map(rowHtml).join("");
    });
    host.innerHTML = html; attach(host);
  }
  $("feedMode").querySelectorAll("button").forEach(b => { b.classList.toggle("on", b.dataset.m === feedMode); b.onclick = () => { feedMode = b.dataset.m; try { localStorage.setItem("feedMode", feedMode); } catch {} $("feedMode").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); renderFeed(); }; });

  // ---------- library ----------
  let boardList = [];
  function visible(g) {
    if (filter.kind !== "all" && g.kind !== filter.kind) return false;
    if (filter.starred && g.rating !== "star") return false;
    if (!filter.rejected && (g.rating === "reject" || g.hidden)) return false;
    if (filter.q) { const q = filter.q.toLowerCase(); if (!((g.prompt || "").toLowerCase().includes(q) || (g.note || "").toLowerCase().includes(q) || (g.name || "").toLowerCase().includes(q))) return false; }
    return true;
  }
  function renderBoard() {
    const host = $("board"); if (!state) return;
    const items = state.generations.filter(visible); boardList = items;
    $("boardCount").textContent = `${items.length} of ${state.generations.length}`;
    if (!items.length) { host.innerHTML = `<div class="empty"><b>Nothing to show</b>Generate something or change the filters.</div>`; return; }
    const days = new Map();
    items.forEach(g => { const d = new Date(g.created_at || g.modified); const k = isNaN(d) ? "Earlier" : d.toLocaleDateString(undefined, {month: "long", day: "numeric", year: "numeric"}); if (!days.has(k)) days.set(k, []); days.get(k).push(g); });
    let html = ""; days.forEach((gs, k) => { html += `<div class="datehead">${esc(k)}</div><div class="masonry">${gs.map(gridCard).join("")}</div>`; });
    host.innerHTML = html; attach(host);
  }
  $("kindFilter").querySelectorAll("button").forEach(b => b.onclick = () => { filter.kind = b.dataset.f; $("kindFilter").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); renderBoard(); });
  $("starFilter").onclick = () => { filter.starred = !filter.starred; $("starFilter").classList.toggle("primary", filter.starred); renderBoard(); };
  $("showRejected").onclick = () => { filter.rejected = !filter.rejected; $("showRejected").classList.toggle("primary", filter.rejected); renderBoard(); };
  $("search").addEventListener("input", () => { filter.q = $("search").value.trim(); renderBoard(); });
  $("compareBtn").onclick = () => openViewer(cmpSel.map(byId).filter(Boolean), 0, true);

  // ---------- actions ----------
  async function setItem(g, patch) {
    Object.assign(g, patch); renderFeed(); renderBoard(); if ($("viewer").classList.contains("on")) renderViewer();
    const r = await api("/api/item", {id: g.id, ...patch});
    if (!r.ok) toast("Could not save: " + (r.error || "unknown"), 4000);
  }
  const toCreate = () => { showView("create"); window.scrollTo({top: 0, behavior: "smooth"}); };
  async function action(a, g, el, host) {
    if (!g) return;
    const resOf = () => (g.params && g.params.resolution) || "1K";
    const stem = (g.name || "").replace(/\.[^.]+$/, "");
    switch (a) {
      case "view": { const list = host && host.id === "board" ? boardList : feedList; const i = list.findIndex(x => x.id === g.id); openViewer(i >= 0 ? list : [g], Math.max(0, i)); break; }
      case "more": openMenu(el, moreItems(g), it => action(it.a, g, el, host)); break;
      case "star": await setItem(g, {rating: g.rating === "star" ? null : "star", hidden: false}); break;
      case "reject": await setItem(g, {rating: g.rating === "reject" ? null : "reject", hidden: g.rating !== "reject"}); break;
      case "note": { const n = prompt("Note for this item", g.note || ""); if (n !== null) await setItem(g, {note: n}); break; }
      case "cmp": { const i = cmpSel.indexOf(g.id); if (i >= 0) cmpSel.splice(i, 1); else { if (cmpSel.length >= 2) cmpSel.shift(); cmpSel.push(g.id); } $("compareBtn").classList.toggle("hidden", cmpSel.length !== 2); if (cmpSel.length === 2) { openViewer(cmpSel.map(byId).filter(Boolean), 0, true); } else toast("Selected. Pick one more to compare."); break; }
      case "pick": { const sibs = state.generations.filter(x => x.group === g.group && x.id !== g.id); await setItem(g, {rating: "star", hidden: false}); for (const s of sibs) if (s.rating !== "star") await setItem(s, {rating: "reject", hidden: true}); toast(`Picked v${g.variant || ""}. The others are rejected.`); break; }
      case "copy": try { await navigator.clipboard.writeText(g.path); toast("Path copied"); } catch { prompt("Path", g.path); } break;
      case "open": window.open(g.url, "_blank", "noopener"); break;
      case "download": await saveFile(g); break;
      case "showfinal": { const f = finalOf(g); if (f) openViewer([g, f], 0, true); break; }
      case "finalize": {
        const dur = (g.params && g.params.duration_s) || 0, pr = state.pricing && state.pricing.video["2.5-final"], rate = (pr && pr.credits_per_second) || 158;
        if (!confirm(`Render this draft at 1080p for about ${fmtUsd(rate * dur * (state.usd_per_credit || 0.005))} (${rate * dur} credits)? It keeps the same motion. This is a paid run.`)) break;
        toast("Starting the 1080p render…");
        try { const j = await api("/api/finalize", {id: g.id, approve: true, override_cap: $("overrideCap").checked, prompt: g.prompt}); if (j.ok && j.job) { jobs.set(j.job.id, j.job); pollJob(j.job.id); renderFeed(); closeViewer(); showView("create"); } else toast(j.error || "could not start the render", 5000); }
        catch (e) { toast(e.message, 5000); }
        break; }
      case "animate": closeViewer(); setKind("video"); if (vmode === "reference") { vmode = "first"; applyVMode(); } source = g; parentId = g.id; renderSource(); toCreate(); $("prompt").focus(); break;
      case "endframe":
        closeViewer(); setKind("video"); vmode = "first-last"; endSource = g; applyVMode(); toCreate();
        toast(source ? "End frame set. Describe the move between the two frames." : "End frame set. Now press Animate on the start image.", 4000); break;
      case "makeend":
        closeViewer(); setKind("image"); refSel.length = 0; refSel.push({ref: refOf(g), url: g.url, name: g.name, path: g.path || null});
        (g.characters || []).forEach(id => { if (!castSel.includes(id) && charById(id)) castSel.push(id); });
        $("imgModel").value = "nano-banana-2"; manualPrompt = true;
        $("prompt").value = "Same scene as the reference image: same people, wardrobe, location, lighting, lens and framing. The moment a few seconds later: ";
        if (g.params) { if (g.params.aspect_ratio) settings.aspect = g.params.aspect_ratio; if (g.params.resolution) settings.resolution = g.params.resolution; }
        parentId = g.id; renderPills(); renderRefs(); renderCastPick(); estimate(); toCreate(); $("prompt").focus();
        toast("Describe what changed. Then use the result as the end frame.", 4000); break;
      case "tochar": addToCharacter(g, el); break;
      case "extendcast": {
        // kie.ai only takes 480p/720p reference clips, so a 1080p clip is extended from its 480p draft.
        const big = (x) => x.model === "2.5-final" || (x.params && x.params.resolution === "1080p") || !!(x.media && x.media.width * x.media.height > 927408);
        const src = big(g) ? state.generations.find(x => x.id === g.parent_id && !big(x)) : g;
        if (!src) { toast("kie.ai only takes 480p or 720p clips as a reference, so this 1080p clip can't be extended with cast. Use Extend from last frame.", 6000); break; }
        closeViewer(); setKind("video"); vmode = "reference";
        const secs = (src.media && src.media.duration_s) || (src.params && src.params.duration_s) || 0;
        if (secs > 15) toast("Long clip: kie.ai caps reference clips at 30 s in total, and every second is billed.", 5000);
        refVideo = {ref: refOf(src), url: src.url, name: src.name, seconds: secs};
        if (!secs) clipSeconds(src.url).then(s => { if (refVideo && refVideo.url === src.url && s) { refVideo.seconds = s; renderRefVideo(); estimate(); } });
        restoreCast(g); refSel.length = 0;
        manualPrompt = true; $("prompt").value = "Continue @Video1 seamlessly from its final frame, same camera and lighting. Next: ";
        settings.vres = "480p"; settings.duration = (g.params && g.params.duration_s) || 10;
        if (g.media && g.media.width) settings.vaspect = ratioOf(g) || settings.vaspect;
        parentId = g.id; applyVMode(); renderPills(); toCreate(); $("prompt").focus();
        toast(castSel.length ? "Clip and cast loaded. Describe what happens next." : "Clip loaded. Pick the characters in it, then describe what happens next.", 5000);
        break; }
      case "tweak":
        closeViewer(); setKind("image"); $("preset").value = g.preset || ""; applyPreset(); setFields(g.fields);
        if (!g.fields || !g.fields.subject) { manualPrompt = true; $("prompt").value = g.prompt || ""; } else { compose(true); $("helper").classList.remove("hidden"); $("helperBtn").classList.add("on"); }
        $("negative").value = g.negative || "";
        if (g.params) { if (g.params.aspect_ratio) settings.aspect = g.params.aspect_ratio; if (g.params.resolution) settings.resolution = g.params.resolution; }
        if (g.model && $("imgModel").querySelector(`option[value="${g.model}"]`)) $("imgModel").value = g.model;
        restoreRefs(g); restoreCast(g);
        if (castSel.length) $("imgModel").value = "nano-banana-2";
        parentId = g.id; renderPills(); renderRefs(); renderCastPick(); estimate(); toCreate(); toast("Loaded. Edit and generate."); break;
      case "ref": closeViewer(); toggleRef({ref: refOf(g), url: g.url, name: g.name, path: g.path || null}); setKind("image"); toCreate(); toast("Added as a reference."); break;
      case "reframe169": case "reframe11": {
        const aspect = a === "reframe169" ? "16:9" : "1:1"; toast(`Generating a ${aspect} version…`);
        try { await submit({kind: "image", prompt: g.prompt, negative: g.negative || "", fields: g.fields || null, model: "nano-banana-2", aspect, resolution: resOf(), count: 1, name: stem + "-" + aspect.replace(":", "x"), refs: [refOf(g)], parent_id: g.id, preset: g.preset || null}); closeViewer(); showView("create"); } catch (e) { toast(e.message, 4000); }
        break; }
      case "hires": {
        const next = resOf() === "1K" ? "2K" : "4K"; toast(`Generating a ${next} version…`);
        try { await submit({kind: "image", prompt: g.prompt, negative: g.negative || "", fields: g.fields || null, model: g.model === "gpt-image-2" ? "gpt-image-2" : "nano-banana-2", aspect: (g.params && g.params.aspect_ratio) || "9:16", resolution: next, count: 1, name: stem + "-" + next, refs: g.model === "gpt-image-2" ? [] : [refOf(g)], parent_id: g.id, preset: g.preset || null}); closeViewer(); showView("create"); } catch (e) { toast(e.message, 4000); }
        break; }
      case "redo":
        closeViewer(); setKind("video");
        vmode = (g.params && g.params.mode) || "first";
        endSource = g.end_image ? (state.generations.find(x => refOf(x) === g.end_image || x.url === g.end_image) || {name: "previous end frame", url: thumbUrl(g.end_image) || g.end_image, path: g.end_image, prompt: ""}) : null;
        refVideo = g.ref_video ? {ref: g.ref_video, url: thumbUrl(g.ref_video) || g.ref_video, name: "previous reference clip", seconds: (g.params && g.params.ref_video_s) || 0} : null;
        restoreCast(g); if (vmode === "reference") restoreRefs(g); else refSel.length = 0;
        if (g.params && g.params.mode === "reference" && g.params.aspect_ratio) settings.vaspect = g.params.aspect_ratio;
        applyVMode();
        source = state.generations.find(x => refOf(x) === g.input_image || x.url === g.input_image || x.path === g.input_image) || (g.input_image ? {name: "previous start frame", url: thumbUrl(g.input_image) || g.input_image, prompt: ""} : null);
        renderSource(); $("still").value = g.input_image || "";
        setFields(g.fields); if (!g.fields || !Object.values(g.fields).some(Boolean)) { manualPrompt = true; $("prompt").value = g.prompt || ""; } else compose(true);
        $("negative").value = g.negative || "";
        settings.duration = (g.params && g.params.duration_s) || 10;
        settings.vres = (g.params && g.params.draft) ? "480p" : ((g.params && /p$/.test(g.params.resolution || "")) ? g.params.resolution : "480p");
        $("audio").checked = !(g.params && g.params.generate_audio === false); $("cameraFixed").checked = !!(g.params && g.params.camera_fixed);
        parentId = g.id; renderPills(); estimate(); toCreate(); toast("Loaded. Change the motion, approve, generate."); break;
      case "extend": {
        try {
          // Seedance 2.5 returns its last frame; older clips fall back to capturing it in the browser
          let up;
          if (g.last_frame_url) up = {name: stem + "-last", url: g.last_frame_url, path: g.last_frame_path || null};
          else { toast("Capturing the last frame…"); up = await uploadDataUrl(await lastFrame(g.url), stem + "-last", "still"); }
          closeViewer(); setKind("video"); vmode = "first"; endSource = null; $("endStill").value = ""; applyVMode();
          source = {name: up.name, url: up.url, path: up.path, prompt: g.prompt}; renderSource();
          setFields(g.fields); if (!g.fields || !Object.values(g.fields).some(Boolean)) { manualPrompt = true; $("prompt").value = g.prompt || ""; } else compose(true);
          settings.vres = "480p"; settings.duration = (g.params && g.params.duration_s) || 10; parentId = g.id; renderPills(); estimate(); toCreate();
          toast("The last frame is the new start frame. Approve and make a draft to continue the shot.");
        } catch (e) { toast("Could not capture the frame: " + e.message, 5000); }
        break; }
    }
  }
  async function saveFile(g) {
    const name = g.name || (g.kind === "video" ? "clip.mp4" : "image.png");
    toast(`Preparing ${name}…`, 8000);
    try {
      const r = await fetch(g.url, {mode: "cors"}); if (!r.ok) throw new Error("HTTP " + r.status);
      const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob()); a.download = name;
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 60000);
      toast(`Saved ${name} to your downloads`);
    } catch { window.open(g.url, "_blank", "noopener"); toast("Opened in a new tab. Use Save as, or long-press on a phone.", 6000); }
  }
  function clipSeconds(url) {
    return new Promise(res => { const v = document.createElement("video"); v.preload = "metadata"; v.muted = true; v.onloadedmetadata = () => res(isFinite(v.duration) ? Math.round(v.duration * 100) / 100 : 0); v.onerror = () => res(0); v.src = url; });
  }
  function lastFrame(url) {
    return new Promise((res, rej) => {
      const v = document.createElement("video"); v.crossOrigin = "anonymous"; v.muted = true; v.preload = "auto"; v.src = url;
      v.onerror = () => rej(new Error("video could not be loaded"));
      v.onloadedmetadata = () => { v.currentTime = Math.max(0, v.duration - 0.05); };
      v.onseeked = () => { try { const c = document.createElement("canvas"); c.width = v.videoWidth; c.height = v.videoHeight; c.getContext("2d").drawImage(v, 0, 0); res(c.toDataURL("image/png")); } catch { rej(new Error("frame is blocked by the video host (CORS)")); } };
    });
  }

  // ---------- viewer ----------
  let viewerPair = false;
  function openViewer(list, i, pair) { if (document.activeElement) document.activeElement.blur(); viewer.list = list; viewer.i = i; viewerPair = !!pair; $("viewer").classList.add("on"); renderViewer(); }
  function closeViewer() { $("viewer").classList.remove("on"); $("vStage").innerHTML = ""; }
  function renderViewer() {
    const st = $("vStage"), side = $("vSide");
    if (viewerPair) {
      st.className = "stage pair";
      st.innerHTML = viewer.list.map(g => isVideoItem(g) ? `<video src="${esc(g.url)}" controls autoplay muted loop playsinline></video>` : `<img src="${esc(g.url)}" alt="">`).join("");
      side.innerHTML = `<div class="actions" style="margin:0"><button class="btn" id="vClose">${ICON.x}Close</button></div>` + viewer.list.map((g, k) => `<div data-id="${esc(g.id)}"><div class="muted">${k ? "Right" : "Left"}</div><div class="chips">${chipsOf(g)}</div><div class="prompt" style="margin-top:6px">${esc(g.prompt || "")}</div><div class="actions" style="margin-top:8px">${iconActions(g)}</div></div>`).join("");
    } else {
      const g = viewer.list[viewer.i]; if (!g) return closeViewer();
      st.className = "stage";
      st.innerHTML = (viewer.list.length > 1 ? `<button class="nav l" id="vPrev">${ICON.left}</button><button class="nav r" id="vNext">${ICON.right}</button>` : "") + (isVideoItem(g) ? `<video src="${esc(g.url)}" controls autoplay loop playsinline></video>` : `<img src="${esc(g.url)}" alt="">`);
      side.innerHTML = `<div class="actions" style="margin:0"><button class="btn" id="vClose">${ICON.x}Close</button><span class="muted" style="margin-left:auto">${viewer.i + 1} / ${viewer.list.length}</span></div>
        <div data-id="${esc(g.id)}" style="display:grid;gap:10px">
          <div class="chips">${chipsOf(g)}</div><div class="muted">${esc(fmtDate(g.created_at || g.modified))}</div>
          <div class="prompt" style="-webkit-line-clamp:unset">${esc(g.prompt || g.name)}</div>${g.note ? `<div class="inote">${esc(g.note)}</div>` : ""}
          <div class="actions"><button class="btn act" data-a="${g.kind === "image" ? "tweak" : "redo"}">${ICON.rerun}Rerun</button>${primaryAction(g)}</div>
          <div class="actions" style="margin-top:0">${iconActions(g)}</div>
        </div><div class="muted">Arrow keys browse · Esc closes</div>`;
      if ($("vPrev")) { $("vPrev").onclick = () => stepViewer(-1); $("vNext").onclick = () => stepViewer(1); }
    }
    $("vClose").onclick = closeViewer;
    attach(side);
  }
  function stepViewer(d) { if (viewerPair || !viewer.list.length) return; viewer.i = (viewer.i + d + viewer.list.length) % viewer.list.length; renderViewer(); }
  $("viewer").addEventListener("click", (e) => { if (e.target === $("viewer") || e.target === $("vStage")) closeViewer(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeMenu(); closeViewer(); }
    if ($("viewer").classList.contains("on") && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) { if (e.key === "ArrowLeft") stepViewer(-1); if (e.key === "ArrowRight") stepViewer(1); }
  });

  // ---------- start ----------
  renderPills();
  $("pillVAspect").onclick = (e) => { e.stopPropagation(); openMenu($("pillVAspect"), menuFor.pillVAspect(), it => { settings.vaspect = it.v; renderPills(); }); };
  loadPresets().then(() => { setKind("image"); let v = "create"; try { v = localStorage.getItem("view") || "create"; } catch {} showView(v); load(); });
})();
