"""Franc Map — offline-capable desktop GIS explorer for Slovenia.

Runs a small local HTTP server (static UI + tile/data cache + proxy) and shows it in a
native WebView2 window via pywebview.

    python app.py            # desktop window
    python app.py --serve    # server only (open printed URL in a browser; for development)
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import sys
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

import collab_server

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
# Test builds (build-test.bat bundles flavor.txt = "test", or run with --test) keep their own data
# folder and title, so they never touch the real app's settings, drawings or cache.
IS_TEST = "--test" in sys.argv or ((BASE_DIR / "flavor.txt").exists() and (BASE_DIR / "flavor.txt").read_text().strip() == "test")
APP_NAME = "FrancMapTest" if IS_TEST else "FrancMap"
APP_VERSION = "1.5.7" + ("-test" if IS_TEST else "")
WEB_DIR = BASE_DIR / "web"
# FRANCMAP_DATA: separate data folder (settings, cache) — for test/dev runs next to the installed app
DATA_DIR = Path(os.environ["FRANCMAP_DATA"]) if os.environ.get("FRANCMAP_DATA") \
    else Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
STORE_DIR = DATA_DIR / "store"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STORE_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = f"FrancMapDesktop/{APP_VERSION} (desktop map viewer for Slovenia)"
TOKEN = secrets.token_urlsafe(16)
TILE_MAX_AGE = 30 * 86400          # refresh cached tiles older than this when online
LIVE_TILE_MAX_AGE = 1800           # weather-type layers
MAX_BULK_TILES = 60000

ALLOWED_FETCH_HOSTS = (
    "nominatim.openstreetmap.org", "brouter.de", "router.project-osrm.org", "api.adsb.lol",
    "api.open-meteo.com", "ipi.eprostor.gov.si", "gis.arso.gov.si", "rkg.gov.si", "bio.discomap.eea.europa.eu",
    "api.rainviewer.com",
)
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")

# ─────────────────────────────── layers ───────────────────────────────

LAYER_DB = json.loads((WEB_DIR / "data" / "layers.json").read_text(encoding="utf-8"))
LAYERS: dict[str, dict] = {l["id"]: l for l in LAYER_DB["base"]}
for _c in LAYER_DB["categories"]:
    for _l in _c["layers"]:
        LAYERS[_l["id"]] = _l
for _l in LAYER_DB.get("internal", []):   # e.g. terrain elevation tiles for the 3D view
    LAYERS[_l["id"]] = _l

HALF = 20037508.342789244


def tile_bounds_3857(z, x, y):
    res = 2 * HALF / (2 ** z)
    minx = -HALF + x * res
    maxy = HALF - y * res
    return minx, maxy - res, minx + res, maxy


def tile_bounds_4326(z, x, y):
    n = 2 ** z

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def to_d96(lat, lng):
    """WGS84 → D96/TM (EPSG:3794) easting, northing (Transverse Mercator on GRS80)."""
    a, f, k0, lon0, fe, fn = 6378137.0, 1 / 298.257222101, 0.9999, math.radians(15), 500000.0, -5000000.0
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    phi, lam = math.radians(lat), math.radians(lng)
    nn = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    t, c, aa = math.tan(phi) ** 2, ep2 * math.cos(phi) ** 2, (lam - lon0) * math.cos(phi)
    e4, e6 = e2 * e2, e2 ** 3
    m = a * ((1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
             + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi) - (35 * e6 / 3072) * math.sin(6 * phi))
    east = fe + k0 * nn * (aa + (1 - t + c) * aa ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * aa ** 5 / 120)
    north = fn + k0 * (m + nn * math.tan(phi) * (aa * aa / 2 + (5 - t + 9 * c + 4 * c * c) * aa ** 4 / 24
                                                 + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * aa ** 6 / 720))
    return east, north


def build_upstream_url(cfg, z, x, y):
    t = cfg.get("type")
    if t == "tile":
        url = cfg["url"]
        if "{s}" in url:
            subs = cfg.get("subdomains", "abc")
            url = url.replace("{s}", subs[(x + y) % len(subs)])
        return url.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y)).replace("{r}", "")
    if t == "wms":
        ver = cfg.get("version", "1.1.1")
        params = {
            "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": ver, "LAYERS": cfg["layers"], "STYLES": "",
            "FORMAT": cfg.get("format", "image/png"),
            "TRANSPARENT": "TRUE" if cfg.get("transparent", True) else "FALSE",
            "WIDTH": 256, "HEIGHT": 256,
            ("CRS" if ver == "1.3.0" else "SRS"): "EPSG:3857",
            "BBOX": ",".join(f"{v:.6f}" for v in tile_bounds_3857(z, x, y)),
        }
        if cfg.get("crs") == "EPSG:3794":
            # some GURS layers crash when rendered in web mercator → ask in D96/TM.
            # Grid rotation vs. web mercator is < 1° in Slovenia (≈2 px per tile).
            w, s, e, n = tile_bounds_4326(z, x, y)
            pts = [to_d96(lat, lng) for lat, lng in ((s, w), (s, e), (n, w), (n, e))]
            params[("CRS" if ver == "1.3.0" else "SRS")] = "EPSG:3794"
            params["BBOX"] = "%.2f,%.2f,%.2f,%.2f" % (min(p[0] for p in pts), min(p[1] for p in pts),
                                                    max(p[0] for p in pts), max(p[1] for p in pts))
        if cfg.get("time"):   # WMS time dimension, e.g. GURS historical orthophoto "2009-01-01/2011-12-31"
            params["TIME"] = cfg["time"]
        return cfg["url"] + ("&" if "?" in cfg["url"] else "?") + urlencode(params)
    if t == "arcgis_export":
        sr = cfg.get("bboxSR", 3857)
        b = tile_bounds_4326(z, x, y) if sr == 4326 else tile_bounds_3857(z, x, y)
        transparent = cfg.get("transparent", True) is not False
        fmt = cfg.get("format") or ("png32" if transparent else "jpg")
        params = {
            "bbox": ",".join(f"{v:.8f}" for v in b), "bboxSR": sr, "imageSR": cfg.get("imageSR", sr),
            "size": "256,256", "f": "image", "format": fmt,
            "transparent": "true" if transparent else "false",
        }
        return cfg["url"] + "?" + urlencode(params)
    return None


# ─────────────────────────────── cache ───────────────────────────────

class Cache:
    """SQLite tile + HTTP cache.

    Self-healing: if the database turns out to be damaged ("database disk image is malformed"),
    the running app stops using it (tiles then come straight from the network) and leaves a
    marker; on the next start the damaged file is moved aside, a fresh cache is created and every
    tile/response that can still be read is copied into it.
    """

    def __init__(self, path: Path):
        self.file = Path(path)
        self.marker = self.file.with_name("cache.needs-repair")
        self.local = threading.local()
        self.wlock = threading.Lock()
        self.broken = False
        self.note = ""
        # One Franc Map process owns the cache. A second window/process (or a dev server running
        # next to the installed app) gets its own temporary cache instead of writing into the
        # same file — the cache was found damaged twice after two processes had shared it.
        if not self._take_lock():
            self.file = self.file.with_name(f"cache-zacasno-{os.getpid()}.sqlite")
            print("cache in use by another Franc Map process -> temporary cache", self.file, flush=True)
        else:
            for old in self.file.parent.glob("cache-zacasno-*.sqlite*"):   # left behind by earlier second windows
                try:
                    old.unlink()
                except OSError:
                    pass
            if self.marker.exists():
                self.repair()
        self.path = str(self.file)
        try:
            self._init_schema()
        except sqlite3.DatabaseError:
            self.repair()
            self._init_schema()

    def _take_lock(self):
        """Non-blocking exclusive lock on cache.lock, held for the life of the process."""
        try:
            self._lockf = open(self.file.with_name("cache.lock"), "a+b")
            if os.name == "nt":
                import msvcrt
                self._lockf.seek(0)
                msvcrt.locking(self._lockf.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _init_schema(self):
        c = self.conn()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("""CREATE TABLE IF NOT EXISTS tiles(layer TEXT, z INT, x INT, y INT, data BLOB, ctype TEXT,
                     ts INT, PRIMARY KEY(layer, z, x, y)) WITHOUT ROWID""")
        c.execute("CREATE TABLE IF NOT EXISTS http(key TEXT PRIMARY KEY, data BLOB, ctype TEXT, ts INT)")
        c.commit()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            self.local.c = c
        return c

    # ── damage handling ──
    def _damaged(self, err):
        if self.broken:
            return
        self.broken = True
        self.note = "Predpomnilnik je poškodovan — karte se nalagajo neposredno s spleta. Ob naslednjem zagonu se popravi sam."
        print("cache damaged:", err, flush=True)
        try:
            self.marker.write_text(str(err), encoding="utf-8")
        except OSError:
            pass

    def _safe(self, fn, default=None):
        if self.broken:
            return default
        try:
            return fn()
        except sqlite3.DatabaseError as e:
            if "locked" in str(e) or "busy" in str(e):
                return default
            self._damaged(e)
            return default

    def repair(self):
        """Move a damaged cache aside and salvage what can still be read into a fresh one."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bad = self.file.with_name(f"cache-poskodovan-{stamp}.sqlite")
        try:
            if self.file.exists():
                for suf in ("", "-wal", "-shm"):
                    src = Path(str(self.file) + suf)
                    if src.exists():
                        os.replace(src, Path(str(bad) + suf))
        except OSError as e:
            # still open in another Franc Map window: run this session on a temporary cache
            self.file = self.file.with_name(f"cache-zacasno-{os.getpid()}.sqlite")
            self.note = "Predpomnilnik je v uporabi v drugem oknu Franc Map — zapri vsa okna in znova zaženi aplikacijo."
            print("cache repair postponed:", e, flush=True)
            return {"repaired": False, "reason": str(e)}
        try:
            self.marker.unlink()
        except OSError:
            pass
        self.path = str(self.file)
        self.local = threading.local()
        self._init_schema()
        copied = {"tiles": 0, "http": 0}
        if bad.exists():
            try:
                old = sqlite3.connect(f"file:{bad}?mode=ro", uri=True, timeout=5)
                new = self.conn()
                # read each table from both ends: a damaged page stops a scan, so what lies past it
                # is still reachable when scanning backwards
                passes = [("tiles", 7, ""), ("tiles", 7, " ORDER BY layer DESC, z DESC, x DESC, y DESC"),
                          ("http", 4, ""), ("http", 4, " ORDER BY key DESC")]
                for table, cols, order in passes:
                    try:
                        cur = old.execute(f"SELECT * FROM {table}{order}")
                        while True:
                            try:
                                rows = cur.fetchmany(500)
                            except sqlite3.DatabaseError:
                                break           # reached the damaged part
                            if not rows:
                                break
                            new.executemany(f"INSERT OR IGNORE INTO {table} VALUES({','.join('?' * cols)})", rows)
                            copied[table] += len(rows)
                        new.commit()
                    except sqlite3.DatabaseError:
                        new.commit()
                old.close()
            except sqlite3.DatabaseError as e:
                print("salvage failed:", e, flush=True)
        try:   # report what is really in the repaired cache
            copied["tiles"] = self.conn().execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
            copied["http"] = self.conn().execute("SELECT COUNT(*) FROM http").fetchone()[0]
        except sqlite3.DatabaseError:
            pass
        self.note = f"Predpomnilnik je bil poškodovan in je popravljen (ohranjenih {copied['tiles']} ploščic)."
        print("cache repaired:", copied, "damaged copy:", bad, flush=True)
        return {"repaired": True, "copied": copied, "damaged_copy": str(bad)}

    # ── reads / writes (all damage-tolerant) ──
    def get_tile(self, layer, z, x, y):
        return self._safe(lambda: self.conn().execute("SELECT data, ctype, ts FROM tiles WHERE layer=? AND z=? AND x=? AND y=?",
                                                      (layer, z, x, y)).fetchone())

    def has_tile(self, layer, z, x, y, max_age):
        r = self._safe(lambda: self.conn().execute("SELECT ts FROM tiles WHERE layer=? AND z=? AND x=? AND y=?",
                                                   (layer, z, x, y)).fetchone())
        return r is not None and time.time() - r[0] < max_age

    def put_tile(self, layer, z, x, y, data, ctype):
        def w():
            with self.wlock:
                c = self.conn()
                c.execute("INSERT OR REPLACE INTO tiles VALUES(?,?,?,?,?,?,?)",
                          (layer, z, x, y, sqlite3.Binary(data), ctype, int(time.time())))
                c.commit()
        self._safe(w)

    def get_http(self, key):
        return self._safe(lambda: self.conn().execute("SELECT data, ctype, ts FROM http WHERE key=?", (key,)).fetchone())

    def put_http(self, key, data, ctype):
        def w():
            with self.wlock:
                c = self.conn()
                c.execute("INSERT OR REPLACE INTO http VALUES(?,?,?,?)", (key, sqlite3.Binary(data), ctype, int(time.time())))
                c.commit()
        self._safe(w)

    def stats(self):
        def q():
            c = self.conn()
            rows = c.execute("SELECT layer, COUNT(*), SUM(LENGTH(data)) FROM tiles GROUP BY layer").fetchall()
            h = c.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(data)),0) FROM http").fetchone()
            return rows, h
        rows, h = self._safe(q, ([], (0, 0)))
        try:
            size = os.path.getsize(self.path) + (os.path.getsize(self.path + "-wal") if os.path.exists(self.path + "-wal") else 0)
        except OSError:
            size = 0
        return {"layers": [{"layer": r[0], "tiles": r[1], "bytes": r[2] or 0} for r in rows],
                "http": {"entries": h[0], "bytes": h[1]}, "fileBytes": size, "path": self.path,
                "broken": self.broken, "note": self.note}

    def clear(self, layer=None, what="tiles"):
        def w():
            with self.wlock:
                c = self.conn()
                if what == "http":
                    c.execute("DELETE FROM http")
                elif layer:
                    c.execute("DELETE FROM tiles WHERE layer=?", (layer,))
                else:
                    c.execute("DELETE FROM tiles")
                c.commit()
                c.execute("VACUUM")
        self._safe(w)


