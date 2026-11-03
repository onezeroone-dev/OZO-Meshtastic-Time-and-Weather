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
- `time_zone`: IANA zone for the displayed time.

## Run manually (venv)
    python3 -m venv venv && venv/bin/pip install -r requirements.txt
    venv/bin/python ozo_meshtastic_tw.py --config config.json --once

## Install as a service
    sudo ./install.sh
Edit `/etc/ozo-meshtastic-tw/config.json`, then `sudo systemctl start ozo-meshtastic-tw`.
Logs: `journalctl -u ozo-meshtastic-tw`. The unit file is `systemd/ozo-meshtastic-tw.service`.
