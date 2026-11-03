#!/usr/bin/env python3
"""Broadcast GPS time and NOAA weather to a local Meshtastic mesh on a schedule."""
import argparse
import json
import logging
import math
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("ozo-meshtastic-tw")


def load_config(path):
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    sched = cfg.get("schedule", {})
    if not sched.get("times") and not sched.get("interval_minutes"):
        raise ValueError("schedule needs 'interval_minutes' or 'times'")
    msg = cfg.get("message", {})
    unknown = [f for f in msg.get("fields", []) if f not in FIELD_DATA]
    if unknown:
        raise ValueError(f"unknown message field(s) {unknown}; valid: {list(FIELD_DATA)}")
    if msg.get("alerts", "separate") not in ALERT_MODES:
        raise ValueError(f"message.alerts must be one of {list(ALERT_MODES)}")
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


# Message fields in default priority order. The value names the NWS data a field needs (None = none).
FIELD_DATA = {
    "time": None,
    "time_sync": None,
    "conditions": "hourly",
    "precip": "hourly",
    "high_low": "daily",
    "observation": "obs",
    "outlook": "daily",
    "sun": None,
    "moon": None,
    "next_broadcast": None,
}
DEFAULT_FIELDS = list(FIELD_DATA)
ALERT_MODES = ("separate", "inline", "off")
SEP = " | "
SYNODIC_DAYS = 29.530588853
MOON_REF = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
MOON_PHASES = [
    ("New", "new moon"), ("Wax Cres", "waxing crescent"), ("1st Qtr", "first quarter"),
    ("Wax Gib", "waxing gibbous"), ("Full", "full moon"), ("Wan Gib", "waning gibbous"),
    ("Last Qtr", "last quarter"), ("Wan Cres", "waning crescent"),
]
ABBREVIATIONS = [(re.compile(rf"\b{word}\b"), short) for word, short in (
    ("Thunderstorms?", "T-storms"), ("Partly", "Pt"), ("Mostly", "Mstly"), ("Cloudy", "Cldy"),
    ("Showers", "Shwrs"), ("Slight Chance", "Slt Chc"), ("Chance", "Chc"), ("Likely", "Lkly"),
    ("Scattered", "Sct"), ("Isolated", "Iso"))]


def required_data(msg_cfg):
    """NWS datasets needed by the configured fields and alert mode."""
    fields = msg_cfg.get("fields", DEFAULT_FIELDS)
    need = {FIELD_DATA[f] for f in fields if FIELD_DATA[f]}
    if msg_cfg.get("alerts", "separate") != "off":
        need.add("alerts")
    return need


def _nws_get(url, headers, timeout):
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _value(props, key):
    return (props.get(key) or {}).get("value")


def _parse_ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _summarize_observation(features):
    latest = features[0]["properties"]
    obs = {"temp_c": _value(latest, "temperature"), "humidity": _value(latest, "relativeHumidity"),
           "gust_kmh": _value(latest, "windGust"), "pressure_hpa": None, "trend": None}
    pressure = _value(latest, "barometricPressure")
    if pressure is not None:
        obs["pressure_hpa"] = pressure / 100
        for old in features[:0:-1]:  # oldest first
            old_pressure = _value(old["properties"], "barometricPressure")
            if old_pressure is None:
                continue
            hours = (_parse_ts(latest["timestamp"]) - _parse_ts(old["properties"]["timestamp"])).total_seconds() / 3600
            if hours >= 2:
                diff = (pressure - old_pressure) / 100
                obs["trend"] = "rising" if diff >= 1 else "falling" if diff <= -1 else "steady"
            break
    return obs


def get_observation(stations_url, headers, timeout):
    """Latest observation from the nearest station that reports a temperature."""
    for station in _nws_get(stations_url, headers, timeout).get("features", [])[:3]:
        features = _nws_get(f"{station['id']}/observations?limit=4", headers, timeout).get("features", [])
        if features and _value(features[0]["properties"], "temperature") is not None:
            return _summarize_observation(features)
    return None


