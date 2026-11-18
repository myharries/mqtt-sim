#!/usr/bin/env python3
"""
myH MQTT simulator: publishes test values to an MQTT broker, driven by CSV files.

  profile CSV : one row per topic, saying how its value is made and how often
  route CSV   : optional GPS waypoints, for rows with mode "route"

Settings come from environment variables (handy in Docker / Unraid) or the
command line. Run `python mqtt_sim.py --help` for the list.

Profile columns (header row required, extra columns are ignored):
  enabled   yes/no            skip the row when "no"
  topic     text              {portal} is replaced with the PORTAL setting
  format    number | bool | text | victron | json
                              victron = {"value": <v>} like the Cerbo's N/ topics
  mode      constant | random | drift | sine | list | route
  value     constant: the value; list: values separated by |;
            route: lat | lon | speed_ms | speed_kmh | fix | course | altitude | coords
  min, max  limits for random / drift / sine
  step      largest change per publish for drift
  period_s  sine: seconds for one full cycle
  every_s   seconds between publishes (default 5)
  decimals  rounding for numbers (default 1)
  retain    yes/no
  notes     free text
"""
import argparse
import csv
import shutil
import json
import math
import os
import random
import signal
import sys
import threading
import time
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import paho.mqtt.client as mqtt  # noqa: E402  (vendored in ./lib)

EARTH_R = 6371000.0
VERSION = "2.0.0"
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or "/data"


def resolve(rel):
    """Absolute paths are used as given. A relative path (e.g. profiles/x.csv) is looked up in the
    data folder first (your own files, kept across updates), then in the files built into the app."""
    if os.path.isabs(rel):
        return rel
    for root in (DATA_DIR, BASE):
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            return p
    return os.path.join(BASE, rel)


def refresh_examples():
    """Copy the built-in profiles and routes to <data>/examples so they can be seen and copied.
    Refreshed on every start; nothing outside examples/ is touched."""
    if not os.path.isdir(DATA_DIR) or not os.access(DATA_DIR, os.W_OK):
        return
    for sub in ("profiles", "routes"):
        src = os.path.join(BASE, sub)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(DATA_DIR, "examples", sub)
        try:
            os.makedirs(dst, exist_ok=True)
            for f in os.listdir(src):
                shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
        except OSError as e:
            print(f"[mqtt-sim] could not refresh examples in {dst}: {e}", flush=True)


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def truthy(s, default=False):
    if s is None or str(s).strip() == "":
        return default
    return str(s).strip().lower() in ("1", "y", "yes", "true", "on")


def fnum(s, default=None):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- GPS route
def haversine(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(d))