CACHE = Cache(DATA_DIR / "cache.sqlite")


def purge_stale_tiles():
    """Layers whose source changed carry "purgeBefore" (unix time) in layers.json: drop tiles cached
    before that, e.g. CARTO's "API KEY REQUIRED" images after the switch to Esri."""
    for lid, cfg in LAYERS.items():
        ts = cfg.get("purgeBefore")
        if not ts:
            continue
        try:
            with CACHE.wlock:
                c = CACHE.conn()
                c.execute("DELETE FROM tiles WHERE layer=? AND ts<?", (lid, int(ts)))
                c.commit()
        except Exception:  # noqa: BLE001 — never block start-up over cache housekeeping
            pass

# ─────────────────────────────── network ───────────────────────────────

# One shared session: HTTP keep-alive + TLS reuse across all request threads
# (the server spawns a thread per browser connection, so per-thread sessions
# meant a fresh TLS handshake to the tile server for almost every tile).
_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = USER_AGENT
_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=0)
_SESSION.mount("https://", _ADAPTER)
_SESSION.mount("http://", _ADAPTER)


def http_session() -> requests.Session:
    return _SESSION


class NetState:
    forced_offline = False
    last_ok = 0.0
    last_fail = 0.0

    @classmethod
    def online(cls):
        if cls.forced_offline:
            return False
        return cls.last_ok >= cls.last_fail or time.time() - cls.last_fail > 20

    @classmethod
    def ok(cls):
        cls.last_ok = time.time()

    @classmethod
    def fail(cls):
        cls.last_fail = time.time()


