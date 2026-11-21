// AR pogled — camera + compass + GPS: shows caves, loaded points of interest and your markers in the
// direction you're facing (like the original ARFieldExplorer). On a PC without a compass you can
// rotate by dragging and the map centre is used as your position.
import { map } from "./map.js";
import { state, emit } from "./state.js";
import { getCaves, getLoadedFeatures } from "./panels/poi.js";
import { getDrawings } from "./panels/draw.js";
import { haversine, esc, fmtDist, h, toast } from "./util.js";
import { icon } from "./icons.js";

const RANGES = [["1 km", 1000], ["5 km", 5000], ["10 km", 10000], ["30 km", 30000]];
const FOV = 60;            // horizontal field of view (degrees)
const MAX_LABELS = 40;

let wrap = null, stream = null, watchId = null, raf = 0;
let pos = null, heading = null, manualHeading = 0, range = 5000, usingCompass = false;

const rad = (d) => d * Math.PI / 180;
function bearing(a, b) {
  const y = Math.sin(rad(b[1] - a[1])) * Math.cos(rad(b[0]));
  const x = Math.cos(rad(a[0])) * Math.sin(rad(b[0])) - Math.sin(rad(a[0])) * Math.cos(rad(b[0])) * Math.cos(rad(b[1] - a[1]));
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}
const CARD = ["S", "SV", "V", "JV", "J", "JZ", "Z", "SZ"];   // sever, severovzhod, vzhod, …
const cardinal = (d) => CARD[Math.round(d / 45) % 8];

export const isAROpen = () => !!wrap;

export async function openAR() {
  if (wrap) return;
  wrap = h(`<div id="ar-view">
    <video id="ar-video" autoplay playsinline muted></video>
    <div class="ar-compass"><div class="ar-strip"></div><div class="ar-needle"></div></div>
    <div class="ar-labels"></div>
    <div class="ar-bar">
      <span class="ar-status">Pripravljam…</span>
      <span class="grow"></span>
      <div class="seg ar-range">${RANGES.map(([l, v]) => `<button data-r="${v}" class="${v === range ? "on" : ""}">${l}</button>`).join("")}</div>
      <button class="btn sm danger" data-close>${icon("x")} Zapri</button>
    </div>
  </div>`);
  document.body.appendChild(wrap);
  wrap.addEventListener("click", (e) => {
    if (e.target.closest("[data-close]")) return closeAR();
    const r = e.target.closest("[data-r]");
    if (r) { range = +r.dataset.r; wrap.querySelectorAll("[data-r]").forEach((b) => b.classList.toggle("on", b === r)); }
    const lab = e.target.closest("[data-ll]");
    if (lab) { const [la, ln] = lab.dataset.ll.split(",").map(Number); closeAR(); map.flyTo([la, ln], 16); }
  });

  // camera
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
    wrap.querySelector("#ar-video").srcObject = stream;
  } catch {
    wrap.classList.add("no-camera");
  }

  // position: GPS, falling back to the map centre
  const c = map.getCenter();
  pos = [c.lat, c.lng];
  if (navigator.geolocation) {
    watchId = navigator.geolocation.watchPosition((p) => { pos = [p.coords.latitude, p.coords.longitude]; pos.gps = true; },
      () => {}, { enableHighAccuracy: true, maximumAge: 3000 });
  }

  // compass
  window.addEventListener("deviceorientationabsolute", onOrient, true);
  window.addEventListener("deviceorientation", onOrient, true);
  if (typeof DeviceOrientationEvent?.requestPermission === "function") {
    try { await DeviceOrientationEvent.requestPermission(); } catch { /* iOS only */ }
  }

  // drag to rotate when there's no compass (PC)
  let drag = null;
  wrap.addEventListener("pointerdown", (e) => { if (!usingCompass && !e.target.closest("button")) drag = { x: e.clientX, h: manualHeading }; });
  wrap.addEventListener("pointermove", (e) => { if (drag) manualHeading = (drag.h - (e.clientX - drag.x) * FOV / wrap.clientWidth + 360) % 360; });
  wrap.addEventListener("pointerup", () => { drag = null; });

  loop();
  emit("ar", true);
}