def bearing(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class Route:
    """Waypoints: lat, lon, speed_kmh (to reach the NEXT point), park_min (stop here first)."""

    def __init__(self, path, speedup=1.0, loop=False, altitude=30.0):
        self.points = []
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                lat, lon = fnum(row.get("lat")), fnum(row.get("lon"))
                if lat is None or lon is None:
                    continue
                self.points.append({
                    "lat": lat, "lon": lon,
                    "speed_kmh": fnum(row.get("speed_kmh"), 60.0),
                    "park_min": fnum(row.get("park_min"), 0.0),
                    "altitude": fnum(row.get("altitude"), altitude),
                    "name": (row.get("name") or "").strip(),
                })
        if not self.points:
            raise ValueError(f"route {os.path.basename(path)} has no waypoints (needs lat and lon columns)")
        self.speedup = max(speedup, 0.01)
        self.loop = loop
        self.segments = []  # (start_s, end_s, kind, i)
        t = 0.0
        n = len(self.points)
        for i, p in enumerate(self.points):
            if p["park_min"] > 0:
                self.segments.append((t, t + p["park_min"] * 60, "park", i))
                t += p["park_min"] * 60
            if i < n - 1:
                dist = haversine((p["lat"], p["lon"]), (self.points[i + 1]["lat"], self.points[i + 1]["lon"]))
                dur = dist / max(p["speed_kmh"] / 3.6, 0.1)
                self.segments.append((t, t + dur, "drive", i))
                t += dur
        self.total = t
        self.t0 = time.monotonic()
        self.last_state = None

    def describe(self):
        km = sum(haversine((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in zip(self.points, self.points[1:])) / 1000
        return f"{len(self.points)} waypoints, {km:.1f} km, {self.total / 60 / self.speedup:.1f} min at speed-up x{self.speedup:g}"

    def state(self):
        el = (time.monotonic() - self.t0) * self.speedup
        if self.loop and self.total > 0:
            el %= self.total
        last = self.points[-1]
        st = {"lat": last["lat"], "lon": last["lon"], "speed_ms": 0.0, "course": 0.0,
              "altitude": last["altitude"], "fix": 1, "phase": "arrived", "name": last["name"]}
        for start, end, kind, i in self.segments:
            if start <= el < end:
                p = self.points[i]
                if kind == "park":
                    st = {"lat": p["lat"], "lon": p["lon"], "speed_ms": 0.0,
                          "course": self.last_state["course"] if self.last_state else 0.0,
                          "altitude": p["altitude"], "fix": 1, "phase": "parked", "name": p["name"]}
                else:
                    q = self.points[i + 1]
                    f = (el - start) / (end - start) if end > start else 1
                    st = {"lat": p["lat"] + (q["lat"] - p["lat"]) * f,
                          "lon": p["lon"] + (q["lon"] - p["lon"]) * f,
                          "speed_ms": p["speed_kmh"] / 3.6,
                          "course": bearing((p["lat"], p["lon"]), (q["lat"], q["lon"])),
                          "altitude": p["altitude"] + (q["altitude"] - p["altitude"]) * f,
                          "fix": 1, "phase": "driving", "name": f"{p['name']} -> {q['name']}"}
                break
        st["speed_kmh"] = st["speed_ms"] * 3.6
        st["coords"] = f"{st['lat']:.3f},{st['lon']:.3f}"
        self.last_state = st
        return st


# ------------------------------------------------------------- topic rows
class Row:
    def __init__(self, d, portal, default_every):
        self.topic = d["topic"].strip().replace("{portal}", portal)
        self.format = (d.get("format") or "number").strip().lower()
        self.mode = (d.get("mode") or "constant").strip().lower()
        self.value = (d.get("value") or "").strip()
        self.min = fnum(d.get("min"), 0.0)
        self.max = fnum(d.get("max"), self.min if fnum(d.get("min")) is not None else 100.0)
        self.step = fnum(d.get("step"), (self.max - self.min) / 20 or 1)
        self.period = fnum(d.get("period_s"), 600.0)
        self.every = max(fnum(d.get("every_s"), default_every), 0.2)
        dec = fnum(d.get("decimals"))
        self.decimals = int(dec) if dec is not None else 1
        self.retain = truthy(d.get("retain"), False)
        self.items = [v.strip() for v in self.value.split("|")] if self.mode == "list" else []
        self.idx = 0
        self.cur = fnum(self.value, (self.min + self.max) / 2)
        self.next_due = 0.0

    def make(self, t, route_state):
        m = self.mode
        if m == "constant":
            v = self.value
        elif m == "random":
            if self.format == "bool":
                v = random.choice([True, False])
            else:
                v = random.uniform(self.min, self.max)
        elif m == "drift":
            self.cur = min(self.max, max(self.min, self.cur + random.uniform(-self.step, self.step)))
            v = self.cur
        elif m == "sine":
            mid, amp = (self.max + self.min) / 2, (self.max - self.min) / 2
            v = mid + amp * math.sin(2 * math.pi * t / self.period)
        elif m == "list":
            v = self.items[self.idx % len(self.items)] if self.items else ""
            self.idx += 1
        elif m == "route":
            if route_state is None:
                return None
            v = route_state.get(self.value)
        else:
            v = self.value
        return self.encode(v)

    def encode(self, v):
        f = self.format
        if f == "bool":
            b = truthy(v) if not isinstance(v, bool) else v
            return "true" if b else "false"
        if f == "text":
            return str(v)
        if f == "json":
            return v if isinstance(v, str) else json.dumps(v)
        n = fnum(v)
        if n is not None:
            n = round(n, self.decimals)
            if self.decimals == 0:
                n = int(n)
        if f == "victron":
            return json.dumps({"value": n if n is not None else v})
        return str(n if n is not None else v)


def load_profile(path, portal, default_every):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for d in csv.DictReader(f):
            if not (d.get("topic") or "").strip() or (d.get("topic") or "").strip().startswith("#"):
                continue
            if not truthy(d.get("enabled"), True):
                continue
            rows.append(Row(d, portal, default_every))
    return rows


# ------------------------------------------------------------------- log
LOG = deque(maxlen=300)


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    LOG.append(line)
    print(f"[mqtt-sim] {msg}", flush=True)


def load_profile_checked(path, portal, default_every):
    with open(path, newline="", encoding="utf-8-sig") as f:
        head = next(csv.reader(f), [])
    if "topic" not in [h.strip().lower() for h in head]:
        raise ValueError(f"{os.path.basename(path)} is not a profile: it needs a 'topic' column")
    return load_profile(path, portal, default_every)


# ------------------------------------------------------------- files
KINDS = ("profiles", "routes")
SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_. ")


def clean_name(name):
    name = os.path.basename(str(name or "")).strip()
    name = "".join(ch for ch in name if ch in SAFE).strip(" .")
    if not name:
        raise ValueError("file name is empty")
    if not name.lower().endswith(".csv"):
        name += ".csv"
    return name


def file_path(src, kind, name):
    if kind not in KINDS:
        raise ValueError("unknown kind")
    root = DATA_DIR if src == "data" else BASE
    return os.path.join(root, kind, clean_name(name))


def list_files(kind):
    out = []
    for src, root in (("data", DATA_DIR), ("example", BASE)):
        d = os.path.join(root, kind)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.lower().endswith(".csv"):
                    out.append({"src": src, "name": f})
    return out


# ------------------------------------------------------------- simulator
class Sim:
    """One simulation: profile (+ optional route) publishing to the broker. Runs in a thread
    so the web page can start, stop and switch files without restarting the container."""

    def __init__(self, a):
        self.a = a
        self.lock = threading.Lock()
        self.thread = None
        self.stop_evt = threading.Event()
        self.client = None
        self.connected = False
        self.rows = []
        self.route = None
        self.rstate = None
        self.values = {}          # topic -> [payload, time]
        self.started = None
        self.error = ""
        self.sel = {"profile": None, "route": None}   # {"src": "data"|"example", "name": ...}
        self.speedup = a.speedup
        self.loop = a.loop
        self.load_state()

    # ---- selection, remembered in <data>/mqtt-sim.json
    def state_file(self):
        return os.path.join(DATA_DIR, "mqtt-sim.json")

    def load_state(self):
        try:
            with open(self.state_file()) as f:
                st = json.load(f)
            self.sel = st.get("sel", self.sel)
            self.speedup = float(st.get("speedup", self.speedup))
            self.loop = bool(st.get("loop", self.loop))
            self.autostart = bool(st.get("running", True))
            return
        except (OSError, ValueError):
            pass
        # first start: settings from the container (PROFILE / ROUTE)
        self.autostart = True
        for key, val in (("profile", self.a.profile), ("route", self.a.route)):
            if val:
                p = resolve(val)
                src = "data" if p.startswith(DATA_DIR + os.sep) else "example"
                self.sel[key] = {"src": src, "name": os.path.basename(p)}

    def save_state(self):
        try:
            if os.path.isdir(DATA_DIR) and os.access(DATA_DIR, os.W_OK):
                with open(self.state_file(), "w") as f:
                    json.dump({"sel": self.sel, "speedup": self.speedup, "loop": self.loop,
                               "running": self.running()}, f, indent=1)
        except OSError as e:
            log(f"could not save settings: {e}")

    def sel_path(self, key):
        s = self.sel.get(key)
        if not s:
            return None
        return file_path(s["src"], "profiles" if key == "profile" else "routes", s["name"])

    # ---- MQTT
    def connect(self):
        a = self.a
        if a.dry_run or self.client:
            return
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=a.client_id, clean_session=True)
        if a.user:
            c.username_pw_set(a.user, a.password or "")

        def on_connect(cl, u, flags, rc, props=None):
            self.connected = (rc == 0)
            if rc == 0:
                log(f"connected to {a.host}:{a.port} as {a.user or '(anonymous)'}")
            else:
                log(f"connection refused: {rc}")

        def on_disconnect(cl, u, flags, rc, props=None):
            self.connected = False
            if rc != 0:
                log(f"disconnected ({rc}), retrying")

        c.on_connect, c.on_disconnect = on_connect, on_disconnect
        c.reconnect_delay_set(2, 30)
        c.connect_async(a.host, a.port, keepalive=60)
        c.loop_start()
        self.client = c
        for _ in range(50):
            if c.is_connected():
                break
            time.sleep(0.1)

    def publish(self, topic, payload, retain):
        if self.a.dry_run:
            print(f"{topic} {payload}{'  (retain)' if retain else ''}", flush=True)
        elif self.client:
            self.client.publish(topic, payload, qos=0, retain=retain)
            if self.a.verbose:
                print(f"{topic} {payload}", flush=True)

    def clear(self, rows):
        n = 0
        for r in rows:
            if r.retain:
                self.publish(r.topic, b"" if not self.a.dry_run else "(clear)", True)
                n += 1
        if n:
            log(f"cleared {n} retained topics")

    # ---- run control
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self):
        with self.lock:
            if self.running():
                return
            self.error = ""
            try:
                ppath = self.sel_path("profile")
                if not ppath or not os.path.isfile(ppath):
                    raise ValueError("choose a profile first")
                rows = load_profile_checked(ppath, self.a.portal, self.a.every)
                route = None
                rpath = self.sel_path("route")
                if rpath and any(r.mode == "route" for r in rows):
                    if os.path.isfile(rpath):
                        route = Route(rpath, self.speedup, self.loop)
                    else:
                        log(f"route {os.path.basename(rpath)} not found: GPS rows are skipped")
            except (OSError, ValueError, csv.Error) as e:
                self.error = str(e)
                log(f"not started: {e}")
                return
            self.rows, self.route, self.rstate = rows, route, None
            self.values = {}
            log(f"profile {self.sel['profile']['name']} ({self.sel['profile']['src']}): {len(rows)} topics")
            if route:
                log(f"route {self.sel['route']['name']}: {route.describe()}")
            elif any(r.mode == "route" for r in rows):
                log("no route: GPS rows are skipped")
            self.connect()
            self.stop_evt.clear()
            self.started = time.time()
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
        self.save_state()

    def stop(self, clear=None, save=True):
        with self.lock:
            if not self.running():
                return
            self.stop_evt.set()
            self.thread.join(5)
            if self.a.clear_on_exit if clear is None else clear:
                self.clear(self.rows)
            self.started = None
            log("stopped")
        if save:
            self.save_state()

    def restart(self):
        was = self.running()
        self.stop()
        if was:
            self.start()

    def run(self):
        a = self.a
        t0 = time.monotonic()
        last_status = 0.0
        while not self.stop_evt.is_set():
            now = time.monotonic()
            t = now - t0
            rs = self.route.state() if self.route else None
            self.rstate = rs
            for r in self.rows:
                if now >= r.next_due:
                    payload = r.make(t, rs)
                    if payload is not None:
                        self.publish(r.topic, payload, r.retain)
                        self.values[r.topic] = [payload if isinstance(payload, str) else payload.decode(), time.time()]
                    r.next_due = now + r.every
            if rs and now - last_status >= 60:
                log(f"gps {rs['phase']} {rs['coords']} {rs['speed_kmh']:.0f} km/h {rs['name']}")
                last_status = now
            if a.duration and t >= a.duration:
                break
            self.stop_evt.wait(0.2)

    def shutdown(self):
        self.stop(save=False)
        if self.client:
            time.sleep(1)
            self.client.loop_stop()
            self.client.disconnect()

    def status(self):
        rs = self.rstate
        return {
            "version": VERSION, "running": self.running(), "connected": self.connected or self.a.dry_run,
            "broker": f"{self.a.host}:{self.a.port}", "user": self.a.user or "", "dry_run": self.a.dry_run,
            "sel": self.sel, "speedup": self.speedup, "loop": self.loop, "error": self.error,
            "topics": len(self.rows), "uptime": int(time.time() - self.started) if self.started else 0,
            "gps": {k: rs[k] for k in ("phase", "name", "lat", "lon", "speed_kmh")} if rs else None,
            "route_info": self.route.describe() if self.route else "",
        }


# ------------------------------------------------------------- web page
def start_web(sim, port, password):
    import base64
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    page_path = os.path.join(BASE, "webui.html")

    class H(BaseHTTPRequestHandler):
        server_version = "mqtt-sim"

        def log_message(self, *args):
            pass

        def authed(self):
            if not password:
                return True
            hdr = self.headers.get("Authorization", "")
            try:
                ok = hdr.startswith("Basic ") and base64.b64decode(hdr[6:]).decode().split(":", 1)[1] == password
            except (ValueError, IndexError):
                ok = False
            if not ok:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="mqtt-sim"')
                self.end_headers()
            return ok

        def send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.authed():
                return
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in ("/", "/index.html"):
                with open(page_path, "rb") as f:
                    return self.send(200, f.read(), "text/html")
            if u.path == "/icon.png":
                p = os.path.join(BASE, "icon.png")
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        return self.send(200, f.read(), "image/png")
            if u.path == "/api/status":
                return self.send(200, sim.status())
            if u.path == "/api/values":
                return self.send(200, {"now": time.time(), "values": sim.values})
            if u.path == "/api/log":
                return self.send(200, {"lines": list(LOG)})
            if u.path == "/api/files":
                return self.send(200, {k: list_files(k) for k in KINDS})
            if u.path == "/api/file":
                try:
                    p = file_path(q.get("src"), q.get("kind"), q.get("name"))
                    with open(p, "rb") as f:
                        data = f.read()
                except (OSError, ValueError) as e:
                    return self.send(404, {"error": str(e)})
                return self.send(200, data, "text/csv",
                                 {"Content-Disposition": f'attachment; filename="{os.path.basename(p)}"'})
            self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.authed():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 2_000_000:
                    return self.send(413, {"error": "file too big (2 MB max)"})
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.send(400, {"error": "bad request"})
            try:
                return self.send(200, self.action(self.path, body))
            except (OSError, ValueError, csv.Error) as e:
                return self.send(400, {"error": str(e)})

        def action(self, path, b):
            if path == "/api/start":
                sim.start()
            elif path == "/api/stop":
                sim.stop()
            elif path == "/api/restart":
                sim.restart()
            elif path == "/api/select":
                key = b.get("key")
                if key not in ("profile", "route"):
                    raise ValueError("unknown selection")
                if b.get("name"):
                    p = file_path(b.get("src"), "profiles" if key == "profile" else "routes", b["name"])
                    if not os.path.isfile(p):
                        raise ValueError("file not found")
                    sim.sel[key] = {"src": "data" if b.get("src") == "data" else "example", "name": os.path.basename(p)}
                elif key == "route":
                    sim.sel["route"] = None
                else:
                    raise ValueError("choose a profile")
                sim.save_state()
                sim.restart()
            elif path == "/api/options":
                if "speedup" in b:
                    sim.speedup = max(0.1, min(1000.0, float(b["speedup"])))
                if "loop" in b:
                    sim.loop = bool(b["loop"])
                sim.save_state()
                sim.restart()
            elif path == "/api/upload":
                kind = b.get("kind")
                if kind not in KINDS:
                    raise ValueError("unknown kind")
                name = clean_name(b.get("name"))
                content = str(b.get("content") or "")
                head = [h.strip().lower() for h in next(csv.reader([content.splitlines()[0] if content else ""]), [])]
                if kind == "profiles" and "topic" not in head:
                    raise ValueError("not a profile CSV: the first row needs a 'topic' column")
                if kind == "routes" and not ("lat" in head and "lon" in head):
                    raise ValueError("not a route CSV: the first row needs 'lat' and 'lon' columns")
                d = os.path.join(DATA_DIR, kind)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, name), "w", encoding="utf-8", newline="") as f:
                    f.write(content)
                log(f"uploaded {kind}/{name}")
                if b.get("use"):
                    sim.sel["profile" if kind == "profiles" else "route"] = {"src": "data", "name": name}
                    sim.save_state()
                    sim.restart()
                return {"ok": True, "name": name}
            elif path == "/api/delete":
                kind = b.get("kind")
                p = file_path("data", kind, b.get("name"))
                key = "profile" if kind == "profiles" else "route"
                cur = sim.sel.get(key)
                if cur and cur["src"] == "data" and cur["name"] == os.path.basename(p) and sim.running():
                    raise ValueError("that file is in use: choose another one first")
                os.remove(p)
                log(f"deleted {kind}/{os.path.basename(p)}")
                if cur and cur["src"] == "data" and cur["name"] == os.path.basename(p):
                    sim.sel[key] = None
                    sim.save_state()
            else:
                raise ValueError("unknown action")
            return {"ok": True}

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"web page on port {port}{' (password set)' if password else ''}")
    return srv


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=env("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(env("MQTT_PORT", "1883")))
    ap.add_argument("--user", default=env("MQTT_USER"))
    ap.add_argument("--password", default=env("MQTT_PASS"))
    ap.add_argument("--client-id", default=env("MQTT_CLIENT_ID", "mqtt-sim"))
    ap.add_argument("--profile", default=env("PROFILE", "profiles/van-victron.csv"))
    ap.add_argument("--route", default=env("ROUTE", ""))
    ap.add_argument("--portal", default=env("PORTAL", "000000000000"))
    ap.add_argument("--speedup", type=float, default=float(env("SPEEDUP", "1")),
                    help="route time multiplier (1 = real time)")
    ap.add_argument("--loop", action="store_true", default=truthy(env("ROUTE_LOOP"), False))
    ap.add_argument("--every", type=float, default=float(env("DEFAULT_EVERY", "5")))
    ap.add_argument("--duration", type=float, default=float(env("DURATION", "0")),
                    help="stop after N seconds (0 = run until stopped)")
    ap.add_argument("--clear-on-exit", action="store_true", default=truthy(env("CLEAR_ON_EXIT"), True),
                    help="remove retained test values when stopping (default on)")
    ap.add_argument("--no-clear-on-exit", dest="clear_on_exit", action="store_false")
    ap.add_argument("--clear-only", action="store_true", help="remove retained values for every profile topic, then exit")
    ap.add_argument("--dry-run", action="store_true", help="print instead of publishing")
    ap.add_argument("--verbose", action="store_true", default=truthy(env("VERBOSE"), False))
    ap.add_argument("--web-port", type=int, default=int(env("WEB_PORT", "8099")),
                    help="port of the web page (0 = no web page)")
    ap.add_argument("--web-password", default=env("WEB_PASSWORD", ""))
    a = ap.parse_args()

    log(f"version {VERSION}")
    refresh_examples()
    for k in KINDS:
        try:
            os.makedirs(os.path.join(DATA_DIR, k), exist_ok=True)
        except OSError:
            pass
    sim = Sim(a)

    if a.clear_only:
        p = sim.sel_path("profile")
        sim.connect()
        sim.clear(load_profile_checked(p, a.portal, a.every))
        sim.shutdown()
        return

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    if a.web_port:
        start_web(sim, a.web_port, a.web_password)
    if sim.autostart or not a.web_port:
        sim.start()
    t0 = time.monotonic()
    while not stop.wait(0.5):
        if a.duration and time.monotonic() - t0 >= a.duration:
            break
        if not a.web_port and not sim.running():
            break
    sim.shutdown()
    log("exit")


if __name__ == "__main__":
    main()