_nominatim_lock = threading.Lock()
_nominatim_last = [0.0]


def fetch_tile(layer_id, z, x, y, allow_network=True):
    """Return (data, ctype, source) or None. Cache-first; refresh when stale and online."""
    cfg = LAYERS.get(layer_id)
    if not cfg:
        return None
    row = CACHE.get_tile(layer_id, z, x, y)
    max_age = LIVE_TILE_MAX_AGE if cfg.get("live") else TILE_MAX_AGE
    if row and (time.time() - row[2] < max_age or not allow_network or NetState.forced_offline):
        return row[0], row[1], "cache"
    if not allow_network or NetState.forced_offline:
        return None
    if row:
        # stale-while-revalidate: answer instantly from cache, refresh in the background
        refresh_tile_later(layer_id, z, x, y)
        return row[0], row[1], "stale"
    return download_tile(layer_id, cfg, z, x, y)


def download_tile(layer_id, cfg, z, x, y):
    url = build_upstream_url(cfg, z, x, y)
    if not url:
        return None
    try:
        r = http_session().get(url, timeout=(6, 20))
        NetState.ok()
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip()
        if r.status_code == 200 and ctype.startswith("image/") and r.content:
            CACHE.put_tile(layer_id, z, x, y, r.content, ctype)
            return r.content, ctype, "net"
    except requests.RequestException:
        NetState.fail()
    return None


