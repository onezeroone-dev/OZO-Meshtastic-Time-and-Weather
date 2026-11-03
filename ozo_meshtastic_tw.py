#!/usr/bin/env python3
"""Broadcast GPS time and NOAA weather to a local Meshtastic mesh on a schedule."""
import argparse
import json
import logging
import math
import os
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
    flt = msg.get("alert_filter", {})
    if flt.get("min_severity", "Minor") not in SEVERITIES:
        raise ValueError(f"message.alert_filter.min_severity must be one of {list(SEVERITIES)}")
    for key in ("include", "exclude"):
        if not isinstance(flt.get(key, []), list):
            raise ValueError(f"message.alert_filter.{key} must be a list of NWS event names")
    quiet = cfg.get("quiet_hours")
    if quiet:
        for key in ("start", "end"):
            try:
                _parse_hhmm(quiet[key])
            except (KeyError, ValueError, AttributeError) as exc:
                raise ValueError(f"quiet_hours.{key} must be HH:MM") from exc
        if quiet.get("mode", "skip") not in QUIET_MODES:
            raise ValueError(f"quiet_hours.mode must be one of {list(QUIET_MODES)}")
    retry = cfg.get("retry", {})
    if int(retry.get("attempts", 3)) < 1 or float(retry.get("delay_seconds", 5)) < 0:
        raise ValueError("retry.attempts must be >= 1 and retry.delay_seconds >= 0")
    return cfg


def _parse_hhmm(text):
    hh, mm = (int(x) for x in text.split(":"))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(text)
    return hh * 60 + mm


def retry_policy(cfg):
    retry = cfg.get("retry", {})
    return int(retry.get("attempts", 3)), float(retry.get("delay_seconds", 5))


def with_retry(func, what, attempts, delay):
    """Call func(); on any exception wait `delay` seconds and retry, up to `attempts` calls in total."""
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:
            if attempt == attempts or not _transient(exc):
                raise
            log.warning("%s failed (attempt %d/%d): %s; retrying in %gs", what, attempt, attempts, exc, delay)
            time.sleep(delay)


