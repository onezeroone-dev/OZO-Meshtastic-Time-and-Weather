#!/usr/bin/env python3
"""Broadcast GPS time and NOAA weather to a local Meshtastic mesh on a schedule."""
# DESCRIPTION
# Reads the time from gpsd, fetches weather and alerts from the National Weather Service (NWS), and broadcasts a
# short message to a Meshtastic LoRa mesh on a schedule (fixed times, or an interval within an optional window).
# EXAMPLE
# venv/bin/python ozo_meshtastic_tw.py --config config.json --dry-run
# NOTES
# Run without arguments to start the service loop; see README.md for installation and configuration.

# Imports: standard library
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

# Imports: third party
import requests

# Create a logger object
log = logging.getLogger("ozo-meshtastic-tw")


# CONSTANTS
# Message fields in default priority order; the value names the NWS data a field needs (None = none)
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
# Default fields, alert modes, severities (lowest to highest) and the separator between message parts
DEFAULT_FIELDS = list(FIELD_DATA)
ALERT_MODES = ("separate", "inline", "off")
SEVERITIES = ("Minor", "Moderate", "Severe", "Extreme")
SEP = " | "
# Moon: length of the synodic month, a reference new moon and the phase names (compact, full)
SYNODIC_DAYS = 29.530588853
MOON_REF = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
MOON_PHASES = [
    ("New", "new moon"), ("Wax Cres", "waxing crescent"), ("1st Qtr", "first quarter"),
    ("Wax Gib", "waxing gibbous"), ("Full", "full moon"), ("Wan Gib", "waning gibbous"),
    ("Last Qtr", "last quarter"), ("Wan Cres", "waning crescent"),
]
# Word abbreviations used by compact messages
ABBREVIATIONS = [(re.compile(rf"\b{word}\b"), short) for word, short in (
    ("Thunderstorms?", "T-storms"), ("Partly", "Pt"), ("Mostly", "Mstly"), ("Cloudy", "Cldy"),
    ("Showers", "Shwrs"), ("Slight Chance", "Slt Chc"), ("Chance", "Chc"), ("Likely", "Lkly"),
    ("Scattered", "Sct"), ("Isolated", "Iso"))]


# CONFIGURATION
# load_config function
def load_config(path):
    """
    DESCRIPTION
    Loads the JSON configuration file and validates it, so that mistakes surface at startup.

    PARAMETERS
    path: The path to the configuration file.

    RETURNS
    The configuration dictionary.

    RAISES
    ValueError if a setting is invalid or has been replaced (quiet_hours).
    """
    # Load the configuration
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    # Get the schedule
    sched = cfg.get("schedule", {})
    # Determine if the retired quiet_hours setting is still in use
    if "quiet_hours" in cfg and cfg["quiet_hours"]:
        # quiet_hours is set; tell the operator what replaced it
        raise ValueError("quiet_hours has been replaced by schedule.broadcast_window")
    # Determine if the schedule uses an interval (times takes precedence over the interval and window)
    if not sched.get("times"):
        # No times are defined; validate the broadcast window, if any
        window = sched.get("broadcast_window")
        if window:
            # Iterate over the window boundaries
            for key in ("start", "end"):
                # Boundary must be HH:MM
                try:
                    _parse_hhmm(window[key])
                except (KeyError, ValueError, AttributeError) as exc:
                    raise ValueError(f"schedule.broadcast_window.{key} must be HH:MM") from exc
            # Window must have a length
            if _parse_hhmm(window["start"]) == _parse_hhmm(window["end"]):
                raise ValueError("schedule.broadcast_window.start and end must differ")
        # Interval must be positive
        if not sched.get("interval_minutes") or int(sched["interval_minutes"]) < 1:
            raise ValueError("schedule needs a positive 'interval_minutes' or non-empty 'times'")
    # Get the message configuration and validate the fields
    msg = cfg.get("message", {})
    unknown = [f for f in msg.get("fields", []) if f not in FIELD_DATA]
    if unknown:
        raise ValueError(f"unknown message field(s) {unknown}; valid: {list(FIELD_DATA)}")
    # Validate the alert mode
    if msg.get("alerts", "separate") not in ALERT_MODES:
        raise ValueError(f"message.alerts must be one of {list(ALERT_MODES)}")
    # Validate the alert filter
    flt = msg.get("alert_filter", {})
    if flt.get("min_severity", "Minor") not in SEVERITIES:
        raise ValueError(f"message.alert_filter.min_severity must be one of {list(SEVERITIES)}")
    for key in ("include", "exclude"):
        if not isinstance(flt.get(key, []), list):
            raise ValueError(f"message.alert_filter.{key} must be a list of NWS event names")
    # Validate the retry policy
    retry = cfg.get("retry", {})
    if int(retry.get("attempts", 3)) < 1 or float(retry.get("delay_seconds", 5)) < 0:
        raise ValueError("retry.attempts must be >= 1 and retry.delay_seconds >= 0")
    # Return
    return cfg


# _parse_hhmm function
def _parse_hhmm(text):
    """
    DESCRIPTION
    Converts an HH:MM string to minutes after midnight.

    PARAMETERS
    text: The time as HH:MM (24-hour).

    RETURNS
    The number of minutes after midnight.

    RAISES
    ValueError if the text is not a valid time.
    """
    # Split the text into hours and minutes
    hh, mm = (int(x) for x in text.split(":"))
    # Hours and minutes must be in range
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(text)
    # Return
    return hh * 60 + mm


# RETRY
# retry_policy function
def retry_policy(cfg):
    """
    DESCRIPTION
    Gets the retry policy from the configuration.

    PARAMETERS
    cfg: The configuration dictionary.

    RETURNS
    A tuple of the number of attempts and the delay in seconds between them.
    """
    # Get the retry configuration and return the policy
    retry = cfg.get("retry", {})
    return int(retry.get("attempts", 3)), float(retry.get("delay_seconds", 5))


