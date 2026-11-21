"""Skupno delo — tiny collaboration server for Franc Map.

One shared session: members, shared pins, shared shapes (drawings), chat messages and live
positions. Every request must carry the session code. Used three ways:
  * the desktop app hosts it on the local network ("Gosti sejo"),
  * phones / other PCs join through their own local backend (proxy at /api/collab/call),
  * headless on any server for sharing over the internet:  python app.py --collab-server [--port 47800]
"""
from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROTOCOL = 1
DEFAULT_PORT = 47800
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I confusion
MAX_BODY = 2_000_000
LIMITS = {"messages": 500, "pins": 2000, "shapes": 1000}


def new_code(n=6):
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def _id():
    return secrets.token_hex(6)


class Session:
    def __init__(self, name, code=None, data_file: Path | None = None):
        self.lock = threading.Lock()
        self.file = data_file
        self.code = code or new_code()
        self.name = name or "Skupna seja"
        self.created = time.time()
        self.members: dict[str, dict] = {}
        self.pins: list[dict] = []
        self.shapes: list[dict] = []
        self.messages: list[dict] = []
        self.rev = 0
        if data_file and data_file.exists():
            try:
                d = json.loads(data_file.read_text(encoding="utf-8"))
                if d.get("code") == self.code or code is None:
                    self.__dict__.update({k: d[k] for k in ("code", "name", "created", "pins", "shapes", "messages") if k in d})
            except Exception:  # noqa: BLE001
                pass

    def save(self):
        if not self.file:
            return
        d = {k: getattr(self, k) for k in ("code", "name", "created", "pins", "shapes", "messages")}
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.file)

    def touch(self, member, name=None):
        m = self.members.setdefault(member, {"id": member, "name": name or "Gost", "joined": time.time()})
        if name:
            m["name"] = name[:40]
        m["seen"] = time.time()
        return m

    def snapshot(self, since_rev=0):
        now = time.time()
        return {
            "protocol": PROTOCOL, "name": self.name, "rev": self.rev, "now": now,
            "members": [{**m, "online": now - m.get("seen", 0) < 20} for m in self.members.values()],
            "pins": self.pins, "shapes": self.shapes,
            "messages": [m for m in self.messages if m["rev"] > since_rev] if since_rev else self.messages[-100:],
        }

    def apply(self, action, member, body):
        """Mutations; returns the created/affected object."""
        with self.lock:
            self.rev += 1
            name = self.members.get(member, {}).get("name", "Gost")
            out = {"ok": True}
            if action == "pin":
                p = body.get("pin") or {}
                pin = {"id": p.get("id") or _id(), "lat": float(p["lat"]), "lng": float(p["lng"]), "name": str(p.get("name", "Oznaka"))[:80],
                       "note": str(p.get("note", ""))[:500], "color": str(p.get("color", "#e11d48"))[:16], "by": name, "member": member,
                       "ts": time.time(), "rev": self.rev}
                self.pins = [x for x in self.pins if x["id"] != pin["id"]] + [pin]
                out = pin
            elif action == "pin/delete":
                self.pins = [x for x in self.pins if x["id"] != body.get("id")]
            elif action == "shape":
                f = body.get("feature") or {}
                shape = {"id": f.get("id") or _id(), "feature": f, "by": name, "member": member, "ts": time.time(), "rev": self.rev}
                self.shapes = [x for x in self.shapes if x["id"] != shape["id"]] + [shape]
                out = shape
            elif action == "shape/delete":
                self.shapes = [x for x in self.shapes if x["id"] != body.get("id")]
            elif action == "message":
                text = str(body.get("text", "")).strip()[:1000]
                if text:
                    msg = {"id": _id(), "by": name, "member": member, "text": text, "ts": time.time(), "rev": self.rev}
                    self.messages.append(msg)
                    out = msg
            elif action == "position":
                m = self.members.get(member)
                if m is not None:
                    m["pos"] = [float(body["lat"]), float(body["lng"])] if body.get("lat") is not None else None
            elif action == "leave":
                self.members.pop(member, None)
            for k, lim in LIMITS.items():
                lst = getattr(self, k)
                if len(lst) > lim:
                    setattr(self, k, lst[-lim:])
            if action not in ("position", "leave"):
                self.save()
            return out


class CollabHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FrancMapCollab"
    session: Session = None  # set on the server class

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _session(self, code):
        s = self.server.session
        if not s or not code or code.strip().upper() != s.code:
            self._send(403, {"error": "Napačna koda seje."})
            return None
        return s

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/collab/ping":
            s = self.server.session
            return self._send(200, {"ok": True, "protocol": PROTOCOL, "name": s.name if s else None})
        if u.path == "/collab/state":
            s = self._session(q.get("code"))
            if not s:
                return
            with s.lock:
                if q.get("member"):
                    s.touch(q["member"])
                return self._send(200, s.snapshot(int(q.get("since") or 0)))
        self._send(404, {"error": "unknown"})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._send(413, {"error": "Prevelika zahteva."})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        s = self._session(body.get("code"))
        if not s:
            return
        member = str(body.get("member") or "")[:40]
        if not member:
            return self._send(400, {"error": "member manjka"})
        action = u.path[len("/collab/"):] if u.path.startswith("/collab/") else ""
        if action == "join":
            with s.lock:
                s.touch(member, str(body.get("name") or "Gost"))
                return self._send(200, s.snapshot())
        if action not in ("pin", "pin/delete", "shape", "shape/delete", "message", "position", "leave"):
            return self._send(404, {"error": "unknown"})
        with s.lock:
            s.touch(member)
        try:
            return self._send(200, s.apply(action, member, body))
        except (KeyError, ValueError, TypeError) as e:
            return self._send(400, {"error": f"Neveljavni podatki ({e})"})


class _Srv(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def lan_addresses():
    """Best-effort list of this machine's local network IPv4 addresses."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outgoing interface
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


class CollabHost:
    """Starts/stops the session server for the desktop app (bound to all interfaces while hosting)."""
    srv: _Srv | None = None

    @classmethod
    def start(cls, name, port=DEFAULT_PORT, data_file: Path | None = None, code=None):
        if cls.srv:
            return cls.info()
        session = Session(name, code=code, data_file=data_file)
        srv = None
        for p in range(port, port + 20):
            try:
                srv = _Srv(("0.0.0.0", p), CollabHandler)
                break
            except OSError:
                continue
        if not srv:
            raise RuntimeError("Ni proste vrat za strežnik seje.")
        srv.session = session
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cls.srv = srv
        session.save()
        return cls.info()

    @classmethod
    def stop(cls):
        if cls.srv:
            cls.srv.shutdown()
            cls.srv.server_close()
            cls.srv = None

    @classmethod
    def info(cls):
        if not cls.srv:
            return {"hosting": False}
        port = cls.srv.server_address[1]
        s = cls.srv.session
        return {"hosting": True, "code": s.code, "name": s.name, "port": port,
                "addresses": [f"http://{ip}:{port}" for ip in lan_addresses()], "local": f"http://127.0.0.1:{port}"}


def run_headless(port=DEFAULT_PORT, name="Skupna seja", data_dir: Path | None = None, code=None):
    data_file = (data_dir / "collab_server_session.json") if data_dir else None
    info = CollabHost.start(name, port, data_file, code=code)
    print(f"Franc Map — strežnik za skupno delo teče na vratih {info['port']}")
    print(f"Koda seje: {info['code']}")
    for a in info["addresses"]:
        print(f"  naslov v lokalnem omrežju: {a}")
    print("Za dostop prek interneta odpri vrata v požarnem zidu / usmerjevalniku ali postavi HTTPS proxy pred strežnik.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        CollabHost.stop()