def _transient(exc):
    """False for errors a retry cannot fix, such as an HTTP 4xx (other than 429)."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return not isinstance(exc, (ValueError, KeyError))


def in_quiet_hours(quiet, now, tz):
    """True if `now` falls inside the configured quiet window (which may span midnight)."""
    if not quiet:
        return False
    local = now.astimezone(tz)
    minute = local.hour * 60 + local.minute
    start, end = _parse_hhmm(quiet["start"]), _parse_hhmm(quiet["end"])
    if start == end:
        return False
    return start <= minute < end if start < end else minute >= start or minute < end


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
SEVERITIES = ("Minor", "Moderate", "Severe", "Extreme")
QUIET_MODES = ("skip", "alerts_only")
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


def _nws_get(url, headers, timeout, retry=(1, 0)):
    def fetch():
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    return with_retry(fetch, "NWS request", *retry)


def _value(props, key):
    return (props.get(key) or {}).get("value")


def _parse_ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _summarize_observation(features):
    latest = features[0]["properties"]
    obs = {"temp_c": _value(latest, "temperature"), "humidity": _value(latest, "relativeHumidity"),
           "gust_kmh": _value(latest, "windGust"), "pressure_hpa": None, "trend": None,
           "time": _parse_ts(latest["timestamp"])}
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


def get_observation(stations_url, headers, timeout, retry=(1, 0)):
    """Latest observation from the nearest station that reports a temperature."""
    for station in _nws_get(stations_url, headers, timeout, retry).get("features", [])[:3]:
        features = _nws_get(f"{station['id']}/observations?limit=4", headers, timeout, retry).get("features", [])
        if features and _value(features[0]["properties"], "temperature") is not None:
            return _summarize_observation(features)
    return None


def filter_alerts(features, flt):
    """Event names of the alerts that pass min_severity / include / exclude (case-insensitive)."""
    flt = flt or {}
    floor = SEVERITIES.index(flt["min_severity"]) if flt.get("min_severity") else 0
    include = {e.lower() for e in flt.get("include", [])}
    exclude = {e.lower() for e in flt.get("exclude", [])}
    events = []
    for feature in features:
        props = feature["properties"]
        event, severity = props["event"], props.get("severity")
        if event.lower() in exclude or (include and event.lower() not in include):
            continue
        # Alerts with an unknown severity are kept rather than risk hiding something important.
        if severity in SEVERITIES and SEVERITIES.index(severity) < floor:
            continue
        events.append(event)
    return events


def get_weather(lat, lon, noaa, need, alert_filter=None, retry=(1, 0)):
    """Fetch the NWS datasets named in `need`; each one fails independently.

    Names of datasets that could not be fetched are listed under the "failed" key.
    """
    headers = {"User-Agent": noaa.get("user_agent", "OZO-Meshtastic-Time-and-Weather"),
               "Accept": "application/geo+json"}
    if noaa.get("api_key"):
        headers["X-Api-Key"] = noaa["api_key"]
    timeout = noaa.get("timeout_seconds", 20)
    api_url = noaa.get("api_url", "https://api.weather.gov").rstrip("/")
    weather = {"failed": []}

    def fail(name, exc):
        log.warning("%s lookup failed: %s", name, exc)
        weather["failed"].append(name)

    if "alerts" in need:
        try:
            data = _nws_get(f"{api_url}/alerts/active?point={lat:.4f},{lon:.4f}", headers, timeout, retry)
            weather["alerts"] = filter_alerts(data.get("features", []), alert_filter)
        except Exception as exc:
            fail("alerts", exc)
    if need & {"hourly", "daily", "obs"}:
        try:
            point = _nws_get(f"{api_url}/points/{lat:.4f},{lon:.4f}", headers, timeout, retry)["properties"]
        except Exception as exc:
            for key in sorted(need & {"hourly", "daily", "obs"}):
                fail(key, exc)
            return weather
        for key, link in (("hourly", "forecastHourly"), ("daily", "forecast")):
            if key in need:
                try:
                    weather[key] = _nws_get(point[link], headers, timeout, retry)["properties"]["periods"]
                except Exception as exc:
                    fail(key, exc)
        if "obs" in need:
            try:
                weather["obs"] = get_observation(point["observationStations"], headers, timeout, retry)
            except Exception as exc:
                fail("obs", exc)
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


def _first_period(weather, key, now=None):
    """First forecast period, skipping hourly periods that ended before `now` (stale cached data)."""
    for period in weather.get(key) or []:
        end = period.get("endTime")
        if key == "hourly" and now is not None and end and _parse_ts(end) <= now:
            continue
        return period
    return None


def _f_time(c):
    local = c["now"].astimezone(c["tz"])
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()}"


def _f_time_sync(c):
    return c.get("time_source", "GPS time")


def _f_conditions(c):
    p = _first_period(c["weather"], "hourly", c["now"])
    if not p:
        return None
    if c["compact"]:
        wind = f"{p['windDirection']} {p['windSpeed']}".replace(" mph", "mph")
        return f"{_abbr(p['shortForecast'], True)} {p['temperature']}{p['temperatureUnit']} wind {wind}"
    return (f"{p['shortForecast']}, {p['temperature']}{p['temperatureUnit']}, "
            f"wind {p['windSpeed']} {p['windDirection']}")


def _f_precip(c):
    p = _first_period(c["weather"], "hourly", c["now"])
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
    max_age = c.get("max_obs_age", 120)
    if max_age and o.get("time") and (c["now"] - o["time"]).total_seconds() > max_age * 60:
        log.warning("observation is %d min old (limit %d); omitting it",
                    (c["now"] - o["time"]).total_seconds() // 60, max_age)
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
           "lat": lat, "lon": lon, "next_time": next_time, "time_source": time_source,
           "max_obs_age": msg_cfg.get("max_observation_age_minutes", 120)}
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


def build_messages(cfg, tz, next_time=None, allow_clock_fallback=False, honor_quiet=True):
    """Gather GPS time and weather and build the message(s).

    Returns a dict: texts (list), time_source, failed (NWS datasets that failed), quiet (None, "skip" or
    "alerts_only").
    """
    attempts, delay = retry_policy(cfg)
    time_source = "GPS time"
    try:
        fix = with_retry(lambda: read_gpsd(cfg.get("gps", {})), "GPS read", attempts, delay)
        now = datetime.fromisoformat(fix["time"].replace("Z", "+00:00"))
    except Exception as exc:
        if not allow_clock_fallback:
            raise
        log.warning("GPS unavailable (%s); using SYSTEM CLOCK", exc)
        fix, now, time_source = {}, datetime.now(timezone.utc), "SYSTEM CLOCK-NOT GPS"
    result = {"texts": [], "time_source": time_source, "failed": [], "quiet": None}
    msg_cfg = cfg.get("message", {})
    alerts_mode = msg_cfg.get("alerts", "separate")
    quiet_cfg = cfg.get("quiet_hours")
    if honor_quiet and in_quiet_hours(quiet_cfg, now, tz):
        result["quiet"] = quiet_cfg.get("mode", "skip")
        if result["quiet"] == "skip" or alerts_mode == "off":
            return result
    loc = cfg.get("location", {})
    lat = loc.get("latitude") if loc.get("latitude") is not None else fix.get("lat")
    lon = loc.get("longitude") if loc.get("longitude") is not None else fix.get("lon")
    need = {"alerts"} if result["quiet"] else required_data(msg_cfg)
    weather = {}
    if need:
        try:
            if lat is None or lon is None:
                raise RuntimeError("no location available")
            weather = get_weather(lat, lon, cfg.get("noaa", {}), need, msg_cfg.get("alert_filter"),
                                  (attempts, delay))
        except Exception as exc:
            log.warning("weather lookup failed: %s", exc)
            weather = {"failed": sorted(need)}
    result["failed"] = weather.get("failed", [])
    max_bytes = cfg.get("meshtastic", {}).get("max_message_bytes", 200)
    compact = msg_cfg.get("compact", True)
    if result["quiet"]:
        texts = [build_alert_message(weather, compact, max_bytes)]
    else:
        texts = [build_message(now, tz, weather, msg_cfg, max_bytes, lat, lon, next_time, time_source)]
        if alerts_mode == "separate":
            texts.append(build_alert_message(weather, compact, max_bytes))
    result["texts"] = [t for t in texts if t]
    return result


def state_path(cfg):
    """Where the last-sent timestamp is kept, or None if no state location is available."""
    path = cfg.get("state_file")
    if path:
        return path
    state_dir = os.environ.get("STATE_DIRECTORY", "").split(":")[0]  # set by systemd StateDirectory=
    return os.path.join(state_dir, "last_sent.json") if state_dir else None


def seconds_since_last_send(cfg):
    path = state_path(cfg)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return time.time() - float(json.load(fh)["last_sent"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def record_send(cfg):
    path = state_path(cfg)
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"last_sent": time.time()}, fh)
    except OSError as exc:
        log.warning("could not write state file %s: %s", path, exc)


def send_messages(mcfg, texts, attempts, delay):
    """Send `texts` in order, reconnecting and retrying on failure without resending delivered messages."""
    sent = 0
    for attempt in range(1, attempts + 1):
        try:
            iface = open_interface(mcfg)
            try:
                while sent < len(texts):
                    if sent:
                        time.sleep(3)  # leave airtime between consecutive messages
                    iface.sendText(texts[sent], channelIndex=mcfg.get("channel_index", 0))
                    log.info("broadcast: %s", texts[sent])
                    sent += 1
            finally:
                iface.close()
            return
        except Exception as exc:
            if attempt == attempts:
                raise
            log.warning("radio send failed (attempt %d/%d): %s; retrying in %gs", attempt, attempts, exc, delay)
            time.sleep(delay)


def summary(status, **fields):
    """One greppable log line per broadcast cycle, e.g. 'broadcast summary: status=ok messages=2 ...'."""
    log.info("broadcast summary: status=%s %s", status,
             " ".join(f"{k}={v if v not in (None, '', []) else '-'}" for k, v in fields.items()))


def broadcast_once(cfg, tz, next_time=None, service=False):
    """Broadcast once. In service mode quiet hours and the minimum-interval guard apply."""
    if service:
        min_gap = cfg.get("schedule", {}).get("min_interval_seconds", 300)
        since = seconds_since_last_send(cfg)
        if since is not None and 0 <= since < min_gap:
            summary("skipped", reason="duplicate-guard", last_sent_s_ago=round(since))
            return
    built = build_messages(cfg, tz, next_time, honor_quiet=service)
    texts, failed = built["texts"], built["failed"]
    if not texts:
        summary("skipped", reason=f"quiet-hours-{built['quiet']}" if built["quiet"] else "nothing-to-send",
                failed=",".join(failed))
        return
    mcfg = cfg.get("meshtastic", {})
    send_messages(mcfg, texts, *retry_policy(cfg))
    if service:
        record_send(cfg)
    summary("partial" if failed else "ok", messages=len(texts),
            bytes="/".join(str(len(t.encode("utf-8"))) for t in texts),
            time_source=built["time_source"], quiet=built["quiet"], failed=",".join(failed))


def dry_run(cfg, tz):
    max_bytes = cfg.get("meshtastic", {}).get("max_message_bytes", 200)
    next_time = next_run(cfg["schedule"], datetime.now(timezone.utc), tz)
    built = build_messages(cfg, tz, next_time, allow_clock_fallback=True)
    if built["time_source"] != "GPS time":
        print("*** WARNING: GPS unavailable - time below is the SYSTEM CLOCK, not GPS ***")
    print("DRY RUN - nothing is transmitted")
    if built["quiet"]:
        print(f"(quiet hours are active, mode: {built['quiet']})")
    if built["failed"]:
        print(f"(NWS lookups that failed: {', '.join(built['failed'])})")
    texts = built["texts"]
    if not texts:
        print("(nothing to broadcast)")
        return 1
    for i, text in enumerate(texts, 1):
        print(f"--- message {i} of {len(texts)} ({len(text.encode('utf-8'))}/{max_bytes} bytes) ---")
        print(text)
    return 0


def check(cfg, tz):
    """Post-install health check: config, GPS, LoRa radio and NOAA reachability. Returns a failure count."""
    import check_radios
    print(f"OK   config: loaded; time zone {tz.key}; "
          f"schedule {'times ' + ','.join(cfg['schedule']['times']) if cfg['schedule'].get('times') else 'every ' + str(cfg['schedule']['interval_minutes']) + ' min'}")
    failures = check_radios.run_checks(cfg)
    loc = cfg.get("location", {})
    lat, lon = loc.get("latitude"), loc.get("longitude")
    try:
        if lat is None or lon is None:
            fix = read_gpsd(cfg.get("gps", {}))
            lat, lon = fix.get("lat"), fix.get("lon")
        if lat is None or lon is None:
            raise RuntimeError("no location: set location.latitude/longitude or get a GPS position fix")
        weather = get_weather(lat, lon, cfg.get("noaa", {}), {"alerts", "hourly", "daily", "obs"},
                              (cfg.get("message", {}).get("alert_filter")), (1, 0))
        failed = weather["failed"]
        if len(failed) == 4:
            raise RuntimeError("every NWS lookup failed; check internet access, noaa.api_url and noaa.user_agent")
        if failed:
            print(f"WARN NOAA: reachable, but these lookups failed: {', '.join(failed)}", file=sys.stderr)
        else:
            print(f"OK   NOAA: all lookups succeeded for {lat:.4f},{lon:.4f}")
    except Exception as exc:
        failures += 1
        print(f"FAIL NOAA: {exc}", file=sys.stderr)
    return failures


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.json")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="broadcast once and exit (ignores quiet hours)")
    mode.add_argument("--dry-run", action="store_true",
                      help="interactive test: print the message(s) that would be sent, without transmitting")
    mode.add_argument("--check", action="store_true",
                      help="check config, GPS, LoRa radio and NOAA, then exit (briefly opens the radio)")
    args = ap.parse_args(argv)
    if args.dry_run and not (sys.stdin.isatty() and sys.stdout.isatty()):
        ap.error("--dry-run runs only from an interactive terminal")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config(args.config)
    tz = ZoneInfo(cfg.get("time_zone", "UTC"))
    if args.check:
        return 1 if check(cfg, tz) else 0
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
            broadcast_once(cfg, tz, next_run(cfg["schedule"], target, tz), service=True)
        except Exception as exc:
            log.error("broadcast failed: %s", exc)
            summary("failed", error=type(exc).__name__ + ": " + str(exc))


if __name__ == "__main__":
    sys.exit(main())
