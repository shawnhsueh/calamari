"use strict";

const VANCOUVER = [49.2577, -123.1207];
const BINS = 5;
const dark = window.matchMedia("(prefers-color-scheme: dark)");
const css = getComputedStyle(document.querySelector(".viz-root"));
const binColor = (i) => css.getPropertyValue(`--bin-${i + 1}`).trim();
const ringColor = () => css.getPropertyValue("--ring").trim();
const money = (v) => (v == null ? "–" : `$${Number(v).toFixed(v % 1 ? 2 : 0)}`);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

const map = L.map("map", { zoomControl: true }).setView(VANCOUVER, 13);
// Esri's neutral gray canvas: no API key, and gray keeps the blue price ramp readable.
const tileUrl = () =>
  `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${dark.matches ? "Dark" : "Light"}_Gray_Base/MapServer/tile/{z}/{y}/{x}`;
const tiles = L.tileLayer(tileUrl(), {
  maxZoom: 19,
  maxNativeZoom: 16,
  attribution: 'Tiles &copy; <a href="https://www.esri.com">Esri</a> &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors',
}).addTo(map);

let features = [];
let breaks = []; // upper bound of each bin except the last
const markers = new Map(); // id -> circle marker

// Quantile breaks over the full dataset, so filtering never repaints a restaurant.
function quantileBreaks(prices) {
  const sorted = [...prices].sort((a, b) => a - b);
  const out = [];
  for (let i = 1; i < BINS; i++) out.push(sorted[Math.floor((i / BINS) * (sorted.length - 1))]);
  return out;
}
const binOf = (price) => {
  const i = breaks.findIndex((b) => price <= b);
  return i === -1 ? BINS - 1 : i;
};

