"""One-off build helper: converts the original React layerConfig.jsx + SearchBar.jsx
category list into web/data/layers.json (used by both the Python backend and the UI).

Usage: python tools/convert_config.py <path-to-original-repo>
"""
import json
import re
import sys
from pathlib import Path

import json5

repo = Path(sys.argv[1])
out = Path(__file__).resolve().parent.parent / "web" / "data" / "layers.json"

src = (repo / "src/components/map/layerConfig.jsx").read_text(encoding="utf-8")
consts = dict(re.findall(r'const (\w+)\s*=\s*"([^"]+)";', src))


def extract_array(text, name):
    start = text.index(f"export const {name} = [") + len(f"export const {name} = ")
    depth, i = 0, start
    while True:
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = text[start:i + 1]
    for k, v in consts.items():
        body = re.sub(rf"(?<![\w\"]){k}(?![\w\"])", json.dumps(v), body)
    return json5.loads(body)


base = extract_array(src, "BASE_LAYERS")
cats = extract_array(src, "OVERLAY_CATEGORIES")

DROP_KEYS = {"thumbnail"}
# Layers that were duplicates / mislabeled in the original (plain OSM posing as other data)
DROP_LAYERS = {"openinframap_power", "openinframap", "arso_vodotoki",
               "raba_farmland"}  # rkg.gov.si WMS returns 404; OWM key (owm_*) is rejected (401)
# Hosts whose tile policy forbids bulk (offline area) downloading
NO_BULK_HOSTS = ("tile.openstreetmap.org", "openstreetmap.fr", "tile.openweathermap.org")


def clean(layer):
    layer = {k: v for k, v in layer.items() if k not in DROP_KEYS}
    if layer.get("type") == "arcgis_export" and "arcgisUrl" in layer:
        layer["url"] = layer.pop("arcgisUrl")
    url = layer.get("url", "")
    if url.startswith("https://{s}.tile.openstreetmap.org"):
        layer["url"] = url.replace("{s}.", "")
    if any(h in layer.get("url", "") for h in NO_BULK_HOSTS):
        layer["noBulk"] = True
    if layer.get("id", "").startswith("owm_"):
        layer["live"] = True  # weather — never serve stale for long
    return layer


base = [clean(l) for l in base]
for l in base:
    if l["type"] == "arcgis_export":
        l.setdefault("transparent", False)
        l.setdefault("format", "jpg")
for c in cats:
    c.pop("thumbnail", None)
    c["layers"] = [clean(l) for l in c["layers"]
                   if l["id"] not in DROP_LAYERS and not l["id"].startswith("owm_")]

# ── Replacements for sources that no longer work ──
GURS = "https://ipi.eprostor.gov.si/wms-si-gurs/ows"
GURS_DTS = "https://ipi.eprostor.gov.si/wms-si-gurs-dts/ows"
EEA_N2K = "https://bio.discomap.eea.europa.eu/arcgis/services/ProtectedSites/Natura2000Sites/MapServer/WMSServer"
EEA_CDDA = "https://bio.discomap.eea.europa.eu/arcgis/services/ProtectedSites/CDDA_Dyna_WM/MapServer/WMSServer"
GURS_LIDAR = {"type": "wms", "url": GURS_DTS, "layers": "SI.GURS.ZPDZ:LIDAR", "format": "image/jpeg",
              "transparent": False, "version": "1.3.0", "enhance": True}