def get_weather(lat, lon, noaa, need):
    """Fetch the NWS datasets named in `need`; each one fails independently."""
    headers = {"User-Agent": noaa.get("user_agent", "OZO-Meshtastic-Time-and-Weather"),
               "Accept": "application/geo+json"}
    if noaa.get("api_key"):
        headers["X-Api-Key"] = noaa["api_key"]
    timeout = noaa.get("timeout_seconds", 20)
    api_url = noaa.get("api_url", "https://api.weather.gov").rstrip("/")
    weather = {}
    if "alerts" in need:
        try:
            data = _nws_get(f"{api_url}/alerts/active?point={lat:.4f},{lon:.4f}", headers, timeout)
            weather["alerts"] = [f["properties"]["event"] for f in data.get("features", [])]
        except Exception as exc:
            log.warning("alerts lookup failed: %s", exc)
    if need & {"hourly", "daily", "obs"}:
        try:
            point = _nws_get(f"{api_url}/points/{lat:.4f},{lon:.4f}", headers, timeout)["properties"]
        except Exception as exc:
            log.warning("NWS point lookup failed: %s", exc)
            return weather
        for key, link in (("hourly", "forecastHourly"), ("daily", "forecast")):
            if key in need:
                try:
                    weather[key] = _nws_get(point[link], headers, timeout)["properties"]["periods"]
                except Exception as exc:
                    log.warning("%s forecast lookup failed: %s", key, exc)
        if "obs" in need:
            try:
                weather["obs"] = get_observation(point["observationStations"], headers, timeout)
            except Exception as exc:
                log.warning("observation lookup failed: %s", exc)
    return weather


def _sun_event(day, lat, lon, rising, tz):
    """Sunrise/sunset as an aware datetime on local date `day`, or 'up'/'down' if the sun never sets/rises."""
    n = day.timetuple().tm_yday
    lng_hour = lon / 15
    t = n + ((6 if rising else 18) - lng_hour) / 24
    mean_anomaly = 0.9856 * t - 3.289
    m = math.radians(mean_anomaly)
    true_lon = (mean_anomaly + 1.916 * math.sin(m) + 0.020 * math.sin(2 * m) + 282.634) % 360
    lam = math.radians(true_lon)
    ra = math.degrees(math.atan(0.91764 * math.tan(lam))) % 360
    ra += math.floor(true_lon / 90) * 90 - math.floor(ra / 90) * 90
    sin_dec = 0.39782 * math.sin(lam)
    cos_dec = math.cos(math.asin(sin_dec))
    phi = math.radians(lat)
    cos_h = (math.cos(math.radians(90.833)) - sin_dec * math.sin(phi)) / (cos_dec * math.cos(phi))
    if cos_h > 1:
        return "down"
    if cos_h < -1:
        return "up"
    hour_angle = math.degrees(math.acos(cos_h))
    if rising:
        hour_angle = 360 - hour_angle
    local_mean = hour_angle / 15 + ra / 15 - 0.06571 * t - 6.622
    utc = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=(local_mean - lng_hour) % 24)
    return utc - timedelta(days=(utc.astimezone(tz).date() - day).days)


def moon_phase(now):
    """Return (phase index into MOON_PHASES, percent illuminated)."""
    age = ((now - MOON_REF).total_seconds() / 86400) % SYNODIC_DAYS
    illumination = (1 - math.cos(2 * math.pi * age / SYNODIC_DAYS)) / 2 * 100
    return int((age + SYNODIC_DAYS / 16) / (SYNODIC_DAYS / 8)) % 8, round(illumination)


def _abbr(text, compact):
    if compact:
        for pattern, short in ABBREVIATIONS:
            text = pattern.sub(short, text)
    return text


def _first_period(weather, key):
    return (weather.get(key) or [None])[0]


def _f_time(c):
    local = c["now"].astimezone(c["tz"])
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()}"


def _f_time_sync(c):
    return c.get("time_source", "GPS time")


def _f_conditions(c):
    p = _first_period(c["weather"], "hourly")
    if not p:
        return None
    if c["compact"]:
        wind = f"{p['windDirection']} {p['windSpeed']}".replace(" mph", "mph")
        return f"{_abbr(p['shortForecast'], True)} {p['temperature']}{p['temperatureUnit']} wind {wind}"
    return (f"{p['shortForecast']}, {p['temperature']}{p['temperatureUnit']}, "
            f"wind {p['windSpeed']} {p['windDirection']}")


def _f_precip(c):
    p = _first_period(c["weather"], "hourly")
    chance = _value(p, "probabilityOfPrecipitation") if p else None
    if chance is None:
        return None
    return f"PoP {chance}%" if c["compact"] else f"{chance}% chance of precipitation"