function median(values) {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function popupHtml(p) {
  const items = (p.items || [])
    .map((i) => `<tr><td>${esc(i.dish_name)}${i.portion_note ? ` · ${esc(i.portion_note)}` : ""}${i.is_happy_hour ? " · happy hour" : ""}</td><td class="num">${money(i.price)}</td></tr>`)
    .join("");
  const level = p.price_level ? ` · Google ${"$".repeat(p.price_level)}` : "";
  const conf = p.confidence && p.confidence !== "high" ? ` <span class="badge">${esc(p.confidence)} confidence</span>` : "";
  return `<div class="popup">
    <h3>${esc(p.name)}</h3>
    <div class="addr">${esc(p.address)}</div>
    <div><span class="big">${money(p.price)}</span><span class="dish">${esc(p.dish || "calamari")}</span>${conf}</div>
    ${items && (p.items || []).length > 1 ? `<table>${items}</table>` : ""}
    ${p.notes ? `<div class="meta">${esc(p.notes)}</div>` : ""}
    <div class="meta">${p.price_source === "manual" ? "Entered manually" : "Read from menu"}${p.last_checked ? ` · checked ${esc(p.last_checked)}` : ""}${level}</div>
    <div class="links">
      ${p.source_url ? `<a href="${esc(p.source_url)}" target="_blank" rel="noopener">Menu</a>` : ""}
      ${p.google_maps_url ? `<a href="${esc(p.google_maps_url)}" target="_blank" rel="noopener">Google Maps</a>` : ""}
    </div>
  </div>`;
}

function markerStyle(p) {
  return { radius: 7, weight: 2, color: ringColor(), fillColor: binColor(binOf(p.price)), fillOpacity: 1 };
}

function buildMarkers() {
  for (const f of features) {
    const p = f.properties;
    const [lng, lat] = f.geometry.coordinates;
    const m = L.circleMarker([lat, lng], markerStyle(p))
      .bindTooltip(`<strong>${esc(p.name)}</strong> · ${money(p.price)}`, { direction: "top", offset: [0, -6] })
      .bindPopup(popupHtml(p), { maxWidth: 300 });
    m.on("mouseover", () => m.setStyle({ radius: 9 }));
    m.on("mouseout", () => m.setStyle({ radius: 7 }));
    markers.set(p.id, m);
  }
}

function renderLegend() {
  const prices = features.map((f) => f.properties.price);
  const lo = Math.min(...prices);
  const hi = Math.max(...prices);
  const edges = [lo, ...breaks, hi];
  document.getElementById("legend-bins").innerHTML = Array.from({ length: BINS }, (_, i) => {
    const from = i === 0 ? edges[0] : edges[i];
    const label = i === 0 ? `≤ ${money(edges[1])}` : i === BINS - 1 ? `> ${money(edges[i])}` : `${money(from)}–${money(edges[i + 1])}`;
    return `<li><span class="swatch" style="background:${binColor(i)}"></span>${label}</li>`;
  }).join("");
}

const els = {
  search: document.getElementById("search"),
  min: document.getElementById("price-min"),
  max: document.getElementById("price-max"),
  hideLow: document.getElementById("hide-low"),
  rangeLabel: document.getElementById("range-label"),
  list: document.getElementById("list"),
  listCount: document.getElementById("list-count"),
};

function applyFilters() {
  let lo = Number(els.min.value);
  let hi = Number(els.max.value);
  if (lo > hi) [lo, hi] = [hi, lo];
  els.rangeLabel.textContent = `${money(lo)} – ${money(hi)}`;
  const q = els.search.value.trim().toLowerCase();

  const visible = features.filter(({ properties: p }) =>
    p.price >= lo && p.price <= hi &&
    !(els.hideLow.checked && p.confidence === "low") &&
    (!q || p.name.toLowerCase().includes(q))
  );
  const ids = new Set(visible.map((f) => f.properties.id));
  for (const [id, m] of markers) {
    if (ids.has(id)) m.addTo(map);
    else m.remove();
  }

  const prices = visible.map((f) => f.properties.price);
  document.getElementById("stat-count").textContent = visible.length;
  document.getElementById("stat-median").textContent = money(median(prices));
  document.getElementById("stat-min").textContent = prices.length ? money(Math.min(...prices)) : "–";
  document.getElementById("stat-max").textContent = prices.length ? money(Math.max(...prices)) : "–";

  els.listCount.textContent = `(${visible.length})`;
  els.list.innerHTML = visible.length
    ? visible
        .map(({ properties: p }) => `<li><button data-id="${esc(p.id)}">
          <span class="dot" style="background:${binColor(binOf(p.price))}"></span>
          <span class="name">${esc(p.name)}</span>
          <span class="price">${money(p.price)}</span></button></li>`)
        .join("")
    : `<li class="empty">No restaurants match these filters.</li>`;
}

els.list.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-id]");
  if (!btn) return;
  const m = markers.get(btn.dataset.id);
  map.flyTo(m.getLatLng(), Math.max(map.getZoom(), 16), { duration: 0.6 });
  m.openPopup();
});
for (const el of [els.search, els.min, els.max, els.hideLow]) el.addEventListener("input", applyFilters);

// Follow OS theme changes: swap tiles and recolor from the new CSS variables.
dark.addEventListener("change", () => {
  tiles.setUrl(tileUrl());
  for (const f of features) markers.get(f.properties.id).setStyle(markerStyle(f.properties));
  renderLegend();
  applyFilters();
});

async function init() {
  let data;
  try {
    const resp = await fetch("data/restaurants.geojson", { cache: "no-cache" });
    if (!resp.ok) throw new Error(resp.status);
    data = await resp.json();
  } catch {
    data = { features: [] };
  }
  features = (data.features || []).filter((f) => typeof f.properties.price === "number");
  if (!features.length) {
    els.list.innerHTML = `<li class="empty">No data yet. Run <code>python -m pipeline.run</code> to build it.</li>`;
    return;
  }
  breaks = quantileBreaks(features.map((f) => f.properties.price));
  const lo = Math.floor(Math.min(...features.map((f) => f.properties.price)));
  const hi = Math.ceil(Math.max(...features.map((f) => f.properties.price)));
  for (const el of [els.min, els.max]) Object.assign(el, { min: lo, max: hi });
  els.min.value = lo;
  els.max.value = hi;

  buildMarkers();
  renderLegend();
  applyFilters();
  map.fitBounds(L.featureGroup([...markers.values()]).getBounds(), { padding: [30, 30], maxZoom: 15 });
}

init();