_refresh_pool = cf.ThreadPoolExecutor(max_workers=4)
_refreshing: set = set()
_refresh_lock = threading.Lock()


def refresh_tile_later(layer_id, z, x, y):
    key = (layer_id, z, x, y)
    with _refresh_lock:
        if key in _refreshing or len(_refreshing) > 400:
            return
        _refreshing.add(key)

    def job():
        try:
            download_tile(layer_id, LAYERS[layer_id], z, x, y)
        finally:
            with _refresh_lock:
                _refreshing.discard(key)

    _refresh_pool.submit(job)


# ─────────────────────────────── layer health check ───────────────────────────────
# Each raster layer is probed at a few sample tiles around Ljubljana (at zoom levels where
# the layer actually draws). Result per layer: ok | empty (valid but blank image) | fail.

HEALTH_FILE = STORE_DIR / "layer_health.json"
HEALTH_MAX_AGE = 6 * 3600
HEALTH = {"running": False, "checked": 0, "results": {}}


def png_is_blank(data: bytes) -> bool:
    """True when a PNG has no visible pixels (alpha 0 everywhere, or all palette
    indices pointing to a fully transparent palette entry)."""
    if not data.startswith(b"\x89PNG"):
        return False
    try:
        pos, idat, trns = 8, b"", b""
        width = height = depth = ctype = interlace = 0
        while pos < len(data):
            ln = struct.unpack(">I", data[pos:pos + 4])[0]
            typ, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + ln]
            if typ == b"IHDR":
                width, height, depth, ctype = struct.unpack(">IIBB", body[:10])
                interlace = body[12]
            elif typ == b"tRNS":
                trns = body
            elif typ == b"IDAT":
                idat += body
            pos += 12 + ln
        if depth != 8 or interlace or ctype not in (3, 4, 6):
            return False
        bpp = {3: 1, 4: 2, 6: 4}[ctype]
        stride = width * bpp
        raw = zlib.decompress(idat)
        prev = bytearray(stride)
        for r in range(height):
            f = raw[r * (stride + 1)]
            line = bytearray(raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)])
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = prev[i]
                if f == 1:
                    line[i] = (line[i] + left) & 255
                elif f == 2:
                    line[i] = (line[i] + up) & 255
                elif f == 3:
                    line[i] = (line[i] + ((left + up) >> 1)) & 255
                elif f == 4:
                    ul = prev[i - bpp] if i >= bpp else 0
                    pa, pb, pc = abs(up - ul), abs(left - ul), abs(left + up - 2 * ul)
                    line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else ul)) & 255
            if ctype == 3:
                if any(idx >= len(trns) or trns[idx] for idx in set(line)):
                    return False
            elif any(line[bpp - 1::bpp]):
                return False
            prev = line
        return True
    except Exception:  # noqa: BLE001
        return False


