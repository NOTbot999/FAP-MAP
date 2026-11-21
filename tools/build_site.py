"""Assembles the public website into FrancMap/site/ — ready for GitHub Pages, Netlify, Cloudflare Pages…

    site/index.html        landing page (from website/franc-map.html)
    site/img/              screenshots
    site/app/              the web version (spletna različica) = a copy of web/
    site/downloads/        Windows .zip and Android .apk from release/

Usage: python tools/build_site.py
"""
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site"
SRC = ROOT / "website" / "franc-map.html"

if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir()

# landing page: the source is an artifact-style fragment (head bits + body) → wrap it in a full document
frag = SRC.read_text(encoding="utf-8")
split = frag.index('<div class="ribbon"')
head, body = frag[:split], frag[split:]
(OUT / "index.html").write_text(
    "<!doctype html>\n<html lang=\"sl\">\n<head>\n<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
    "<meta name=\"description\" content=\"Franc Map — zemljevid Slovenije z uradnimi kartami, katastrom jam in delom brez signala.\">\n"
    "<style>[hidden]{display:none!important}</style>\n"
    + head + "</head>\n<body>\n" + body + "\n</body>\n</html>\n",
    encoding="utf-8")

# screenshots still used by the page
img_out = OUT / "img"
img_out.mkdir()
used = set(re.findall(r'src="img/([^"]+)"', frag))
for f in used:
    shutil.copy2(ROOT / "website" / "img" / f, img_out / f)

# the web app
shutil.copytree(ROOT / "web", OUT / "app")

# downloads referenced by the page
dl = OUT / "downloads"
dl.mkdir()
missing = []
for name in re.findall(r'href="downloads/([^"]+)"', frag):
    src = ROOT / "release" / name
    if src.exists():
        shutil.copy2(src, dl / name)
    else:
        missing.append(name)

(OUT / ".nojekyll").write_text("")   # GitHub Pages: serve files as-is
size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()) / 1e6
print(f"site built: {OUT}  ({size:.1f} MB, {len(used)} images)")
if missing:
    print("MISSING downloads:", ", ".join(missing))
