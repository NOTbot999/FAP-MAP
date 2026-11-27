// Raster tiles that exist only in the D96/TM (EPSG:3794) tile grid of an ArcGIS MapServer cache,
// e.g. GURS DTK25 on GeoHub (the dynamic "export" of those services draws only the sheet index).
// For every web-mercator tile we look up the D96 tiles under it and draw them onto a canvas with an
// affine transform (three tile corners → exact to well under a pixel inside one 256 px tile).
// Source tiles go through the app's cached /api/fetch, so viewed areas also work offline.
import { proxiedImage } from "../services/api.js";
import { toD96 } from "../shared/util.js";
import { SI_TILE_BOUNDS } from "../runtime/state.js";

// GeoHub cache grid (all TEMELJNE_KARTE services share it)
const ORIGIN_E = -5122600, ORIGIN_N = 5001100, RES0 = 138718.1441029549, TS = 256;

// Neighbouring map tiles share source tiles at their seams → keep recently decoded images briefly
const images = new Map();   // url -> Promise<HTMLImageElement|null>
function loadImage(url) {
  let p = images.get(url);
  if (p) { images.delete(url); images.set(url, p); return p; }   // refresh LRU position
  p = new Promise((resolve) => {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => resolve(img);
    img.onerror = () => { images.delete(url); resolve(null); };   // allow a retry later
    img.src = url;
  });
  images.set(url, p);
  if (images.size > 96) images.delete(images.keys().next().value);
  return p;
}

const D96TileLayer = L.GridLayer.extend({
  createTile(coords, done) {
    const canvas = document.createElement("canvas");
    // a pinned cache level has fixed detail → a hi-dpi canvas would only add memory, not sharpness
    const scale = this.options.cfg.level != null ? 1 : Math.min(2, window.devicePixelRatio || 1);
    canvas.width = canvas.height = TS * scale;
    this._draw(canvas, coords).then(() => done(null, canvas), (e) => done(e, canvas));
    return canvas;
  },

  async _draw(canvas, coords) {
    const cfg = this.options.cfg;
    const W = canvas.width, z = coords.z;
    const corner = (dx, dy) => {
      const ll = this._map.unproject(L.point((coords.x + dx) * TS, (coords.y + dy) * TS), z);
      return toD96(ll.lat, ll.lng);
    };
    const nw = corner(0, 0), ne = corner(1, 0), sw = corner(0, 1), se = corner(1, 1);
    // cfg.level pins one cache level: GeoHub's "Topografska karta" holds a different map per level
    // (DTK50 at 14, colour DTK25 at 15, vector map from 16) and we want DTK25 at every zoom.
    // Otherwise pick the level whose pixels are about as big as the canvas's.
    const groundRes = Math.hypot(ne.e - nw.e, ne.n - nw.n) / W;          // metres per canvas pixel
    const level = cfg.level ?? Math.min(cfg.maxLevel ?? 17, Math.round(Math.log2(RES0 / groundRes)));
    const res = RES0 / 2 ** level, span = res * TS;

    // D96 → canvas pixel: solve M·(NE−NW) = (W,0), M·(SW−NW) = (0,W)
    const ux = ne.e - nw.e, uy = ne.n - nw.n, vx = sw.e - nw.e, vy = sw.n - nw.n;
    const det = ux * vy - vx * uy;
    const m00 = W * vy / det, m01 = -W * vx / det, m10 = -W * uy / det, m11 = W * ux / det;

    const es = [nw.e, ne.e, sw.e, se.e], ns = [nw.n, ne.n, sw.n, se.n];
    const c0 = Math.floor((Math.min(...es) - ORIGIN_E) / span), c1 = Math.floor((Math.max(...es) - ORIGIN_E) / span);
    const r0 = Math.floor((ORIGIN_N - Math.max(...ns)) / span), r1 = Math.floor((ORIGIN_N - Math.min(...ns)) / span);
    if ((c1 - c0 + 1) * (r1 - r0 + 1) > 36) return;   // a pinned level far below its minZoom; guards a runaway request

    const jobs = [];
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) {
      jobs.push(loadImage(proxiedImage(`${cfg.url}/tile/${level}/${r}/${c}`)).then((img) => ({ img, r, c })));
    }
    const tiles = await Promise.all(jobs);
    if (!tiles.some((t) => t.img)) throw new Error("no source tiles");
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingQuality = "high";
    for (const { img, r, c } of tiles) {
      if (!img) continue;
      const dx = ORIGIN_E + c * span - nw.e, dy = ORIGIN_N - r * span - nw.n;   // tile's top-left, relative to NW
      // image pixel (i,j) → D96 (e0 + i·res, n0 − j·res) → canvas
      ctx.setTransform(m00 * res, m10 * res, -m01 * res, -m11 * res, m00 * dx + m01 * dy, m10 * dx + m11 * dy);
      ctx.drawImage(img, 0, 0, TS, TS);
    }
  },
});

export function createD96TileLayer(cfg, opacity) {
  return new D96TileLayer({
    cfg,
    pane: "overlays",
    opacity: opacity ?? cfg.opacity ?? 1,
    minZoom: cfg.minZoom || 0,
    maxZoom: 20,
    // past the source's detail Leaflet just scales the last drawn tiles instead of re-projecting
    maxNativeZoom: cfg.level != null ? 16 : 18,
    bounds: L.latLngBounds(SI_TILE_BOUNDS),
    keepBuffer: 2,
    updateWhenIdle: false,
    updateWhenZooming: false,
    attribution: cfg.attribution || "",
  });
}
