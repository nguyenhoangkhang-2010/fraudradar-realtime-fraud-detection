(() => {
  const $ = (id) => document.getElementById(id);
  let usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const num = new Intl.NumberFormat("en-US");
  const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

  const COL = { allow: [61, 217, 235], review: [255, 176, 32], block: [255, 59, 92], safe: [61, 220, 151] };
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  const RANK = { allow: 0, review: 1, block: 2 };

  const ARMED_TIMEOUT_MS = 15000; // an attack nobody blocked is reported as undetected
  const QUIET_MS = 3500; // no new ring activity for this long means the incident is over
  const CONTAINED_SHOW_MS = 22000;
  const GRAPH_TTL_MS = 25000;
  const GRAPH_MAX_NODES = 90;
  const FLAG_WINDOW_MS = 60000;
  const ARC_MAX = 300;
  const STATE_MS = 500; // how often the choropleth and the side panel refresh
  const DISTRICT_ZOOM = 8.5; // from here on, arcs end at the district rather than the city centre
  const WORLD_BOUNDS = [[-170, -52], [190, 76]];

  // ---------- shared state ----------
  const S = {
    map: null, fx: null,
    cities: [], cityByPid: new Map(), // active simulated places, indexed by the id transactions carry
    countries: null, places: null, districts: null,
    countryByIso: new Map(), districtById: new Map(), districtCenter: new Map(),
    arcs: [], pulses: [], dim: 1,
    heat: [], heatFlag: [], flagsCity: [], flagsCountry: new Map(), flagsDistrict: new Map(),
    written: { countries: new Map(), districts: new Map(), places: new Map() }, // last feature-state sent to the map
    hover: { country: null, district: null }, listSig: "",
  };
  const NAV = { level: "world", iso: null, place: null, district: null, token: 0 };
  const ST = {
    phase: "calm", // calm | launched | armed | alert | contained
    booted: false, prev: null, incidentId: null, userNav: false,
    startedAt: 0, lastActivity: 0, launchedAt: 0, containedAt: 0,
    accounts: new Set(), target: null, why: "", origins: new Map(), originText: "",
  };
  const E = { blocked: 0, stopped: 0, leaked: 0 }; // eased on-screen values
  const T = { blocked: 0, tx: 0, stopped: 0, leaked: 0 }; // targets
  const narrow = () => innerWidth < 900;

  function fit(canvas) {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(r.width * dpr));
    canvas.height = Math.max(1, Math.round(r.height * dpr));
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w: r.width, h: r.height, dpr };
  }

  // ---------- geometry ----------
  const polygonsOf = (g) => (g.type === "MultiPolygon" ? g.coordinates : [g.coordinates]);

  function ringBounds(ring) {
    let x0 = 180, y0 = 90, x1 = -180, y1 = -90;
    for (const [x, y] of ring) {
      if (x < x0) x0 = x; if (x > x1) x1 = x;
      if (y < y0) y0 = y; if (y > y1) y1 = y;
    }
    return [[x0, y0], [x1, y1]];
  }

  // Bounds of a shape. With `mainOnly`, only its largest polygon: the contiguous US rather than Alaska and
  // Hawaii as well, mainland France rather than its overseas territories.
  function boundsOf(geom, mainOnly) {
    let polys = polygonsOf(geom).map((p) => p[0]);
    if (mainOnly) {
      const area = (r) => { const [[a, b], [c, d]] = ringBounds(r); return (c - a) * (d - b); };
      polys = [polys.reduce((best, r) => (area(r) > area(best) ? r : best))];
    }
    let x0 = 180, y0 = 90, x1 = -180, y1 = -90;
    for (const r of polys) {
      const [[a, b], [c, d]] = ringBounds(r);
      x0 = Math.min(x0, a); y0 = Math.min(y0, b); x1 = Math.max(x1, c); y1 = Math.max(y1, d);
    }
    return [[x0, y0], [x1, y1]];
  }

  function unionBounds(list) {
    let x0 = 180, y0 = 90, x1 = -180, y1 = -90;
    for (const [[a, b], [c, d]] of list) {
      x0 = Math.min(x0, a); y0 = Math.min(y0, b); x1 = Math.max(x1, c); y1 = Math.max(y1, d);
    }
    return [[x0, y0], [x1, y1]];
  }

  const centerOf = (geom) => {
    const [[a, b], [c, d]] = boundsOf(geom, true);
    return [(a + c) / 2, (b + d) / 2];
  };

  // Say where the payments come from, so a replay of real data is never mistaken for the simulator.
  fetch("/api/health").then((r) => r.json()).then((h) => {
    const tag = document.querySelector(".brand small");
    if (h.source !== "paysim") return;
    if (tag) tag.textContent = "real PaySim log · places assigned";
    usd = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });  // PaySim's currency is unspecified: no $ sign
    const credits = document.getElementById("credits");
    if (credits) credits.textContent = credits.textContent.replace(
      "Locations and traffic are simulated.",
      "Payments are the real PaySim log (itself a simulation); places are assigned from account names; attacks are injected.");
  }).catch(() => {});

  // ---------- data ----------
  async function loadData() {
    const get = (url) => fetch(url).then((r) => { if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); });
    const [cities, countries, places, districts] = await Promise.all([
      get("/api/cities"), get("/data/countries.geojson"), get("/data/places.geojson"), get("/data/vn_districts.geojson"),
    ]);
    S.cities = cities; S.countries = countries; S.places = places; S.districts = districts;

    const activePids = new Set(cities.map((c) => c.pid));
    for (const f of places.features) f.properties.active = activePids.has(f.id) ? 1 : 0;
    for (const f of countries.features) S.countryByIso.set(f.properties.iso, f);
    for (const f of districts.features) {
      S.districtById.set(f.properties.id, f);
      S.districtCenter.set(f.properties.id, centerOf(f.geometry));
    }
    S.cityByPid = new Map(cities.map((c) => [c.pid, c]));
    S.heat = cities.map(() => -1e9);
    S.heatFlag = cities.map(() => 0);
    S.flagsCity = cities.map(() => []);
    S.countryLabels = {
      type: "FeatureCollection",
      features: countries.features.map((f) => ({
        type: "Feature", id: f.id,
        properties: { iso: f.properties.iso, name: f.properties.name, pop: f.properties.pop },
        geometry: { type: "Point", coordinates: [f.properties.lx, f.properties.ly] },
      })),
    };
  }

  // ---------- the map ----------
  // Coarser places first; each group appears from its zoom level. The groups are mutually exclusive, otherwise a
  // place in two of them would be labelled twice.
  const POP_EXPR = ["get", "pop"];
  const NOT_CAPITAL = ["!=", ["get", "kind"], "capital"];
  const PLACE_GROUPS = [
    { id: "cap", minzoom: 2.2, filter: ["==", ["get", "kind"], "capital"] },
    { id: "mega", minzoom: 2.8, filter: ["all", NOT_CAPITAL, [">=", POP_EXPR, 5000000]] },
    { id: "big", minzoom: 4.2, filter: ["all", NOT_CAPITAL, [">=", POP_EXPR, 1000000], ["<", POP_EXPR, 5000000]] },
    { id: "mid", minzoom: 5.5, filter: ["all", NOT_CAPITAL, [">=", POP_EXPR, 200000], ["<", POP_EXPR, 1000000]] },
    { id: "all", minzoom: 7, filter: ["all", NOT_CAPITAL, ["<", POP_EXPR, 200000]] },
  ];
  // Once a country is selected, all of its places come from one layer, so leave that country out of the groups.
  const groupFilter = (g, iso) => (iso ? ["all", g.filter, ["!=", ["get", "iso"], iso]] : g.filter);
  const NONE = ["==", ["get", "iso"], ""]; // matches nothing
  const HEAT = (stops) => ["interpolate", ["linear"], ["coalesce", ["feature-state", "flagged"], 0], ...stops];

  function createMap() {
    const map = new maplibregl.Map({
      container: "map",
      style: {
        version: 8,
        glyphs: "/fonts/{fontstack}/{range}.pbf",
        sources: {},
        layers: [{ id: "ocean", type: "background", paint: { "background-color": "#050b14" } }],
      },
      center: [100, 24], zoom: 1.6, minZoom: 0.2, maxZoom: 14,
      attributionControl: false, dragRotate: false, pitchWithRotate: false, touchPitch: false, doubleClickZoom: false,
    });
    map.touchZoomRotate.disableRotation();
    return map;
  }

  function addLayers(map) {
    map.addSource("countries", { type: "geojson", data: S.countries });
    map.addSource("country-labels", { type: "geojson", data: S.countryLabels });
    map.addSource("places", { type: "geojson", data: S.places });
    map.addSource("districts", { type: "geojson", data: S.districts });

    // land, borders, and a red wash on countries that currently have flagged traffic
    map.addLayer({
      id: "land", type: "fill", source: "countries",
      paint: { "fill-color": ["case", ["boolean", ["feature-state", "hover"], false], "#18293f", "#0f1b2b"] },
    });
    map.addLayer({
      id: "country-heat", type: "fill", source: "countries",
      paint: {
        "fill-color": HEAT([0, "rgba(255,59,92,0)", 1, "rgba(255,176,32,0.20)", 8, "rgba(255,59,92,0.42)"]),
        "fill-opacity": ["interpolate", ["linear"], ["zoom"], 5, 1, 9, 0.15], // fade out as the district layer takes over
      },
    });
    map.addLayer({
      id: "borders", type: "line", source: "countries",
      paint: { "line-color": "#34506f", "line-width": ["interpolate", ["linear"], ["zoom"], 1, 0.5, 6, 1.2, 10, 1.7] },
    });
    map.addLayer({ id: "country-dim", type: "fill", source: "countries", filter: NONE, paint: { "fill-color": "#04070c", "fill-opacity": 0.6 } });
    map.addLayer({ id: "country-sel", type: "line", source: "countries", filter: NONE, paint: { "line-color": "#3dd9eb", "line-width": 1.8 } });

    // districts of the selected city
    map.addLayer({
      id: "dist-fill", type: "fill", source: "districts", filter: NONE,
      paint: {
        "fill-color": HEAT([0, "rgba(61,217,235,0.13)", 1, "rgba(255,176,32,0.40)", 6, "rgba(255,59,92,0.62)"]),
        "fill-opacity": ["case", ["boolean", ["feature-state", "hover"], false], 1, 0.8],
      },
    });
    map.addLayer({ id: "dist-line", type: "line", source: "districts", filter: NONE, paint: { "line-color": "rgba(160,205,240,0.6)", "line-width": 1 } });
    map.addLayer({ id: "dist-sel", type: "line", source: "districts", filter: ["==", ["get", "id"], -1], paint: { "line-color": "#ffffff", "line-width": 2.4 } });
    map.addLayer({
      id: "dist-labels", type: "symbol", source: "districts", filter: NONE, minzoom: 7,
      layout: { "text-field": ["get", "name"], "text-font": ["Open Sans Regular"], "text-size": 11.5, "text-max-width": 7 },
      paint: { "text-color": "#d6e8fa", "text-halo-color": "#050b14", "text-halo-width": 1.3 },
    });

    // every place of the selected country, whatever the zoom
    map.addLayer({
      id: "pl-country-dot", type: "circle", source: "places", filter: NONE,
      paint: {
        "circle-radius": ["case", ["==", ["get", "kind"], "capital"], 4, 3],
        "circle-color": ["case", ["==", ["get", "kind"], "capital"], "#ffffff", "#8fb3d9"],
        "circle-stroke-color": "#050b14", "circle-stroke-width": 1,
      },
    });
    for (const g of PLACE_GROUPS) {
      const isCap = g.id === "cap";
      map.addLayer({
        id: `pl-dot-${g.id}`, type: "circle", source: "places", minzoom: g.minzoom, filter: g.filter,
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 2, isCap ? 2.6 : 2, 8, isCap ? 4.4 : 3.4],
          "circle-color": isCap ? "#ffffff" : "#8fb3d9",
          "circle-stroke-color": "#050b14", "circle-stroke-width": 1,
        },
      });
    }
    map.addLayer({
      id: "active-ring", type: "circle", source: "places", filter: ["==", ["get", "active"], 1], minzoom: 1.2,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, 3.5, 6, 8, 10, 11],
        "circle-color": "rgba(0,0,0,0)", "circle-stroke-width": 1.6,
        "circle-stroke-color": ["case", [">", ["coalesce", ["feature-state", "flagged"], 0], 0], "#ff3b5c", "#3dd9eb"],
      },
    });

    // names: countries first, then places
    map.addLayer({
      id: "country-names", type: "symbol", source: "country-labels", maxzoom: 7,
      layout: {
        "text-field": ["get", "name"], "text-font": ["Open Sans Bold"], "text-transform": "uppercase", "text-letter-spacing": 0.14,
        "text-size": ["interpolate", ["linear"], ["zoom"], 1, 9, 4, 12.5, 6, 15], "text-max-width": 8,
        "symbol-sort-key": ["-", 0, ["get", "pop"]],
      },
      paint: { "text-color": "#7f97b3", "text-halo-color": "#050b14", "text-halo-width": 1.4, "text-opacity": ["interpolate", ["linear"], ["zoom"], 5, 1, 7, 0] },
    });
    const labelLayout = (cap) => ({
      "text-field": ["get", "name"], "text-font": [cap ? "Open Sans Bold" : "Open Sans Regular"],
      "text-size": ["interpolate", ["linear"], ["zoom"], 2, 10, 8, 13],
      "text-variable-anchor": ["left", "right", "top", "bottom"], "text-radial-offset": 0.55, "text-justify": "auto",
      "symbol-sort-key": ["-", 0, ["get", "pop"]], "text-max-width": 8,
    });
    const labelPaint = (cap) => ({ "text-color": cap ? "#f3f8ff" : "#b9cde2", "text-halo-color": "#050b14", "text-halo-width": 1.4 });
    const isCap = ["==", ["get", "kind"], "capital"];
    map.addLayer({
      id: "pl-country-label", type: "symbol", source: "places", filter: NONE,
      layout: {
        ...labelLayout(false),
        "text-font": ["case", isCap, ["literal", ["Open Sans Bold"]], ["literal", ["Open Sans Regular"]]],
      },
      paint: { ...labelPaint(false), "text-color": ["case", isCap, "#f3f8ff", "#b9cde2"] },
    });
    for (const g of PLACE_GROUPS) {
      map.addLayer({
        id: `pl-lbl-${g.id}`, type: "symbol", source: "places", minzoom: g.minzoom, filter: g.filter,
        layout: labelLayout(g.id === "cap"), paint: labelPaint(g.id === "cap"),
      });
    }
  }

  const PLACE_HIT = () => ["active-ring", "pl-country-dot", ...PLACE_GROUPS.map((g) => `pl-dot-${g.id}`)].filter((id) => S.map.getLayer(id));

  function pick(point) {
    const map = S.map;
    const d = map.queryRenderedFeatures(point, { layers: ["dist-fill"] })[0];
    if (d) return { kind: "district", id: d.properties.id };
    const r = 8;
    const hits = map.queryRenderedFeatures([[point.x - r, point.y - r], [point.x + r, point.y + r]], { layers: PLACE_HIT() });
    if (hits.length) {
      const best = hits.reduce((a, b) => ((b.properties.pop || 0) > (a.properties.pop || 0) ? b : a));
      return { kind: "place", id: best.properties.id };
    }
    const c = map.queryRenderedFeatures(point, { layers: ["land"] })[0];
    if (c) return { kind: "country", iso: c.properties.iso, id: c.id };
    return null;
  }

  // ---------- navigation: world > country > city > district ----------
  const padding = () => (narrow() ? { top: 150, bottom: 270, left: 16, right: 16 } : { top: 120, bottom: 150, left: 40, right: 350 });

  function setFilters() {
    const map = S.map;
    const iso = NAV.iso || "";
    map.setFilter("country-dim", iso ? ["!=", ["get", "iso"], iso] : NONE);
    map.setFilter("country-sel", iso ? ["==", ["get", "iso"], iso] : NONE);
    map.setFilter("pl-country-dot", iso ? ["==", ["get", "iso"], iso] : NONE);
    map.setFilter("pl-country-label", iso ? ["==", ["get", "iso"], iso] : NONE);
    for (const g of PLACE_GROUPS) {
      map.setFilter(`pl-dot-${g.id}`, groupFilter(g, iso));
      map.setFilter(`pl-lbl-${g.id}`, groupFilter(g, iso));
    }
    const hasDistricts = NAV.place && NAV.place.properties.districts > 0;
    const cityFilter = hasDistricts ? ["==", ["get", "city"], NAV.place.properties.ascii] : NONE;
    for (const id of ["dist-fill", "dist-line", "dist-labels"]) map.setFilter(id, cityFilter);
    map.setFilter("dist-sel", ["==", ["get", "id"], NAV.district == null ? -1 : NAV.district]);
    renderCrumbs();
    updateRegion(true);
  }

  function userNavigated() {
    NAV.token++; // cancels any camera sequence still waiting to run
    ST.userNav = true;
  }

  function goWorld(animate = true) {
    Object.assign(NAV, { level: "world", iso: null, place: null, district: null });
    setFilters();
    S.map.fitBounds(WORLD_BOUNDS, { padding: padding(), duration: animate ? 1300 : 0, maxZoom: 3.2 });
  }

  function goCountry(iso, animate = true) {
    const f = S.countryByIso.get(iso);
    if (!f) return false;
    Object.assign(NAV, { level: "country", iso, place: null, district: null });
    setFilters();
    S.map.fitBounds(boundsOf(f.geometry, true), { padding: padding(), duration: animate ? 1300 : 0, maxZoom: 7.5 });
    return true;
  }

  function districtsOf(city) {
    return S.districts.features.filter((f) => f.properties.city === city);
  }

  function goCity(place, animate = true) {
    const p = place.properties;
    if (S.countryByIso.has(p.iso)) NAV.iso = p.iso;
    Object.assign(NAV, { level: "city", place, district: null });
    setFilters();
    const duration = animate ? 1300 : 0;
    if (p.districts > 0) {
      const b = unionBounds(districtsOf(p.ascii).map((f) => boundsOf(f.geometry, false)));
      S.map.fitBounds(b, { padding: padding(), duration, maxZoom: 11 });
    } else {
      const [lon, lat] = place.geometry.coordinates;
      // keep the city clear of the side panel (wide) or the incident cards (narrow)
      S.map.flyTo({ center: [lon, lat], zoom: 8.5, duration, offset: narrow() ? [0, -190] : [-170, 0], essential: true });
    }
  }

  function goDistrict(id, animate = true) {
    const f = S.districtById.get(id);
    if (!f) return;
    NAV.level = "district";
    NAV.district = id;
    setFilters();
    S.map.fitBounds(boundsOf(f.geometry, false), { padding: padding(), duration: animate ? 1100 : 0, maxZoom: 13 });
  }

  function goUp() {
    if (NAV.level === "district") goCity(NAV.place);
    else if (NAV.level === "city") (NAV.iso ? goCountry(NAV.iso) : goWorld());
    else if (NAV.level === "country") goWorld();
  }

  const placeFeature = (id) => S.places.features[id];

  function renderCrumbs() {
    const nav = $("crumbs");
    nav.replaceChildren();
    const items = [{ label: "World", go: () => goWorld(), level: "world" }];
    if (NAV.iso) {
      const c = S.countryByIso.get(NAV.iso);
      items.push({ label: c ? c.properties.name : NAV.iso, go: () => goCountry(NAV.iso), level: "country" });
    }
    if (NAV.place && NAV.level !== "country") items.push({ label: NAV.place.properties.name, go: () => goCity(NAV.place), level: "city" });
    if (NAV.district != null) {
      items.push({ label: S.districtById.get(NAV.district).properties.name, go: () => {}, level: "district" });
    }
    items.forEach((it, i) => {
      if (i) { const sep = document.createElement("span"); sep.className = "crumb-sep"; sep.textContent = "›"; nav.appendChild(sep); }
      const b = document.createElement("button");
      b.className = "crumb" + (i === items.length - 1 ? " current" : "");
      b.type = "button";
      b.textContent = it.label;
      if (i < items.length - 1) b.addEventListener("click", () => { userNavigated(); it.go(); });
      nav.appendChild(b);
    });
  }

  // ---------- flagged activity per region ----------
  const recent = (arr, now) => {
    if (!arr) return 0;
    while (arr.length && now - arr[0] > FLAG_WINDOW_MS) arr.shift();
    return arr.length;
  };

  function bump(map, key, now) {
    let q = map.get(key);
    if (!q) { q = []; map.set(key, q); }
    q.push(now);
    if (q.length > 400) q.shift();
  }

  function touch(it, now) {
    const flagged = it.action !== "allow";
    for (const [cityId, districtId] of [[it.src_city, it.src_district], [it.dst_city, it.dst_district]]) {
      const c = S.cities[cityId];
      if (!c) continue;
      S.heat[cityId] = now;
      S.heatFlag[cityId] = flagged ? 1 : 0;
    }
    if (!flagged) return;
    const seenCity = new Set(), seenIso = new Set(), seenDist = new Set();
    for (const [cityId, districtId] of [[it.src_city, it.src_district], [it.dst_city, it.dst_district]]) {
      const c = S.cities[cityId];
      if (!c) continue;
      if (!seenCity.has(cityId)) { seenCity.add(cityId); const q = S.flagsCity[cityId]; q.push(now); if (q.length > 400) q.shift(); }
      if (!seenIso.has(c.iso)) { seenIso.add(c.iso); bump(S.flagsCountry, c.iso, now); }
      if (districtId >= 0 && !seenDist.has(districtId)) { seenDist.add(districtId); bump(S.flagsDistrict, districtId, now); }
    }
  }

  function syncFeatureState(now) {
    const map = S.map;
    const write = (source, store, id, value) => {
      if (store.get(id) === value) return;
      store.set(id, value);
      map.setFeatureState({ source, id }, { flagged: value });
    };
    for (const f of S.countries.features) write("countries", S.written.countries, f.id, recent(S.flagsCountry.get(f.properties.iso), now));
    for (const f of S.districts.features) write("districts", S.written.districts, f.id, recent(S.flagsDistrict.get(f.properties.id), now));
    S.cities.forEach((c, i) => write("places", S.written.places, c.pid, recent(S.flagsCity[i], now)));
  }

  // ---------- region panel ----------
  const POP = (n) => (n >= 1000 ? compact.format(n) : String(n));

  function regionRows(now) {
    if (NAV.level === "world") {
      const rows = [...S.flagsCountry.entries()]
        .map(([iso, q]) => ({ iso, n: recent(q, now) }))
        .filter((r) => r.n > 0 && S.countryByIso.has(r.iso))
        .sort((a, b) => b.n - a.n).slice(0, 12)
        .map((r) => ({ label: S.countryByIso.get(r.iso).properties.name, n: r.n, hot: true, go: () => goCountry(r.iso) }));
      return { rows, head: "Hot countries", empty: "No flagged activity right now. Launch an attack to see it light up." };
    }
    if (NAV.level === "country") {
      const rows = S.cities.filter((c) => c.iso === NAV.iso).map((c) => ({ c, n: recent(S.flagsCity[c.id], now) }))
        .sort((a, b) => b.n - a.n || placeFeature(b.c.pid).properties.pop - placeFeature(a.c.pid).properties.pop)
        .map(({ c, n }) => ({
          label: c.name, n: n || POP(placeFeature(c.pid).properties.pop), hot: n > 0,
          go: () => goCity(placeFeature(c.pid)),
        }));
      return { rows, head: "Simulated cities", empty: "No simulated traffic in this country." };
    }
    const city = NAV.place.properties;
    if (city.districts > 0) {
      const rows = districtsOf(city.ascii)
        .map((f) => ({ f, n: recent(S.flagsDistrict.get(f.properties.id), now) }))
        .sort((a, b) => b.n - a.n || a.f.properties.name.localeCompare(b.f.properties.name))
        .map(({ f, n }) => ({ label: f.properties.name, n: n || "–", hot: n > 0, go: () => goDistrict(f.properties.id) }));
      return { rows, head: "Districts", empty: "" };
    }
    return { rows: [], head: "Districts", empty: "" };
  }

  function updateRegion(force) {
    const now = performance.now();
    let title, level, sub, flagged, count, countLabel, note = "";
    if (NAV.level === "world") {
      level = "World"; title = "World";
      sub = "Click a country to drill down";
      flagged = [...S.flagsCountry.values()].reduce((s, q) => s + recent(q, now), 0);
      count = S.countries.features.length; countLabel = "Countries";
    } else if (NAV.level === "country") {
      const c = S.countryByIso.get(NAV.iso).properties;
      level = "Country"; title = c.name;
      sub = `${c.name_vi !== c.name ? c.name_vi + " · " : ""}${c.continent} · pop. ${POP(c.pop)}`;
      flagged = recent(S.flagsCountry.get(NAV.iso), now);
      count = S.cities.filter((x) => x.iso === NAV.iso).length; countLabel = "Simulated cities";
      note = "Click a city to zoom in. Districts are available for Viet Nam's five big cities.";
    } else {
      const p = NAV.place.properties;
      const cid = (S.cityByPid.get(NAV.place.id) || {}).id;
      level = NAV.level === "district" ? "District" : (p.kind === "capital" ? "Capital city" : "City");
      if (NAV.level === "district") {
        title = S.districtById.get(NAV.district).properties.name;
        sub = `${p.name} · ${p.country}`;
        flagged = recent(S.flagsDistrict.get(NAV.district), now);
        count = districtsOf(p.ascii).length; countLabel = "Districts in city";
      } else {
        title = p.name; sub = `${p.country} · pop. ${POP(p.pop)}`;
        flagged = cid == null ? 0 : recent(S.flagsCity[cid], now);
        count = p.districts; countLabel = "Districts";
        if (!p.districts) note = "No district boundaries for this city. They are built for Viet Nam's five big cities: Hanoi, Ho Chi Minh City, Da Nang, Haiphong and Can Tho.";
        else if (cid == null) note = "";
      }
      if (cid == null && NAV.level !== "district") note = (note ? note + " " : "") + "No simulated traffic here.";
    }

    const { rows, head, empty } = regionRows(now);
    const sig = [NAV.level, NAV.iso, NAV.district, title, flagged, count, head, note, rows.map((r) => `${r.label}:${r.n}`).join("|")].join("~");
    if (!force && sig === S.listSig) return;
    S.listSig = sig;

    $("rg-level").textContent = level;
    $("rg-title").textContent = title;
    $("rg-sub").textContent = sub;
    $("rg-flagged").textContent = num.format(flagged);
    $("rg-flagged").className = flagged ? "bad" : "";
    $("rg-count").textContent = num.format(count || 0);
    $("rg-count-label").textContent = countLabel;
    $("rg-list-head").textContent = head;
    $("rg-note").textContent = note;
    const ul = $("rg-list");
    ul.replaceChildren();
    if (!rows.length && empty) {
      const li = document.createElement("li"); li.className = "empty"; li.textContent = empty; ul.appendChild(li);
    }
    for (const r of rows) {
      const li = document.createElement("li");
      const a = document.createElement("span"); a.textContent = r.label;
      const b = document.createElement("span"); b.className = "n" + (r.hot ? " hot" : ""); b.textContent = r.n;
      li.append(a, b);
      li.addEventListener("click", () => { userNavigated(); r.go(); });
      ul.appendChild(li);
    }
    $("rg-count-label").parentElement.style.display = count == null ? "none" : "";
  }

  // ---------- hover tooltip ----------
  function showTip(lines, x, y) {
    const tip = $("tip");
    tip.replaceChildren();
    lines.forEach(([tag, text, cls], i) => {
      const el = document.createElement(i === 0 ? "b" : "span");
      el.textContent = text;
      if (cls) el.className = cls;
      tip.appendChild(el);
    });
    tip.style.left = `${Math.min(x + 16, innerWidth - 220)}px`;
    tip.style.top = `${y + 16}px`;
    tip.style.opacity = 1;
  }

  function setHover(kind, id) {
    const map = S.map;
    const prev = S.hover[kind];
    if (prev === id) return;
    const source = kind === "country" ? "countries" : "districts";
    if (prev != null) map.setFeatureState({ source, id: prev }, { hover: false });
    if (id != null) map.setFeatureState({ source, id }, { hover: true });
    S.hover[kind] = id;
  }

  function onMouseMove(e) {
    const hit = pick(e.point);
    S.map.getCanvas().style.cursor = hit ? "pointer" : "";
    const now = performance.now();
    setHover("country", hit && hit.kind === "country" ? hit.id : null);
    setHover("district", hit && hit.kind === "district" ? S.districtById.get(hit.id).id : null);
    if (!hit) { $("tip").style.opacity = 0; return; }

    if (hit.kind === "country") {
      const p = S.countryByIso.get(hit.iso).properties;
      const n = recent(S.flagsCountry.get(hit.iso), now);
      showTip([["b", p.name], ["s", `${p.continent} · pop. ${POP(p.pop)}`], ["s", `${n} flagged in last 60 s`, n ? "hot" : ""]], e.originalEvent.clientX, e.originalEvent.clientY);
    } else if (hit.kind === "place") {
      const f = placeFeature(hit.id), p = f.properties, c = S.cityByPid.get(f.id);
      const n = c ? recent(S.flagsCity[c.id], now) : 0;
      const lines = [["b", p.name], ["s", `${p.country} · ${p.kind === "capital" ? "capital · " : ""}pop. ${POP(p.pop)}`]];
      lines.push(c ? ["s", `${n} flagged in last 60 s`, n ? "hot" : ""] : ["s", "no simulated traffic"]);
      showTip(lines, e.originalEvent.clientX, e.originalEvent.clientY);
    } else {
      const f = S.districtById.get(hit.id), n = recent(S.flagsDistrict.get(hit.id), now);
      showTip([["b", f.properties.name], ["s", f.properties.city], ["s", `${n} flagged in last 60 s`, n ? "hot" : ""]], e.originalEvent.clientX, e.originalEvent.clientY);
    }
  }

  function onClick(e) {
    const hit = pick(e.point);
    if (!hit) return;
    userNavigated();
    if (hit.kind === "district") goDistrict(hit.id);
    else if (hit.kind === "place") goCity(placeFeature(hit.id));
    else if (hit.iso !== NAV.iso || NAV.level !== "country") goCountry(hit.iso);
  }

  // ---------- fraud overlay on a canvas above the map ----------
  const quad = (a, c, b, t) => (1 - t) * (1 - t) * a + 2 * (1 - t) * t * c + t * t * b;

  function anchor(cityId, districtId) {
    const c = S.cities[cityId];
    if (!c) return null;
    if (districtId >= 0 && S.map.getZoom() >= DISTRICT_ZOOM) {
      const d = S.districtCenter.get(districtId);
      if (d) { const p = S.map.project(d); return [p.x, p.y]; }
    }
    const p = S.map.project([c.lon, c.lat]);
    return [p.x, p.y];
  }

  function addArc(it, now) {
    touch(it, now);
    const flagged = it.action !== "allow";
    if (it.src_city === it.dst_city && it.src_district === it.dst_district) {
      if (flagged) S.pulses.push({ city: it.dst_city, district: it.dst_district, t0: now, c: COL[it.action] });
      return;
    }
    S.arcs.push({
      sc: it.src_city, sd: it.src_district, dc: it.dst_city, dd: it.dst_district,
      t0: now, dur: (flagged ? 900 : 1100) + Math.random() * 500, action: it.action,
    });
    if (S.arcs.length > ARC_MAX) {
      const keep = S.arcs.filter((x, i) => x.action !== "allow" || i > S.arcs.length - ARC_MAX / 2);
      S.arcs = keep.slice(-ARC_MAX);
    }
  }

  function drawFx(now) {
    const { ctx, w, h } = S.fx;
    ctx.clearRect(0, 0, w, h);
    const quiet = ST.phase === "armed" || ST.phase === "alert";
    S.dim += ((quiet ? 0.25 : 1) - S.dim) * 0.06;

    ctx.globalCompositeOperation = "lighter";
    for (let i = 0; i < S.cities.length; i++) { // cities glow briefly after traffic touches them
      const hot = Math.max(0, 1 - (now - S.heat[i]) / 1600);
      if (hot <= 0) continue;
      const a = anchor(i, -1);
      if (!a || a[0] < -20 || a[0] > w + 20 || a[1] < -20 || a[1] > h + 20) continue;
      const c = S.heatFlag[i] ? COL.block : COL.allow;
      ctx.beginPath(); ctx.arc(a[0], a[1], 3 + hot * 6, 0, 6.283);
      ctx.fillStyle = rgba(c, 0.22 * hot * (S.heatFlag[i] ? 1 : S.dim)); ctx.fill();
    }

    const alive = [];
    for (const a of S.arcs) {
      const p = (now - a.t0) / a.dur;
      const A = anchor(a.sc, a.sd), B = anchor(a.dc, a.dd);
      if (p >= 1) {
        if (a.action !== "allow") S.pulses.push({ city: a.dc, district: a.dd, t0: now, c: COL[a.action] });
        continue;
      }
      alive.push(a);
      if (!A || !B) continue;
      const m = 120;
      if ((A[0] < -m && B[0] < -m) || (A[0] > w + m && B[0] > w + m) || (A[1] < -m && B[1] < -m) || (A[1] > h + m && B[1] > h + m)) continue;
      const flagged = a.action !== "allow", c = COL[a.action];
      const dist = Math.hypot(B[0] - A[0], B[1] - A[1]);
      const cx = (A[0] + B[0]) / 2, cy = (A[1] + B[1]) / 2 - dist * (flagged ? 0.3 : 0.2);
      const from = Math.max(0, p - 0.4);
      ctx.beginPath();
      for (let i = 0; i <= 9; i++) {
        const t = from + ((p - from) * i) / 9;
        const x = quad(A[0], cx, B[0], t), y = quad(A[1], cy, B[1], t);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.lineCap = "round";
      if (flagged) {
        ctx.strokeStyle = rgba(c, 0.16); ctx.lineWidth = 7; ctx.stroke();
        ctx.strokeStyle = rgba(c, 0.95); ctx.lineWidth = 2.2; ctx.stroke();
      } else {
        ctx.strokeStyle = rgba(c, 0.36 * S.dim); ctx.lineWidth = 1; ctx.stroke();
      }
      const hx = quad(A[0], cx, B[0], p), hy = quad(A[1], cy, B[1], p);
      ctx.beginPath(); ctx.arc(hx, hy, flagged ? 3 : 1.5, 0, 6.283);
      ctx.fillStyle = rgba(flagged ? [255, 230, 235] : c, flagged ? 1 : 0.75 * S.dim); ctx.fill();
    }
    S.arcs = alive;

    const pulses = [];
    for (const q of S.pulses) {
      const p = (now - q.t0) / 1000;
      if (p >= 1) continue;
      pulses.push(q);
      const a = anchor(q.city, q.district);
      if (!a) continue;
      ctx.beginPath(); ctx.arc(a[0], a[1], 3 + p * 26, 0, 6.283);
      ctx.strokeStyle = rgba(q.c, (1 - p) * 0.9); ctx.lineWidth = 2; ctx.stroke();
    }
    S.pulses = pulses.slice(-120);
    ctx.globalCompositeOperation = "source-over";
    drawReticle(ctx, now, w, h);
  }

  function drawReticle(ctx, now, w, h) {
    if (!ST.target || (ST.phase !== "alert" && ST.phase !== "contained")) return;
    const a = anchor(ST.target.city, ST.target.district);
    if (!a || a[0] < -60 || a[0] > w + 60 || a[1] < -60 || a[1] > h + 60) return;
    const col = ST.phase === "contained" ? COL.safe : COL.block;
    const t = now / 1000;
    ctx.save();
    ctx.translate(a[0], a[1]);
    ctx.strokeStyle = rgba(col, 0.95); ctx.lineWidth = 1.6;
    ctx.rotate(t * 0.9); ctx.setLineDash([7, 6]);
    ctx.beginPath(); ctx.arc(0, 0, 26, 0, 6.283); ctx.stroke();
    ctx.rotate(-t * 0.9); ctx.setLineDash([]);
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      ctx.beginPath(); ctx.moveTo(dx * 18, dy * 18); ctx.lineTo(dx * 36, dy * 36); ctx.stroke();
    }
    const city = (S.cities[ST.target.city] || {}).name || "";
    const dist = ST.target.district >= 0 ? (S.districtById.get(ST.target.district) || { properties: {} }).properties.name : "";
    const label = `MULE  ${ST.target.acct}  ·  ${dist ? dist + ", " : ""}${city}`;
    ctx.font = "600 11px system-ui, sans-serif"; ctx.textBaseline = "middle";
    const tw = ctx.measureText(label).width;
    // wide screens: beside the reticle; narrow screens: above it, centred, clear of the incident cards
    const flip = a[0] + 44 + tw + 16 > w - 12;
    let x0 = flip ? -44 - tw - 16 : 44, y0 = 0;
    if (narrow()) { x0 = -(tw + 16) / 2; y0 = -50; }
    x0 = Math.max(x0, -a[0] + 6);
    ctx.fillStyle = "rgba(4,7,12,.82)"; ctx.fillRect(x0, y0 - 11, tw + 16, 22);
    ctx.fillStyle = rgba(col, 1); ctx.fillText(label, x0 + 8, y0);
    ctx.restore();
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

  function beginIncident(sc, now) {
    ST.incidentId = sc.id;
    ST.startedAt = now;
    ST.userNav = false;
    ST.accounts = new Set();
    ST.target = null;
    ST.why = "";
    ST.origins = new Map();
    E.blocked = E.stopped = E.leaked = 0;
    $("inc-title").textContent = `Incident · ${sc.id}`;
    $("inc-clock-label").textContent = "attack in progress, undetected";
    // reset the network so only this incident is on screen
    G.nodes.clear(); G.links = []; G.pending = []; G.dirty = true;
    setPhase("armed");
  }

  // The camera follows the money: country, then city, then the district of the mule account.
  function drillToTarget() {
    if (ST.userNav || !ST.target) return;
    const city = S.cities[ST.target.city];
    if (!city) return;
    const place = placeFeature(city.pid);
    const token = ++NAV.token;
    const stillMine = () => token === NAV.token && !ST.userNav;
    if (!goCountry(city.iso)) { goCity(place); return; }
    setTimeout(() => {
      if (!stillMine()) return;
      goCity(place);
      if (ST.target.district >= 0 && place.properties.districts > 0) {
        setTimeout(() => { if (stillMine()) goDistrict(ST.target.district); }, 1500);
      }
    }, 1400);
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
          if (!ST.target) ST.target = { acct: it.dst, city: it.dst_city, district: it.dst_district };
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
      drillToTarget();
    }

    if (ST.phase === "armed" && now - ST.startedAt > ARMED_TIMEOUT_MS) {
      setPhase("contained");
      ST.containedAt = now;
      $("inc-clock-label").textContent = "attack was not detected";
      moment("NOT DETECTED", "the ring got through", "warn", 3500);
    } else if (ST.phase === "alert" && now - ST.lastActivity > QUIET_MS) {
      setPhase("contained");
      ST.containedAt = now;
      retrigger($("flash"), "safe");
      const pctBlocked = sc.tx ? Math.round((sc.blocked / sc.tx) * 100) : 0;
      moment("RING CONTAINED", `${sc.blocked} of ${sc.tx} transactions blocked (${pctBlocked}%) · ${usd.format(sc.amount_blocked)} stopped`, "safe", 4500);
    } else if (ST.phase === "contained" && now - ST.containedAt > CONTAINED_SHOW_MS) {
      setPhase("calm");
      if (!ST.userNav) goWorld(); // pull back out, unless the viewer is exploring on their own
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
    if (ST.target) {
      const c = S.cities[ST.target.city] || {};
      const d = ST.target.district >= 0 ? (S.districtById.get(ST.target.district) || { properties: {} }).properties.name : "";
      $("inc-target").textContent = `${ST.target.acct} · ${d ? d + ", " : ""}${c.name || ""}`;
    } else {
      $("inc-target").textContent = "–";
    }
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
      drawFx(now);
      drawGraph(now);
      renderIncident(now);
    } catch (e) {
      console.error(e); // keep the loop alive; one bad frame must not freeze the scene
    }
    requestAnimationFrame(frame);
  }

  async function boot() {
    try {
      await loadData();
    } catch (e) {
      console.error(e);
      $("rg-sub").textContent = "Could not load map data.";
      return;
    }
    const map = (S.map = createMap());
    window.__map = map; // debug handles: inspect the map and drive navigation from the browser console
    window.__nav = { NAV, goWorld, goCountry, goCity, goDistrict, goUp, places: () => S.places.features, districts: () => S.districts.features };
    S.fx = fit($("fx"));
    await new Promise((resolve) => map.on("load", resolve));
    addLayers(map);
    goWorld(false);

    let moveQueued = false, lastMove = null;
    map.on("mousemove", (e) => {
      lastMove = e;
      if (moveQueued) return;
      moveQueued = true;
      requestAnimationFrame(() => { moveQueued = false; onMouseMove(lastMove); });
    });
    map.getCanvas().addEventListener("mouseleave", () => { $("tip").style.opacity = 0; setHover("country", null); setHover("district", null); });
    map.on("click", onClick);
    window.addEventListener("keydown", (e) => { if (e.key === "Escape") { userNavigated(); goUp(); } });
    window.addEventListener("resize", () => { S.fx = fit($("fx")); });
    new ResizeObserver(buildGraphView).observe($("network"));
    buildGraphView();
    setInterval(() => { const now = performance.now(); syncFeatureState(now); updateRegion(false); }, STATE_MS);

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
