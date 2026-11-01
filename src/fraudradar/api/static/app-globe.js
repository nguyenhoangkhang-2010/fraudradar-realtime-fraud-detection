(() => {
  const $ = (id) => document.getElementById(id);
  const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const num = new Intl.NumberFormat("en-US");

  const COL = { allow: [61, 217, 235], review: [255, 176, 32], block: [255, 59, 92], safe: [61, 220, 151] };
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  const RANK = { allow: 0, review: 1, block: 2 };
  const ATMOSPHERE = { calm: "#3dd9eb", launched: "#3dd9eb", armed: "#ffb020", alert: "#ff3b5c", contained: "#3dd9ab" };

  const ARMED_TIMEOUT_MS = 15000; // an attack nobody blocked is reported as undetected
  const QUIET_MS = 3500; // no new ring activity for this long means the incident is over
  const CONTAINED_SHOW_MS = 22000;
  const GRAPH_TTL_MS = 25000;
  const GRAPH_MAX_NODES = 90;
  const FLAG_WINDOW_MS = 60000;
  const ARC_MAX_ALLOWED = 90;
  const ARC_MAX_FLAGGED = 160;
  const REFRESH_MS = 200;
  const OVERVIEW = { lat: 16, lng: 105, altitude: 1.9 }; // opens over Southeast Asia
  const MAJOR_HUBS = new Set(["New York", "London", "Dubai", "Singapore", "Tokyo", "Sydney", "Sao Paulo", "Lagos", "Moscow"]);

  // ---------- shared state ----------
  const S = {
    globe: null, cities: [], land: null, dim: 1,
    heat: [], heatFlag: [], flags: [], hoverCity: -1, mouse: { x: 0, y: 0 },
    arcs: [], rings: [], pendingRings: [], lastPulse: new Map(), muleRing: null, labelSig: "",
  };
  const ST = {
    phase: "calm", // calm | launched | armed | alert | contained
    booted: false, prev: null, incidentId: null,
    startedAt: 0, lastActivity: 0, launchedAt: 0, containedAt: 0,
    accounts: new Set(), target: null, why: "", origins: new Map(), originText: "",
  };
  const E = { blocked: 0, stopped: 0, leaked: 0 }; // eased on-screen values
  const T = { blocked: 0, tx: 0, stopped: 0, leaked: 0 }; // targets

  function fit(canvas) {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(r.width * dpr));
    canvas.height = Math.max(1, Math.round(r.height * dpr));
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w: r.width, h: r.height, dpr };
  }

  // ---------- city activity ----------
  function touchCity(i, flagged, now) {
    if (i == null || i < 0 || i >= S.heat.length) return;
    S.heat[i] = now;
    S.heatFlag[i] = flagged ? 1 : 0;
    if (flagged) {
      const q = S.flags[i];
      q.push(now);
      if (q.length > 300) q.shift();
    }
  }

  function flaggedRecently(i, now) {
    const q = S.flags[i];
    while (q.length && now - q[0] > FLAG_WINDOW_MS) q.shift();
    return q.length;
  }

  const heatOf = (i, now) => Math.max(0, 1 - (now - S.heat[i]) / 2000);

  // ---------- 3D globe ----------
  function buildGlobe() {
    const host = $("globe");
    if (typeof Globe !== "function") {
      host.innerHTML = '<div class="fallback">The 3D globe needs internet access to load globe.gl.<br>Open /index2d.html for the flat map.</div>';
      return;
    }
    const g = Globe({ animateIn: false })(host)
      .backgroundColor("rgba(0,0,0,0)")
      .showAtmosphere(true).atmosphereColor(ATMOSPHERE.calm).atmosphereAltitude(0.22)
      .globeImageUrl(globeTexture(null))
      // arcs: comet-like dashes travelling from sender to recipient
      .arcStartLat((d) => d.sLat).arcStartLng((d) => d.sLng).arcEndLat((d) => d.eLat).arcEndLng((d) => d.eLng)
      .arcColor((d) => d.color).arcStroke((d) => d.stroke).arcAltitudeAutoScale((d) => d.alt)
      .arcDashLength((d) => d.dash).arcDashGap(2).arcDashAnimateTime((d) => d.dur).arcsTransitionDuration(0)
      // rings: shock waves where blocked payments land
      .ringLat((d) => d.lat).ringLng((d) => d.lng)
      .ringMaxRadius((d) => d.r).ringPropagationSpeed((d) => d.speed).ringRepeatPeriod((d) => d.repeat)
      .ringColor((d) => (t) => rgba(d.color, Math.max(0, 1 - t)))
      // cities
      .pointLat((d) => d.lat).pointLng((d) => d.lon).pointsMerge(false).pointsTransitionDuration(0)
      .pointLabel(() => "").onPointHover(onCityHover)
      .labelLat((d) => d.lat).labelLng((d) => d.lng).labelText((d) => d.text).labelSize((d) => d.size)
      .labelColor((d) => d.color).labelAltitude(0.012).labelResolution(2)
      .labelIncludeDot(true).labelDotRadius(0.04).labelDotOrientation(() => "right").labelsTransitionDuration(0);

    try { // dark, slightly lit ocean so the dotted land stands out
      const mat = g.globeMaterial();
      mat.color.set("#ffffff"); // the texture carries the colours
      mat.emissive.set("#0b1c30");
      mat.emissiveIntensity = 0.45;
      mat.shininess = 4;
    } catch (e) {
      console.warn("globe material tweak failed", e);
    }

    const controls = g.controls();
    controls.autoRotate = true;
    controls.autoRotateSpeed = 0.45;
    controls.enableDamping = true;
    g.pointOfView(OVERVIEW, 0);

    new ResizeObserver(() => g.width(host.clientWidth).height(host.clientHeight)).observe(host);
    host.addEventListener("mousemove", (e) => {
      S.mouse = { x: e.clientX, y: e.clientY };
      if ($("tip").style.opacity === "1") placeTip();
    });
    S.globe = g;
    window.__globe = g; // debug handle: lets you inspect the globe from the browser console
    refreshCities(performance.now());
  }

  // Dot-matrix world drawn into an equirectangular texture. Dots are spaced evenly on the sphere, so each row is
  // sampled more sparsely towards the poles and its dots are stretched horizontally to look round once wrapped.
  function globeTexture(land) {
    const W = 2048, H = 1024;
    const cv = document.createElement("canvas");
    cv.width = W; cv.height = H;
    const ctx = cv.getContext("2d");
    ctx.fillStyle = "#0a1626";
    ctx.fillRect(0, 0, W, H);

    ctx.strokeStyle = "rgba(127,147,171,.10)";
    ctx.lineWidth = 1;
    for (let lng = -180; lng <= 180; lng += 30) {
      const x = ((lng + 180) / 360) * W;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    }
    for (let lat = -60; lat <= 60; lat += 30) {
      const y = ((90 - lat) / 180) * H;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }

    if (land && window.d3) {
      const mask = document.createElement("canvas");
      mask.width = W; mask.height = H;
      const mctx = mask.getContext("2d", { willReadFrequently: true });
      const proj = d3.geoEquirectangular().scale(W / (2 * Math.PI)).translate([W / 2, H / 2]);
      mctx.beginPath(); d3.geoPath(proj, mctx)(land);
      mctx.fillStyle = "#fff"; mctx.fill();
      const px = mctx.getImageData(0, 0, W, H).data;

      const step = 1.5; // degrees between dot centres
      ctx.fillStyle = "rgba(125,175,220,0.88)";
      for (let lat = -84; lat <= 84; lat += step) {
        const stretch = 1 / Math.cos((lat * Math.PI) / 180);
        const y = ((90 - lat) / 180) * H;
        for (let lng = -180; lng < 180; lng += step * stretch) {
          const x = ((lng + 180) / 360) * W;
          if (px[((Math.round(y) * W + Math.round(x)) << 2) + 3] > 128) {
            ctx.beginPath();
            ctx.ellipse(x, y, 1.8 * Math.min(stretch, 6), 1.8, 0, 0, 6.283);
            ctx.fill();
          }
        }
      }
    }
    return cv.toDataURL("image/png");
  }

  async function loadWorld() {
    if (!window.topojson) return;
    try {
      const topo = await (await fetch("https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/land-110m.json")).json();
      S.land = topojson.feature(topo, topo.objects.land);
      if (S.globe) S.globe.globeImageUrl(globeTexture(S.land));
    } catch (e) {
      console.warn("land outline unavailable, showing a plain globe", e);
    }
  }

  function flyTo(city, altitude, ms) {
    if (!S.globe || !city) return;
    S.globe.pointOfView({ lat: city.lat, lng: city.lon, altitude }, ms);
  }

  function syncRotation() {
    if (!S.globe) return;
    S.globe.controls().autoRotate = (ST.phase === "calm" || ST.phase === "launched") && S.hoverCity < 0;
  }

  function addArc(it, now) {
    const flagged = it.action !== "allow";
    touchCity(it.src_city, flagged, now);
    if (it.dst_city !== it.src_city) touchCity(it.dst_city, flagged, now);
    if (!S.globe) return;

    const a = S.cities[it.src_city], b = S.cities[it.dst_city];
    if (!a || !b) return;
    if (it.src_city === it.dst_city) {
      if (flagged) queueRing(b, it.action, now);
      return;
    }
    const c = COL[it.action];
    const dur = (flagged ? 1100 : 1300) + Math.random() * 500;
    const alpha = flagged ? 1 : 0.9 * S.dim;
    S.arcs.push({
      sLat: a.lat, sLng: a.lon, eLat: b.lat, eLng: b.lon, flagged, t0: now, dur, life: dur * 1.25,
      color: [rgba(c, 0), rgba(flagged ? [255, 150, 165] : c, alpha)],
      stroke: flagged ? 0.75 : 0.26,
      alt: flagged ? 0.5 : 0.22 + Math.random() * 0.12,
      dash: flagged ? 0.5 : 0.32,
    });
    if (flagged) queueRing(b, it.action, now + dur);
    S.arcsDirty = true;
    trimArcs();
  }

  function trimArcs() {
    const allowed = S.arcs.filter((a) => !a.flagged);
    const flaggedArcs = S.arcs.filter((a) => a.flagged);
    if (allowed.length > ARC_MAX_ALLOWED || flaggedArcs.length > ARC_MAX_FLAGGED) {
      S.arcs = allowed.slice(-ARC_MAX_ALLOWED).concat(flaggedArcs.slice(-ARC_MAX_FLAGGED));
    }
  }

  function queueRing(city, action, when) {
    if (action === "allow") return;
    const key = city.id;
    if (when - (S.lastPulse.get(key) || 0) < 350) return; // one shock wave per city at a time
    S.lastPulse.set(key, when);
    S.pendingRings.push({ lat: city.lat, lng: city.lon, when, color: COL[action] });
  }

  function refreshArcsAndRings(now) {
    const aliveArcs = S.arcs.filter((a) => now - a.t0 < a.life);
    const arcsChanged = aliveArcs.length !== S.arcs.length || S.arcsDirty;
    S.arcs = aliveArcs;
    S.arcsDirty = false;
    if (arcsChanged) S.globe.arcsData(S.arcs.slice());

    const due = S.pendingRings.filter((p) => p.when <= now);
    if (due.length) S.pendingRings = S.pendingRings.filter((p) => p.when > now);
    S.rings = S.rings.filter((r) => r.expire > now);
    for (const p of due) S.rings.push({ lat: p.lat, lng: p.lng, color: p.color, r: 4.5, speed: 3.2, repeat: 0, expire: now + 1600 });
    const rings = S.muleRing ? S.rings.concat([S.muleRing]) : S.rings;
    S.globe.ringsData(rings);
    return arcsChanged;
  }

  function refreshCities(now) {
    const g = S.globe;
    if (!g || !S.cities.length) return;
    const state = S.cities.map((c, i) => {
      const hot = heatOf(i, now);
      const flagged = hot > 0 && S.heatFlag[i] === 1;
      const hovered = S.hoverCity === i;
      const isTarget = !!ST.target && ST.phase !== "calm" && ST.target.city === i;
      return { c, i, hot, flagged, hovered, isTarget };
    });

    g.pointsData(S.cities)
      .pointColor((d) => {
        const s = state[d.id];
        if (s.flagged) return "#ff5a76";
        return s.hot > 0 ? rgba(COL.allow, 0.6 + 0.4 * s.hot) : "rgba(190,220,245,0.75)";
      })
      .pointRadius((d) => 0.5 + state[d.id].hot * 0.25 + (state[d.id].hovered ? 0.3 : 0))
      .pointAltitude((d) => 0.006 + state[d.id].hot * 0.012);

    const labels = [];
    for (const s of state) {
      if (s.isTarget) continue; // the mule label below replaces it
      if (s.hovered || s.flagged) {
        labels.push({ lat: s.c.lat, lng: s.c.lon, text: s.c.name, size: s.hovered ? 2.0 : 1.7, color: s.flagged ? "#ffa0ae" : "#eaf6ff" });
      } else if (MAJOR_HUBS.has(s.c.name)) {
        labels.push({ lat: s.c.lat, lng: s.c.lon, text: s.c.name, size: 1.3, color: rgba([205, 228, 247], (0.5 * (0.4 + 0.6 * S.dim)).toFixed(2)) });
      }
    }
    if (ST.target && ST.phase !== "calm" && S.cities[ST.target.city]) {
      const tc = S.cities[ST.target.city];
      const col = ST.phase === "contained" ? "#7dffc4" : "#ff7a92";
      labels.push({ lat: tc.lat, lng: tc.lon, text: `MULE ${ST.target.acct} · ${tc.name}`, size: 2.7, color: col });
    }
    const sig = labels.map((l) => `${l.text}|${l.color}|${l.size}`).join(";");
    if (sig !== S.labelSig) { S.labelSig = sig; g.labelsData(labels); }
  }

  function refresh() {
    if (!S.globe) return;
    const now = performance.now();
    refreshArcsAndRings(now);
    refreshCities(now);
  }

  // ---------- hover tooltip ----------
  function placeTip() {
    const tip = $("tip");
    tip.style.left = `${Math.min(S.mouse.x + 16, innerWidth - 180)}px`;
    tip.style.top = `${S.mouse.y + 16}px`;
  }

  function onCityHover(city) {
    const tip = $("tip");
    S.hoverCity = city ? city.id : -1;
    syncRotation();
    if (!city) { tip.style.opacity = 0; return; }
    const n = flaggedRecently(city.id, performance.now());
    tip.replaceChildren();
    for (const [tag, text, cls] of [
      ["b", city.name],
      ["span", "simulated location"],
      ["span", `${n} flagged in last 60 s`, n ? "hot" : ""],
    ]) {
      const el = document.createElement(tag);
      el.textContent = text;
      if (cls) el.className = cls;
      tip.appendChild(el);
    }
    placeTip();
    tip.style.opacity = 1;
    refreshCities(performance.now());
  }

  // ---------- account network ----------
  // d3 holds a reference to `links`, so new links are staged in `pending` and merged on rebuild.
  const G = { nodes: new Map(), links: [], pending: [], sim: null, dirty: false, lastRebuild: 0, view: null };
  const lid = (x) => (typeof x === "object" ? x.id : x);

  function buildGraphView() {
    G.view = fit($("graph"));
    if (!window.d3) return;
    const { w, h } = G.view;
    if (!G.sim) {
      G.sim = d3.forceSimulation()
        .force("link", d3.forceLink().id((d) => d.id).distance(20).strength(0.7))
        .force("charge", d3.forceManyBody().strength(-24))
        .force("x", d3.forceX(w / 2).strength(0.12))
        .force("y", d3.forceY(h / 2).strength(0.14))
        .force("collide", d3.forceCollide((d) => 4 + Math.min(d.deg || 0, 12) * 0.5))
        .stop();
    } else {
      G.sim.force("x").x(w / 2);
      G.sim.force("y").y(h / 2);
    }
  }

  function graphIngest(items, now) {
    if (!G.sim || ST.phase === "calm" || ST.phase === "launched") return; // the panel is hidden, so skip the work
    for (const it of items) {
      if (it.action === "allow" && Math.random() > 0.006) continue; // sparse honest background
      for (const id of [it.src, it.dst]) {
        let n = G.nodes.get(id);
        if (!n) {
          n = { id, rank: 0, deg: 0, last: now, x: G.view.w / 2 + (Math.random() - 0.5) * 80, y: G.view.h / 2 + (Math.random() - 0.5) * 80 };
          G.nodes.set(id, n);
        }
        n.last = now;
      }
      const rank = RANK[it.action];
      const s = G.nodes.get(it.src), d = G.nodes.get(it.dst);
      s.rank = Math.max(s.rank, rank);
      if (rank === 2) d.rank = Math.max(d.rank, 1);
      G.pending.push({ source: it.src, target: it.dst, action: it.action, t: now });
    }
    G.dirty = true;
  }

  function graphRebuild(now) {
    for (const [id, n] of G.nodes) if (now - n.last > GRAPH_TTL_MS) G.nodes.delete(id);
    if (G.nodes.size > GRAPH_MAX_NODES) {
      const oldest = [...G.nodes.values()].sort((a, b) => a.last - b.last);
      for (const n of oldest.slice(0, G.nodes.size - GRAPH_MAX_NODES)) G.nodes.delete(n.id);
    }
    G.links = G.links
      .concat(G.pending)
      .filter((l) => now - l.t <= GRAPH_TTL_MS && G.nodes.has(lid(l.source)) && G.nodes.has(lid(l.target)));
    G.pending = [];
    for (const n of G.nodes.values()) n.deg = 0;
    for (const l of G.links) { G.nodes.get(lid(l.source)).deg++; G.nodes.get(lid(l.target)).deg++; }
    G.sim.nodes([...G.nodes.values()]);
    G.sim.force("link").links(G.links);
    G.sim.alpha(Math.max(G.sim.alpha(), 0.45));
    G.dirty = false;
    G.lastRebuild = now;
  }

  function drawGraph(now) {
    if (!G.view) return;
    const { ctx, w, h } = G.view;
    ctx.clearRect(0, 0, w, h);
    if (!G.sim) {
      ctx.fillStyle = "#7f93ab"; ctx.font = "13px system-ui";
      ctx.fillText("d3 failed to load (offline?): graph disabled", 18, 30);
      return;
    }
    if (G.dirty && now - G.lastRebuild > 350) graphRebuild(now);
    if (G.sim.alpha() > G.sim.alphaMin()) { G.sim.tick(); G.sim.tick(); }
    for (const n of G.nodes.values()) { // keep every node (and its glow) inside the panel
      const x = Math.min(w - 16, Math.max(16, n.x)), y = Math.min(h - 16, Math.max(18, n.y));
      if (x !== n.x) n.vx = 0; // drop the momentum too, or nodes pile up along the wall
      if (y !== n.y) n.vy = 0;
      n.x = x; n.y = y;
    }

    ctx.globalCompositeOperation = "lighter";
    ctx.lineWidth = 1;
    for (const l of G.links) {
      const s = l.source, t = l.target;
      if (typeof s !== "object" || typeof t !== "object") continue;
      ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(t.x, t.y);
      ctx.strokeStyle = rgba(COL[l.action], l.action === "allow" ? 0.14 : 0.5);
      ctx.stroke();
    }

    let hub = null;
    for (const n of G.nodes.values()) if (n.rank === 2 && (!hub || n.deg > hub.deg)) hub = n;

    for (const n of G.nodes.values()) {
      const c = n.rank === 2 ? COL.block : n.rank === 1 ? COL.review : [95, 125, 160];
      const r = 2.6 + n.rank * 1.3 + Math.min(n.deg, 14) * 0.45;
      if (n.rank > 0) {
        ctx.beginPath(); ctx.arc(n.x, n.y, r * 2.6, 0, 6.283);
        ctx.fillStyle = rgba(c, 0.13); ctx.fill();
      }
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 6.283);
      ctx.fillStyle = rgba(c, 0.95); ctx.fill();
    }
    ctx.globalCompositeOperation = "source-over";

    if (hub && hub.deg >= 4) {
      ctx.font = "600 10px system-ui, sans-serif"; ctx.textBaseline = "middle";
      ctx.fillStyle = "rgba(255,226,232,.95)";
      ctx.fillText(hub.id, hub.x + 14, hub.y - 12);
    }
  }

  // ---------- incident story ----------
  const SAY = {
    calm: ["calm", "Monitoring · all clear"],
    launched: ["calm", "Attack launched · arming"],
    armed: ["armed", "Attack in progress · undetected"],
    alert: ["alert", "Threat detected · blocking"],
    contained: ["contained", "Ring contained"],
  };

  function setPhase(p) {
    ST.phase = p;
    const [cls, text] = SAY[p];
    document.body.className = cls;
    $("status").querySelector("span").textContent = text;
    if (S.globe) S.globe.atmosphereColor(ATMOSPHERE[p]);
    syncRotation();
  }

  function retrigger(el, cls) {
    el.classList.remove("go", "safe");
    void el.offsetWidth; // restart the CSS animation
    el.classList.add("go");
    if (cls) el.classList.add(cls);
  }

  let momentTimer = 0;
  function moment(title, sub, kind, ms) {
    const el = $("moment");
    $("moment-title").textContent = title;
    $("moment-sub").textContent = sub;
    el.className = `show ${kind}`;
    clearTimeout(momentTimer);
    momentTimer = setTimeout(() => { el.className = kind; }, ms);
  }

  function setMuleRing(color) {
    const tc = ST.target && S.cities[ST.target.city];
    S.muleRing = tc ? { lat: tc.lat, lng: tc.lon, color, r: 7, speed: 2.4, repeat: 900 } : null;
  }

  function beginIncident(sc, now) {
    ST.incidentId = sc.id;
    ST.startedAt = now;
    ST.accounts = new Set();
    ST.target = null;
    ST.why = "";
    ST.origins = new Map();
    S.muleRing = null;
    E.blocked = E.stopped = E.leaked = 0;
    $("inc-title").textContent = `Incident · ${sc.id}`;
    $("inc-clock-label").textContent = "attack in progress, undetected";
    // reset the network so only this incident is on screen
    G.nodes.clear(); G.links = []; G.pending = []; G.dirty = true;
    setPhase("armed");
  }

  function onScenario(msg, now) {
    const sc = msg.stats.scenario;
    if (!ST.booted) { ST.booted = true; ST.prev = sc ? { ...sc } : null; return; }

    const active = !!sc && (!ST.prev || sc.id !== ST.prev.id || sc.tx !== ST.prev.tx);
    ST.prev = sc ? { ...sc } : null;

    if (active) {
      ST.lastActivity = now;
      if (ST.phase !== "armed" && ST.phase !== "alert") beginIncident(sc, now);
    }

    if (ST.phase === "armed" || ST.phase === "alert") {
      for (const it of msg.items) {
        if (it.action === "block") {
          ST.accounts.add(it.src);
          ST.origins.set(it.src_city, (ST.origins.get(it.src_city) || 0) + 1);
          if (!ST.target) ST.target = { acct: it.dst, city: it.dst_city };
        }
        if (it.action !== "allow" && it.reasons.length) {
          ST.why = `${it.src} → ${it.dst}: ${it.reasons[0]}`;
        }
      }
    }

    if (sc && sc.id === ST.incidentId) {
      T.blocked = sc.blocked; T.tx = sc.tx; T.stopped = sc.amount_blocked; T.leaked = sc.amount_passed;
    }

    if (ST.phase === "armed" && sc && sc.id === ST.incidentId && sc.blocked > 0) {
      setPhase("alert");
      const ttd = sc.seconds_to_first_block;
      $("inc-clock-label").textContent = "time to first block";
      $("inc-clock").textContent = `${ttd.toFixed(1)}s`;
      retrigger($("flash"));
      moment("THREAT DETECTED", `first block after ${ttd.toFixed(1)} s`, "threat", 2200);
      setMuleRing(COL.block);
      if (ST.target) flyTo(S.cities[ST.target.city], 1.35, 1800); // the camera swings to the mule
    }

    if (ST.phase === "armed" && now - ST.startedAt > ARMED_TIMEOUT_MS) {
      setPhase("contained");
      ST.containedAt = now;
      $("inc-clock-label").textContent = "attack was not detected";
      moment("NOT DETECTED", "the ring got through", "warn", 3500);
    } else if (ST.phase === "alert" && now - ST.lastActivity > QUIET_MS) {
      setPhase("contained");
      ST.containedAt = now;
      setMuleRing(COL.safe);
      retrigger($("flash"), "safe");
      const pctBlocked = sc.tx ? Math.round((sc.blocked / sc.tx) * 100) : 0;
      moment("RING CONTAINED", `${sc.blocked} of ${sc.tx} transactions blocked (${pctBlocked}%) · ${usd.format(sc.amount_blocked)} stopped`, "safe", 4500);
    } else if (ST.phase === "contained" && now - ST.containedAt > CONTAINED_SHOW_MS) {
      setPhase("calm");
      S.muleRing = null;
      if (S.globe) S.globe.pointOfView(OVERVIEW, 2200); // pull back out to the whole planet
    } else if (ST.phase === "launched" && now - ST.launchedAt > 8000) {
      setPhase("calm");
    }
  }

  function renderIncident(now) {
    if (ST.phase === "armed") $("inc-clock").textContent = `${((now - ST.startedAt) / 1000).toFixed(1)}s`;
    for (const k of ["blocked", "stopped", "leaked"]) {
      E[k] += (T[k] - E[k]) * 0.2;
      if (Math.abs(T[k] - E[k]) < 0.5) E[k] = T[k];
    }
    $("inc-blocked").textContent = `${Math.round(E.blocked)} / ${T.tx}`;
    $("inc-stopped").textContent = usd.format(E.stopped);
    $("inc-leaked").textContent = usd.format(E.leaked);
    $("inc-accounts").textContent = ST.accounts.size;
    $("inc-bar").style.width = `${T.tx ? Math.min(100, (E.blocked / T.tx) * 100) : 0}%`;
    $("inc-target").textContent = ST.target
      ? `${ST.target.acct} · ${(S.cities[ST.target.city] || {}).name || ""}`
      : "–";
    $("inc-why").textContent = ST.why;

    const names = [...ST.origins.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([id]) => (S.cities[id] || {}).name)
      .filter(Boolean);
    const text = names.length
      ? names.slice(0, 3).join(", ") + (names.length > 3 ? ` +${names.length - 3}` : "")
      : "–";
    if (text !== ST.originText) { ST.originText = text; $("inc-from").textContent = text; }
  }

  // ---------- KPIs and websocket ----------
  const pct = (v) => (v == null ? "–" : `${(v * 100).toFixed(1)}%`);

  function renderKpis(s) {
    $("k-tps").textContent = num.format(Math.round(s.tps));
    $("k-p99").textContent = Math.round(s.latency_p99_ms);
    $("k-total").textContent = num.format(s.total);
    $("k-blocked").textContent = num.format(s.blocked);
    $("k-saved").textContent = usd.format(s.fraud_blocked_amount);
    $("k-pr").textContent = `${pct(s.precision)} / ${pct(s.recall)}`;
  }

  function connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => { $("conn").textContent = "live"; $("conn").className = "conn on"; };
    ws.onclose = () => {
      $("conn").textContent = "reconnecting"; $("conn").className = "conn off";
      setTimeout(connect, 1500);
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type !== "tick") return;
      const now = performance.now();
      renderKpis(msg.stats);
      onScenario(msg, now); // first, so a new incident clears the network before this tick's items land in it
      for (const it of msg.items) addArc(it, now);
      graphIngest(msg.items, now);
    };
  }

  // ---------- boot ----------
  function frame(now) {
    try {
      const quiet = ST.phase === "armed" || ST.phase === "alert";
      S.dim += ((quiet ? 0.22 : 1) - S.dim) * 0.06;
      drawGraph(now);
      renderIncident(now);
    } catch (e) {
      console.error(e); // keep the loop alive; one bad frame must not freeze the scene
    }
    requestAnimationFrame(frame);
  }

  async function boot() {
    S.cities = await (await fetch("/api/cities")).json();
    S.heat = S.cities.map(() => -1e9);
    S.heatFlag = S.cities.map(() => 0);
    S.flags = S.cities.map(() => []);
    buildGlobe();
    buildGraphView();
    loadWorld();
    new ResizeObserver(buildGraphView).observe($("network"));
    setInterval(refresh, REFRESH_MS);

    $("attack").addEventListener("click", async () => {
      const btn = $("attack");
      btn.disabled = true;
      if (ST.phase === "calm" || ST.phase === "contained") {
        setPhase("launched");
        ST.launchedAt = performance.now();
        moment("ATTACK LAUNCHED", `synthetic ring of ${$("size").value} accounts`, "warn", 1600);
      }
      try {
        await fetch("/api/attack", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ size: Number($("size").value) }),
        });
      } finally {
        setTimeout(() => { btn.disabled = false; }, 2500);
      }
    });

    setPhase("calm");
    connect();
    requestAnimationFrame(frame);
  }

  boot();
})();
