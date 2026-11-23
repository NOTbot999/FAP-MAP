"""napProxy — backend proxy for NAP Slovenija (Nacionalna točka dostopa, nap.si) traffic data.

The app (frontend) only ever calls this proxy: /api/nap/data?sloj=<key>.
Credentials NEVER reach the frontend or git:
  1. environment variables NAP_USER / NAP_PASS (preferred), or
  2. %LOCALAPPDATA%\\FrancMap\\nap_credentials.json  {"user": "...", "pass": "..."}
     (outside the project folder; written by "Nastavitve → Promet v živo → Shrani prijavo",
      never sent back to the app — the status call only says whether it is set).

Datasets (whitelist) come from web/data/nap_datasets.json; codes can be overridden per user in
store/nap_codes.json (entered in the app). OAuth2 token kept in memory, renewed 60 s before expiry
or on HTTP 401 (one retry). Each dataset response cached 60 s. Errors → {"error", "status"}.
"""
import json
import os
import threading
import time
from pathlib import Path

import requests


class NapProxy:
    def __init__(self, web_dir: Path, data_dir: Path, store_dir: Path, session: requests.Session):
        self.cfg = json.loads((web_dir / "data" / "nap_datasets.json").read_text(encoding="utf-8"))
        self.cred_file = data_dir / "nap_credentials.json"       # NOT in store/ (store files are readable by the app)
        self.codes_file = store_dir / "nap_codes.json"
        self.http = session
        self.lock = threading.Lock()
        self.token = None           # {access_token, refresh_token, expires_at}
        self.cache = {}             # key → (ts, status, body, ctype)

    # ── configuration ──
    def datasets(self):
        ds = {k: dict(v) for k, v in self.cfg["DATASETS"].items()}
        try:
            for k, code in json.loads(self.codes_file.read_text(encoding="utf-8")).items():
                if k in ds and isinstance(code, str):
                    ds[k]["code"] = code.strip()
        except (OSError, ValueError):
            pass
        return ds

    def set_codes(self, codes: dict):
        ds = self.cfg["DATASETS"]
        clean = {k: str(v).strip() for k, v in (codes or {}).items() if k in ds}
        self.codes_file.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
        self.cache.clear()

    def credentials(self):
        u, p = os.environ.get("NAP_USER"), os.environ.get("NAP_PASS")
        if u and p:
            return u, p, "env"
        try:
            d = json.loads(self.cred_file.read_text(encoding="utf-8"))
            if d.get("user") and d.get("pass"):
                return d["user"], d["pass"], "file"
        except (OSError, ValueError):
            pass
        return None, None, None

    def set_credentials(self, user, password):
        if not user or not password:
            try:
                self.cred_file.unlink()
            except OSError:
                pass
        else:
            self.cred_file.write_text(json.dumps({"user": user, "pass": password}), encoding="utf-8")
        with self.lock:
            self.token = None
        self.cache.clear()

    def status(self):
        u, _, src = self.credentials()
        return {
            "configured": bool(u), "source": src,
            "user": (u[:2] + "…" + u[-2:]) if u and len(u) > 5 else ("…" if u else None),
            "attribution": self.cfg.get("atribucija"),
            "datasets": {k: {"ime": v.get("ime"), "code": v.get("code", ""), "configured": bool(v.get("code")),
                             "osvezi": v.get("osvezi"), "vklop": v.get("vklop", False), "tip": v.get("tip", "geojson"),
                             "opomba": v.get("opomba", "")} for k, v in self.datasets().items()},
        }

    # ── OAuth2 ──
    def _token_request(self, body):
        r = self.http.post(self.cfg["tokenUrl"], data=body, timeout=(8, 30),
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code != 200:
            raise PermissionError(f"prijava v NAP ni uspela (HTTP {r.status_code})")
        j = r.json()
        self.token = {"access_token": j["access_token"], "refresh_token": j.get("refresh_token"),
                      "expires_at": time.time() + float(j.get("expires_in", 3600))}

    def _ensure_token(self, force=False):
        with self.lock:
            if self.token and not force and time.time() < self.token["expires_at"] - 60:
                return self.token["access_token"]
            if self.token and self.token.get("refresh_token"):
                try:
                    self._token_request({"grant_type": "refresh_token", "refresh_token": self.token["refresh_token"]})
                    return self.token["access_token"]
                except (PermissionError, requests.RequestException, KeyError, ValueError):
                    self.token = None
            user, password, _ = self.credentials()
            if not user:
                raise LookupError("NAP prijava ni nastavljena (NAP_USER / NAP_PASS)")
            self._token_request({"grant_type": "password", "username": user, "password": password})
            return self.token["access_token"]

    def _get(self, code, token):
        url = self.cfg["dataUrl"] + code
        h = {"Authorization": f"bearer {token}", "Accept": "application/json"}
        r = self.http.get(url, headers=h, timeout=(8, 40))
        if r.status_code == 405:                    # documentation also mentions POST
            r = self.http.post(url, headers=h, timeout=(8, 40))
        return r

    # ── data ──
    def fetch(self, key):
        """→ (http_status, body_bytes, content_type)"""
        ds = self.datasets()
        if key not in ds:                           # whitelist
            return 400, json.dumps({"error": "neznan sloj", "status": 400}).encode(), "application/json"
        code = ds[key].get("code")
        if not code:
            return 400, json.dumps({"error": "ni nastavljeno (manjka koda nabora)", "status": 400}).encode(), "application/json"
        ttl = float(self.cfg.get("cacheSekund", 60))
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1], hit[2], hit[3]
        try:
            r = self._get(code, self._ensure_token())
            if r.status_code == 401:                # token expired / revoked → renew once and retry
                r = self._get(code, self._ensure_token(force=True))
            if r.status_code != 200:
                msg = {401: "prijava deluje, dostop do nabora pa še ni odobren (nap.si → profil → Pravice)",
                       403: "ni dostopa do nabora ali napačna koda (preveri kodo in Pravice na nap.si)",
                       404: "nabor ne obstaja (preveri kodo)"}.get(r.status_code, f"NAP HTTP {r.status_code}")
                return r.status_code, json.dumps({"error": msg, "status": r.status_code}).encode(), "application/json"
            out = (200, r.content, r.headers.get("Content-Type", "application/json").split(";")[0])
            self.cache[key] = (time.time(), *out)
            return out
        except LookupError as e:
            return 503, json.dumps({"error": str(e), "status": 503}).encode(), "application/json"
        except PermissionError as e:
            return 401, json.dumps({"error": str(e), "status": 401}).encode(), "application/json"
        except requests.RequestException as e:
            return 502, json.dumps({"error": f"NAP ni dosegljiv ({e.__class__.__name__})", "status": 502}).encode(), "application/json"
        except Exception as e:  # noqa: BLE001 — the app must never crash on a bad response
            return 500, json.dumps({"error": f"napaka: {e}", "status": 500}).encode(), "application/json"
