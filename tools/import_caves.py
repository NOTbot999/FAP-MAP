"""Imports the cave cadastre CSV (kataster jam) into web/data/caves.json.

Usage: python tools/import_caves.py <kataster_jame.csv>
CSV columns: cave_id,name,latitude,longitude,length_m,depth_m[,area_m2]
"""
import csv
import json
import sys
import time
from pathlib import Path

src = Path(sys.argv[1])
out = Path(__file__).resolve().parent.parent / "web" / "data" / "caves.json"

caves, skipped = [], 0
with src.open(encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        try:
            lat, lng = float(r["latitude"]), float(r["longitude"])
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not (45.3 < lat < 47.0 and 13.3 < lng < 16.7):
            skipped += 1
            continue
        num = lambda v: int(round(float(v))) if v not in (None, "") else 0  # noqa: E731
        caves.append([int(r["cave_id"]), r["name"].strip(), round(lat, 5), round(lng, 5),
                      num(r.get("length_m")), num(r.get("depth_m"))])

caves.sort(key=lambda c: c[0])
data = {"source": src.name, "imported": time.strftime("%Y-%m-%d"), "count": len(caves),
        "fields": ["id", "name", "lat", "lng", "length_m", "depth_m"], "caves": caves}
out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(f"wrote {out} — {len(caves)} caves ({skipped} skipped), {out.stat().st_size / 1024:.0f} kB")