def sample_tiles(cfg):
    """A few tile coords around Ljubljana/central Slovenia at zooms where the layer draws."""
    lo = max(cfg.get("minZoom") or 0, 9)
    zooms = sorted({lo, min(lo + 3, 17), min(max(lo, 15), 17)})
    pts = [(46.0569, 14.5058), (46.2389, 14.3556), (46.5547, 15.6459)]  # Ljubljana, Kranj, Maribor
    out = []
    for z in zooms:
        for lat, lng in pts:
            x, y = lonlat_to_tile(lng, lat, z)
            out.append((z, x, y))
    return out


def check_layer(lid, cfg):
    valid = blank = 0
    detail = ""
    for z, x, y in sample_tiles(cfg):
        url = build_upstream_url(cfg, z, x, y)
        try:
            r = http_session().get(url, timeout=(6, 25))
            ctype = r.headers.get("Content-Type", "").split(";")[0]
            if r.status_code == 200 and ctype.startswith("image/") and r.content:
                valid += 1
                CACHE.put_tile(lid, z, x, y, r.content, ctype)
                if png_is_blank(r.content):
                    blank += 1
                elif valid - blank >= 1:
                    break  # clearly working
            else:
                detail = f"HTTP {r.status_code} {ctype or ''}".strip()
                if "html" in ctype:
                    detail = "Strežnik vrača stran z napako (vir ne deluje)"
        except requests.RequestException as e:
            detail = f"Ni odziva ({e.__class__.__name__})"
    if valid == 0:
        return {"status": "fail", "detail": detail or "Ni slike"}
    if valid == blank:
        return {"status": "empty", "detail": "Vir vrača samo prazne slike"}
    return {"status": "ok"}


def run_health_check(force=False):
    if HEALTH["running"]:
        return
    try:
        saved = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        saved = None
    if saved and not force and time.time() - saved.get("checked", 0) < HEALTH_MAX_AGE \
            and saved.get("version") == APP_VERSION \
            and set(saved.get("results", {})) >= {k for k, v in LAYERS.items() if v.get("type") in ("tile", "wms", "arcgis_export")}:
        HEALTH.update(saved, running=False)
        return
    if NetState.forced_offline:
        if saved:
            HEALTH.update(saved, running=False)
        return
    HEALTH["running"] = True
    if saved:
        HEALTH["results"] = saved.get("results", {})

    def job():
        todo = [(k, v) for k, v in LAYERS.items() if v.get("type") in ("tile", "wms", "arcgis_export")]
        with cf.ThreadPoolExecutor(max_workers=4) as pool:
            for lid, res in zip([k for k, _ in todo], pool.map(lambda kv: check_layer(*kv), todo)):
                HEALTH["results"][lid] = res
        HEALTH["checked"] = time.time()
        HEALTH["running"] = False
        HEALTH_FILE.write_text(json.dumps({"checked": HEALTH["checked"], "version": APP_VERSION,   # new version = new layer sources → re-check
                                           "results": HEALTH["results"]}), encoding="utf-8")

    threading.Thread(target=job, daemon=True).start()


def cached_get(url, ttl, ctype_hint=None):
    """GET a JSON/XML/image resource through the persistent cache. Returns (data, ctype, status, source)."""
    key = "GET " + url
    row = CACHE.get_http(key)
    if row and (time.time() - row[2] < ttl or NetState.forced_offline):
        return row[0], row[1], 200, "cache"
    if NetState.forced_offline:
        return b'{"error":"offline"}', "application/json", 503, "offline"
    host = urlparse(url).hostname or ""
    if "nominatim" in host:  # usage policy: max 1 request / second
        with _nominatim_lock:
            wait = 1.05 - (time.time() - _nominatim_last[0])
            if wait > 0:
                time.sleep(wait)
            _nominatim_last[0] = time.time()
    try:
        r = http_session().get(url, timeout=(6, 40), headers={"Accept-Language": "sl,en;q=0.7"})
        NetState.ok()
        ctype = r.headers.get("Content-Type", ctype_hint or "application/octet-stream").split(";")[0].strip()
        if r.status_code == 200:
            CACHE.put_http(key, r.content, ctype)
            return r.content, ctype, 200, "net"
        if row:
            return row[0], row[1], 200, "stale"
        return r.content, ctype, r.status_code, "net"
    except requests.RequestException as e:
        NetState.fail()
        if row:
            return row[0], row[1], 200, "stale"
        return json.dumps({"error": f"Ni povezave: {e.__class__.__name__}"}).encode(), "application/json", 503, "offline"


