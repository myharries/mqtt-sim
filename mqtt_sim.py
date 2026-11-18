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
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import paho.mqtt.client as mqtt  # noqa: E402  (vendored in ./lib)

EARTH_R = 6371000.0
VERSION = "1.2.0"
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
            raise SystemExit(f"Route {path} has no waypoints")
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
    a = ap.parse_args()

    print(f"[mqtt-sim] version {VERSION}", flush=True)
    refresh_examples()
    prof_path = resolve(a.profile)
    rows = load_profile(prof_path, a.portal, a.every)
    route = None
    if any(r.mode == "route" for r in rows) and a.route:
        rpath = resolve(a.route)
        if os.path.isfile(rpath):
            route = Route(rpath, a.speedup, a.loop)
        else:
            print(f"[mqtt-sim] route {a.route} not found (looked in {DATA_DIR} and the built-in files): GPS rows are skipped", flush=True)
    print(f"[mqtt-sim] profile {prof_path}: {len(rows)} topics", flush=True)
    if route:
        print(f"[mqtt-sim] route {os.path.basename(a.route)}: {route.describe()}", flush=True)
    elif any(r.mode == "route" for r in rows) and not a.route:
        print("[mqtt-sim] no ROUTE set: GPS (route) rows are skipped", flush=True)

    client = None
    if not a.dry_run:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=a.client_id, clean_session=True)
        if a.user:
            client.username_pw_set(a.user, a.password or "")

        def on_connect(c, u, flags, rc, props=None):
            if rc == 0:
                print(f"[mqtt-sim] connected to {a.host}:{a.port} as {a.user or '(anonymous)'}", flush=True)
            else:
                print(f"[mqtt-sim] connection refused: {rc}", flush=True)

        def on_disconnect(c, u, flags, rc, props=None):
            if rc != 0:
                print(f"[mqtt-sim] disconnected ({rc}), retrying", flush=True)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.reconnect_delay_set(2, 30)
        client.connect_async(a.host, a.port, keepalive=60)
        client.loop_start()
        for _ in range(100):
            if client.is_connected():
                break
            time.sleep(0.1)

    def publish(topic, payload, retain):
        if a.dry_run:
            print(f"{topic} {payload}{'  (retain)' if retain else ''}", flush=True)
        else:
            client.publish(topic, payload, qos=0, retain=retain)
            if a.verbose:
                print(f"{topic} {payload}", flush=True)

    def clear():
        n = 0
        for r in rows:
            if r.retain:
                publish(r.topic, b"" if not a.dry_run else "(clear)", True)
                n += 1
        print(f"[mqtt-sim] cleared {n} retained topics", flush=True)

    if a.clear_only:
        clear()
        if client:
            time.sleep(1)
            client.loop_stop()
            client.disconnect()
        return

    stop = {"flag": False}

    def handle(sig, frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    start = time.monotonic()
    last_status = 0.0
    while not stop["flag"]:
        now = time.monotonic()
        t = now - start
        rs = route.state() if route else None
        for r in rows:
            if now >= r.next_due:
                payload = r.make(t, rs)
                if payload is not None:
                    publish(r.topic, payload, r.retain)
                r.next_due = now + r.every
        if route and now - last_status >= 60:
            print(f"[mqtt-sim] gps {rs['phase']} {rs['coords']} {rs['speed_kmh']:.0f} km/h {rs['name']}", flush=True)
            last_status = now
        if a.duration and t >= a.duration:
            break
        time.sleep(0.2)

    if a.clear_on_exit:
        clear()
    if client:
        time.sleep(1)
        client.loop_stop()
        client.disconnect()
    print("[mqtt-sim] stopped", flush=True)


if __name__ == "__main__":
    main()