# with_retry function
def with_retry(func, what, attempts, delay):
    """
    DESCRIPTION
    Calls a function; on a transient failure waits and tries again, up to the given number of calls in total.

    PARAMETERS
    func: The function to call (no arguments).
    what: A short description of the operation, for the log.
    attempts: The total number of calls to make.
    delay: The number of seconds to wait between calls.

    RETURNS
    Whatever func returns.

    RAISES
    The last exception, or immediately for an error that a retry cannot fix.
    """
    # Iterate over the attempts
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:
            # Out of attempts, or a retry cannot help; give up
            if attempt == attempts or not _transient(exc):
                raise
            # Log, wait and try again
            log.warning("%s failed (attempt %d/%d): %s; retrying in %gs", what, attempt, attempts, exc, delay)
            time.sleep(delay)


# _transient function
def _transient(exc):
    """
    DESCRIPTION
    Determines if an error might clear up on its own.

    PARAMETERS
    exc: The exception to evaluate.

    RETURNS
    False for errors a retry cannot fix, such as an HTTP 4xx (other than 429); otherwise True.
    """
    # Determine if this is an HTTP error with a response
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        # Only server errors and rate limiting are worth retrying
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    # Bad data will not improve on retry
    return not isinstance(exc, (ValueError, KeyError))


# GPS
# read_gpsd function
def read_gpsd(cfg):
    """
    DESCRIPTION
    Reads the first usable position and time report from gpsd.

    PARAMETERS
    cfg: The "gps" section of the configuration.

    RETURNS
    The first TPV report that contains a time and at least a 2D fix.

    RAISES
    RuntimeError if no such report arrives before the timeout; OSError if gpsd cannot be reached.
    """
    # Get the connection settings and compute the deadline
    host = cfg.get("gpsd_host", "127.0.0.1")
    port = cfg.get("gpsd_port", 2947)
    timeout = cfg.get("timeout_seconds", 15)
    deadline = time.monotonic() + timeout
    # Connect to gpsd
    with socket.create_connection((host, port), timeout=timeout) as sock:
        # Ask gpsd to stream JSON reports
        sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
        buf = b""
        # Read until the deadline
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            # gpsd closed the connection
            if not chunk:
                break
            # Keep any partial line in the buffer and process the complete lines
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                # Skip lines that are not JSON
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                # Determine if this is a usable TPV report
                if msg.get("class") == "TPV" and msg.get("time") and msg.get("mode", 0) >= 2:
                    # Report has a time and a fix; return it
                    return msg
    # No usable report arrived
    raise RuntimeError("no GPS fix/time received from gpsd")


# NWS WEATHER
# required_data function
def required_data(msg_cfg):
    """
    DESCRIPTION
    Determines which NWS datasets the configured fields and alert mode need.

    PARAMETERS
    msg_cfg: The "message" section of the configuration.

    RETURNS
    A set of dataset names (hourly, daily, obs, alerts).
    """
    # Get the datasets needed by the fields
    fields = msg_cfg.get("fields", DEFAULT_FIELDS)
    need = {FIELD_DATA[f] for f in fields if FIELD_DATA[f]}
    # Alerts are needed unless they are turned off
    if msg_cfg.get("alerts", "separate") != "off":
        need.add("alerts")
    # Return
    return need


# _nws_get function
def _nws_get(url, headers, timeout, retry=(1, 0)):
    """
    DESCRIPTION
    Requests a JSON document from the NWS API, with retries.

    PARAMETERS
    url: The URL to request.
    headers: The HTTP headers to send.
    timeout: The request timeout in seconds.
    retry: A tuple of attempts and delay in seconds.

    RETURNS
    The decoded JSON document.
    """
    # Define the request
    def fetch():
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    # Return
    return with_retry(fetch, "NWS request", *retry)


# _value function
def _value(props, key):
    """
    DESCRIPTION
    Gets the "value" of an NWS quantity (a dictionary with a unit and a value), or None if it is missing.
    """
    # Return
    return (props.get(key) or {}).get("value")


# _parse_ts function
def _parse_ts(text):
    """
    DESCRIPTION
    Converts an ISO 8601 timestamp (including the "Z" suffix) to an aware datetime.
    """
    # Return
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# _summarize_observation function
def _summarize_observation(features):
    """
    DESCRIPTION
    Reduces a station's recent observations to the latest readings plus a pressure trend.

    PARAMETERS
    features: The observation features, newest first.

    RETURNS
    A dictionary of temp_c, humidity, gust_kmh, pressure_hpa, trend and time.
    """
    # Get the latest observation and set the readings
    latest = features[0]["properties"]
    obs = {"temp_c": _value(latest, "temperature"), "humidity": _value(latest, "relativeHumidity"),
           "gust_kmh": _value(latest, "windGust"), "pressure_hpa": None, "trend": None,
           "time": _parse_ts(latest["timestamp"])}
    # Determine if the latest observation includes pressure
    pressure = _value(latest, "barometricPressure")
    if pressure is not None:
        # Pressure is reported; convert it to hPa
        obs["pressure_hpa"] = pressure / 100
        # Iterate over the older observations, oldest first
        for old in features[:0:-1]:
            old_pressure = _value(old["properties"], "barometricPressure")
            if old_pressure is None:
                continue
            # Determine if the observation is old enough to establish a trend
            hours = (_parse_ts(latest["timestamp"]) - _parse_ts(old["properties"]["timestamp"])).total_seconds() / 3600
            if hours >= 2:
                # At least two hours apart; a change of 1 hPa or more is rising or falling
                diff = (pressure - old_pressure) / 100
                obs["trend"] = "rising" if diff >= 1 else "falling" if diff <= -1 else "steady"
            break
    # Return
    return obs


