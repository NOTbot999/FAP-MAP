"""Collects legends (label + small symbol image) of all ArcGIS layers in web/data/layers.json into
web/data/legends.json. ArcGIS servers such as GeoHub don't allow the web version to read their
legend JSON directly (no CORS), and bundling also makes legends work offline.
WMS legends (GURS, PIS) are not bundled: they load live as GetLegendGraphic images.

Usage: python tools/build_legends.py      (re-run when layers change)
"""
import json
import re
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
LAYERS = ROOT / "web" / "data" / "layers.json"
OUT = ROOT / "web" / "data" / "legends.json"

db = json.loads(LAYERS.read_text(encoding="utf-8"))
layers = [*db["base"], *[l for c in db["categories"] for l in c["layers"]]]
s = requests.Session()
s.headers["User-Agent"] = "FrancMap legend import (map viewer for Slovenia)"
cache, out = {}, {}

for l in layers:
    if l.get("type") != "arcgis_export":
        continue
    base = l["url"].rsplit("/export", 1)[0]
    if base not in cache:
        try:
            cache[base] = s.get(base + "/legend", params={"f": "json"}, timeout=40).json()
        except Exception as e:  # noqa: BLE001
            print("  !", l["id"], e)
            cache[base] = {}
    legend = cache[base].get("layers") or []
    show = None
    m = re.match(r"show:([\d,]+)", l.get("layers") or "")
    if m:
        show = [int(x) for x in m.group(1).split(",")]
        legend = sorted((x for x in legend if x["layerId"] in show), key=lambda x: show.index(x["layerId"]))
    groups = []
    for x in legend:
        items = [{"label": (e.get("label") or "").strip(),
                  "img": f"data:{e.get('contentType', 'image/png')};base64,{e['imageData']}"}
                 for e in x.get("legend", []) if e.get("imageData")]
        if items:
            groups.append({"title": x.get("layerName", ""), "items": items[:40]})
    if groups:
        out[l["id"]] = groups
    print(f"  {l['id']}: {sum(len(g['items']) for g in groups)} oznak")

OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(f"saved {OUT} — {len(out)} layers, {OUT.stat().st_size / 1e3:.0f} kB")