REPLACE = {
    # ARSO ArcGIS LIDAR server is offline → same national LIDAR hillshade from GURS
    "lidar_hillshade": {**GURS_LIDAR, "attribution": "&copy; GURS LIDAR"},
    "gurs_lidar": {**GURS_LIDAR, "description": "LIDAR senčenje reliefa (GURS) — državno aerolasersko snemanje"},
    "ol_lidar": {**GURS_LIDAR, "description": "LIDAR senčenje (GURS) kot prekrivni sloj"},
    "lidar_overlay": {**GURS_LIDAR, "description": "LIDAR kot overlay (GURS) — jarki, nasipi, temelji zgradb"},
    # GURS GeoServer crashes (NullPointerException) rendering TTN in EPSG:3857 → request in D96/TM
    "gurs_dtk25": {"url": GURS_DTS, "crs": "EPSG:3794"},
    # original description wrongly said "Vodotoki iz ARSO registra voda" — it's the GURS utility cadastre
    "openinframap_water": {"name": "Vodovod & Kanalizacija (GJI)",
                           "description": "Vodovod in kanalizacija iz katastra gospodarske javne infrastrukture GURS"},
    # ARSO GeoServer offline → EU (EEA) official datasets covering Slovenia
    "arso_natura2000_plg": {"type": "wms", "url": EEA_N2K, "layers": "0", "format": "image/png", "transparent": True,
                            "version": "1.3.0", "description": "Natura 2000 (EEA) — ptice in habitati"},
    "arso_zavarovana_obmocja": {"type": "wms", "url": EEA_CDDA, "layers": "0,1", "format": "image/png",
                                "transparent": True, "version": "1.3.0",
                                "description": "Zavarovana območja narave (EEA CDDA) — parki, rezervati"},
}
# CARTO raster tiles now require an API key → keyless Esri equivalents (same layer ids)
_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/"
_STREET = {"url": _ESRI + "World_Street_Map/MapServer/tile/{z}/{y}/{x}", "attribution": "&copy; Esri, HERE, Garmin, OpenStreetMap", "maxNativeZoom": 19}
_DARK = {"url": _ESRI + "Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", "attribution": "&copy; Esri", "maxNativeZoom": 16}
_LIGHT = {"url": _ESRI + "Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}", "attribution": "&copy; Esri", "maxNativeZoom": 16}
_ROADS = {"url": _ESRI + "Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}", "attribution": "&copy; Esri, HERE, OpenStreetMap", "maxNativeZoom": 19, "transparent": True}
_PLACES = {"url": _ESRI + "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", "attribution": "&copy; Esri, HERE", "maxNativeZoom": 19, "transparent": True}
REPLACE.update({
    "carto_voyager": {**_STREET, "name": "Esri Ulice", "description": "Ulična karta (Esri) — nadomešča Carto Voyager, ki zdaj zahteva ključ"},
    "ol_carto_voyager": {**_STREET, "name": "Esri Ulice", "description": "Ulična karta Esri kot prekrivni sloj (nadomešča Carto Voyager)"},
    "dark": {**_DARK, "name": "Temna", "description": "Temno siva podlaga (Esri)"},
    "ol_dark": {**_DARK, "name": "Temna", "description": "Temno siva karta (Esri) kot prekrivni sloj"},
    "stamen_toner": {**_LIGHT, "name": "Svetla siva (visok kontrast)", "description": "Svetlo siva karta (Esri) — izpostavi ceste in zgradbe"},
    "carto_roads_only": {**_ROADS, "name": "Ceste z imeni (Esri)", "description": "Ceste in imena ulic brez ozadja (prozoren sloj, Esri)"},
    "osm_roads_transparent": {**_PLACES, "name": "Imena krajev in meje (Esri)", "description": "Imena krajev in upravne meje brez ozadja — za prekrivanje na satelitu ali LIDARu"},
})
for l in base + [x for c in cats for x in c["layers"]]:
    if l["id"] in REPLACE:
        for k in ("bboxSR", "imageSR", "maxNativeZoom"):
            l.pop(k, None)
        l.update(REPLACE[l["id"]])

# ── Extra layers added in the desktop version ──
def wms(id_, name, layers, desc, opacity=0.7, url=GURS):
    return {"id": id_, "name": name, "type": "wms", "url": url, "layers": layers, "format": "image/png",
            "transparent": True, "version": "1.3.0", "opacity": opacity, "description": desc}

