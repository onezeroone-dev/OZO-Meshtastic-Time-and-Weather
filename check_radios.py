#!/usr/bin/env python3
"""Verify that the GPS (via gpsd) and the Meshtastic LoRa radio are usable. Exits non-zero on failure."""
# DESCRIPTION
# Checks the GPS and the LoRa radio the same way the service uses them. Nothing is transmitted.
# Prints an OK or FAIL line for each radio, with a hint for each failure, and exits non-zero if either fails.
# EXAMPLE
# venv/bin/python check_radios.py --config config.json
# NOTES
# install.sh runs this script as the service user before it installs the service.
# This script briefly opens the radio; stop other radio clients first.

# Imports: standard library
import argparse
import socket
import sys

# Imports: local
import ozo_meshtastic_tw as app


# GpsdUnreachable exception
class GpsdUnreachable(Exception):
    """
    DESCRIPTION
    Raised when gpsd could not be contacted at all, as opposed to contacted but unable to supply a fix.
    """


# check_gps function
def check_gps(cfg):
    """
    DESCRIPTION
    Confirms that gpsd accepts connections and returns a fix that includes the time.

    PARAMETERS
    cfg: The configuration dictionary (the "gps" section is used).

    RETURNS
    Nothing. Prints an OK line on success.

    RAISES
    GpsdUnreachable if the gpsd port cannot be reached; RuntimeError if gpsd supplies no usable fix.
    """
    # Get the GPS configuration
    gcfg = cfg.get("gps", {})
    # Try to open (and immediately close) a connection to gpsd
    try:
        socket.create_connection((gcfg.get("gpsd_host", "127.0.0.1"), gcfg.get("gpsd_port", 2947)),
                                 timeout=gcfg.get("timeout_seconds", 15)).close()
    except OSError as exc:
        # gpsd could not be reached; raise a distinct exception so the hint can say why
        raise GpsdUnreachable(exc) from exc
    # gpsd is reachable; read a fix (raises if none arrives before the timeout)
    fix = app.read_gpsd(gcfg)
    # Report success
    print(f"OK   GPS: gpsd returned a fix (time {fix['time']}, mode {fix.get('mode')})")


# check_lora function
def check_lora(cfg):
    """
    DESCRIPTION
    Confirms that the Meshtastic radio can be opened and answers with its node information.

    PARAMETERS
    cfg: The configuration dictionary (the "meshtastic" section is used).

    RETURNS
    Nothing. Prints an OK line on success.

    RAISES
    Whatever the Meshtastic library raises when the radio cannot be opened.
    """
    # Open the radio (serial or TCP, per the configuration)
    iface = app.open_interface(cfg.get("meshtastic", {}))
    # Make sure the radio is always closed, even if the node query fails
    try:
        # Get the node information; an empty reply is treated as no information
        info = iface.getMyNodeInfo() or {}
        user = info.get("user", {})
        # Report success, naming the node
        print(f"OK   LoRa: connected to Meshtastic node {user.get('longName') or info.get('num')}")
    finally:
        # Close the radio so the port lock is released
        iface.close()


# gps_hint function
def gps_hint(exc, cfg):
    """
    DESCRIPTION
    Builds a plain-language hint for a failed GPS check.

    PARAMETERS
    exc: The exception raised by check_gps.
    cfg: The configuration dictionary (used to name the gpsd host and port).

    RETURNS
    A string describing the likely cause and what to try next.
    """
    # Get the GPS configuration and describe where gpsd is expected
    gcfg = cfg.get("gps", {})
    where = f"{gcfg.get('gpsd_host', '127.0.0.1')}:{gcfg.get('gpsd_port', 2947)}"
    # Determine if gpsd could not be contacted
    if isinstance(exc, GpsdUnreachable):
        # gpsd could not be contacted; determine if the connection was actively refused
        if isinstance(exc.args[0], ConnectionRefusedError):
            # Connection refused; gpsd is probably not running or not configured
            return (f"nothing is listening on gpsd at {where}. Check 'systemctl status gpsd' and that "
                    "/etc/default/gpsd points DEVICES at the GNSS port (e.g. /dev/ttyAMA0 or /dev/ttyUSB0).")
        # Some other connection problem; suggest checking the host and port
        return f"could not connect to gpsd at {where}; check gps.gpsd_host/gpsd_port and that gpsd is running."
    # gpsd was reached but did not supply a fix
    return ("gpsd is reachable but gave no 3D-quality fix with time. Make sure the GNSS antenna has a clear "
            "view of the sky, wait a few minutes after a cold start, and consider raising "
            "gps.timeout_seconds. Test with 'gpsmon' or 'cgps'.")


