// 3D pogled — MapLibre GL with terrain (AWS Terrarium elevation tiles), draped with the current
// base map + visible raster overlays. All tiles go through the local cache, so terrain you have
// seen once also works offline. Same library/version and terrain source as the original app.
import { map } from "./map.js";
import { state, emit } from "./state.js";
import { tileHost, TILE_SUBDOMAINS } from "./api.js";
import { icon } from "./icons.js";
import { toast, h } from "./util.js";

let ml = null, wrap = null, loading = null;

function loadLib() {
  if (window.maplibregl) return Promise.resolve();
  loading ||= new Promise((resolve, reject) => {
    const css = document.createElement("link");
    css.rel = "stylesheet"; css.href = "vendor/maplibre/maplibre-gl.css";
    document.head.appendChild(css);
    const s = document.createElement("script");
    s.src = "vendor/maplibre/maplibre-gl.js";
    s.onload = resolve; s.onerror = () => reject(new Error("MapLibre se ni naložil"));
    document.head.appendChild(s);
  });
  return loading;
}

const tiles = (id) => TILE_SUBDOMAINS.map((s) => `${tileHost(s)}/tiles/${id}/{z}/{x}/{y}`);

export const is3DOpen = () => !!ml;
export function toggle3D() { return ml ? close3D() : open3D(); }

export async function open3D() {
  if (ml) return;
  try { await loadLib(); } catch (e) { toast(e.message, "err"); return; }
  if (!window.maplibregl.supported?.() && window.maplibregl.supported) { toast("Naprava ne podpira WebGL — 3D pogled ni na voljo.", "err", 5000); return; }
  const s = state.settings;
  wrap = h(`<div id="view3d">
    <div id="view3d-map"></div>
    <div class="v3d-bar">
      <b>3D pogled</b>
      <button class="btn sm" data-v="tilt">${icon("box")} 2D / 3D</button>
      <button class="btn sm" data-v="north">${icon("navigation")} Sever</button>
      <label class="v3d-ex">Pretiravanje <input type="range" min="1" max="3" step="0.1" value="1.3" data-v="ex"><span>1.3×</span></label>
      <label class="check small"><input type="checkbox" data-v="shade" checked> Senčenje</label>
      <button class="btn sm danger" data-v="close">${icon("x")} Zapri</button>
    </div>
    <div class="v3d-help">Vrti in nagibaj: desni gumb / Ctrl + vlečenje · na zaslonu na dotik z dvema prstoma</div>
  </div>`);
  document.getElementById("map-wrap").appendChild(wrap);

  const sources = {
    base: { type: "raster", tiles: tiles(s.base), tileSize: 256, maxzoom: state.layerIndex[s.base]?.maxNativeZoom || 19, attribution: state.layerIndex[s.base]?.attribution || "" },
    dem: { type: "raster-dem", tiles: tiles("terrarium"), tileSize: 256, encoding: "terrarium", maxzoom: 15 },
  };
  const layers = [{ id: "base", type: "raster", source: "base", paint: { "raster-opacity": s.baseOpacity ?? 1 } }];
  for (const o of s.overlays) {
    const cfg = state.layerIndex[o.id];
    if (!cfg || o.visible === false || !["tile", "wms", "arcgis_export"].includes(cfg.type)) continue;
    sources["ov_" + o.id] = { type: "raster", tiles: tiles(o.id), tileSize: 256, maxzoom: cfg.maxNativeZoom || 19, ...(cfg.minZoom ? { minzoom: cfg.minZoom - 1 } : {}) };
    layers.push({ id: "ov_" + o.id, type: "raster", source: "ov_" + o.id, paint: { "raster-opacity": o.opacity ?? 0.8 } });
  }
  layers.push({ id: "shade", type: "hillshade", source: "dem", paint: { "hillshade-exaggeration": 0.35, "hillshade-shadow-color": "#1e293b" } });

  const c = map.getCenter();
  ml = new window.maplibregl.Map({
    container: wrap.querySelector("#view3d-map"),
    style: { version: 8, sources, layers },
    center: [c.lng, c.lat],
    zoom: Math.max(0, map.getZoom() - 1),   // MapLibre zoom levels are 512-px based
    pitch: 60, bearing: 0, maxPitch: 85,
    attributionControl: { compact: true },
  });
  ml.addControl(new window.maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");
  ml.on("load", () => ml.setTerrain({ source: "dem", exaggeration: 1.3 }));
  ml.on("error", (e) => console.warn("3D:", e?.error?.message || e));

  wrap.addEventListener("input", (e) => {
    if (e.target.dataset.v === "ex") {
      ml.setTerrain({ source: "dem", exaggeration: +e.target.value });
      e.target.nextElementSibling.textContent = `${(+e.target.value).toFixed(1)}×`;
    }
  });
  wrap.addEventListener("change", (e) => {
    if (e.target.dataset.v === "shade") ml.setLayoutProperty("shade", "visibility", e.target.checked ? "visible" : "none");
  });
  wrap.addEventListener("click", (e) => {
    const v = e.target.closest("[data-v]")?.dataset.v;
    if (v === "tilt") ml.easeTo({ pitch: ml.getPitch() > 5 ? 0 : 60, duration: 600 });
    if (v === "north") ml.easeTo({ bearing: 0, duration: 600 });
    if (v === "close") close3D();
  });
  emit("view3d", true);
}

export function close3D() {
  if (!ml) return;
  const c = ml.getCenter();
  map.setView([c.lat, c.lng], Math.min(20, ml.getZoom() + 1), { animate: false });   // keep the place you looked at
  ml.remove(); ml = null;
  wrap.remove(); wrap = null;
  emit("view3d", false);
}
