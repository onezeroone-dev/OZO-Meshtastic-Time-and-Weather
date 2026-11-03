# OZO Meshtastic Time and Weather

## Description
Service for Raspberry Pi OS (32-bit, Debian Trixie) with an SX1262 LoRa/GNSS HAT such as [this module expansion board for Raspberry Pi](https://www.amazon.com/dp/B0D4DPB68J) (non-sponsored). It reads the
current time from GPS (via `gpsd`), fetches local weather from the NOAA/NWS API
(`api.weather.gov`), and broadcasts both on the local Meshtastic mesh on a schedule.

**Compatibility.** The service works with any GNSS receiver that `gpsd` supports (it only talks to `gpsd` over TCP and never opens the GPS device) and any LoRa radio running Meshtastic firmware, either attached by serial/USB or exposed through [`meshtasticd`](https://meshtastic.org/docs/software/linux/). Plain serial LoRa modules without Meshtastic firmware are not supported.

**Region.** Weather and alerts come from the NOAA/NWS API, which covers the United States and its territories only. Elsewhere the weather fields are unavailable; the time, sun and moon fields still work.

**Tested hardware.** This project has been tested only with the SX1262 LoRa/GNSS HAT linked above, on a Raspberry Pi running 32-bit Raspberry Pi OS (Debian Trixie). Other hardware that meets the compatibility requirements should work but has not been verified; reports are welcome.

Contents: [Prerequisites](#prerequisites-and-hardware-setup) · [Configuration](#configuration) · [Example output](#example-output) · [Run manually](#run-manually-venv) · [Install](#install-as-a-service) · [Upgrading](#upgrading) · [Checking the radios](#checking-the-radios-check_radiospy) · [Device access](#device-access-and-locking) · [Troubleshooting](#logging-and-troubleshooting) · [Uninstall](#uninstall) · [Limitations](#limitations) · [Mesh etiquette](#mesh-etiquette-and-airtime) · [Security](#security-notes) · [Development](#development-notes) · [License](#license) · [Attribution](#attribution)

## Prerequisites and hardware setup
Complete these before running `install.sh`. Exact device names depend on your HAT and Pi model; adapt the examples.
1. **GPS serial port.** If the GNSS receiver is on the Pi's UART, enable the serial hardware port and disable the login shell on it (`sudo raspi-config` → Interface Options → Serial Port). Reboot afterwards.
2. **gpsd.** `install.sh` installs the package. Point gpsd at your GNSS device in `/etc/default/gpsd`, for example `DEVICES="/dev/ttyAMA0"` and `GPSD_OPTIONS="-n"` (`-n` starts polling without waiting for a client), then `sudo systemctl restart gpsd`. Confirm with `gpsmon` or `cgps`: you should see a 3D fix and a UTC time. Use a clear view of the sky; a first fix can take several minutes.
3. **LoRa radio.** How it attaches decides the connection type. A radio running Meshtastic firmware over USB serial works with `meshtastic.connection: serial`. An SPI HAT driven by the Pi itself needs [`meshtasticd`](https://meshtastic.org/docs/software/linux/) running, with `meshtastic.connection: tcp`.
4. **Meshtastic settings.** Set the radio's LoRa region (for example `meshtastic --set lora.region US`) and make sure the channel at `meshtastic.channel_index` is the one you want to broadcast on.
5. **Internet access** to `api.weather.gov`.
6. **Time zone and config.** Decide your `time_zone` and whether to set a fixed `location` (see Configuration).

## Configuration
Copy `config.example.json` to `config.json`:
- `meshtastic`: `connection` (`serial` or `tcp` for `meshtasticd`), `serial_port` (null = autodetect), `tcp_host`, `channel_index`, `max_message_bytes` (message is truncated to this many UTF-8 bytes; default 200).
- `gps`: gpsd host/port and fix timeout.
- `location`: optional fixed latitude/longitude; if null, the GPS position is used.
- `noaa`: `api_url` (base URL for the NOAA/NWS API), `user_agent` (NOAA asks for a contact string), and optional `api_key` (sent as `X-Api-Key`; the API does not currently require one).
- `message`: controls the broadcast content.
  - `fields`: ordered list of parts to include (remove or reorder to taste). Valid names: `time`, `time_sync` (shows "GPS time" when the time comes from the GPS), `conditions` (current forecast hour: sky, temperature, wind), `precip` (chance of precipitation), `high_low` (high and low from the daily forecast; after the day's high period has passed this may be tomorrow's), `observation` (latest station observation: temperature, humidity, wind, pressure in hPa with a rising/falling trend when available), `outlook` (next forecast period), `sun` (sunrise and sunset), `moon` (phase and percent illuminated), `next_broadcast` (local time of the next scheduled broadcast; omitted with `--once`).
  - `alerts`: `separate` (default; active NWS alerts are sent as their own second message), `inline` (alert text goes at the front of the main message), or `off`.
  - `compact`: `true` (default) uses abbreviations to save bytes; `false` uses full words.
  - `alert_filter`: limits which NWS alerts are broadcast. `min_severity` (`Minor`, `Moderate`, `Severe` or `Extreme`; alerts with an unknown severity are always kept), `include` (if non-empty, only these event names, e.g. `"Tornado Warning"`) and `exclude` (event names to drop, e.g. `"Special Weather Statement"`). Event names are matched case-insensitively.
  - `max_observation_age_minutes` (default 120; 0 disables): an observation older than this is omitted rather than shown as current. Hourly forecast periods that have already ended are skipped for the same reason.
  - Parts are added in the listed order as whole units; any part that would exceed `meshtastic.max_message_bytes` is skipped, so the message is never cut mid-field. Sun and moon values are calculated locally. Weather values are in US units (°F, mph). Each NWS lookup fails independently, so a partial outage still yields a partial message. Only the lookups needed by the chosen fields are made.
- `schedule`: `times` (list of local `HH:MM`) or, if empty, `interval_minutes`. `min_interval_seconds` (default 300) is a duplicate guard: the service will not broadcast again within this many seconds of the last successful broadcast, even if it is restarted (the timestamp is kept in the systemd state directory, `/var/lib/ozo-meshtastic-tw`, or in `state_file` if you set one).
- `quiet_hours`: optional, e.g. `{"start": "22:00", "end": "06:00", "mode": "skip"}` (local times; the window may span midnight). `mode` is `skip` (send nothing; no weather is fetched) or `alerts_only` (send only the alert message, if there are active alerts and `message.alerts` is not `off`). Applies to the running service and `--dry-run`, not `--once`. `null` disables it.
- `retry`: `attempts` (default 3) and `delay_seconds` (default 5) for GPS reads, NWS requests and radio sends. Only transient errors are retried (network errors, HTTP 5xx and 429, a busy radio); a failed send reconnects and resends only the messages not yet delivered.
- `time_zone`: IANA zone for the displayed time, e.g. `America/Denver` (see the [list of tz database time zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) for valid names).

## Example output
With the default fields and `compact: true`, a broadcast looks like this (illustrative values):

    2026-11-03 10:48 MST | GPS time | Pt Cldy 52F wind NW 5mph | PoP 10% | H58/L34 | obs 50F RH41% G12 1015hPa steady | Tonight: Mstly Clear | sun 06:30-16:56 | moon Last Qtr 32% | next 11:48

and, when there is an active alert and `alerts` is `separate`, a second message follows a few seconds later:

    ALERT: Winter Storm Warning, Wind Advisory

Legend (compact form): `Pt`/`Mstly`/`Cldy`/`Chc`/`Lkly`/`Sct`/`Iso` are Partly/Mostly/Cloudy/Chance/Likely/Scattered/Isolated; `wind NW 5mph` is the forecast wind; `PoP` is the chance of precipitation; `H`/`L` are the forecast high and low (°F); `obs` is the latest station observation: temperature, relative humidity (`RH`), gust speed (`G`, mph), pressure with a `rising`/`falling`/`steady` trend; `sun` is sunrise-sunset in local time; `moon` is the phase and percent illuminated; `next` is the next scheduled broadcast. Fields are separated by ` | `. With `compact: false` the same information is spelled out in words. Fields that do not fit in `max_message_bytes` are left out, so real messages are often shorter than this example; use `--dry-run` to see yours.

## Run manually (venv)

    python3 -m venv venv && venv/bin/pip install -r requirements.txt
    venv/bin/python ozo_meshtastic_tw.py --config config.json --once

Options (mutually exclusive):
- `--once`: broadcast once and exit. Ignores quiet hours and the duplicate guard.
- `--dry-run`: from an interactive terminal, ignore the schedule, build the message(s) now and print each with its byte count instead of transmitting. It never opens the LoRa radio. If gpsd is unreachable it falls back to the system clock and prints a prominent warning (the time-sync field then reads `SYSTEM CLOCK-NOT GPS`). Quiet hours and failed NWS lookups are reported. It refuses to run when not attached to a terminal.
- `--check`: post-install health check of the config, GPS, LoRa radio and NOAA reachability; exits non-zero on any failure. It briefly opens the radio, like `check_radios.py`.

## Install as a service
    sudo ./install.sh
The installer first runs `check_radios.py` as the service user and aborts (without installing the service) if gpsd cannot supply a GPS fix/time or the Meshtastic radio cannot be reached. It uses `/etc/ozo-meshtastic-tw/config.json` (created from the example if absent), so a non-default serial port or gpsd host must be set there first; re-run the installer after fixing any problem. You can run the check on its own with `venv/bin/python check_radios.py --config config.json`.
Edit `/etc/ozo-meshtastic-tw/config.json`, then `sudo systemctl start ozo-meshtastic-tw`.
Logs: `journalctl -u ozo-meshtastic-tw`. Each broadcast cycle ends with one summary line you can grep, e.g. `journalctl -u ozo-meshtastic-tw | grep 'broadcast summary'`: `status=ok|partial|skipped|failed` plus message count, byte sizes, time source and any failed NWS lookups (`partial` means some lookups failed). The unit file is `systemd/ozo-meshtastic-tw.service`.

## Upgrading
Pull or copy the new version of the repository and re-run `sudo ./install.sh`. It refreshes the code and Python dependencies in `/opt/ozo-meshtastic-tw` and keeps your existing `/etc/ozo-meshtastic-tw/config.json`, so compare it with the new `config.example.json` for added settings (missing optional keys use defaults). Then `sudo systemctl restart ozo-meshtastic-tw`. The installer runs the radio check again, so stop other radio clients first.

## Checking the radios (`check_radios.py`)
`check_radios.py` verifies that the two radios this service depends on are usable, using the same code paths and settings as the service. It transmits nothing.
- **GPS:** connects to gpsd (`gps.gpsd_host`/`gpsd_port`) and waits up to `gps.timeout_seconds` for a fix with a valid time.
- **LoRa:** opens the Meshtastic radio (serial or `meshtasticd` TCP, per `meshtastic.*`), reads the node's name, and closes it.

Run it from the repo directory, pointing at the config the service will use:

    venv/bin/python check_radios.py --config config.json

It prints `OK` lines on success. For each failure it prints a `FAIL` line with a plain-language hint (for example gpsd not running, no sky view, missing `dialout` group membership, wrong serial port) plus the underlying error, and exits with status 1. Both checks always run, so you see every problem in one pass.

When to use it:
- Before installing or after changing hardware, ports or `config.json`. `install.sh` runs it automatically and stops on failure.
- When broadcasts stop working, to separate a radio problem from a weather or config problem. For a broader check that also tests the config and NOAA, use `ozo_meshtastic_tw.py --check`.

Guidance:
- Stop other radio clients first (Meshtastic CLI or app, monitors); the serial port is locked while the check runs. See "Device access and locking".
- The GPS check needs a real fix. Indoors it may time out; test near a window or raise `gps.timeout_seconds`. `gpsmon` or `cgps` show gpsd's view independently.
- Run it as the service user (the installer does) so a permissions problem shows up here and not at the first broadcast: `sudo -u ozo-meshtastic /opt/ozo-meshtastic-tw/venv/bin/python /opt/ozo-meshtastic-tw/check_radios.py --config /etc/ozo-meshtastic-tw/config.json`.

## Device access and locking
- **GPS:** the service never opens the GPS device. It connects to `gpsd` over TCP (default port 2947) as a client, so other gpsd clients (`cgps`, `gpsmon`, etc.) can run at the same time.
- **LoRa (serial mode):** the Meshtastic library opens the serial port with an exclusive lock (`flock`). The service holds it only while broadcasting: it connects, sends one message, and disconnects. Between broadcasts (hourly by default) the port is free.
- **Conflicts:** if another program (Meshtastic CLI or app, a monitor, etc.) has the port open when a broadcast fires, the send fails with "Could not exclusively lock port". It is retried per the `retry` setting; if every attempt fails it is logged and the next scheduled broadcast tries again. Likewise, a tool you start during a broadcast will fail to open the port for a few seconds.
- **Always-on clients:** if something must keep the radio open continuously, run `meshtasticd` and set `meshtastic.connection` to `tcp` (with `tcp_host`) so multiple clients can share it.
- **Separate ports:** `gpsd` and the Meshtastic serial port must use different device nodes. If more than one Meshtastic device is attached, set `meshtastic.serial_port` explicitly rather than relying on autodetect.
- `install.sh` runs `check_radios.py` before installing the service, so it briefly opens the radio at that time; stop other radio clients before installing.

## Logging and troubleshooting
**How logging works.** The service writes plain-text lines to stderr, which systemd captures in the journal. Each line starts with its level (`INFO`, `WARNING` or `ERROR`). The level is part of the message text, not the journal priority, so `journalctl -p warning` will not filter them; use grep instead:

    journalctl -u ozo-meshtastic-tw -f                           # follow live
    journalctl -u ozo-meshtastic-tw --since today | grep -E 'WARNING|ERROR'
    journalctl -u ozo-meshtastic-tw | grep 'broadcast summary'   # one line per cycle

- `INFO`: normal activity: `next broadcast at ...`, `broadcast: <text>` for each message sent, and the `broadcast summary` line.
- `WARNING`: something went wrong but the service carried on: a retried or failed GPS/NWS/radio call, a missing NWS dataset, an omitted stale observation, GPS unavailable, or an unwritable state file.
- `ERROR`: `broadcast failed: ...`: the whole cycle failed. The service keeps running and tries again at the next scheduled time.
- Startup problems (bad config, unknown time zone) end the process with a Python traceback; systemd restarts it every 30 seconds, so the same error repeats in the journal until you fix it. Check `systemctl status ozo-meshtastic-tw`.

**Reading the summary line:** `status=ok` (all sent), `partial` (sent, but some NWS lookups failed; see `failed=`), `skipped` (`reason=duplicate-guard`, `quiet-hours-skip`, `quiet-hours-alerts_only` or `nothing-to-send`; expected, not a fault), `failed` (nothing sent; see `error=`).

**First steps for any problem**
1. `sudo systemctl status ozo-meshtastic-tw` and the recent journal.
2. Stop the service (`sudo systemctl stop ozo-meshtastic-tw`) and run the checks as the service user: `check_radios.py` (GPS and LoRa) and `ozo_meshtastic_tw.py --check` (adds config and NOAA). Both print hints.
3. Run `ozo_meshtastic_tw.py --dry-run` from a terminal to see exactly what would be sent and which lookups fail. It does not use the radio.

**Common symptoms**

| Log message or symptom | Likely cause and fix |
|---|---|
| `GPS unavailable (...); using SYSTEM CLOCK` (time field reads `SYSTEM CLOCK-NOT GPS`) | gpsd is stopped or has no fix. Check `systemctl status gpsd`, `gpsmon`/`cgps`, antenna view, `gps.*` settings. |
| `radio send failed (attempt n/m)` with "Could not exclusively lock port" | Another program has the serial port. Close it, or use `meshtasticd` with `connection: tcp`. See "Device access and locking". |
| `radio send failed` with "Permission denied" | The service user is not in `dialout`, or the wrong `serial_port` is set. |
| `... lookup failed: ...` (alerts, hourly, daily, obs) | NWS unreachable or erroring. Check internet access, `noaa.api_url` and `noaa.user_agent`. HTTP 4xx errors (other than 429) are not retried; NWS is occasionally down, and the next cycle usually recovers. Fields for failed lookups are left out (`status=partial`). |
| `observation is N min old (limit M); omitting it` | The nearest station has stopped reporting. Raise `message.max_observation_age_minutes` or drop the `observation` field. |
| `weather lookup failed: no location available` | No `location` set and no GPS position. Set `location.latitude`/`longitude` or fix the GPS. |
| `could not write state file ...` | The state directory is not writable. The duplicate guard is then ineffective. Check `/var/lib/ozo-meshtastic-tw` ownership, or set `state_file`. |
| Nothing is sent, `status=skipped` | Expected if inside `quiet_hours`, within `schedule.min_interval_seconds` of the last send, or the message has no content. |
| Message missing fields | Fields that would exceed `meshtastic.max_message_bytes` are skipped whole; use `--dry-run` to see byte counts, shorten the field list, or enable `compact`. |
| No log lines at all | Service not running. Check `systemctl status`, and `systemctl is-enabled ozo-meshtastic-tw`. |

After changing `config.json`, restart with `sudo systemctl restart ozo-meshtastic-tw`.

## Uninstall
    sudo ./uninstall.sh [--purge]
Stops and removes the service, `/opt/ozo-meshtastic-tw`, and the `ozo-meshtastic` user. Your `/etc/ozo-meshtastic-tw/config.json` is kept unless you pass `--purge`. The apt packages installed by `install.sh` (`python3`, `python3-venv`, `gpsd`) are left in place.

## Limitations
- **US coverage only.** The NOAA/NWS API serves the United States and its territories; elsewhere the weather lookups fail (the time, sun and moon fields still work).
- **Message size.** Messages are limited to `meshtastic.max_message_bytes` (default 200); fields that do not fit are dropped whole, and a single alert message longer than the limit is truncated.
- **Forecast, not measurement.** Conditions come from NWS gridded forecasts, and the observation is from the nearest reporting station, which may be some distance away.
- **System clock.** The schedule and the duplicate guard use the Pi's system clock (quiet hours use the GPS time when available). A Pi without a battery-backed RTC that boots offline may have a wrong system clock until it syncs.
- **Sun and moon** are calculated locally with standard approximations: sunrise and sunset are accurate to within about a couple of minutes, moon phase to the nearest eighth of the cycle.
- **Fields are US units** (°F, mph, hPa).
- **Tested on one hardware setup.** See "Tested hardware" above. On different hardware, run `check_radios.py`, `--check` and `--dry-run` before relying on it.

## Mesh etiquette and airtime
LoRa airtime is shared by every node on the channel, and every broadcast is heard by all of them. Use a sensible interval (the default is hourly; avoid going much faster), consider a dedicated channel or a long interval on busy meshes, and keep `fields` lean. Each message is sent once with a short pause between messages. Use `quiet_hours` to stay silent overnight, and follow the norms of your local mesh community.

## Security notes
- `/etc/ozo-meshtastic-tw/config.json` is installed root-owned with mode `0640`, readable by the service group only. It may contain the optional NOAA `api_key`.
- The service runs as the unprivileged `ozo-meshtastic` user (in `dialout` only for serial access), with `NoNewPrivileges`, `ProtectSystem=full` and `ProtectHome` set in the unit file. It only makes outbound HTTPS requests to NOAA and a local connection to gpsd (and `meshtasticd` in TCP mode); it listens on no ports.
- Messages contain time, weather, sun and moon data only; the service never sends your coordinates over the mesh. Coordinates are sent to `api.weather.gov` to look up the forecast. Note that the Meshtastic radio itself may broadcast its position according to its own settings, independent of this service.

## Development notes
Development happens on Windows (the repo root doubles as a venv) while the target is Raspberry Pi OS, so Pi-only pieces (`gpsd`, serial radio, systemd, the shell scripts) cannot be exercised there. The project has no automated test suite; changes were checked with Pylance diagnostics and ad-hoc scripts that mock the NWS, gpsd and Meshtastic interfaces. `ozo_meshtastic_tw.py` imports cleanly without hardware, so `--dry-run` (which falls back to the system clock when gpsd is unreachable) is a quick way to exercise the message builder anywhere. Windows has no time zone database, so `pip install -r requirements.txt` (which includes `tzdata`) is required. Shell scripts must keep LF line endings.

## License
This project is licensed under the GNU General Public License, version 2. See the [LICENSE](LICENSE) file for the full text. It is distributed in the hope that it will be useful, but without any warranty. Weather and alert information is relayed from NOAA/NWS and must not be relied on for safety-critical decisions.

## Attribution
- Weather data: [NOAA National Weather Service API](https://www.weather.gov/documentation/services-web-api) (`api.weather.gov`).
- Built on [Meshtastic](https://meshtastic.org/) (Python library) and [gpsd](https://gpsd.io/).
- Sunrise/sunset and moon phase are computed locally with standard astronomical approximations; sunrise/sunset results were cross-checked against the [astral](https://github.com/sffjunkie/astral) library during development.
- Development assistance: much of the design, code, documentation and testing in this project (including the multi-field message builder, retry and reliability features, `--dry-run` and `--check` modes, `check_radios.py`, the install and uninstall scripts, and this documentation) was developed collaboratively with an AI assistant using Copilot SDK in VS Code. All changes were reviewed by the project maintainer, who directed the work and remains responsible for it.