def _f_high_low(c):
    periods = c["weather"].get("daily") or []
    high = next((p for p in periods if p.get("isDaytime")), None)
    low = next((p for p in periods if not p.get("isDaytime")), None)
    if c["compact"]:
        return "/".join(x for x in (high and f"H{high['temperature']}", low and f"L{low['temperature']}") if x) or None
    return ", ".join(x for x in (high and f"high {high['temperature']}{high['temperatureUnit']}",
                                 low and f"low {low['temperature']}{low['temperatureUnit']}") if x) or None


def _f_observation(c):
    o = c["weather"].get("obs")
    if not o:
        return None
    temp = f"{round(o['temp_c'] * 9 / 5 + 32)}F"
    gust = round(o["gust_kmh"] / 1.609344) if o["gust_kmh"] is not None else None
    pressure = f"{round(o['pressure_hpa'])}hPa" if o["pressure_hpa"] is not None else None
    if pressure and o["trend"]:
        pressure += f" {o['trend']}"
    if c["compact"]:
        bits = [temp, o["humidity"] is not None and f"RH{round(o['humidity'])}%",
                gust is not None and f"G{gust}", pressure]
        return "obs " + " ".join(b for b in bits if b)
    bits = [temp, o["humidity"] is not None and f"{round(o['humidity'])}% humidity",
            gust is not None and f"gusts {gust} mph", pressure]
    return "observed: " + ", ".join(b for b in bits if b)


def _f_outlook(c):
    periods = c["weather"].get("daily") or []
    if len(periods) < 2:
        return None
    return f"{periods[1]['name']}: {_abbr(periods[1]['shortForecast'], c['compact'])}"


def _f_sun(c):
    if c["lat"] is None or c["lon"] is None:
        return None
    tz = c["tz"]
    day = c["now"].astimezone(tz).date()
    rise = _sun_event(day, c["lat"], c["lon"], True, tz)
    sset = _sun_event(day, c["lat"], c["lon"], False, tz)
    state = next((x for x in (rise, sset) if isinstance(x, str)), None)
    if state:
        return "sun up all day" if state == "up" else "sun down all day"
    rise, sset = rise.astimezone(tz), sset.astimezone(tz)
    if c["compact"]:
        return f"sun {rise:%H:%M}-{sset:%H:%M}"
    return f"sunrise {rise:%H:%M}, sunset {sset:%H:%M}"


def _f_moon(c):
    index, percent = moon_phase(c["now"])
    short, full = MOON_PHASES[index]
    return f"moon {short} {percent}%" if c["compact"] else f"moon: {full} ({percent}% illuminated)"


def _f_next_broadcast(c):
    if c["next_time"] is None:
        return None
    tz = c["tz"]
    nxt = c["next_time"].astimezone(tz)
    stamp = f"{nxt:%H:%M}" if nxt.date() == c["now"].astimezone(tz).date() else f"{nxt:%m-%d %H:%M}"
    return f"next {stamp}" if c["compact"] else f"next broadcast {stamp}"


FIELD_RENDERERS = {
    "time": _f_time, "time_sync": _f_time_sync, "conditions": _f_conditions, "precip": _f_precip,
    "high_low": _f_high_low, "observation": _f_observation, "outlook": _f_outlook,
    "sun": _f_sun, "moon": _f_moon, "next_broadcast": _f_next_broadcast,
}


def _truncate(text, max_bytes):
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def _fit(parts, max_bytes):
    """Join whole parts in priority order, skipping any that would exceed the byte limit."""
    out = ""
    for part in parts:
        candidate = f"{out}{SEP}{part}" if out else part
        if len(candidate.encode("utf-8")) <= max_bytes:
            out = candidate
        elif not out:
            out = _truncate(part, max_bytes)
    return out


def _alert_text(weather, compact):
    events = list(dict.fromkeys(weather.get("alerts") or []))
    if not events:
        return None
    return ("ALERT: " if compact else "Weather alert: ") + ", ".join(events)


def build_alert_message(weather, compact, max_bytes=200):
    text = _alert_text(weather, compact)
    return _truncate(text, max_bytes) if text else None