def overpass(query, ttl):
    key = "OVERPASS " + hashlib.sha1(query.encode()).hexdigest()
    row = CACHE.get_http(key)
    if row and (time.time() - row[2] < ttl or NetState.forced_offline):
        return row[0], 200, "cache"
    if NetState.forced_offline:
        return b'{"error":"offline","elements":[]}', 503, "offline"
    # Race all mirrors in parallel; first valid JSON wins (public mirrors are often overloaded)
    def ask(mirror):
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        r = s.post(mirror, data={"data": query}, timeout=(6, 120))
        if r.status_code == 200 and "json" in r.headers.get("Content-Type", ""):
            return r.content
        raise RuntimeError(f"HTTP {r.status_code}")

    last = "?"
    pool = cf.ThreadPoolExecutor(max_workers=len(OVERPASS_MIRRORS))
    futures = [pool.submit(ask, m) for m in OVERPASS_MIRRORS]
    try:
        for fut in cf.as_completed(futures, timeout=130):
            try:
                content = fut.result()
            except Exception as e:  # noqa: BLE001
                last = str(e) or e.__class__.__name__
                continue
            NetState.ok()
            CACHE.put_http(key, content, "application/json")
            return content, 200, "net"
    except cf.TimeoutError:
        last = "timeout"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if row:
        return row[0], 200, "stale"
    NetState.fail()
    return json.dumps({"error": f"Overpass ni dosegljiv ({last})", "elements": []}).encode(), 503, "offline"


# ─────────────────────────────── offline area download ───────────────────────────────

