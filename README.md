<img src="icon.png" width="128" align="right" alt="MQTT Simulator icon">

# mqtt-sim

A small MQTT simulator for testing. It reads a **profile** CSV (one row per topic, saying how the value is made and how often) and, optionally, a **route** CSV of GPS waypoints, then publishes to any MQTT broker.

Made for the Van Home Assistant (stand-in for the Victron Cerbo and Node-RED), but any project can use it with its own profile.

## How it is packaged

Every push to `main` builds a Docker image with GitHub Actions and publishes it as **`ghcr.io/myharries/mqtt-sim:latest`** (amd64 and arm64). The van profile and a demo route are built into the image. Your own profiles and routes go in the **data folder** (`/data`), which updates never touch.

```
mqtt-sim/
  mqtt_sim.py                 the simulator
  lib/paho/...                MQTT library (bundled)
  profiles/van-victron.csv    Node-RED victron/info topics + the Cerbo N/ topics
  routes/demo-wollongong-kiama.csv  demo drive: Wollongong Harbour → Kiama Blowhole, ~29 km, ~64 min, then parked 30 min
  Dockerfile                  image build
  .github/workflows/docker.yml  builds and publishes the image
  unraid/my-mqtt-sim.xml      Unraid container template
  icon.png / icon.svg         container icon (512 px PNG, editable SVG)
```

Data folder (`/data` in the container):

```
data/
  profiles/   your own profile CSVs (looked for here first)
  routes/     your own route CSVs
  examples/   copies of the built-in profiles and routes, refreshed on every start
```

A relative `PROFILE` / `ROUTE` such as `profiles/van-victron.csv` is looked for in the data folder first, then in the built-in files. To customise the van profile, copy `examples/profiles/van-victron.csv` to `profiles/` and edit it there.

## Before you start

The simulator logs in to the broker like any MQTT client. With the Home Assistant Mosquitto app, logins are Home Assistant users:

1. In HA: **Settings → People → Users → Add user**, name `mqtt-sim`, a password, **Can only log in from the local network** on, **Administrator** off.
2. Use that name and password as the container's **Broker user** / **Broker password**.

## Install on Unraid

1. Copy `unraid/my-mqtt-sim.xml` to the flash drive: `\\<unraid>\flash\config\plugins\dockerMan\templates-user\`.
2. Unraid → **Docker** → **Add Container** → Template: **mqtt-sim**.
3. Enter the **Broker password** for the `mqtt-sim` HA user, check the other fields, **Apply**.

Without the template, Add Container by hand: Repository `ghcr.io/myharries/mqtt-sim:latest`, Path `/data` → `/mnt/user/appdata/mqtt-sim/data`, Icon URL `https://raw.githubusercontent.com/myharries/mqtt-sim/main/icon.png?v=2`, and the variables below.

## Checking it works

- Unraid → **Docker** → mqtt-sim → **Logs**. The log starts with `[mqtt-sim] version …`, then the profile it loaded and how many topics, then (with a route) one GPS line per minute.
- In HA, **Settings → Devices & services → MQTT → Configure → Listen to a topic**: enter `victron/info/#` (or `#`) and **Start listening**. Values appear every few seconds.
- Stopping the container removes the retained test values (unless **Clear on stop** is `no`).

## Updating

Push a change to `main` (or edit on GitHub). The Action builds a new image in a couple of minutes; Unraid's Docker page then shows **update ready** for mqtt-sim, and **Update** installs it. Files in the data folder are kept.

The repository and the `mqtt-sim` package on GitHub must both be **public** so Unraid can pull the image and the icon without a login.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `MQTT_HOST` | localhost | Broker address (Van HA: 192.168.10.240) |
| `MQTT_PORT` | 1883 | |
| `MQTT_USER` / `MQTT_PASS` | | Broker login (`mqtt-sim` HA user) |
| `PROFILE` | profiles/van-victron.csv | Profile CSV: data folder first, then built-in |
| `DATA_DIR` | /data | Folder for your own profiles and routes |
| `ROUTE` | (none) | Route CSV; without it, `route` rows are skipped |
| `SPEEDUP` | 1 | Route time multiplier (10 = ten times faster) |
| `ROUTE_LOOP` | no | Start the route again when it ends |
| `PORTAL` | 000000000000 | Your Victron portal ID (VRM → Settings → General); replaces `{portal}` in topics |
| `DEFAULT_EVERY` | 5 | Seconds between publishes when a row leaves `every_s` blank |
| `CLEAR_ON_EXIT` | yes | When stopped, remove the retained test values from the broker |
| `DURATION` | 0 | Stop after N seconds (0 = keep running) |
| `VERBOSE` | no | Log every message |