# get_observation function
def get_observation(stations_url, headers, timeout, retry=(1, 0)):
    """
    DESCRIPTION
    Gets the latest observation from the nearest station that reports a temperature.

    PARAMETERS
    stations_url: The NWS observation stations URL for the point.
    headers: The HTTP headers to send.
    timeout: The request timeout in seconds.
    retry: A tuple of attempts and delay in seconds.

    RETURNS
    The observation dictionary, or None if none of the three nearest stations reports a temperature.
    """
    # Iterate over the nearest stations
    for station in _nws_get(stations_url, headers, timeout, retry).get("features", [])[:3]:
        # Get the station's recent observations
        features = _nws_get(f"{station['id']}/observations?limit=4", headers, timeout, retry).get("features", [])
        # Determine if the station reports a temperature
        if features and _value(features[0]["properties"], "temperature") is not None:
            # Station reports a temperature; summarize it
            return _summarize_observation(features)
    # No station qualified
    return None


# filter_alerts function
def filter_alerts(features, flt):
    """
    DESCRIPTION
    Filters active alerts by minimum severity and by event name (case-insensitive).

    PARAMETERS
    features: The alert features from the NWS API.
    flt: The alert filter (min_severity, include, exclude).

    RETURNS
    A list of the event names of the alerts that pass the filter.
    """
    # Get the filter settings
    flt = flt or {}
    floor = SEVERITIES.index(flt["min_severity"]) if flt.get("min_severity") else 0
    include = {e.lower() for e in flt.get("include", [])}
    exclude = {e.lower() for e in flt.get("exclude", [])}
    events = []
    # Iterate over the alerts
    for feature in features:
        props = feature["properties"]
        event, severity = props["event"], props.get("severity")
        # Skip excluded events, and events not in a non-empty include list
        if event.lower() in exclude or (include and event.lower() not in include):
            continue
        # Skip alerts below the severity floor; alerts with an unknown severity are kept rather than risk hiding something important
        if severity in SEVERITIES and SEVERITIES.index(severity) < floor:
            continue
        # Alert passes the filter
        events.append(event)
    # Return
    return events


# get_weather function
def get_weather(lat, lon, noaa, need, alert_filter=None, retry=(1, 0)):
    """
    DESCRIPTION
    Fetches the NWS datasets named in need; each one fails independently.

    PARAMETERS
    lat: The latitude.
    lon: The longitude.
    noaa: The "noaa" section of the configuration.
    need: A set of dataset names (hourly, daily, obs, alerts).
    alert_filter: The alert filter (min_severity, include, exclude).
    retry: A tuple of attempts and delay in seconds.

    RETURNS
    A dictionary keyed by dataset name; the names of datasets that could not be fetched are listed under "failed".
    """
    # Build the request headers
    headers = {"User-Agent": noaa.get("user_agent", "OZO-Meshtastic-Time-and-Weather"),
               "Accept": "application/geo+json"}
    # Determine if an API key is configured
    if noaa.get("api_key"):
        # API key is configured; send it
        headers["X-Api-Key"] = noaa["api_key"]
    # Get the request settings
    timeout = noaa.get("timeout_seconds", 20)
    api_url = noaa.get("api_url", "https://api.weather.gov").rstrip("/")
    weather = {"failed": []}

    # Define a function to record a failed dataset
    def fail(name, exc):
        log.warning("%s lookup failed: %s", name, exc)
        weather["failed"].append(name)

    # Determine if alerts are needed
    if "alerts" in need:
        # Get the active alerts for the point
        try:
            data = _nws_get(f"{api_url}/alerts/active?point={lat:.4f},{lon:.4f}", headers, timeout, retry)
            weather["alerts"] = filter_alerts(data.get("features", []), alert_filter)
        except Exception as exc:
            fail("alerts", exc)
    # Determine if any dataset that depends on the point lookup is needed
    if need & {"hourly", "daily", "obs"}:
        # Look up the point to get the forecast and station URLs
        try:
            point = _nws_get(f"{api_url}/points/{lat:.4f},{lon:.4f}", headers, timeout, retry)["properties"]
        except Exception as exc:
            # Point lookup failed; every dependent dataset fails with it
            for key in sorted(need & {"hourly", "daily", "obs"}):
                fail(key, exc)
            return weather
        # Iterate over the forecasts
        for key, link in (("hourly", "forecastHourly"), ("daily", "forecast")):
            if key in need:
                # Forecast is needed; get its periods
                try:
                    weather[key] = _nws_get(point[link], headers, timeout, retry)["properties"]["periods"]
                except Exception as exc:
                    fail(key, exc)
        # Determine if the observation is needed
        if "obs" in need:
            # Observation is needed; get it from the nearest station
            try:
                weather["obs"] = get_observation(point["observationStations"], headers, timeout, retry)
            except Exception as exc:
                fail("obs", exc)
    # Return
    return weather


# SUN AND MOON
# _sun_event function
def _sun_event(day, lat, lon, rising, tz):
    """
    DESCRIPTION
    Calculates sunrise or sunset locally using the NOAA general solar position algorithm.

    PARAMETERS
    day: The local date.
    lat: The latitude.
    lon: The longitude.
    rising: True for sunrise; False for sunset.
    tz: The local time zone.

    RETURNS
    An aware datetime on the local date, or "up" or "down" if the sun never sets or never rises that day.
    """
    # Day of the year, longitude hour and approximate event time
    n = day.timetuple().tm_yday
    lng_hour = lon / 15
    t = n + ((6 if rising else 18) - lng_hour) / 24
    # Sun's mean anomaly and true longitude
    mean_anomaly = 0.9856 * t - 3.289
    m = math.radians(mean_anomaly)
    true_lon = (mean_anomaly + 1.916 * math.sin(m) + 0.020 * math.sin(2 * m) + 282.634) % 360
    lam = math.radians(true_lon)
    # Right ascension, adjusted to the same quadrant as the true longitude
    ra = math.degrees(math.atan(0.91764 * math.tan(lam))) % 360
    ra += math.floor(true_lon / 90) * 90 - math.floor(ra / 90) * 90
    # Declination
    sin_dec = 0.39782 * math.sin(lam)
    cos_dec = math.cos(math.asin(sin_dec))
    # Local hour angle
    phi = math.radians(lat)
    cos_h = (math.cos(math.radians(90.833)) - sin_dec * math.sin(phi)) / (cos_dec * math.cos(phi))
    # Sun never rises
    if cos_h > 1:
        return "down"
    # Sun never sets
    if cos_h < -1:
        return "up"
    # Convert the hour angle to hours
    hour_angle = math.degrees(math.acos(cos_h))
    if rising:
        hour_angle = 360 - hour_angle
    # Compute the UTC time of the event
    local_mean = hour_angle / 15 + ra / 15 - 0.06571 * t - 6.622
    utc = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=(local_mean - lng_hour) % 24)
    # Return, shifted so that the event falls on the local date
    return utc - timedelta(days=(utc.astimezone(tz).date() - day).days)


