# OZO-Meshtastic-Time-and-Weather

Service for Raspberry Pi OS (32-bit, Debian Trixie) with an SX1262 LoRa/GNSS HAT. It reads the
current time from GPS (via `gpsd`), fetches local weather from the NOAA/NWS API
(`api.weather.gov`), and broadcasts both on the local Meshtastic mesh on a schedule.

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

## Device access and locking
- **GPS:** the service never opens the GPS device. It connects to `gpsd` over TCP (default port 2947) as a client, so other gpsd clients (`cgps`, `gpsmon`, etc.) can run at the same time.
- **LoRa (serial mode):** the Meshtastic library opens the serial port with an exclusive lock (`flock`). The service holds it only while broadcasting: it connects, sends one message, and disconnects. Between broadcasts (hourly by default) the port is free.
- **Conflicts:** if another program (Meshtastic CLI or app, a monitor, etc.) has the port open when a broadcast fires, the send fails with "Could not exclusively lock port". It is retried per the `retry` setting; if every attempt fails it is logged and the next scheduled broadcast tries again. Likewise, a tool you start during a broadcast will fail to open the port for a few seconds.
- **Always-on clients:** if something must keep the radio open continuously, run `meshtasticd` and set `meshtastic.connection` to `tcp` (with `tcp_host`) so multiple clients can share it.
- **Separate ports:** `gpsd` and the Meshtastic serial port must use different device nodes. If more than one Meshtastic device is attached, set `meshtastic.serial_port` explicitly rather than relying on autodetect.
- `install.sh` runs `check_radios.py` before installing the service, so it briefly opens the radio at that time; stop other radio clients before installing.

## Uninstall
    sudo ./uninstall.sh [--purge]
Stops and removes the service, `/opt/ozo-meshtastic-tw`, and the `ozo-meshtastic` user. Your `/etc/ozo-meshtastic-tw/config.json` is kept unless you pass `--purge`. The apt packages installed by `install.sh` (`python3`, `python3-venv`, `gpsd`) are left in place.
