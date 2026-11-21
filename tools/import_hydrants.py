"""Downloads all fire hydrants from the state utility cadastre (zbirni kataster GJI, GeoHub)
into web/data/hydrants.json — bundled with the app like the cave cadastre, works offline.

Source: https://geohub.gov.si/ags/rest/services/DRSV/Raba_vode/MapServer/4724  ("GJI vodovod - točke")
        objects with GJI_VRSTA_OBJEKTA_OPIS = 'Hidrant'

Usage: python tools/import_hydrants.py      (re-run to refresh; ~30 requests of 2000 points)
"""
import json
import time
from pathlib import Path

import requests

URL = "https://geohub.gov.si/ags/rest/services/DRSV/Raba_vode/MapServer/4724/query"
OUT = Path(__file__).resolve().parent.parent / "web" / "data" / "hydrants.json"
WHERE = "GJI_VRSTA_OBJEKTA_OPIS='Hidrant'"
FIELDS = ["OBJECTID", "GJI_ATR2_OPIS", "GJI_OPUSCENOST_OPIS", "LETO_IZGRADNJE",
          "NATANCNOST_DOL_POLOZAJA_SIFRA", "NATANCNOST_DOL_POLOZAJA_OPIS", "DATUM_SYS"]

s = requests.Session()
s.headers["User-Agent"] = "FrancMap data import (map viewer for Slovenia)"


def page(after):
    for attempt in range(4):
        try:
            r = s.get(URL, params={
                "where": f"{WHERE} AND OBJECTID > {after}", "outFields": ",".join(FIELDS),
                "orderByFields": "OBJECTID", "resultRecordCount": 2000,
                "returnGeometry": "true", "outSR": 4326, "geometryPrecision": 6, "f": "json",
            }, timeout=60)
            j = r.json()
            if "error" in j:
                raise RuntimeError(j["error"])
            return j.get("features", [])
        except Exception as e:  # noqa: BLE001
            print("  retry", attempt + 1, e)
            time.sleep(3 * (attempt + 1))
    raise SystemExit("GeoHub ni dosegljiv")


TYPE = {"nad zemljo": 1, "pod zemljo": 2}
rows, acc_text, last, newest = [], {}, 0, 0
while True:
    feats = page(last)
    if not feats:
        break
    for f in feats:
        a, g = f["attributes"], f.get("geometry") or {}
        last = max(last, a["OBJECTID"])
        if g.get("x") is None:
            continue
        code = a.get("NATANCNOST_DOL_POLOZAJA_SIFRA") or 0
        if code and code not in acc_text:
            acc_text[code] = a.get("NATANCNOST_DOL_POLOZAJA_OPIS") or ""
        newest = max(newest, a.get("DATUM_SYS") or 0)
        rows.append([round(g["y"], 6), round(g["x"], 6),
                     TYPE.get(a.get("GJI_ATR2_OPIS"), 0),
                     a.get("LETO_IZGRADNJE") or 0,
                     code,
                     1 if a.get("GJI_OPUSCENOST_OPIS") == "opuščeni objekt" else 0])
    print(f"  {len(rows)} …")

out = {
    "source": "Zbirni kataster GJI (GURS), GeoHub – GJI vodovod, točke, vrsta »Hidrant«",
    "sourceUrl": URL.rsplit("/query", 1)[0],
    "imported": time.strftime("%Y-%m-%d"),
    "newestChange": time.strftime("%Y-%m-%d", time.gmtime(newest / 1000)) if newest else "",
    "count": len(rows),
    "fields": ["lat", "lng", "type (1 nadzemni, 2 podzemni, 0 ni podatka)", "year", "accuracyCode", "abandoned"],
    "accuracy": {str(k): v for k, v in sorted(acc_text.items())},
    "rows": rows,
}
OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(f"saved {OUT} — {len(rows)} hydrants, {OUT.stat().st_size / 1e6:.2f} MB")
