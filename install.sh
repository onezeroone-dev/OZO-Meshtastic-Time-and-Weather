#!/usr/bin/env bash
# Install as a systemd service on Debian-based Linux. Run with sudo.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/ozo-meshtastic-tw
CONF=/etc/ozo-meshtastic-tw

apt-get update
apt-get install -y python3 python3-venv gpsd
id ozo-meshtastic >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin -G dialout ozo-meshtastic
install -d "$DEST" "$CONF"
install -m 0644 "$SRC/ozo_meshtastic_tw.py" "$SRC/check_radios.py" "$SRC/requirements.txt" "$DEST/"
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -r "$DEST/requirements.txt"
if [ ! -f "$CONF/config.json" ]; then
  install -m 0640 -o root -g ozo-meshtastic "$SRC/config.example.json" "$CONF/config.json"
fi
# Verify GPS and LoRa access as the service user; abort before installing the service if it fails.
if ! runuser -u ozo-meshtastic -- "$DEST/venv/bin/python" "$DEST/check_radios.py" --config "$CONF/config.json"; then
  echo "Radio check failed; the service was not installed. Fix the problem (see above) and re-run." >&2
  exit 1
fi
install -m 0644 "$SRC/systemd/ozo-meshtastic-tw.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable ozo-meshtastic-tw.service
echo "Edit $CONF/config.json, then: systemctl start ozo-meshtastic-tw"
