#!/usr/bin/env bash
# Reverse install.sh. Run with sudo. Pass --purge to also delete /etc/ozo-meshtastic-tw (your config).
# The apt packages (python3, python3-venv, gpsd) are left installed; other software may depend on them.
set -euo pipefail
DEST=/opt/ozo-meshtastic-tw
CONF=/etc/ozo-meshtastic-tw
UNIT=ozo-meshtastic-tw.service

PURGE=0
case "${1:-}" in
  "") ;;
  --purge) PURGE=1 ;;
  *) echo "Usage: $0 [--purge]" >&2; exit 2 ;;
esac

if systemctl list-unit-files "$UNIT" >/dev/null 2>&1; then
  systemctl disable --now "$UNIT" 2>/dev/null || true
fi
rm -f "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl reset-failed "$UNIT" 2>/dev/null || true

rm -rf "$DEST" /var/lib/ozo-meshtastic-tw
if id ozo-meshtastic >/dev/null 2>&1; then
  userdel ozo-meshtastic
fi

if [ "$PURGE" -eq 1 ]; then
  rm -rf "$CONF"
  echo "Uninstalled, including $CONF."
else
  echo "Uninstalled. Kept $CONF (re-run with --purge to delete it)."
fi