# moon_phase function
def moon_phase(now):
    """
    DESCRIPTION
    Calculates the moon phase from the age of the moon since a reference new moon.

    PARAMETERS
    now: The current time (aware datetime).

    RETURNS
    A tuple of the phase index into MOON_PHASES and the percent illuminated.
    """
    # Compute the age of the moon and its illumination
    age = ((now - MOON_REF).total_seconds() / 86400) % SYNODIC_DAYS
    illumination = (1 - math.cos(2 * math.pi * age / SYNODIC_DAYS)) / 2 * 100
    # Return
    return int((age + SYNODIC_DAYS / 16) / (SYNODIC_DAYS / 8)) % 8, round(illumination)


# MESSAGE BUILDING
# _abbr function
def _abbr(text, compact):
    """
    DESCRIPTION
    Abbreviates common forecast words when the compact format is in use.
    """
    # Determine if compact messages are enabled
    if compact:
        # Compact; apply each abbreviation
        for pattern, short in ABBREVIATIONS:
            text = pattern.sub(short, text)
    # Return
    return text


# _first_period function
def _first_period(weather, key, now=None):
    """
    DESCRIPTION
    Gets the first forecast period, skipping hourly periods that ended before now (stale cached data).

    PARAMETERS
    weather: The weather dictionary.
    key: The forecast name (hourly or daily).
    now: The current time (aware datetime).

    RETURNS
    The period dictionary, or None if there is none.
    """
    # Iterate over the periods
    for period in weather.get(key) or []:
        end = period.get("endTime")
        # Skip hourly periods that have already ended
        if key == "hourly" and now is not None and end and _parse_ts(end) <= now:
            continue
        # Period is current
        return period
    # No period qualified
    return None


# Field renderers: each takes the context dictionary and returns the field text, or None to omit the field
# _f_time function
def _f_time(c):
    """Renders the local date, time and time zone."""
    # Return
    local = c["now"].astimezone(c["tz"])
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()}"


# _f_time_sync function
def _f_time_sync(c):
    """Renders the time source, so a fallback to the system clock is visible to the mesh."""
    # Return
    return c.get("time_source", "GPS time")


# _f_conditions function
def _f_conditions(c):
    """Renders the current conditions, temperature and wind from the hourly forecast."""
    # Get the current hourly period
    p = _first_period(c["weather"], "hourly", c["now"])
    # No forecast available; omit the field
    if not p:
        return None
    # Determine if compact messages are enabled
    if c["compact"]:
        # Compact; abbreviate the forecast and the wind
        wind = f"{p['windDirection']} {p['windSpeed']}".replace(" mph", "mph")
        return f"{_abbr(p['shortForecast'], True)} {p['temperature']}{p['temperatureUnit']} wind {wind}"
    # Return
    return (f"{p['shortForecast']}, {p['temperature']}{p['temperatureUnit']}, "
            f"wind {p['windSpeed']} {p['windDirection']}")


# _f_precip function
def _f_precip(c):
    """Renders the chance of precipitation from the hourly forecast."""
    # Get the current hourly period and its chance of precipitation
    p = _first_period(c["weather"], "hourly", c["now"])
    chance = _value(p, "probabilityOfPrecipitation") if p else None
    # No chance reported; omit the field
    if chance is None:
        return None
    # Return
    return f"PoP {chance}%" if c["compact"] else f"{chance}% chance of precipitation"


# _f_high_low function
def _f_high_low(c):
    """Renders the next daytime high and nighttime low from the daily forecast."""
    # Get the first daytime and nighttime periods
    periods = c["weather"].get("daily") or []
    high = next((p for p in periods if p.get("isDaytime")), None)
    low = next((p for p in periods if not p.get("isDaytime")), None)
    # Determine if compact messages are enabled
    if c["compact"]:
        # Compact; return the high and low, or None if neither is available
        return "/".join(x for x in (high and f"H{high['temperature']}", low and f"L{low['temperature']}") if x) or None
    # Return
    return ", ".join(x for x in (high and f"high {high['temperature']}{high['temperatureUnit']}",
                                 low and f"low {low['temperature']}{low['temperatureUnit']}") if x) or None