def build_message(now, tz, weather, msg_cfg, max_bytes=200, lat=None, lon=None, next_time=None,
                  time_source="GPS time"):
    compact = msg_cfg.get("compact", True)
    fields = msg_cfg.get("fields", DEFAULT_FIELDS)
    ctx = {"now": now, "tz": tz, "weather": weather, "compact": compact,
           "lat": lat, "lon": lon, "next_time": next_time, "time_source": time_source}
    parts = [text for text in (FIELD_RENDERERS[f](ctx) for f in fields) if text]
    if msg_cfg.get("alerts", "separate") == "inline":
        alert = _alert_text(weather, compact)
        if alert:
            parts.insert(min(1, len(parts)), alert)
    nws_keys = {FIELD_DATA[f] for f in fields} & {"hourly", "daily", "obs"}
    if nws_keys and not any(weather.get(k) for k in nws_keys):
        parts.insert(min(1, len(parts)), "weather unavailable")
    return _fit(parts, max_bytes)


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


def build_messages(cfg, tz, next_time=None, allow_clock_fallback=False):
    time_source = "GPS time"
    try:
        fix = read_gpsd(cfg.get("gps", {}))
        now = datetime.fromisoformat(fix["time"].replace("Z", "+00:00"))
    except Exception as exc:
        if not allow_clock_fallback:
            raise
        log.warning("GPS unavailable (%s); using SYSTEM CLOCK", exc)
        fix, now, time_source = {}, datetime.now(timezone.utc), "SYSTEM CLOCK-NOT GPS"
    loc = cfg.get("location", {})
    lat = loc.get("latitude") if loc.get("latitude") is not None else fix.get("lat")
    lon = loc.get("longitude") if loc.get("longitude") is not None else fix.get("lon")
    msg_cfg = cfg.get("message", {})
    need = required_data(msg_cfg)
    weather = {}
    if need:
        try:
            if lat is None or lon is None:
                raise RuntimeError("no location available")
            weather = get_weather(lat, lon, cfg.get("noaa", {}), need)
        except Exception as exc:
            log.warning("weather lookup failed: %s", exc)
    mcfg = cfg.get("meshtastic", {})
    max_bytes = mcfg.get("max_message_bytes", 200)
    texts = [build_message(now, tz, weather, msg_cfg, max_bytes, lat, lon, next_time, time_source)]
    if msg_cfg.get("alerts", "separate") == "separate":
        texts.append(build_alert_message(weather, msg_cfg.get("compact", True), max_bytes))
    return [t for t in texts if t], time_source != "GPS time"


def broadcast_once(cfg, tz, next_time=None):
    mcfg = cfg.get("meshtastic", {})
    texts, _ = build_messages(cfg, tz, next_time)
    if not texts:
        log.warning("nothing to broadcast")
        return
    iface = open_interface(mcfg)
    try:
        for i, text in enumerate(texts):
            if i:
                time.sleep(3)  # leave airtime between consecutive messages
            iface.sendText(text, channelIndex=mcfg.get("channel_index", 0))
            log.info("broadcast: %s", text)
    finally:
        iface.close()


def dry_run(cfg, tz):
    max_bytes = cfg.get("meshtastic", {}).get("max_message_bytes", 200)
    next_time = next_run(cfg["schedule"], datetime.now(timezone.utc), tz)
    texts, fallback = build_messages(cfg, tz, next_time, allow_clock_fallback=True)
    if fallback:
        print("*** WARNING: GPS unavailable - time below is the SYSTEM CLOCK, not GPS ***")
    print("DRY RUN - nothing is transmitted")
    if not texts:
        print("(nothing to broadcast)")
        return 1
    for i, text in enumerate(texts, 1):
        print(f"--- message {i} of {len(texts)} ({len(text.encode('utf-8'))}/{max_bytes} bytes) ---")
        print(text)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true", help="broadcast once and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="interactive test: print the message(s) that would be sent, without transmitting")
    args = ap.parse_args(argv)
    if args.dry_run and (args.once or not (sys.stdin.isatty() and sys.stdout.isatty())):
        ap.error("--dry-run runs only from an interactive terminal and cannot be combined with --once")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config(args.config)
    tz = ZoneInfo(cfg.get("time_zone", "UTC"))
    if args.dry_run:
        return dry_run(cfg, tz)
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
            broadcast_once(cfg, tz, next_run(cfg["schedule"], target, tz))
        except Exception as exc:
            log.error("broadcast failed: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