# lora_hint function
def lora_hint(exc, cfg):
    """
    DESCRIPTION
    Builds a plain-language hint for a failed LoRa check.

    PARAMETERS
    exc: The exception raised by check_lora.
    cfg: The configuration dictionary (used to tell serial from TCP connections).

    RETURNS
    A string describing the likely cause and what to try next.
    """
    # Get the Meshtastic configuration
    mcfg = cfg.get("meshtastic", {})
    # Determine if the radio is reached through meshtasticd over TCP
    if mcfg.get("connection", "serial") == "tcp":
        # TCP connection; the problem is meshtasticd or the host name
        return (f"could not reach meshtasticd at {mcfg.get('tcp_host', 'localhost')}; check that it is "
                "running ('systemctl status meshtasticd') and meshtastic.tcp_host.")
    # Serial connection; determine if the failure was a permissions problem
    if isinstance(exc, PermissionError):
        # Permission denied; the service user is missing from dialout
        return "permission denied on the serial port; the service user must be in the 'dialout' group."
    # Any other serial failure; name the configured port in the hint
    port = mcfg.get("serial_port") or "autodetect"
    return (f"no usable Meshtastic serial device (serial_port: {port}). Check the radio is connected and "
            "powered, that its port exists (ls /dev/ttyUSB* /dev/ttyACM* /dev/ttyAMA*), and set "
            "meshtastic.serial_port in config.json. If you use meshtasticd, set meshtastic.connection to 'tcp'.")


# run_checks function
def run_checks(cfg):
    """
    DESCRIPTION
    Runs the GPS and LoRa checks, printing the result of each. Both checks always run so every problem is reported.

    PARAMETERS
    cfg: The configuration dictionary.

    RETURNS
    The number of failed checks (0 when both radios are usable).

    NOTES
    Also called by "ozo_meshtastic_tw.py --check".
    """
    # Control variable
    failures = 0
    # Iterate over the checks, each paired with its hint function
    for name, check, hint in (("GPS", check_gps, gps_hint), ("LoRa", check_lora, lora_hint)):
        # Run the check
        try:
            check(cfg)
        except (Exception, SystemExit) as exc:
            # The check failed; count it and report the hint and the underlying error
            if name == "GPS" and app.has_fixed_location(cfg):
                # A fixed location makes the GPS optional; the service falls back to the system clock
                print(f"WARN GPS: unavailable, but a fixed location is configured so the service will use the "
                      f"SYSTEM CLOCK. ({exc or type(exc).__name__})", file=sys.stderr)
                continue
            failures += 1
            print(f"FAIL {name}: {hint(exc, cfg)}", file=sys.stderr)
            print(f"     (details: {exc or type(exc).__name__})", file=sys.stderr)
    # Return
    return failures


# main function
def main(argv=None):
    """
    DESCRIPTION
    Command-line entry point.

    PARAMETERS
    argv: Optional list of arguments (defaults to sys.argv).

    RETURNS
    The process exit code: 0 if both radios are usable, otherwise 1.
    """
    # Define and parse the command-line arguments
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args(argv)
    # Load the configuration, run the checks, and convert the failure count to an exit code
    return 1 if run_checks(app.load_config(args.config)) else 0


# Entry point
if __name__ == "__main__":
    sys.exit(main())
