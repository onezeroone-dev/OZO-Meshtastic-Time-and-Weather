#!/usr/bin/env python3
"""Broadcast GPS time and NOAA weather to a local Meshtastic mesh on a schedule."""
import argparse
import json
import logging
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

NOAA_API = "https://api.weather.gov"
MAX_MESSAGE_BYTES = 200
log = logging.getLogger("ozo-meshtastic-tw")


def load_config(path):
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    sched = cfg.get("schedule", {})
    if not sched.get("times") and not sched.get("interval_minutes"):
        raise ValueError("schedule needs 'interval_minutes' or 'times'")
    return cfg


def read_gpsd(cfg):
    """Return the first TPV report from gpsd that contains a time."""
    host = cfg.get("gpsd_host", "127.0.0.1")
    port = cfg.get("gpsd_port", 2947)
    timeout = cfg.get("timeout_seconds", 15)
    deadline = time.monotonic() + timeout
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
        buf = b""
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("class") == "TPV" and msg.get("time") and msg.get("mode", 0) >= 2:
                    return msg
    raise RuntimeError("no GPS fix/time received from gpsd")


def get_weather(lat, lon, noaa):
    headers = {"User-Agent": noaa.get("user_agent", "OZO-Meshtastic-Time-and-Weather"),
               "Accept": "application/geo+json"}
    if noaa.get("api_key"):
        headers["X-Api-Key"] = noaa["api_key"]
    timeout = noaa.get("timeout_seconds", 20)
    point = requests.get(f"{NOAA_API}/points/{lat:.4f},{lon:.4f}", headers=headers, timeout=timeout)
    point.raise_for_status()
    url = point.json()["properties"]["forecastHourly"]
    fc = requests.get(url, headers=headers, timeout=timeout)
    fc.raise_for_status()
    return fc.json()["properties"]["periods"][0]


def build_message(now, weather, tz):
    local = now.astimezone(tz)
    msg = f"{local:%Y-%m-%d %H:%M} {local.tzname()}"
    if weather:
        msg += (f" | {weather['shortForecast']}, {weather['temperature']}"
                f"{weather['temperatureUnit']}, wind {weather['windSpeed']} {weather['windDirection']}")
    else:
        msg += " | weather unavailable"
    return msg.encode("utf-8")[:MAX_MESSAGE_BYTES].decode("utf-8", "ignore")


def next_run(sched, now, tz):
    """Next scheduled time (aware datetime) after now."""
    times = sched.get("times") or []
    if times:
        local = now.astimezone(tz)
        candidates = []
        for t in times:
            hh, mm = (int(x) for x in t.split(":"))
            for day in (0, 1):
                c = (local + timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                if c > local:
                    candidates.append(c)
        return min(candidates)
    return now + timedelta(minutes=sched["interval_minutes"])


def open_interface(mcfg):
    import meshtastic.serial_interface
    import meshtastic.tcp_interface
    if mcfg.get("connection", "serial") == "tcp":
        return meshtastic.tcp_interface.TCPInterface(hostname=mcfg.get("tcp_host", "localhost"))
    return meshtastic.serial_interface.SerialInterface(devPath=mcfg.get("serial_port"))


def broadcast_once(cfg, tz):
    fix = read_gpsd(cfg.get("gps", {}))
    now = datetime.fromisoformat(fix["time"].replace("Z", "+00:00"))
    loc = cfg.get("location", {})
    lat = loc.get("latitude") if loc.get("latitude") is not None else fix.get("lat")
    lon = loc.get("longitude") if loc.get("longitude") is not None else fix.get("lon")
    weather = None
    try:
        if lat is None or lon is None:
            raise RuntimeError("no location available")
        weather = get_weather(lat, lon, cfg.get("noaa", {}))
    except Exception as exc:
        log.warning("weather lookup failed: %s", exc)
    text = build_message(now, weather, tz)
    iface = open_interface(cfg.get("meshtastic", {}))
    try:
        iface.sendText(text, channelIndex=cfg.get("meshtastic", {}).get("channel_index", 0))
    finally:
        iface.close()
    log.info("broadcast: %s", text)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true", help="broadcast once and exit")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config(args.config)
    tz = ZoneInfo(cfg.get("time_zone", "UTC"))
    if args.once:
        broadcast_once(cfg, tz)
        return 0
    while True:
        now = datetime.now(timezone.utc)
        target = next_run(cfg["schedule"], now, tz)
        log.info("next broadcast at %s", target.isoformat())
        while (remaining := (target - datetime.now(timezone.utc)).total_seconds()) > 0:
            time.sleep(min(remaining, 30))
        try:
            broadcast_once(cfg, tz)
        except Exception as exc:
            log.error("broadcast failed: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