Command line equivalents: `python mqtt_sim.py --help`. Useful extras: `--dry-run` (print, don't publish) and `--clear-only` (remove retained values for every profile topic, then exit).

## Profile CSV

Edit in Excel. Header row required; extra columns are ignored. Rows with `enabled` = `no`, or a topic starting with `#`, are skipped.

| Column | Values |
|---|---|
| `enabled` | yes / no |
| `topic` | e.g. `victron/info/battery_soc`, `N/{portal}/system/0/Dc/Battery/Soc` |
| `format` | `number`, `bool` (true/false), `text`, `victron` (`{"value": 87.5}`, the Cerbo's N/ format), `json` (raw) |
| `mode` | `constant`, `random`, `drift`, `sine`, `list`, `route` |
| `value` | constant: the value. drift: starting value. list: values separated by `\|`. route: `lat`, `lon`, `speed_ms`, `speed_kmh`, `fix`, `course`, `altitude` or `coords` (`lat,lon` text) |
| `min` / `max` | limits for random, drift and sine |
| `step` | drift: largest change per publish |
| `period_s` | sine: seconds for one full cycle (e.g. 1800 = solar up and down every 30 min) |
| `every_s` | seconds between publishes |
| `decimals` | rounding (0 = whole numbers) |
| `retain` | yes / no |
| `notes` | anything |

Each row is independent: `victron/info/battery_soc` and `N/.../Dc/Battery/Soc` wander separately, so they won't match each other.

In the van profile, `vancab/button/dump-valve` is **disabled on purpose**: it is a command that opens the dump valve for 45 s. The disabled Gen1 Shelly BLU topics are wildcards and can't be published.

## Route CSV

| Column | Meaning |
|---|---|
| `name` | label shown in the log |
| `lat`, `lon` | waypoint |
| `speed_kmh` | speed to the **next** waypoint |
| `park_min` | minutes parked at this waypoint before leaving (or after arriving, for the last one) |
| `altitude` | metres (optional) |

The simulator drives in straight lines between waypoints, with speed 0 while parked, and works out the course from the direction of travel. After the last waypoint it stays there (or starts again with `ROUTE_LOOP=yes`).

## Testing on another project

Make a new profile CSV (and route, if needed), put it in the data folder's `profiles/` (and `routes/`), and change `PROFILE` (and `MQTT_HOST` / login) in the container settings. No rebuild needed.

Running without Docker: `python mqtt_sim.py --help` (set `DATA_DIR` to any folder, or leave it; relative paths then resolve next to `mqtt_sim.py`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `can't open file '/app/mqtt_sim.py'` | A path is mapped to **`/app`**, hiding the program inside the image. Edit the container: the folder entry's **Container Path** must be **`/data`** (Host Path `/mnt/user/appdata/mqtt-sim/data`), nothing may map `/app`, and **Post Arguments** must be empty |
| `Not authorized` / `connection refused` in the log | Wrong broker user or password. Check the spelling of the user (`mqtt-sim`) and re-enter the password. The Mosquitto app log shows `Unauthorized login` for the attempt |
| `profile …: 0 topics` or file not found | `PROFILE` is relative to the data folder (then the built-in files): e.g. `profiles/my-project.csv` means `data/profiles/my-project.csv` |
| GPS rows skipped | Set `ROUTE` (e.g. `routes/demo-wollongong-kiama.csv`) to a file that exists; rows with mode `route` need it |
| Unraid can't pull the image | The repository **and** the `mqtt-sim` package on GitHub must be public (GitHub → Packages → mqtt-sim → Package settings) |
| Docker page still shows an old icon after changing Icon URL | Unraid keeps three cached copies. In the Unraid terminal: `rm -f /var/lib/docker/unraid/images/mqtt-sim-icon.png /boot/config/plugins/dockerMan/images/mqtt-sim-icon.png /usr/local/emhttp/state/plugins/dynamix.docker.manager/images/mqtt-sim-icon.png`, then Edit → **Apply** and refresh the page |

## Moving from the old setup (code in appdata)

Earlier versions ran the plain `python:3.12-alpine` image with the code copied into `/mnt/user/appdata/mqtt-sim` mapped to `/app`. To switch: Edit the container, set **Repository** to `ghcr.io/myharries/mqtt-sim:latest`, empty **Post Arguments**, change the folder entry to Container Path `/data` / Host Path `/mnt/user/appdata/mqtt-sim/data`, set the Icon URL, **Apply**. The old files in `appdata/mqtt-sim` (outside `data/`) are no longer used and can be deleted.
