#!/usr/bin/env python3
"""Verify that the GPS (via gpsd) and the Meshtastic LoRa radio are usable. Exits non-zero on failure."""
import argparse
import socket
import sys

import ozo_meshtastic_tw as app


class GpsdUnreachable(Exception):
    """gpsd could not be contacted at all (as opposed to contacted but no fix)."""


def check_gps(cfg):
    gcfg = cfg.get("gps", {})
    try:
        socket.create_connection((gcfg.get("gpsd_host", "127.0.0.1"), gcfg.get("gpsd_port", 2947)),
                                 timeout=gcfg.get("timeout_seconds", 15)).close()
    except OSError as exc:
        raise GpsdUnreachable(exc) from exc
    fix = app.read_gpsd(gcfg)
    print(f"OK   GPS: gpsd returned a fix (time {fix['time']}, mode {fix.get('mode')})")


def check_lora(cfg):
    iface = app.open_interface(cfg.get("meshtastic", {}))
    try:
        info = iface.getMyNodeInfo() or {}
        user = info.get("user", {})
        print(f"OK   LoRa: connected to Meshtastic node {user.get('longName') or info.get('num')}")
    finally:
        iface.close()


def gps_hint(exc, cfg):
    gcfg = cfg.get("gps", {})
    where = f"{gcfg.get('gpsd_host', '127.0.0.1')}:{gcfg.get('gpsd_port', 2947)}"
    if isinstance(exc, GpsdUnreachable):
        if isinstance(exc.args[0], ConnectionRefusedError):
            return (f"nothing is listening on gpsd at {where}. Check 'systemctl status gpsd' and that "
                    "/etc/default/gpsd points DEVICES at the GNSS port (e.g. /dev/ttyAMA0 or /dev/ttyUSB0).")
        return f"could not connect to gpsd at {where}; check gps.gpsd_host/gpsd_port and that gpsd is running."
    return ("gpsd is reachable but gave no 3D-quality fix with time. Make sure the GNSS antenna has a clear "
            "view of the sky, wait a few minutes after a cold start, and consider raising "
            "gps.timeout_seconds. Test with 'gpsmon' or 'cgps'.")


def lora_hint(exc, cfg):
    mcfg = cfg.get("meshtastic", {})
    if mcfg.get("connection", "serial") == "tcp":
        return (f"could not reach meshtasticd at {mcfg.get('tcp_host', 'localhost')}; check that it is "
                "running ('systemctl status meshtasticd') and meshtastic.tcp_host.")
    if isinstance(exc, PermissionError):
        return "permission denied on the serial port; the service user must be in the 'dialout' group."
    port = mcfg.get("serial_port") or "autodetect"
    return (f"no usable Meshtastic serial device (serial_port: {port}). Check the radio is connected and "
            "powered, that its port exists (ls /dev/ttyUSB* /dev/ttyACM* /dev/ttyAMA*), and set "
            "meshtastic.serial_port in config.json. If you use meshtasticd, set meshtastic.connection to 'tcp'.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args(argv)
    cfg = app.load_config(args.config)
    failures = 0
    for name, check, hint in (("GPS", check_gps, gps_hint), ("LoRa", check_lora, lora_hint)):
        try:
            check(cfg)
        except (Exception, SystemExit) as exc:
            failures += 1
            print(f"FAIL {name}: {hint(exc, cfg)}", file=sys.stderr)
            print(f"     (details: {exc or type(exc).__name__})", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