# _f_observation function
def _f_observation(c):
    """Renders the latest station observation, unless it is older than the configured limit."""
    # No observation available; omit the field
    o = c["weather"].get("obs")
    if not o:
        return None
    # Determine if the observation is too old to be useful
    max_age = c.get("max_obs_age", 120)
    if max_age and o.get("time") and (c["now"] - o["time"]).total_seconds() > max_age * 60:
        # Observation is stale; log it and omit the field
        log.warning("observation is %d min old (limit %d); omitting it",
                    (c["now"] - o["time"]).total_seconds() // 60, max_age)
        return None
    # Convert the readings
    temp = f"{round(o['temp_c'] * 9 / 5 + 32)}F"
    gust = round(o["gust_kmh"] / 1.609344) if o["gust_kmh"] is not None else None
    pressure = f"{round(o['pressure_hpa'])}hPa" if o["pressure_hpa"] is not None else None
    # Append the pressure trend, if known
    if pressure and o["trend"]:
        pressure += f" {o['trend']}"
    # Determine if compact messages are enabled
    if c["compact"]:
        # Compact; use short labels
        bits = [temp, o["humidity"] is not None and f"RH{round(o['humidity'])}%",
                gust is not None and f"G{gust}", pressure]
        return "obs " + " ".join(b for b in bits if b)
    # Return
    bits = [temp, o["humidity"] is not None and f"{round(o['humidity'])}% humidity",
            gust is not None and f"gusts {gust} mph", pressure]
    return "observed: " + ", ".join(b for b in bits if b)


# _f_outlook function
def _f_outlook(c):
    """Renders the forecast for the next daily period."""
    # Need at least two periods; omit the field otherwise
    periods = c["weather"].get("daily") or []
    if len(periods) < 2:
        return None
    # Return
    return f"{periods[1]['name']}: {_abbr(periods[1]['shortForecast'], c['compact'])}"


# _f_sun function
def _f_sun(c):
    """Renders today's sunrise and sunset, calculated locally."""
    # No location available; omit the field
    if c["lat"] is None or c["lon"] is None:
        return None
    # Calculate sunrise and sunset for the local date
    tz = c["tz"]
    day = c["now"].astimezone(tz).date()
    rise = _sun_event(day, c["lat"], c["lon"], True, tz)
    sset = _sun_event(day, c["lat"], c["lon"], False, tz)
    # Determine if the sun stays up or down all day
    state = next((x for x in (rise, sset) if isinstance(x, str)), None)
    if state:
        # Polar day or night
        return "sun up all day" if state == "up" else "sun down all day"
    # Convert to local time
    rise, sset = rise.astimezone(tz), sset.astimezone(tz)
    # Determine if compact messages are enabled
    if c["compact"]:
        # Compact; use a single range
        return f"sun {rise:%H:%M}-{sset:%H:%M}"
    # Return
    return f"sunrise {rise:%H:%M}, sunset {sset:%H:%M}"


# _f_moon function
def _f_moon(c):
    """Renders the moon phase and percent illuminated."""
    # Get the phase
    index, percent = moon_phase(c["now"])
    short, full = MOON_PHASES[index]
    # Return
    return f"moon {short} {percent}%" if c["compact"] else f"moon: {full} ({percent}% illuminated)"


# _f_next_broadcast function
def _f_next_broadcast(c):
    """Renders the time of the next scheduled broadcast."""
    # Next broadcast unknown (for example, --once); omit the field
    if c["next_time"] is None:
        return None
    # Show the date only if the next broadcast is not today
    tz = c["tz"]
    nxt = c["next_time"].astimezone(tz)
    stamp = f"{nxt:%H:%M}" if nxt.date() == c["now"].astimezone(tz).date() else f"{nxt:%m-%d %H:%M}"
    # Return
    return f"next {stamp}" if c["compact"] else f"next broadcast {stamp}"


# Map of field names to renderers
FIELD_RENDERERS = {
    "time": _f_time, "time_sync": _f_time_sync, "conditions": _f_conditions, "precip": _f_precip,
    "high_low": _f_high_low, "observation": _f_observation, "outlook": _f_outlook,
    "sun": _f_sun, "moon": _f_moon, "next_broadcast": _f_next_broadcast,
}


# _truncate function
def _truncate(text, max_bytes):
    """
    DESCRIPTION
    Truncates text to a number of UTF-8 bytes, without splitting a multi-byte character.
    """
    # Return
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


# _fit function
def _fit(parts, max_bytes):
    """
    DESCRIPTION
    Joins whole parts in priority order, skipping any that would exceed the byte limit.

    PARAMETERS
    parts: The message parts, highest priority first.
    max_bytes: The maximum message size in bytes.

    RETURNS
    The message text.
    """
    out = ""
    # Iterate over the parts
    for part in parts:
        candidate = f"{out}{SEP}{part}" if out else part
        # Determine if the part fits
        if len(candidate.encode("utf-8")) <= max_bytes:
            # Part fits; keep it
            out = candidate
        elif not out:
            # Not even the first part fits; truncate it
            out = _truncate(part, max_bytes)
    # Return
    return out


# _alert_text function
def _alert_text(weather, compact):
    """
    DESCRIPTION
    Builds the alert text from the unique event names, or None if there are no alerts.
    """
    # Get the unique events, preserving order
    events = list(dict.fromkeys(weather.get("alerts") or []))
    # No alerts; nothing to say
    if not events:
        return None
    # Return
    return ("ALERT: " if compact else "Weather alert: ") + ", ".join(events)


# build_alert_message function
def build_alert_message(weather, compact, max_bytes=200):
    """
    DESCRIPTION
    Builds the separate alert message.

    PARAMETERS
    weather: The weather dictionary.
    compact: True for the compact format.
    max_bytes: The maximum message size in bytes.

    RETURNS
    The alert text, or None if there are no alerts.
    """
    # Return
    text = _alert_text(weather, compact)
    return _truncate(text, max_bytes) if text else None


# build_message function
def build_message(now, tz, weather, msg_cfg, max_bytes=200, lat=None, lon=None, next_time=None,
                  time_source="GPS time"):
    """
    DESCRIPTION
    Builds the main message from the configured fields, in priority order, within the byte limit.

    PARAMETERS
    now: The current time (aware datetime).
    tz: The local time zone.
    weather: The weather dictionary.
    msg_cfg: The "message" section of the configuration.
    max_bytes: The maximum message size in bytes.
    lat: The latitude.
    lon: The longitude.
    next_time: The time of the next broadcast, if known.
    time_source: Where the time came from ("GPS time" or the system clock).

    RETURNS
    The message text.
    """
    # Get the message settings and build the context for the renderers
    compact = msg_cfg.get("compact", True)
    fields = msg_cfg.get("fields", DEFAULT_FIELDS)
    ctx = {"now": now, "tz": tz, "weather": weather, "compact": compact,
           "lat": lat, "lon": lon, "next_time": next_time, "time_source": time_source,
           "max_obs_age": msg_cfg.get("max_observation_age_minutes", 120)}
    # Render the fields, dropping those with nothing to say
    parts = [text for text in (FIELD_RENDERERS[f](ctx) for f in fields) if text]
    # Determine if alerts are shown inline
    if msg_cfg.get("alerts", "separate") == "inline":
        # Inline; place the alert after the first field
        alert = _alert_text(weather, compact)
        if alert:
            parts.insert(min(1, len(parts)), alert)
    # Determine if every NWS forecast dataset that was wanted is missing
    nws_keys = {FIELD_DATA[f] for f in fields} & {"hourly", "daily", "obs"}
    if nws_keys and not any(weather.get(k) for k in nws_keys):
        # Weather is unavailable; say so after the first field
        parts.insert(min(1, len(parts)), "weather unavailable")
    # Return
    return _fit(parts, max_bytes)


# SCHEDULING AND RADIO
# next_run function
def next_run(sched, now, tz):
    """
    DESCRIPTION
    Determines the next scheduled broadcast. The times list takes precedence over the broadcast window and the interval.

    PARAMETERS
    sched: The "schedule" section of the configuration.
    now: The current time (aware datetime).
    tz: The local time zone.

    RETURNS
    The next scheduled time (aware datetime) after now.

    RAISES
    ValueError if the broadcast window has no future broadcast.
    """
    # Determine if fixed times are defined
    times = sched.get("times") or []
    if times:
        # Times are defined; find the next occurrence of each today and tomorrow
        local = now.astimezone(tz)
        candidates = []
        for t in times:
            hh, mm = (int(x) for x in t.split(":"))
            for day in (0, 1):
                c = (local + timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                # Candidate is in the future (compared in UTC)
                if c.astimezone(timezone.utc) > now.astimezone(timezone.utc):
                    candidates.append(c)
        # Return the earliest
        return min(candidates, key=lambda candidate: candidate.astimezone(timezone.utc))
    # Determine if a broadcast window is defined
    window = sched.get("broadcast_window")
    if window:
        # Window is defined; interval slots are anchored to its start
        local_now = now.astimezone(tz)
        start = _parse_hhmm(window["start"])
        end = _parse_hhmm(window["end"])
        duration = (end - start) % (24 * 60)
        candidates = []
        # Iterate over yesterday, today and tomorrow (a window may span midnight)
        for day in (-1, 0, 1):
            date = local_now.date() + timedelta(days=day)
            hour, minute = divmod(start, 60)
            window_start = local_now.replace(year=date.year, month=date.month, day=date.day,
                                             hour=hour, minute=minute, second=0, microsecond=0)
            # Iterate over the slots within the window; the end is exclusive
            for offset in range(0, duration, int(sched["interval_minutes"])):
                candidate = window_start + timedelta(minutes=offset)
                # Slot is in the future; missed slots are not replayed
                if candidate.astimezone(timezone.utc) > now.astimezone(timezone.utc):
                    candidates.append(candidate)
        # Determine if a slot was found
        if candidates:
            # Return the earliest
            return min(candidates, key=lambda candidate: candidate.astimezone(timezone.utc))
        # No slot was found
        raise ValueError("schedule.broadcast_window contains no future broadcast")
    # Neither times nor a window; the interval is relative to now
    return now + timedelta(minutes=sched["interval_minutes"])


# open_interface function
def open_interface(mcfg):
    """
    DESCRIPTION
    Opens a connection to the Meshtastic radio. The libraries are imported here so that --help and the other
    modes work without them.

    PARAMETERS
    mcfg: The "meshtastic" section of the configuration.

    RETURNS
    A Meshtastic interface object; the caller must close it.
    """
    # Import the Meshtastic interfaces
    import meshtastic.serial_interface
    import meshtastic.tcp_interface
    # Determine if the radio is reached through meshtasticd over TCP
    if mcfg.get("connection", "serial") == "tcp":
        # TCP; connect to meshtasticd
        return meshtastic.tcp_interface.TCPInterface(hostname=mcfg.get("tcp_host", "localhost"))
    # Serial; connect to the configured port (None autodetects)
    return meshtastic.serial_interface.SerialInterface(devPath=mcfg.get("serial_port"))


# BROADCASTING
# has_fixed_location function
def has_fixed_location(cfg):
    """
    DESCRIPTION
    Determines whether the configuration supplies a fixed latitude and longitude, which makes the GPS optional.

    PARAMETERS
    cfg: The configuration dictionary.

    RETURNS
    True if both location.latitude and location.longitude are set.
    """
    loc = cfg.get("location") or {}
    return loc.get("latitude") is not None and loc.get("longitude") is not None


# build_messages function
def build_messages(cfg, tz, next_time=None, allow_clock_fallback=False):
    """
    DESCRIPTION
    Gathers the GPS time and weather and builds the message(s).

    PARAMETERS
    cfg: The configuration dictionary.
    tz: The local time zone.
    next_time: The time of the next broadcast, if known.
    allow_clock_fallback: True to use the system clock if the GPS is unavailable (always true for --dry-run; a
    live broadcast also falls back when a fixed location is configured).

    RETURNS
    A dictionary with texts (list), time_source, and failed (NWS datasets that failed).
    """
    # Get the retry policy and read the time from the GPS
    attempts, delay = retry_policy(cfg)
    time_source = "GPS time"
    try:
        fix = with_retry(lambda: read_gpsd(cfg.get("gps", {})), "GPS read", attempts, delay)
        now = datetime.fromisoformat(fix["time"].replace("Z", "+00:00"))
    except Exception as exc:
        # GPS is unavailable; the system clock may stand in for --dry-run or when a fixed location is configured
        if not (allow_clock_fallback or has_fixed_location(cfg)):
            raise
        # Fallback is allowed; label the time source clearly
        log.warning("GPS unavailable (%s); using SYSTEM CLOCK", exc)
        fix, now, time_source = {}, datetime.now(timezone.utc), "SYSTEM CLOCK-NOT GPS"
    # Create the result and get the message settings
    result = {"texts": [], "time_source": time_source, "failed": []}
    msg_cfg = cfg.get("message", {})
    alerts_mode = msg_cfg.get("alerts", "separate")
    # Get the location; a configured location takes precedence over the GPS fix
    loc = cfg.get("location", {})
    lat = loc.get("latitude") if loc.get("latitude") is not None else fix.get("lat")
    lon = loc.get("longitude") if loc.get("longitude") is not None else fix.get("lon")
    # Get the weather, if any is needed
    need = required_data(msg_cfg)
    weather = {}
    if need:
        try:
            # Weather requires a location
            if lat is None or lon is None:
                raise RuntimeError("no location available")
            weather = get_weather(lat, lon, cfg.get("noaa", {}), need, msg_cfg.get("alert_filter"),
                                  (attempts, delay))
        except Exception as exc:
            # Weather lookup failed; continue without it
            log.warning("weather lookup failed: %s", exc)
            weather = {"failed": sorted(need)}
    result["failed"] = weather.get("failed", [])
    # Build the messages
    max_bytes = cfg.get("meshtastic", {}).get("max_message_bytes", 200)
    compact = msg_cfg.get("compact", True)
    texts = [build_message(now, tz, weather, msg_cfg, max_bytes, lat, lon, next_time, time_source)]
    # Determine if alerts are sent as a separate message
    if alerts_mode == "separate":
        # Separate; add the alert message
        texts.append(build_alert_message(weather, compact, max_bytes))
    # Drop empty messages and return
    result["texts"] = [t for t in texts if t]
    return result


# state_path function
def state_path(cfg):
    """
    DESCRIPTION
    Determines where the last-sent timestamp is kept.

    PARAMETERS
    cfg: The configuration dictionary.

    RETURNS
    The path, or None if no state location is available.
    """
    # Determine if the configuration names a state file
    path = cfg.get("state_file")
    if path:
        # State file is configured; use it
        return path
    # Fall back to the systemd state directory
    state_dir = os.environ.get("STATE_DIRECTORY", "").split(":")[0]  # set by systemd StateDirectory=
    # Return
    return os.path.join(state_dir, "last_sent.json") if state_dir else None


# seconds_since_last_send function
def seconds_since_last_send(cfg):
    """
    DESCRIPTION
    Determines how long ago the last broadcast was sent.

    PARAMETERS
    cfg: The configuration dictionary.

    RETURNS
    The number of seconds, or None if it is unknown.
    """
    # No state location; unknown
    path = state_path(cfg)
    if not path:
        return None
    # Read the timestamp; a missing or unreadable file means unknown
    try:
        with open(path, encoding="utf-8") as fh:
            return time.time() - float(json.load(fh)["last_sent"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


# record_send function
def record_send(cfg):
    """
    DESCRIPTION
    Records the time of a successful broadcast; a failure to record it is logged but is not fatal.

    PARAMETERS
    cfg: The configuration dictionary.
    """
    # No state location; nothing to record
    path = state_path(cfg)
    if not path:
        return
    # Write the timestamp
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"last_sent": time.time()}, fh)
    except OSError as exc:
        log.warning("could not write state file %s: %s", path, exc)


# send_messages function
def send_messages(mcfg, texts, attempts, delay):
    """
    DESCRIPTION
    Sends the messages in order, reconnecting and retrying on failure without resending submitted messages.

    PARAMETERS
    mcfg: The "meshtastic" section of the configuration.
    texts: The messages to send.
    attempts: The total number of connection attempts.
    delay: The number of seconds to wait between attempts.

    RAISES
    The last exception if every attempt fails.
    """
    # Control variable
    sent = 0
    # Iterate over the attempts
    for attempt in range(1, attempts + 1):
        try:
            # Open the radio
            iface = open_interface(mcfg)
            try:
                # Send the messages that have not been submitted
                while sent < len(texts):
                    if sent:
                        time.sleep(3)  # leave airtime between consecutive messages
                    iface.sendText(texts[sent], wantAck=True, channelIndex=mcfg.get("channel_index", 0),
                                   onResponse=iface.localNode.onAckNak)
                    log.info("broadcast: %s", texts[sent])
                    sent += 1
            finally:
                # Close the radio so the port lock is released
                iface.close()
            # Everything was sent
            return
        except Exception as exc:
            # Out of attempts; give up
            if attempt == attempts:
                raise
            # Log, wait and try again
            log.warning("radio send failed (attempt %d/%d): %s; retrying in %gs", attempt, attempts, exc, delay)
            time.sleep(delay)


# summary function
def summary(status, **fields):
    """
    DESCRIPTION
    Logs one greppable line per broadcast cycle, e.g. "broadcast summary: status=ok messages=2 ...".

    PARAMETERS
    status: The outcome (ok, partial, skipped or failed).
    fields: Additional key=value details; empty values are shown as "-".
    """
    # Log the summary
    log.info("broadcast summary: status=%s %s", status,
             " ".join(f"{k}={v if v not in (None, '', []) else '-'}" for k, v in fields.items()))


# broadcast_once function
def broadcast_once(cfg, tz, next_time=None, service=False):
    """
    DESCRIPTION
    Builds and broadcasts the message(s) once. In service mode the minimum-interval guard applies.

    PARAMETERS
    cfg: The configuration dictionary.
    tz: The local time zone.
    next_time: The time of the next broadcast, if known.
    service: True when called from the service loop.
    """
    # Determine if this is the service
    if service:
        # Service; skip the broadcast if one was sent very recently (for example, after a restart loop)
        min_gap = cfg.get("schedule", {}).get("min_interval_seconds", 300)
        since = seconds_since_last_send(cfg)
        if since is not None and 0 <= since < min_gap:
            summary("skipped", reason="duplicate-guard", last_sent_s_ago=round(since))
            return
    # Build the messages
    built = build_messages(cfg, tz, next_time)
    texts, failed = built["texts"], built["failed"]
    # Nothing to send
    if not texts:
        summary("skipped", reason="nothing-to-send", failed=",".join(failed))
        return
    # Send the messages
    mcfg = cfg.get("meshtastic", {})
    send_messages(mcfg, texts, *retry_policy(cfg))
    # Record the send for the duplicate guard
    if service:
        record_send(cfg)
    # Log the summary
    summary("partial" if failed else "ok", messages=len(texts),
            bytes="/".join(str(len(t.encode("utf-8"))) for t in texts),
            time_source=built["time_source"], failed=",".join(failed))


# MODES
# dry_run function
def dry_run(cfg, tz):
    """
    DESCRIPTION
    Prints the message(s) that would be sent without transmitting. The system clock stands in if the GPS is unavailable.

    PARAMETERS
    cfg: The configuration dictionary.
    tz: The local time zone.

    RETURNS
    The process exit code: 0 if there is something to broadcast, otherwise 1.
    """
    # Build the messages
    max_bytes = cfg.get("meshtastic", {}).get("max_message_bytes", 200)
    next_time = next_run(cfg["schedule"], datetime.now(timezone.utc), tz)
    built = build_messages(cfg, tz, next_time, allow_clock_fallback=True)
    # Warn if the time did not come from the GPS
    if built["time_source"] != "GPS time":
        print("*** WARNING: GPS unavailable - time below is the SYSTEM CLOCK, not GPS ***")
    print("DRY RUN - nothing is transmitted")
    # Report failed NWS lookups
    if built["failed"]:
        print(f"(NWS lookups that failed: {', '.join(built['failed'])})")
    # Nothing to show
    texts = built["texts"]
    if not texts:
        print("(nothing to broadcast)")
        return 1
    # Iterate over the messages
    for i, text in enumerate(texts, 1):
        print(f"--- message {i} of {len(texts)} ({len(text.encode('utf-8'))}/{max_bytes} bytes) ---")
        print(text)
    # Return
    return 0


# check function
def check(cfg, tz):
    """
    DESCRIPTION
    Post-install health check of the configuration, GPS, LoRa radio and NOAA reachability. Nothing is transmitted.

    PARAMETERS
    cfg: The configuration dictionary.
    tz: The local time zone.

    RETURNS
    The number of failed checks.
    """
    # Import the radio checks
    import check_radios
    # Describe the schedule
    sched = cfg["schedule"]
    if sched.get("times"):
        schedule_text = "times " + ",".join(sched["times"])
    elif sched.get("broadcast_window"):
        window = sched["broadcast_window"]
        schedule_text = (f"every {sched['interval_minutes']} min from "
                         f"{window['start']} to {window['end']}")
    else:
        schedule_text = f"every {sched['interval_minutes']} min"
    print(f"OK   config: loaded; time zone {tz.key}; schedule {schedule_text}")
    # Check the radios
    failures = check_radios.run_checks(cfg)
    # Get the location
    loc = cfg.get("location", {})
    lat, lon = loc.get("latitude"), loc.get("longitude")
    # Check NOAA
    try:
        # Determine if the location must come from the GPS
        if lat is None or lon is None:
            # No configured location; use the GPS position
            fix = read_gpsd(cfg.get("gps", {}))
            lat, lon = fix.get("lat"), fix.get("lon")
        # Still no location
        if lat is None or lon is None:
            raise RuntimeError("no location: set location.latitude/longitude or get a GPS position fix")
        # Request every dataset
        weather = get_weather(lat, lon, cfg.get("noaa", {}), {"alerts", "hourly", "daily", "obs"},
                              (cfg.get("message", {}).get("alert_filter")), (1, 0))
        failed = weather["failed"]
        # Every lookup failed; NOAA is unreachable
        if len(failed) == 4:
            raise RuntimeError("every NWS lookup failed; check internet access, noaa.api_url and noaa.user_agent")
        # Report the result
        if failed:
            print(f"WARN NOAA: reachable, but these lookups failed: {', '.join(failed)}", file=sys.stderr)
        else:
            print(f"OK   NOAA: all lookups succeeded for {lat:.4f},{lon:.4f}")
    except Exception as exc:
        failures += 1
        print(f"FAIL NOAA: {exc}", file=sys.stderr)
    # Return
    return failures


# main function
def main(argv=None):
    """
    DESCRIPTION
    Command-line entry point. Without a mode option it runs the service loop.

    PARAMETERS
    argv: Optional list of arguments (defaults to sys.argv).

    RETURNS
    The process exit code.
    """
    # Define and parse the command-line arguments
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.json")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="broadcast once and exit")
    mode.add_argument("--dry-run", action="store_true",
                      help="interactive test: print the message(s) that would be sent, without transmitting")
    mode.add_argument("--check", action="store_true",
                      help="check config, GPS, LoRa radio and NOAA, then exit (briefly opens the radio)")
    args = ap.parse_args(argv)
    # --dry-run is for interactive use only
    if args.dry_run and not (sys.stdin.isatty() and sys.stdout.isatty()):
        ap.error("--dry-run runs only from an interactive terminal")
    # Configure logging, load the configuration and set the time zone
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config(args.config)
    tz = ZoneInfo(cfg.get("time_zone", "UTC"))
    # Determine if this is a health check
    if args.check:
        return 1 if check(cfg, tz) else 0
    # Determine if this is a dry run
    if args.dry_run:
        return dry_run(cfg, tz)
    # Determine if this is a single broadcast
    if args.once:
        broadcast_once(cfg, tz)
        return 0
    # Service loop
    while True:
        # Determine the next broadcast
        now = datetime.now(timezone.utc)
        target = next_run(cfg["schedule"], now, tz)
        log.info("next broadcast at %s", target.isoformat())
        # Sleep in short steps until the target time
        while (remaining := (target - datetime.now(timezone.utc)).total_seconds()) > 0:
            time.sleep(min(remaining, 30))
        # Broadcast; a failure is logged and the loop continues
        try:
            broadcast_once(cfg, tz, next_run(cfg["schedule"], target, tz), service=True)
        except Exception as exc:
            log.error("broadcast failed: %s", exc)
            summary("failed", error=type(exc).__name__ + ": " + str(exc))


# Entry point
if __name__ == "__main__":
    sys.exit(main())
