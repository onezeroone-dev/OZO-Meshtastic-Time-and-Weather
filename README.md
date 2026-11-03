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