function onOrient(e) {
  let hd = null;
  if (typeof e.webkitCompassHeading === "number") hd = e.webkitCompassHeading;
  else if (e.absolute && e.alpha != null) hd = (360 - e.alpha) % 360;
  else if (e.type === "deviceorientation" && e.alpha != null && heading == null) hd = (360 - e.alpha) % 360;
  if (hd == null) return;
  // light smoothing (shortest-angle)
  if (heading == null) heading = hd;
  else { const d = ((hd - heading + 540) % 360) - 180; heading = (heading + d * 0.25 + 360) % 360; }
  usingCompass = true;
}

function candidates() {
  const out = [];
  for (const c of getCaves()) out.push({ lat: c.lat, lng: c.lng, name: c.name, emoji: "🕳️" });
  for (const f of getLoadedFeatures()) if (!f.id?.startsWith("cave/")) out.push({ lat: f.lat, lng: f.lng, name: f.name, emoji: f.emoji });
  for (const d of getDrawings()) if (d.type === "marker") out.push({ lat: d.coords[0], lng: d.coords[1], name: d.name, emoji: "📍" });
  return out;
}

let lastList = 0, visible = [];
function loop() {
  if (!wrap) return;
  raf = requestAnimationFrame(loop);
  const hd = usingCompass ? heading : manualHeading;
  // compass strip
  const strip = wrap.querySelector(".ar-strip");
  strip.style.setProperty("--hd", hd);
  strip.textContent = `${Math.round(hd)}° ${cardinal(hd)}`;
  // recompute the candidate list a few times per second (13k caves)
  const now = performance.now();
  if (now - lastList > 400) {
    lastList = now;
    visible = [];
    for (const p of candidates()) {
      const d = haversine(pos, [p.lat, p.lng]);
      if (d > range || d < 5) continue;
      visible.push({ ...p, d, b: bearing(pos, [p.lat, p.lng]) });
    }
    visible.sort((a, b) => a.d - b.d);
    wrap.querySelector(".ar-status").textContent =
      `${pos.gps ? "GPS" : "Središče karte"} · ${usingCompass ? "kompas" : "povleci za obračanje"} · ${visible.length} točk v ${fmtDist(range)}`;
  }
  // place labels inside the field of view, nearest first, avoiding overlaps
  const placed = [];
  const html = [];
  for (const p of visible) {
    const rel = ((p.b - hd + 540) % 360) - 180;
    if (Math.abs(rel) > FOV / 2) continue;
    const x = 50 + rel / FOV * 100;
    let y = 72 - Math.min(1, p.d / range) * 52;               // nearer → lower on screen
    while (placed.some((q) => Math.abs(q.x - x) < 16 && Math.abs(q.y - y) < 7)) y -= 7;
    if (y < 8) continue;
    placed.push({ x, y });
    html.push(`<button class="ar-label" style="left:${x}%;top:${y}%" data-ll="${p.lat},${p.lng}"><span>${p.emoji || "📍"}</span><b>${esc(p.name || "Točka")}</b><small>${fmtDist(p.d)}</small></button>`);
    if (placed.length >= MAX_LABELS) break;
  }
  const labels = wrap.querySelector(".ar-labels");
  const joined = html.join("");
  if (labels._last !== joined) { labels.innerHTML = joined; labels._last = joined; }
}

export function closeAR() {
  if (!wrap) return;
  cancelAnimationFrame(raf);
  window.removeEventListener("deviceorientationabsolute", onOrient, true);
  window.removeEventListener("deviceorientation", onOrient, true);
  if (watchId != null) navigator.geolocation.clearWatch(watchId);
  stream?.getTracks().forEach((t) => t.stop());
  stream = null; watchId = null; heading = null; usingCompass = false;
  wrap.remove(); wrap = null;
  emit("ar", false);
}
