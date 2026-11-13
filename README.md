# OZO Meshtastic Time and Weather

## Description
A service for a Linux host that reads the current time from GPS, fetches local weather from the NOAA/NWS API, and broadcasts both on the local Meshtastic mesh on a schedule. If GPS time is unavailable and both fixed coordinates are configured, it falls back to the system clock and uses the fixed location.

**Compatibility.** The Python service should work on Linux systems that support its Python dependencies and have compatible hardware configured. It gets GPS data from any receiver supported by `gpsd` (the service connects to `gpsd` over TCP and never opens the GPS device). For LoRa, `meshtastic.connection: serial` connects to a self-contained Meshtastic device with firmware over serial/USB. Alternatively, `meshtastic.connection: tcp` connects to [`meshtasticd`](https://meshtastic.org/docs/software/linux/), which runs the Meshtastic node software on the Linux host and can control supported SPI or USB radios directly; the radio module itself does not need Meshtastic firmware. Hardware and pinouts must be supported by `meshtasticd`; see its [hardware documentation](https://meshtastic.org/docs/meshtasticd/hardware/). The included `install.sh` is specifically for Debian-based systems that use `apt` and `systemd`; other Linux distributions may need adapted installation steps.

**Region.** Weather and alerts come from the NOAA/NWS API, which covers the United States and its territories only. Elsewhere the weather fields are unavailable; the time, sun and moon fields still work.

Contents: [Prerequisites](#prerequisites-and-hardware-setup) · [USB/Serial Radios](#usbserial-radios) · [SPI Radios](#spi-radios) · [Configuration](#configuration) · [Example output](#example-output) · [Run manually](#run-manually-venv) · [Install](#install-as-a-service) · [Upgrading](#upgrading) · [Checking the radios](#checking-the-radios-check_radiospy) · [Device access](#device-access-and-locking) · [Tested hardware](#hardware-and-software-testing) · [Troubleshooting](#logging-and-troubleshooting) · [Uninstall](#uninstall) · [Limitations](#limitations) · [Mesh etiquette](#mesh-etiquette-and-airtime) · [Security](#security-notes) · [Development](#development-notes) · [License](#license) · [Attribution](#attribution)

## Prerequisites and hardware setup
Complete these before running `install.sh`. Device names and setup steps depend on your Linux distribution and hardware; adapt the examples.
1. **GNSS serial port (optional).** If using a GNSS receiver on the host UART, enable that serial port and disable any login shell using it. Follow the host's instructions and reboot if required. A receiver is optional when both fixed coordinates are configured.
2. **gpsd (if using GNSS).** Install and configure `gpsd` for your distribution. On Debian-based systems, `install.sh` installs the package. Point gpsd at the discovered GNSS device, for example a UART or USB-to-UART device. On distributions using `/etc/default/gpsd`, `GPSD_OPTIONS="-n"` starts polling without waiting for a client. Restart gpsd and confirm with `gpsmon` or `cgps` that it reports a 3D fix and UTC time. Use a clear view of the sky; a first fix can take several minutes.
3. **Meshtastic radio.** Choose the setup matching how the radio connects: a self-contained Meshtastic node with firmware over USB serial, or a supported SPI radio controlled by `meshtasticd`. See [USB/Serial Radios](#usbserial-radios) and [SPI Radios](#spi-radios) below.
4. **Internet access** to `api.weather.gov`.
5. **Time zone and config.** Set your `time_zone`, coordinates and NOAA contact information (see Configuration).

The radio setup examples below use the Meshtastic CLI, which can be installed in a separate virtual environment:

    python3 -m venv ~/meshtastic-cli-venv
    ~/meshtastic-cli-venv/bin/pip install "meshtastic[cli]"
    . ~/meshtastic-cli-venv/bin/activate

The CLI must have access to the serial device or daemon. Its `--info` output can include private keys; do not share the full output publicly.

## USB/Serial Radios

Use this setup for a self-contained Meshtastic device that runs Meshtastic firmware and appears as a serial device over USB. **This connection does not require `meshtasticd` or a YAML file under `/etc/meshtasticd/config.d/`.**

Connect the device and list stable USB serial names:

    ls -l /dev/serial/by-id/

If that directory has no matching entry, compare `ls /dev/ttyACM* /dev/ttyUSB*` before and after connecting the device. Prefer a `/dev/serial/by-id/...` path in the config when one is available, since a numbered `/dev/ttyACM*` path can change.

Set `meshtastic.connection` to `serial` and `meshtastic.serial_port` to the discovered device path. Ensure the service can access it; the systemd unit runs with group `dialout`. For example:

    "connection": "serial",
    "serial_port": "/dev/ttyACM0"

Configure the node with the Meshtastic CLI, using the same port for each command (replace it with your discovered path). These are persistent node settings, separate from the OZO config:

- Set the region required for your location. A new node with region `UNSET` cannot communicate over LoRa. The node reboots after changing region; wait for it to reconnect. See the [Meshtastic region list](https://meshtastic.org/docs/configuration/radio/lora/#region).

      meshtastic --port /dev/ttyACM0 --set lora.region US
      meshtastic --port /dev/ttyACM0 --info | grep '"region"'

- Set the long and short names; the short name is up to four characters:

      meshtastic --port /dev/ttyACM0 --set-owner "Weather Station"
      meshtastic --port /dev/ttyACM0 --set-owner-short "WX01"

- Configure the public primary LongFast channel at index 0. `default` is the publicly known default key; `--ch-longfast` sets the modem preset:

      meshtastic --port /dev/ttyACM0 --ch-set name LongFast --ch-index 0
      meshtastic --port /dev/ttyACM0 --ch-set psk default --ch-index 0
      meshtastic --port /dev/ttyACM0 --ch-longfast

The LoRa region, channel key and modem preset must match the other mesh nodes. Set `channel_index` to `0` in this service config to use the primary channel. Check settings with `meshtastic --port /dev/ttyACM0 --info` before testing. Choose a node role appropriate for the hardware and network.

## SPI Radios

Use this setup when the Linux host directly controls an SPI-attached radio. In this arrangement `meshtasticd` runs the Meshtastic node software on the host; the radio module itself does not need Meshtastic firmware. Configure this service with `meshtastic.connection: tcp` and the `tcp_host` of the daemon (typically `localhost` when they run on the same host). No USB serial port is used by the OZO service.

Enable SPI and configure the radio's SPI, GPIO and other required pins. Install `meshtasticd` for your distribution. Find the preset that matches the exact radio board and pinout in `/etc/meshtasticd/available.d`, then copy that preset to `/etc/meshtasticd/config.d` as described in the [meshtasticd configuration guide](https://meshtastic.org/docs/meshtasticd/usage/). Do not select a preset based only on the radio chip; a mismatched pinout will not work. Start the daemon and confirm its TCP API is reachable on the default port 4403.

Configure the node through the Meshtastic CLI connected to the daemon. These are persistent node settings, separate from the OZO config:

- Set the region required for your location. A new node with region `UNSET` cannot communicate over LoRa. The node reboots after changing region; wait for it to reconnect. See the [Meshtastic region list](https://meshtastic.org/docs/configuration/radio/lora/#region).

      meshtastic --host localhost --set lora.region US
      meshtastic --host localhost --info | grep '"region"'

- Set the long and short names; the short name is up to four characters:

      meshtastic --host localhost --set-owner "Weather Station"
      meshtastic --host localhost --set-owner-short "WX01"

- Configure the public primary LongFast channel at index 0. `default` is the publicly known default key; `--ch-longfast` sets the modem preset:

      meshtastic --host localhost --ch-set name LongFast --ch-index 0
      meshtastic --host localhost --ch-set psk default --ch-index 0
      meshtastic --host localhost --ch-longfast

The LoRa region, channel key and modem preset must match the other mesh nodes. Set `channel_index` to `0` in this service config to use the primary channel. Check settings with `meshtastic --host localhost --info` before testing. Only set a role such as `CLIENT_MUTE` when appropriate for the specific radio hardware and network behavior.

## Configuration
Copy `config.example.json` to `config.json`:
- `meshtastic`: `connection` (`serial` for a self-contained Meshtastic device or `tcp` for `meshtasticd`; the example uses serial), `serial_port` (null = autodetect; set the discovered port for predictable selection), `tcp_host`, `channel_index`, `max_message_bytes` (UTF-8 byte limit; default 200), and `post_send_delay_seconds` (default 5; a non-negative number of seconds to keep the connection open after the last submitted message; set to 0 for no delay).

  The post-send delay gives the radio time to queue messages before disconnecting; in some setups this may help with delayed delivery. It is a precaution, not a confirmed fix or a guarantee of queueing or delivery. The radio connection remains occupied during the delay. Existing configurations that omit the setting also use the five-second default.
- `gps`: gpsd host/port and fix timeout. The physical GNSS serial device is configured in gpsd, not this file.
- `location`: fixed latitude/longitude take precedence over GPS position for weather and sun calculations. The example contains approximate downtown Denver coordinates: replace both with your own, or set both to `null` to use the GPS position. **Setting both also makes GPS time optional**: if gpsd is unreachable or has no fix, the service broadcasts using the system clock (the time-sync field reads `SYSTEM CLOCK-NOT GPS`). Keep the system clock accurate, for example with NTP; `--check` reports the missing GPS as a warning rather than a failure.
- `noaa`: `api_url` (base URL for the NOAA/NWS API), `user_agent` (NOAA asks for a contact string), and optional `api_key` (sent as `X-Api-Key`; the API does not currently require one).
- `message`: controls the broadcast content.
  - `fields`: ordered list of parts to include (remove or reorder to taste). Valid names: `time`, `time_sync` (shows "GPS time" when the time comes from the GPS), `conditions` (current forecast hour: sky, temperature, wind), `precip` (chance of precipitation), `high_low` (high and low from the daily forecast; after the day's high period has passed this may be tomorrow's), `observation` (latest station observation: temperature, humidity, wind, pressure in hPa with a rising/falling trend when available), `outlook` (next forecast period), `sun` (sunrise and sunset), `moon` (phase and percent illuminated), `next_broadcast` (local time of the next scheduled broadcast; omitted with `--once`).
  - `alerts`: `separate` (default; active NWS alerts are sent as their own second message), `inline` (alert text goes at the front of the main message), or `off`.
  - `compact`: `true` (default) uses abbreviations to save bytes; `false` uses full words.
  - `alert_filter`: limits which NWS alerts are broadcast. `min_severity` (`Minor`, `Moderate`, `Severe` or `Extreme`; alerts with an unknown severity are always kept), `include` (if non-empty, only these event names, e.g. `"Tornado Warning"`) and `exclude` (event names to drop, e.g. `"Special Weather Statement"`). Event names are matched case-insensitively.
  - `max_observation_age_minutes` (default 120; 0 disables): an observation older than this is omitted rather than shown as current. Hourly forecast periods that have already ended are skipped for the same reason.
  - Parts are added in the listed order as whole units; any part that would exceed `meshtastic.max_message_bytes` is skipped, so the message is never cut mid-field. Sun and moon values are calculated locally. Weather values are in US units (°F, mph). Each NWS lookup fails independently, so a partial outage still yields a partial message. Only the lookups needed by the chosen fields are made.
- `schedule`: `times` (list of local `HH:MM`) takes precedence when non-empty; otherwise `interval_minutes` controls repeats. With `broadcast_window` set, its `start` and `end` are local `HH:MM` times; the first broadcast is at `start`, then repeats every `interval_minutes` until (but not including) `end`. For example, `06:00`–`22:00` hourly schedules 06:00 through 21:00. Windows may cross midnight, such as `18:00`–`02:00`. If the service starts within the window, it waits until the next interval slot; outside the window, it waits for the next start. Missed broadcasts are not replayed. If no window is configured, interval scheduling remains relative to service startup. `min_interval_seconds` (default 300) is a duplicate guard: the service will not broadcast again within this many seconds of the last successful broadcast, even if it is restarted (the timestamp is kept in the systemd state directory, `/var/lib/ozo-meshtastic-tw`, or in `state_file` if you set one).
- `retry`: `attempts` (default 3) and `delay_seconds` (default 5) for GPS reads, NWS requests and radio sends. A failed send reconnects and resends only the messages not yet submitted to the radio; a submission does not confirm delivery.
- `time_zone`: IANA zone for the displayed time, e.g. `America/Denver` (see the [list of tz database time zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) for valid names).

## Example output
With the default fields and `compact: true`, a broadcast looks like this (illustrative values):

    2026-11-03 10:48 MST | GPS time | Pt Cldy 52F wind NW 5mph | PoP 10% | H58/L34 | obs 50F RH41% G12 1015hPa steady | Tonight: Mstly Clear | sun 06:30-16:56 | moon Last Qtr 32% | next 11:48

and, when there is an active alert and `alerts` is `separate`, a second message follows a few seconds later:

    ALERT: Winter Storm Warning, Wind Advisory

Legend (compact form): `Pt`/`Mstly`/`Cldy`/`Chc`/`Lkly`/`Sct`/`Iso` are Partly/Mostly/Cloudy/Chance/Likely/Scattered/Isolated; `wind NW 5mph` is the forecast wind; `PoP` is the chance of precipitation; `H`/`L` are the forecast high and low (°F); `obs` is the latest station observation: temperature, relative humidity (`RH`), gust speed (`G`, mph), pressure with a `rising`/`falling`/`steady` trend; `sun` is sunrise-sunset in local time; `moon` is the phase and percent illuminated; `next` is the next scheduled broadcast. Fields are separated by ` | `. With `compact: false` the same information is spelled out in words. Fields that do not fit in `max_message_bytes` are left out, so real messages are often shorter than this example; use `--dry-run` to see yours.

## Run manually (venv)

    python3 -m venv venv && venv/bin/pip install -r requirements.txt
    venv/bin/python ozo_meshtastic_tw.py --config config.json --once

This is for running from a checkout, with a `config.json` you copied from `config.example.json` in the current directory. For an installed service, see "Install as a service" for the `/opt` and `/etc` paths.

Options (mutually exclusive):
- `--once`: broadcast once and exit. Ignores the schedule and duplicate guard.
- `--dry-run`: from an interactive terminal, ignore the schedule, build the message(s) now and print each with its byte count instead of transmitting. It never opens the LoRa radio. If GPS time is unavailable it falls back to the system clock and prints a prominent warning (the time-sync field then reads `SYSTEM CLOCK-NOT GPS`). Failed NWS lookups are reported. It refuses to run when not attached to a terminal.
- `--check`: post-install health check of the config, GPS, LoRa radio and NOAA reachability. It briefly opens the radio, like `check_radios.py`; missing GPS is a warning if both fixed coordinates are set. Failed checks result in a non-zero exit.

## Install as a service
    sudo ./install.sh
The installer is for Debian-based Linux systems using `apt` and `systemd`. It copies the code, creates a virtual environment and runs `check_radios.py` as the service user before installing the unit. It aborts before installing the unit if the Meshtastic radio cannot be reached, or if GPS is unavailable and both fixed coordinates are not set. A missing GPS is a warning when both coordinates are set. It uses `/etc/ozo-meshtastic-tw/config.json`, created from the example if absent; if a check fails, update that file and re-run the installer. Existing configuration is not overwritten.

The installed layout is: code and venv in `/opt/ozo-meshtastic-tw`, configuration in `/etc/ozo-meshtastic-tw/config.json` (not in `/opt`). A newly installed config is owned by `root:ozo-meshtastic` with mode `0640`. The unit runs as user `ozo-meshtastic`, with primary group `dialout` for serial access and supplementary group `ozo-meshtastic` for config access. `--config` defaults to a `config.json` in the current directory, so pass the installed path explicitly. With the service stopped (the checks open the radio):

    sudo systemctl stop ozo-meshtastic-tw
    sudo -u ozo-meshtastic /opt/ozo-meshtastic-tw/venv/bin/python /opt/ozo-meshtastic-tw/ozo_meshtastic_tw.py --config /etc/ozo-meshtastic-tw/config.json --check

`--dry-run` needs an interactive terminal and may not work under `sudo -u`; if it refuses, run it with `sudo` instead (`sudo /opt/ozo-meshtastic-tw/venv/bin/python /opt/ozo-meshtastic-tw/ozo_meshtastic_tw.py --config /etc/ozo-meshtastic-tw/config.json --dry-run`).

Edit `/etc/ozo-meshtastic-tw/config.json` (`sudo nano`), especially the location, time zone and NOAA contact, then `sudo systemctl start ozo-meshtastic-tw`. The installer enables the unit at boot but does not start it. Check the service status and journal after starting it; the installer check runs as the same user but does not reproduce the unit's group settings.
Logs: `journalctl -u ozo-meshtastic-tw`. Each broadcast cycle ends with one summary line you can grep, e.g. `journalctl -u ozo-meshtastic-tw | grep 'broadcast summary'`: `status=ok|partial|skipped|failed` plus message count, byte sizes, time source and any failed NWS lookups (`partial` means some lookups failed). The unit file is `systemd/ozo-meshtastic-tw.service`.

OZO does not wait for an acknowledgment, and `status=ok` does not confirm that another node received the message; verify reception on a second node.

## Upgrading
Stop the service with `sudo systemctl stop ozo-meshtastic-tw` and close other radio clients before upgrading, because the installer checks the radio. Download the [latest release](https://github.com/onezeroone-dev/OZO-Meshtastic-Time-and-Weather/releases) and re-run `sudo ./install.sh`. It refreshes the code and Python dependencies in `/opt/ozo-meshtastic-tw` and keeps your existing `/etc/ozo-meshtastic-tw/config.json`, so compare it with the new `config.example.json` for added settings (missing optional keys use defaults). After a successful install, start the service with `sudo systemctl start ozo-meshtastic-tw` and check its journal.

## Checking the radios (`check_radios.py`)
`check_radios.py` checks GPS and the Meshtastic radio using the same connection code and settings as the service. It transmits nothing.
- **GPS:** connects to gpsd (`gps.gpsd_host`/`gpsd_port`) and waits up to `gps.timeout_seconds` for a fix with a valid time.
- **LoRa:** opens the Meshtastic radio (serial or `meshtasticd` TCP), reads the node's name, and closes it.

Run it from the repo directory, pointing at the config the service will use:

    venv/bin/python check_radios.py --config config.json

It prints `OK` lines on success. If GPS is unavailable with both fixed coordinates set, it prints `WARN GPS` and continues; otherwise a GPS failure is reported as `FAIL`. A failed radio check also prints `FAIL` with a hint and the underlying error. Both checks always run, and the script exits with status 1 if either required check fails.

When to use it:
- Before installing or after changing hardware, ports or `config.json`. `install.sh` runs it automatically and stops on failure.
- When broadcasts stop working, to separate a radio problem from a weather or config problem. For a broader check that also tests the config and NOAA, use `ozo_meshtastic_tw.py --check`.

Guidance:
- Stop other radio clients first (Meshtastic CLI or app, monitors); the serial port is locked while the check runs. See "Device access and locking".
- The GPS check needs a real fix. Indoors it may time out; test near a window or raise `gps.timeout_seconds`. `gpsmon` or `cgps` show gpsd's view independently.
- Run it as the service user (the installer does) to check that user's access: `sudo -u ozo-meshtastic /opt/ozo-meshtastic-tw/venv/bin/python /opt/ozo-meshtastic-tw/check_radios.py --config /etc/ozo-meshtastic-tw/config.json`. A successful manual check does not guarantee that the systemd unit has the same groups; check service startup too.

## Device access and locking
- **GPS:** the service never opens the GPS device. It connects to `gpsd` over TCP (default port 2947) as a client, so other gpsd clients (`cgps`, `gpsmon`, etc.) can run at the same time.
- **LoRa (serial mode):** the Meshtastic library opens the serial port with an exclusive lock (`flock`). The service holds it while broadcasting: it connects, sends the message or messages, waits for `post_send_delay_seconds`, and disconnects. Between broadcasts the port is free.
- **Conflicts:** if another program (Meshtastic CLI or app, a monitor, etc.) has the port open when a broadcast fires, the send fails with "Could not exclusively lock port". It is retried per the `retry` setting; if every attempt fails it is logged and the next scheduled broadcast tries again. Likewise, a tool you start during a broadcast will fail to open the port for a few seconds.
- **Always-on clients:** the USB/serial connection is exclusive. If another client must keep a radio open continuously, use a radio and hardware setup supported by `meshtasticd`, then configure this service to connect to its TCP API.
- **Separate ports:** `gpsd` and the Meshtastic serial port must use different device nodes. If more than one Meshtastic device is attached, set `meshtastic.serial_port` explicitly rather than relying on autodetect.
- `install.sh` runs `check_radios.py` before installing the service, so it briefly opens the radio at that time; stop other radio clients before installing.

## Hardware and Software Testing
For best results, use [Meshtastic-supported hardware](https://meshtastic.org/docs/hardware/devices/). The following hardware and software combinations have been tested with varying results. Reports are welcome.

### Compute
|Computer|Operating System|Recommended|Comments|
|--------|----------------|-----------|--------|
|Raspberry Pi Zero W v1.1|Raspberry Pi OS (32-bit, Debian Trixie)|Yes||


### Meshtastic/LoRa
|Device|Connection|Requires `meshtasticd`|Recommended|Comments|
|------|----------|----------------------|-----------|--------|
|[Waveshare SX1262 LoRa/GNSS HAT](https://www.waveshare.com/wiki/SX1262_XXXM_LoRaWAN/GNSS_HAT)|SPI|Yes|No|Meshtastic's [Linux hardware notes](https://meshtastic.org/docs/meshtasticd/hardware/) caution that this HAT is not recommended for deployment because of hardware limitations that may affect longer messages. In testing, `check_radios.py` and `--check` passed and `meshtasticd` accepted broadcasts (`status=ok`), but only a 40-character plain text message reached a second node. Messages of 80 characters or more (including this service's roughly 170-byte broadcasts) and NodeInfo packets did not, and the daemon logged `error=-7` receive failures. A successful submission does not confirm reception; this HAT is not considered suitable for this service.|
|[Seeed XIAO nRF52840 & Wio-SX1262 Meshtastic Kit](https://wiki.seeedstudio.com/xiao_nrf52840&_wio_SX1262_kit_for_meshtastic/)|USB/Serial|No|Yes|Manual and scheduled broadcasts reached a second node. Earlier tests showed intermittent delays; subsequent tests using a five-second post-send delay arrived promptly, but do not establish its cause or guarantee delivery.|

### GPS
|Device|Connection|Recommended|Comments|
|------|----------|-----------|--------|
|[Waveshare SX1262 LoRa/GNSS HAT](https://www.waveshare.com/wiki/SX1262_XXXM_LoRaWAN/GNSS_HAT)|UART|No|The GNSS receiver worked with `gpsd`, but the included LoRa radio is not recommended for this service. See the Meshtastic/LoRa table above.|
|[Waveshare L76K GPS HAT](https://www.waveshare.com/wiki/L76K_GPS_HAT)|UART|Yes|A GPS fix and GPS-time broadcast were verified. Use its selector jumper to choose the connection and identify the resulting device path.|

## Logging and troubleshooting
**How logging works.** The service writes plain-text lines to stderr, which systemd captures in the journal. Each line starts with its level (`INFO`, `WARNING` or `ERROR`). The level is part of the message text, not the journal priority, so `journalctl -p warning` will not filter them; use grep instead:

    journalctl -u ozo-meshtastic-tw -f                           # follow live
    journalctl -u ozo-meshtastic-tw --since today | grep -E 'WARNING|ERROR'
    journalctl -u ozo-meshtastic-tw | grep 'broadcast summary'   # one line per cycle

- `INFO`: normal activity: `next broadcast at ...`, `broadcast: <text>` for each message sent, and the `broadcast summary` line.
- `WARNING`: something went wrong but the service carried on: a retried or failed GPS/NWS/radio call, a missing NWS dataset, an omitted stale observation, GPS unavailable, or an unwritable state file.
- `ERROR`: `broadcast failed: ...`: the whole cycle failed. The service keeps running and tries again at the next scheduled time.
- Startup problems (bad config, unknown time zone) end the process with a Python traceback; systemd restarts it every 30 seconds, so the same error repeats in the journal until you fix it. Check `systemctl status ozo-meshtastic-tw`.

**Reading the summary line:** `status=ok` (all messages submitted), `partial` (messages submitted, but some NWS lookups failed; see `failed=`), `skipped` (`reason=duplicate-guard` or `nothing-to-send`; expected, not a fault), `failed` (the cycle failed; some messages may already have been submitted; see `error=`). Submission does not confirm reception.

**First steps for any problem**
1. `sudo systemctl status ozo-meshtastic-tw` and the recent journal.
2. Stop the service (`sudo systemctl stop ozo-meshtastic-tw`) and run the checks as the service user: `check_radios.py` (GPS and LoRa) and `ozo_meshtastic_tw.py --check` (adds config and NOAA). Both print hints.
3. Run `ozo_meshtastic_tw.py --dry-run` from a terminal to see exactly what would be sent and which lookups fail. It does not use the radio.

**Common symptoms**

| Log message or symptom | Likely cause and fix |
|---|---|
| Startup `PermissionError` opening `/etc/ozo-meshtastic-tw/config.json` | Stop the service to end the restart loop. Check the installed unit's groups with `systemctl show ozo-meshtastic-tw -p User -p Group -p SupplementaryGroups` and inspect path permissions with `namei -l /etc/ozo-meshtastic-tw/config.json`. A newly installed config is `root:ozo-meshtastic` mode `0640`; the unit needs `SupplementaryGroups=ozo-meshtastic` as well as `Group=dialout`. A manual `sudo -u` check can pass with different group settings. Reinstall the current unit, check for overrides with `systemctl cat ozo-meshtastic-tw`, and restart it. |
| `GPS unavailable (...); using SYSTEM CLOCK` (time field reads `SYSTEM CLOCK-NOT GPS`) | gpsd is stopped or has no fix, and a fixed `location` is set (or this is `--dry-run`), so the system clock was used. Without a fixed location a live broadcast is skipped instead. Check `systemctl status gpsd`, `gpsmon`/`cgps`, antenna view, `gps.*` settings. |
| `radio send failed (attempt n/m)` with "Could not exclusively lock port" | Another program has the serial port. Close it, or use `meshtasticd` with `connection: tcp`. See "Device access and locking". |
| `radio send failed` with "Permission denied" | The service user is not in `dialout`, or the wrong `serial_port` is set. |
| `... lookup failed: ...` (alerts, hourly, daily, obs) | NWS unreachable or erroring. Check internet access, `noaa.api_url` and `noaa.user_agent`. HTTP 4xx errors (other than 429) are not retried; NWS is occasionally down, and the next cycle usually recovers. Fields for failed lookups are left out (`status=partial`). |
| `observation is N min old (limit M); omitting it` | The nearest station has stopped reporting. Raise `message.max_observation_age_minutes` or drop the `observation` field. |
| `weather lookup failed: no location available` | No `location` set and no GPS position. Set `location.latitude`/`longitude` or fix the GPS. |
| `could not write state file ...` | The state directory is not writable. The duplicate guard is then ineffective. Check `/var/lib/ozo-meshtastic-tw` ownership, or set `state_file`. |
| Nothing is sent, `status=skipped` | Expected if there is no message content or the duplicate guard is active. With `broadcast_window`, the service waits for the next scheduled slot outside the window. |
| Message missing fields | Fields that would exceed `meshtastic.max_message_bytes` are skipped whole; use `--dry-run` to see byte counts, shorten the field list, or enable `compact`. |
| No log lines at all | Service not running. Check `systemctl status`, and `systemctl is-enabled ozo-meshtastic-tw`. |

After changing `config.json`, restart with `sudo systemctl restart ozo-meshtastic-tw`.

## Uninstall
    sudo ./uninstall.sh [--purge]
Stops and removes the service, `/opt/ozo-meshtastic-tw`, and the `ozo-meshtastic` user. Your `/etc/ozo-meshtastic-tw/config.json` is kept unless you pass `--purge`. The apt packages installed by `install.sh` (`python3`, `python3-venv`, `gpsd`) are left in place.

## Limitations
- **US coverage only.** The NOAA/NWS API serves the United States and its territories; elsewhere the weather lookups fail (the time, sun and moon fields still work).
- **Message size.** Messages are limited to `meshtastic.max_message_bytes` (default 200); fields that do not fit are dropped whole, and a single alert message longer than the limit is truncated.
- **Forecast, not measurement.** Conditions come from NWS gridded forecasts, and the observation is from the nearest reporting station, which may be some distance away.
- **System clock.** The schedule and duplicate guard use the system clock. A system without a battery-backed RTC that boots offline may have a wrong system clock until it syncs.
- **Sun and moon** are calculated locally with standard approximations: sunrise and sunset are accurate to within about a couple of minutes, moon phase to the nearest eighth of the cycle.
- **Fields are US units** (°F, mph, hPa).
- **Hardware and delivery vary.** See the tested-hardware tables for results and limitations. Run `check_radios.py`, `--check` and `--dry-run`, then confirm a real broadcast on a second node before relying on a new setup.

## Mesh etiquette and airtime
LoRa airtime is shared by every node on the channel, and every broadcast is heard by all of them. Use a sensible interval (the example config broadcasts hourly from 06:00 through 21:00; avoid going much faster), consider a dedicated channel or a long interval on busy meshes, and keep `fields` lean. Each message is sent once with a short pause between messages. Use `schedule.broadcast_window` to restrict broadcasts to certain hours, and follow the norms of your local mesh community.

## Security notes
- `/etc/ozo-meshtastic-tw/config.json` is installed root-owned with mode `0640`, readable by the service group only. It may contain the optional NOAA `api_key`.
- The service runs as the unprivileged `ozo-meshtastic` user, with `dialout` as its primary group for serial access and `ozo-meshtastic` as a supplementary group for config access. The unit also sets `NoNewPrivileges`, `ProtectSystem=full` and `ProtectHome`. It only makes outbound HTTPS requests to NOAA and a local connection to gpsd (and `meshtasticd` in TCP mode); it listens on no ports.
- Messages contain time, weather, sun and moon data only; the service never sends your coordinates over the mesh. Coordinates are sent to `api.weather.gov` to look up the forecast. Note that the Meshtastic radio itself may broadcast its position according to its own settings, independent of this service.

## Development notes
Development happens on Windows (the repo root doubles as a venv); Linux-only pieces (`gpsd`, serial radio, systemd, the shell scripts) cannot be exercised there. Focused automated tests can be run with `python -m unittest discover -s tests`; other changes have been checked with Pylance diagnostics and ad-hoc scripts that mock the NWS, gpsd and Meshtastic interfaces. `ozo_meshtastic_tw.py` imports cleanly without hardware, so `--dry-run` (which falls back to the system clock when gpsd is unreachable) is a quick way to exercise the message builder anywhere. Windows has no time zone database, so `pip install -r requirements.txt` (which includes `tzdata`) is required. Shell scripts must keep LF line endings.

## License
This project is licensed under the GNU General Public License, version 2. See the [LICENSE](LICENSE) file for the full text. It is distributed in the hope that it will be useful, but without any warranty. Weather and alert information is relayed from NOAA/NWS and must not be relied on for safety-critical decisions.

## Attribution
- Weather data: [NOAA National Weather Service API](https://www.weather.gov/documentation/services-web-api) (`api.weather.gov`).
- Built on [Meshtastic](https://meshtastic.org/) (Python library) and [gpsd](https://gpsd.io/).
- Sunrise/sunset and moon phase are computed locally with standard astronomical approximations; sunrise/sunset results were cross-checked against the [astral](https://github.com/sffjunkie/astral) library during development.
- Development assistance: much of the design, code, documentation and testing in this project (including the multi-field message builder, retry and reliability features, `--dry-run` and `--check` modes, `check_radios.py`, the install and uninstall scripts, and this documentation) was developed collaboratively with an AI assistant using Copilot SDK in VS Code. All changes were reviewed by the project maintainer, who directed the work and remains responsible for it.