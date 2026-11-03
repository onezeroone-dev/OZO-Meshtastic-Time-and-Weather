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
- `schedule`: `times` (list of local `HH:MM`) or, if empty, `interval_minutes`.
- `time_zone`: IANA zone for the displayed time, e.g. `America/Denver` (see the [list of tz database time zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) for valid names).

## Run manually (venv)
    python3 -m venv venv && venv/bin/pip install -r requirements.txt
    venv/bin/python ozo_meshtastic_tw.py --config config.json --once

## Install as a service
    sudo ./install.sh
The installer first runs `check_radios.py` as the service user and aborts (without installing the service) if gpsd cannot supply a GPS fix/time or the Meshtastic radio cannot be reached. It uses `/etc/ozo-meshtastic-tw/config.json` (created from the example if absent), so a non-default serial port or gpsd host must be set there first; re-run the installer after fixing any problem. You can run the check on its own with `venv/bin/python check_radios.py --config config.json`.
Edit `/etc/ozo-meshtastic-tw/config.json`, then `sudo systemctl start ozo-meshtastic-tw`.
Logs: `journalctl -u ozo-meshtastic-tw`. The unit file is `systemd/ozo-meshtastic-tw.service`.

## Device access and locking
- **GPS:** the service never opens the GPS device. It connects to `gpsd` over TCP (default port 2947) as a client, so other gpsd clients (`cgps`, `gpsmon`, etc.) can run at the same time.
- **LoRa (serial mode):** the Meshtastic library opens the serial port with an exclusive lock (`flock`). The service holds it only while broadcasting: it connects, sends one message, and disconnects. Between broadcasts (hourly by default) the port is free.
- **Conflicts:** if another program (Meshtastic CLI or app, a monitor, etc.) has the port open when a broadcast fires, that broadcast fails with "Could not exclusively lock port". It is logged and retried at the next scheduled time. Likewise, a tool you start during a broadcast will fail to open the port for a few seconds.
- **Always-on clients:** if something must keep the radio open continuously, run `meshtasticd` and set `meshtastic.connection` to `tcp` (with `tcp_host`) so multiple clients can share it.
- **Separate ports:** `gpsd` and the Meshtastic serial port must use different device nodes. If more than one Meshtastic device is attached, set `meshtastic.serial_port` explicitly rather than relying on autodetect.
- `install.sh` runs `check_radios.py` before installing the service, so it briefly opens the radio at that time; stop other radio clients before installing.

## Uninstall
    sudo ./uninstall.sh [--purge]
Stops and removes the service, `/opt/ozo-meshtastic-tw`, and the `ozo-meshtastic` user. Your `/etc/ozo-meshtastic-tw/config.json` is kept unless you pass `--purge`. The apt packages installed by `install.sh` (`python3`, `python3-venv`, `gpsd`) are left in place.
