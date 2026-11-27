"""Builds web/data/aed.json — all public defibrillators (AED) in Slovenia, bundled with the app
(works offline, like the cave and hydrant cadastres).

Sources
  1. Register AED of Dispečerska služba zdravstva (DSZ, the 112 medical dispatch) — https://aed.dsz.si
     The public page carries the full list as JSON ("var model = [...]"); ~4000 devices with status,
     24/7 access, opening hours and directions. This is what the dispatcher sees.
  2. OpenStreetMap (emergency=defibrillator, ODbL) — only devices that are NOT within 60 m of a
     DSZ device are added (the register misses some, e.g. ones whose owners didn't report them).

Usage: python tools/import_aed.py      (re-run to refresh)
"""
import json
import math
import time
from datetime import date
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent.parent / "web" / "data" / "aed.json"
DSZ_URL = "https://aed.dsz.si/"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter"]
OSM_Q = '[out:json][timeout:90];(node["emergency"="defibrillator"](45.4,13.4,46.9,16.6);way["emergency"="defibrillator"](45.4,13.4,46.9,16.6););out center tags;'
SAME_M = 60

s = requests.Session()
s.headers["User-Agent"] = "FrancMap data import (map viewer for Slovenia)"

STATUS = {"Deluje": 0, "Ni na kraju": 1, "V okvari": 2, "Na servisu": 3, "Uporabljen": 4}


def dsz():
    html = s.get(DSZ_URL, timeout=90).text
    i = html.index('{"AedID"')
    start = html.rfind("[", 0, i)
    arr, _ = json.JSONDecoder().raw_decode(html[start:])
    rows = []
    for a in arr:
        lat, lng = a.get("GisLatitude"), a.get("GisLongitude")
        if not lat or not lng:
            continue
        flags = (1 if a.get("IsAlwaysAccesible") else 0) | (2 if a.get("IsFreeAccesible") else 0) | (4 if a.get("IsReadyToUse") else 0)
        rows.append([round(lat, 6), round(lng, 6), (a.get("AedName") or "").strip(), (a.get("LocationText") or "").strip(),
                     STATUS.get(a.get("AedStatus"), 9), flags, (a.get("AccessTimeText") or "").strip(),
                     (a.get("AccessPathText") or "").strip(), (a.get("AccessProtocolText") or "").strip(), a.get("AedID")])
    return rows


def osm():
    for url in OVERPASS:
        try:
            r = s.post(url, data={"data": OSM_Q}, timeout=120)
            if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("application/json"):
                return r.json()["elements"]
        except Exception as e:  # noqa: BLE001
            print("  overpass", url, e)
        time.sleep(3)
    raise SystemExit("Overpass ni dosegljiv")


def dist_m(a, b):
    dy = (a[0] - b[0]) * 111320
    dx = (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def main():
    d = dsz()
    print("DSZ:", len(d))
    grid = {}
    for r in d:
        grid.setdefault((round(r[0], 2), round(r[1], 2)), []).append(r)
    extra = []
    for el in osm():
        lat, lng = el.get("lat") or el.get("center", {}).get("lat"), el.get("lon") or el.get("center", {}).get("lon")
        if lat is None:
            continue
        p = (lat, lng)
        near = [q for dy in (-0.01, 0, 0.01) for dx in (-0.01, 0, 0.01) for q in grid.get((round(lat + dy, 2), round(lng + dx, 2)), [])]
        if any(dist_m(p, q) < SAME_M for q in near):
            continue
        t = el.get("tags", {})
        extra.append([round(lat, 6), round(lng, 6), t.get("name") or t.get("operator") or "",
                      t.get("defibrillator:location:sl") or t.get("defibrillator:location") or t.get("description") or "",
                      t.get("indoor", ""), t.get("access", ""), t.get("opening_hours", ""), f"{el['type']}/{el['id']}"])
    print("OSM extra (not in DSZ):", len(extra))
    OUT.write_text(json.dumps({
        "imported": date.today().isoformat(),
        "source": "Register AED, Dispečerska služba zdravstva (aed.dsz.si) + OpenStreetMap (ODbL)",
        "status": {v: k for k, v in STATUS.items()},
        "dsz": d, "osm": extra,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("written", OUT, f"{OUT.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