def lonlat_to_tile(lon, lat, z):
    lat = max(min(lat, 85.0511), -85.0511)
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_in_bbox(bbox, zmin, zmax):
    s, w, n, e = bbox
    for z in range(zmin, zmax + 1):
        x0, y0 = lonlat_to_tile(w, n, z)
        x1, y1 = lonlat_to_tile(e, s, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def count_tiles(bbox, zmin, zmax):
    s, w, n, e = bbox
    total = 0
    for z in range(zmin, zmax + 1):
        x0, y0 = lonlat_to_tile(w, n, z)
        x1, y1 = lonlat_to_tile(e, s, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


class OfflineJob:
    lock = threading.Lock()
    current: "OfflineJob | None" = None

    def __init__(self, layers, bbox, zmin, zmax, name):
        self.layers, self.bbox, self.zmin, self.zmax, self.name = layers, bbox, zmin, zmax, name
        self.total = count_tiles(bbox, zmin, zmax) * len(layers)
        self.done = self.failed = self.skipped = 0
        self.cancelled = False
        self.finished = False
        self.started = time.time()
        self.error = None

    def status(self):
        return {"name": self.name, "total": self.total, "done": self.done, "failed": self.failed,
                "skipped": self.skipped, "finished": self.finished, "cancelled": self.cancelled,
                "elapsed": round(time.time() - self.started, 1), "error": self.error}

    def run(self):
        def work(args):
            if self.cancelled:
                return
            layer, z, x, y = args
            max_age = LIVE_TILE_MAX_AGE if LAYERS[layer].get("live") else TILE_MAX_AGE
            if CACHE.has_tile(layer, z, x, y, max_age):
                self.skipped += 1
            elif fetch_tile(layer, z, x, y):
                self.done += 1
            else:
                self.failed += 1

        try:
            with cf.ThreadPoolExecutor(max_workers=6) as pool:
                jobs = ((l, z, x, y) for l in self.layers for z, x, y in tiles_in_bbox(self.bbox, self.zmin, self.zmax))
                # bounded submission so a cancel takes effect quickly
                pending = set()
                for j in jobs:
                    if self.cancelled:
                        break
                    pending.add(pool.submit(work, j))
                    if len(pending) >= 48:
                        _, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
                cf.wait(pending)
        except Exception as e:  # pragma: no cover
            self.error = str(e)
        self.finished = True
        save_offline_area(self)


def save_offline_area(job):
    path = STORE_DIR / "offline_areas.json"
    try:
        areas = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        areas = []
    areas.append({"name": job.name, "bbox": job.bbox, "zmin": job.zmin, "zmax": job.zmax, "layers": job.layers,
                  "tiles": job.done + job.skipped, "failed": job.failed, "cancelled": job.cancelled,
                  "date": time.strftime("%Y-%m-%d %H:%M")})
    path.write_text(json.dumps(areas, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────── HTTP server ───────────────────────────────

STORE_NAME = re.compile(r"^[a-z0-9_\-]{1,64}$")
TILE_PATH = re.compile(r"^/tiles/([\w\-]+)/(\d+)/(\d+)/(\d+)(?:\.\w+)?$")
ALLOWED_HOST = re.compile(r"^(?:[a-z0-9\-]+\.)?localhost(?::\d+)?$|^127\.0\.0\.1(?::\d+)?$|^\[::1\](?::\d+)?$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FrancMap"

    def log_message(self, fmt, *args):  # quiet
        pass

    # helpers
    def send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8",
                  {"Cache-Control": "no-store"})

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def guard(self):
        host = (self.headers.get("Host") or "").lower()
        if not ALLOWED_HOST.match(host):
            self.send(403, b"forbidden host", "text/plain")
            return False
        return True

    def api_guard(self, qs):
        if self.headers.get("X-Token") != TOKEN and qs.get("t", [""])[0] != TOKEN:
            self.send(403, b'{"error":"bad token"}')
            return False
        return True

    # routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.guard():
            return
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        m = TILE_PATH.match(u.path)
        if m:
            return self.tile(m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        if u.path.startswith("/api/"):
            if not self.api_guard(qs):
                return
            return self.api_get(u.path, qs)
        return self.static(u.path)

    def do_POST(self):
        self.do_write("POST")

    def do_PUT(self):
        self.do_write("PUT")

    def do_write(self, method):
        if not self.guard():
            return
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not u.path.startswith("/api/") or not self.api_guard(qs):
            return self.send(404, b"{}")
        try:
            payload = json.loads(self.body() or b"null")
        except json.JSONDecodeError:
            return self.json({"error": "bad json"}, 400)
        return self.api_post(u.path, payload)

    def tile(self, layer, z, x, y):
        res = fetch_tile(layer, z, x, y)
        if not res:
            return self.send(404, b"", "text/plain", {"Cache-Control": "no-store"})
        data, ctype, src = res
        live = LAYERS.get(layer, {}).get("live")
        age = 300 if live or src != "cache" else 86400  # let WebView2 keep fresh tiles in memory/disk
        self.send(200, data, ctype, {"Cache-Control": f"max-age={age}", "X-Source": src, "Access-Control-Allow-Origin": "*"})

    def static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            return self.send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript") or ctype.endswith("json"):
            ctype += "; charset=utf-8"
        self.send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def api_get(self, path, qs):
        q = lambda k, d=None: qs.get(k, [d])[0]
        if path == "/api/status":
            job = OfflineJob.current
            return self.json({"online": NetState.online(), "forcedOffline": NetState.forced_offline,
                              "version": APP_VERSION, "dataDir": str(DATA_DIR),
                              "job": job.status() if job else None, "cacheNote": CACHE.note})
        if path == "/api/fetch":
            url = q("url", "")
            host = urlparse(url).hostname or ""
            if not url.startswith("https://") or not any(host == h or host.endswith("." + h) for h in ALLOWED_FETCH_HOSTS):
                return self.json({"error": "host not allowed"}, 400)
            ttl = int(q("ttl", "86400"))
            data, ctype, status, src = cached_get(url, ttl)
            return self.send(status, data, ctype, {"X-Source": src, "Cache-Control": "no-store"})
        if path.startswith("/api/store/"):
            name = path.rsplit("/", 1)[1]
            if not STORE_NAME.match(name):
                return self.json({"error": "bad name"}, 400)
            f = STORE_DIR / f"{name}.json"
            if not f.exists():
                return self.send(200, b"null")
            return self.send(200, f.read_bytes(), "application/json; charset=utf-8", {"Cache-Control": "no-store"})
        if path == "/api/health":
            return self.json({"running": HEALTH["running"], "checked": HEALTH["checked"], "results": HEALTH["results"]})
        if path == "/api/cache/stats":
            return self.json(CACHE.stats())
        if path == "/api/offline/status":
            job = OfflineJob.current
            return self.json(job.status() if job else None)
        return self.json({"error": "unknown endpoint"}, 404)

    def api_post(self, path, p):
        if path == "/api/overpass":
            data, status, src = overpass(p.get("query", ""), int(p.get("ttl", 7 * 86400)))
            return self.send(status, data, "application/json", {"X-Source": src})
        if path.startswith("/api/store/"):
            name = path.rsplit("/", 1)[1]
            if not STORE_NAME.match(name):
                return self.json({"error": "bad name"}, 400)
            f = STORE_DIR / f"{name}.json"
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, f)
            return self.json({"ok": True})
        if path == "/api/network":
            NetState.forced_offline = bool(p.get("forcedOffline"))
            return self.json({"online": NetState.online(), "forcedOffline": NetState.forced_offline})
        if path == "/api/offline/start":
            with OfflineJob.lock:
                if OfflineJob.current and not OfflineJob.current.finished:
                    return self.json({"error": "Prenos že poteka."}, 409)
                layers = [l for l in p.get("layers", []) if l in LAYERS and not LAYERS[l].get("noBulk")]
                if not layers:
                    return self.json({"error": "Ni izbranih slojev, ki dovoljujejo prenos."}, 400)
                bbox = [float(v) for v in p["bbox"]]
                zmin, zmax = int(p.get("zmin", 8)), int(p.get("zmax", 15))
                if not (0 <= zmin <= zmax <= 19):
                    return self.json({"error": "Neveljaven razpon povečav."}, 400)
                job = OfflineJob(layers, bbox, zmin, zmax, str(p.get("name") or "Območje")[:80])
                if job.total > MAX_BULK_TILES:
                    return self.json({"error": f"Preveč ploščic ({job.total:,}). Največ {MAX_BULK_TILES:,} — zmanjšaj območje ali povečavo."}, 400)
                OfflineJob.current = job
                threading.Thread(target=job.run, daemon=True).start()
                return self.json(job.status())
        if path == "/api/offline/cancel":
            if OfflineJob.current:
                OfflineJob.current.cancelled = True
            return self.json({"ok": True})
        if path == "/api/health/run":
            run_health_check(force=True)
            return self.json({"running": HEALTH["running"]})
        if path == "/api/cache/clear":
            CACHE.clear(p.get("layer"), p.get("what", "tiles"))
            return self.json(CACHE.stats())
        # ── Skupno delo (shared work) ──
        if path == "/api/collab/host":
            try:
                return self.json(collab_server.CollabHost.start(str(p.get("name") or "Skupna seja")[:60],
                                                                data_file=STORE_DIR / "collab_host_session.json"))
            except Exception as e:  # noqa: BLE001
                return self.json({"error": str(e)}, 500)
        if path == "/api/collab/stophost":
            collab_server.CollabHost.stop()
            return self.json({"hosting": False})
        if path == "/api/collab/hostinfo":
            return self.json(collab_server.CollabHost.info())
        if path == "/api/collab/call":
            return self.collab_call(p)
        return self.json({"error": "unknown endpoint"}, 404)

    def collab_call(self, p):
        """Proxy a call to a collaboration server (LAN address or internet URL)."""
        server = str(p.get("server") or "").rstrip("/")
        cpath = str(p.get("path") or "")
        u = urlparse(server)
        if u.scheme not in ("http", "https") or not u.hostname or not cpath.startswith("/collab/"):
            return self.json({"error": "Neveljaven naslov strežnika."}, 400)
        try:
            if p.get("method", "GET") == "GET":
                r = http_session().get(server + cpath, timeout=(4, 10))
            else:
                r = http_session().post(server + cpath, json=p.get("body") or {}, timeout=(4, 15))
            return self.send(r.status_code, r.content, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
        except requests.RequestException as e:
            return self.json({"error": f"Strežnik seje ni dosegljiv ({e.__class__.__name__})."}, 502)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 128


def start_servers():
    """Bind the same port on 127.0.0.1 and ::1 (Chromium may resolve *.localhost to either)."""
    for _ in range(20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        try:
            v4 = Server(("127.0.0.1", port), Handler)
        except OSError:
            continue
        servers = [v4]
        try:
            class V6(Server):
                address_family = socket.AF_INET6
            servers.append(V6(("::1", port), Handler))
        except OSError:
            pass
        for srv in servers:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        return port
    raise RuntimeError("Ni proste vrat za lokalni strežnik")


# ─────────────────────────────── desktop window ───────────────────────────────

class JsApi:
    """Exposed to the UI as window.pywebview.api.* (native file dialogs)."""

    def __init__(self):
        self._window = None

    def save_text(self, filename, content):
        import webview
        ext = Path(filename).suffix.lstrip(".") or "txt"
        res = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=filename,
                                             file_types=(f"{ext.upper()} (*.{ext})", "Vse datoteke (*.*)"))
        if not res:
            return None
        target = res if isinstance(res, str) else res[0]
        Path(target).write_text(content, encoding="utf-8")
        return target

    def open_text(self, kinds="*.gpx;*.geojson;*.json;*.kml"):
        import webview
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True,
                                             file_types=(f"Podprte datoteke ({kinds})", "Vse datoteke (*.*)"))
        if not res:
            return []
        out = []
        for f in res:
            p = Path(f)
            out.append({"name": p.name, "content": p.read_text(encoding="utf-8", errors="replace")})
        return out

    def open_data_dir(self):
        os.startfile(str(DATA_DIR))  # noqa: S606 (Windows only)


def main():
    if "--collab-server" in sys.argv:   # headless shared-work server (LAN or internet)
        port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else collab_server.DEFAULT_PORT
        return collab_server.run_headless(port, data_dir=STORE_DIR)
    purge_stale_tiles()
    port = start_servers()
    # check layer sources after the UI has loaded its first tiles (don't compete for bandwidth)
    try:  # show the last known results right away
        HEALTH.update(json.loads(HEALTH_FILE.read_text(encoding="utf-8")), running=False)
    except Exception:  # noqa: BLE001
        pass
    hc = threading.Timer(20, run_health_check)
    hc.daemon = True
    hc.start()
    url = f"http://localhost:{port}/?t={TOKEN}&p={port}"
    if "--serve" in sys.argv:
        print(url, flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    import webview
    api = JsApi()
    win = webview.create_window("Franc Map TEST" if IS_TEST else "Franc Map", url, width=1440, height=900, min_size=(820, 560),
                                js_api=api, background_color="#0f172a", text_select=True)
    api._window = win
    webview.start(private_mode=False, storage_path=str(DATA_DIR / "webview"), debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
