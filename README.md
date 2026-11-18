<img src="icon.png" width="128" align="right" alt="MQTT Simulator icon">

# mqtt-sim

A small MQTT simulator for testing. It reads a **profile** CSV (one row per topic, saying how the value is made and how often) and, optionally, a **route** CSV of GPS waypoints, then publishes to any MQTT broker.

Made for the Van Home Assistant (stand-in for the Victron Cerbo and Node-RED), but any project can use it with its own profile.

It has a **web page** (port 8099) where you pick a profile and route, upload your own CSVs from your PC, start and stop, and watch every value as it is published. The CSVs in this repo are **examples** only.

## Web page

Open `http://<unraid-ip>:8099` (on Unraid: Docker → mqtt-sim icon → **WebUI**).

- **Status**: running or stopped, broker connection, **Start / Stop / Restart**. Stopping removes the retained test values from the broker.
- **Profile** and **GPS route**: choose from **My files** (your uploads) or **Examples** (built in). **Use a CSV from this PC…** uploads a file into the data folder and switches to it straight away. **Download** gets the current file (start from an example, edit it in Excel, upload it back). **Delete** removes one of your own files.
- **Speed-up** and **Repeat route** for the GPS drive.
- **Topic variables**: every `{placeholder}` the profile uses (e.g. `{portal}` for the Victron portal ID), each with a box. Values are saved per profile. Topics whose placeholders are still empty are not published, and the page says so.
- **Live values**: every topic with its last value and age, with a filter box.
- **Log**: the last few hundred log lines.

Your choices are remembered in `data/mqtt-sim.json`, so the simulator comes back the same way after a restart or an update (including staying stopped if you stopped it). `PROFILE` and `ROUTE` in the container settings are only used the very first time.

The page has no login by default (it is meant for your own network). Set **Web page password** (`WEB_PASSWORD`) to ask for a password; any user name works.

## How it is packaged

Every push to `main` builds a Docker image with GitHub Actions and publishes it as **`ghcr.io/myharries/mqtt-sim:latest`** (amd64 and arm64). An example profile (Victron van) and an example route (Wollongong → Kiama) are built into the image. Your own profiles and routes go in the **data folder** (`/data`), which updates never touch.

```
mqtt-sim/
  mqtt_sim.py                 the simulator and its web server
  webui.html                  the web page
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
  profiles/   your own profile CSVs (uploaded on the web page, or copied here)
  routes/     your own route CSVs
  mqtt-sim.json  what the web page last chose (profile, route, speed-up, running)
  examples/   copies of the built-in profiles and routes, refreshed on every start
```

To customise an example: on the web page choose it, **Download**, edit, and upload it back (it becomes one of My files). Or copy it from `examples/` into `profiles/` / `routes/` on the share.

## Before you start

The simulator logs in to the broker like any MQTT client. With the Home Assistant Mosquitto app, logins are Home Assistant users:

1. In HA: **Settings → People → Users → Add user**, name `mqtt-sim`, a password, **Can only log in from the local network** on, **Administrator** off.
2. Use that name and password as the container's **Broker user** / **Broker password**.

## Install on Unraid

1. Copy `unraid/my-mqtt-sim.xml` to the flash drive: `\\<unraid>\flash\config\plugins\dockerMan\templates-user\`.
2. Unraid → **Docker** → **Add Container** → Template: **mqtt-sim**.
3. Check **Web page port** (8099) and enter the **Broker password** for the `mqtt-sim` HA user, check the other fields, **Apply**.

Without the template, Add Container by hand: Repository `ghcr.io/myharries/mqtt-sim:latest`, Path `/data` → `/mnt/user/appdata/mqtt-sim/data`, Icon URL `https://raw.githubusercontent.com/myharries/mqtt-sim/main/icon.png?v=2`, and the variables below.

## Checking it works

- Open the web page (`http://<unraid-ip>:8099`): **Running**, broker **connected**, and the Live values table filling up.
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
| `PROFILE` | profiles/van-victron.csv | Profile for the very first start (then chosen on the web page) |
| `DATA_DIR` | /data | Folder for your own profiles and routes |
| `ROUTE` | (none) | Route for the very first start; without one, `route` rows are skipped |
| `WEB_PORT` | 8099 | Web page port (0 = no web page; the simulator then just runs the `PROFILE`) |
| `WEB_PASSWORD` | | Optional password for the web page |
| `SPEEDUP` | 1 | Route time multiplier (10 = ten times faster) |
| `ROUTE_LOOP` | no | Start the route again when it ends |
| `PORTAL` | | Starting value for `{portal}` (Victron portal ID, VRM → Settings → General); can be changed on the web page |
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
| `topic` | e.g. `victron/info/battery_soc`, `N/{portal}/system/0/Dc/Battery/Soc`. Any `{name}` is a placeholder filled in under **Topic variables** on the web page |
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

Make a new profile CSV (and route, if needed) and upload it on the web page with **Use a CSV from this PC…**. Point the container's broker settings (`MQTT_HOST`, login) at that project's broker. No rebuild needed. Add a second container (different name, port and data folder) to test two projects at once.

Running without Docker: `python mqtt_sim.py --help` (set `DATA_DIR` to any folder, or leave it; relative paths then resolve next to `mqtt_sim.py`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `can't open file '/app/mqtt_sim.py'` | A path is mapped to **`/app`**, hiding the program inside the image. Edit the container: the folder entry's **Container Path** must be **`/data`** (Host Path `/mnt/user/appdata/mqtt-sim/data`), nothing may map `/app`, and **Post Arguments** must be empty |
| `Not authorized` / `connection refused` in the log | Wrong broker user or password. Check the spelling of the user (`mqtt-sim`) and re-enter the password. The Mosquitto app log shows `Unauthorized login` for the attempt |
| Upload refused | A profile needs a `topic` column in its first row; a route needs `lat` and `lon` |
| Web page doesn't open | Check the container's **Web page port** mapping (8099) and that `WEB_PORT` isn't 0 |
| GPS rows skipped | Set `ROUTE` (e.g. `routes/demo-wollongong-kiama.csv`) to a file that exists; rows with mode `route` need it |
| Unraid can't pull the image | The repository **and** the `mqtt-sim` package on GitHub must be public (GitHub → Packages → mqtt-sim → Package settings) |
| Docker page still shows an old icon after changing Icon URL | Unraid keeps three cached copies. In the Unraid terminal: `rm -f /var/lib/docker/unraid/images/mqtt-sim-icon.png /boot/config/plugins/dockerMan/images/mqtt-sim-icon.png /usr/local/emhttp/state/plugins/dynamix.docker.manager/images/mqtt-sim-icon.png`, then Edit → **Apply** and refresh the page |

## Moving from the old setup (code in appdata)

Earlier versions ran the plain `python:3.12-alpine` image with the code copied into `/mnt/user/appdata/mqtt-sim` mapped to `/app`. To switch: Edit the container, set **Repository** to `ghcr.io/myharries/mqtt-sim:latest`, empty **Post Arguments**, change the folder entry to Container Path `/data` / Host Path `/mnt/user/appdata/mqtt-sim/data`, set the Icon URL, **Apply**. The old files in `appdata/mqtt-sim` (outside `data/`) are no longer used and can be deleted.