EXTRA = {
    "rpe_naslovi": [
        wms("rpe_obcine", "Občine (meje)", "SI.GURS.RPE:OBCINE", "Meje občin iz registra prostorskih enot (RPE)", 0.8),
        wms("rpe_naselja", "Naselja", "SI.GURS.RPE:NASELJA", "Meje naselij (RPE)", 0.6),
        wms("rpe_ulice", "Ulice", "SI.GURS.RPE:ULICE", "Ulice iz registra prostorskih enot", 0.8),
        wms("rpe_upravne_enote", "Upravne enote", "SI.GURS.RPE:UPRAVNE_ENOTE", "Meje upravnih enot", 0.6),
        wms("rpe_stat_regije", "Statistične regije", "SI.GURS.RPE:STATISTICNE_REGIJE", "Statistične regije Slovenije", 0.5),
        wms("rpe_postni", "Poštni okoliši", "SI.GURS.RPE:POSTNI_OKOLISI", "Meje poštnih okolišev", 0.6),
        wms("rpe_solski", "Šolski okoliši", "SI.GURS.RPE:SOLSKI_OKOLISI", "Meje šolskih okolišev", 0.6),
    ],
    "karte": [
        wms("gurs_dejanska_raba", "Dejanska raba (kataster)", "SI.GURS.KN:DEJANSKE_RABE",
            "Dejanska raba zemljišč iz katastra nepremičnin (nadomešča MKGP RABA)", 0.65),
        wms("gurs_namenska_raba", "Namenska raba prostora", "SI.GURS.KN:NAMENSKE_RABE",
            "Namenska raba iz prostorskih aktov občin", 0.6),
    ],
    "promet": [
        wms("gji_zeleznice", "Železnice (GJI)", "SI.GURS.KGI:LINIJE_ZELEZNICE_G", "Železniška infrastruktura iz katastra GJI", 0.85),
        wms("gji_zicnice", "Žičnice (GJI)", "SI.GURS.KGI:LINIJE_ZICNICE_G", "Žičnice in vlečnice iz katastra GJI", 0.85),
        wms("gji_parkirisca", "Parkirišča (GJI)", "SI.GURS.KGI:PARKIRISCA", "Parkirišča iz katastra GJI", 0.7),
    ],
}
for c in cats:
    for i, l in enumerate(EXTRA.get(c["id"], [])):
        c["layers"].insert(i, l)

# ── Visible zoom range from each WMS server's capabilities (avoids requesting blank tiles) ──
import math
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "FrancMapDesktop/1.0 (desktop map viewer for Slovenia)"}
caps = {}


def scale_ranges(url):
    if url not in caps:
        caps[url] = {}
        try:
            req = urllib.request.Request(url + ("&" if "?" in url else "?") + "SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0", headers=UA)
            root = ET.fromstring(urllib.request.urlopen(req, timeout=60).read())
            for lyr in root.iter():
                if not lyr.tag.endswith("Layer"):
                    continue
                name = next((ch.text for ch in lyr if ch.tag.endswith("Name")), None)
                mn = next((float(ch.text) for ch in lyr if ch.tag.endswith("MinScaleDenominator")), None)
                mx = next((float(ch.text) for ch in lyr if ch.tag.endswith("MaxScaleDenominator")), None)
                if name:
                    caps[url][name] = (mn, mx)
        except Exception as e:  # noqa: BLE001
            print("  ! capabilities failed:", url, e)
    return caps[url]


def scale_at(z):  # scale denominator of a 256px web-mercator tile at Slovenia's latitude
    return 156543.034 * math.cos(math.radians(46.1)) / 2 ** z / 0.00028


for l in base + [x for c in cats for x in c["layers"]]:
    if l.get("type") != "wms":
        continue
    rng = [scale_ranges(l["url"]).get(n) for n in l["layers"].split(",")]
    mxs = [r[1] for r in rng if r and r[1]]
    if mxs and len(mxs) == len(rng):
        mx = max(mxs)
        minz = next(z for z in range(0, 21) if scale_at(z) <= mx)
        if minz > 7:
            l["minZoom"] = minz
            l["description"] = (l.get("description", "").split(" (vidno")[0] + f" (vidno od povečave {minz})").strip()

# POI categories (Overpass)
sb = (repo / "src/components/map/SearchBar.jsx").read_text(encoding="utf-8")
start = sb.index("export const CATEGORIES = [")
end = sb.index("\n];", start)
pois = []
for m in re.finditer(r'\{ id: "(\w+)",\s*label: "([^"]+)",\s*emoji: "([^"]+)",\s*color: "([^"]+)",(.*?)\},?\s*$',
                     sb[start:end], re.M):
    cid, label, emoji, color, rest = m.groups()
    q = re.search(r"query: `([^`]*)`", rest)
    query = q.group(1) if q else ""
    if cid == "cave":
        query = ('[out:json][timeout:60];(node["natural"="cave_entrance"](45.4,13.4,46.9,16.6););out;')
    if not query:
        continue  # non-OSM sources (Wikidata, OpenChargeMap, Base44 DB) not ported
    pois.append({"id": cid, "label": label, "emoji": emoji, "color": color, "query": query})

# Technical layers used internally (not in the layer menu). Empty since the 3D view was removed.
internal = []

out.write_text(json.dumps({"base": base, "categories": cats, "pois": pois, "internal": internal}, ensure_ascii=False, indent=1),
               encoding="utf-8")
print(f"wrote {out}: {len(base)} base, {sum(len(c['layers']) for c in cats)} overlays in {len(cats)} cats, {len(pois)} POI cats")
