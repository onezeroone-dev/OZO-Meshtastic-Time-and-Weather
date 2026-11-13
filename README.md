# OZO Meshtastic Time and Weather

## Description
A service for Linux host that reads the current time from GPS, fetches local weather from the NOAA/NWS API, and broadcasts both on the local Meshtastic mesh on a schedule. In the absence of a GPS signal (or radio), it falls back to the system time and a user-configured fixed location.

**Compatibility.** The Python service should work on Linux systems that support its Python dependencies and have compatible hardware configured. It gets GPS data from any receiver supported by `gpsd` (the service connects to `gpsd` over TCP and never opens the GPS device). For LoRa, `meshtastic.connection: serial` connects to a self-contained Meshtastic device with firmware over serial/USB. Alternatively, `meshtastic.connection: tcp` connects to [`meshtasticd`](https://meshtastic.org/docs/software/linux/), which runs the Meshtastic node software on the Linux host and can control supported SPI or USB radios directly; the radio module itself does not need Meshtastic firmware. Hardware and pinouts must be supported by `meshtasticd`; see its [hardware documentation](https://meshtastic.org/docs/meshtasticd/hardware/). The included `install.sh` is specifically for Debian-based systems that use `apt` and `systemd`; other Linux distributions may need adapted installation steps.

**Region.** Weather and alerts come from the NOAA/NWS API, which covers the United States and its territories only. Elsewhere the weather fields are unavailable; the time, sun and moon fields still work.

Contents: [Prerequisites](#prerequisites-and-hardware-setup) · [USB/Serial Radios](#usbserial-radios) · [SPI Radios](#spi-radios) · [Configuration](#configuration) · [Example output](#example-output) · [Run manually](#run-manually-venv) · [Install](#install-as-a-service) · [Upgrading](#upgrading) · [Checking the radios](#checking-the-radios-check_radiospy) · [Device access](#device-access-and-locking) · [Troubleshooting](#logging-and-troubleshooting) · [Uninstall](#uninstall) · [Limitations](#limitations) · [Mesh etiquette](#mesh-etiquette-and-airtime) · [Security](#security-notes) · [Development](#development-notes) · [License](#license) · [Attribution](#attribution)

## Prerequisites and hardware setup
Complete these before running `install.sh`. Device names and setup steps depend on your Linux distribution and hardware; adapt the examples.
1. **GNSS serial port.** If the receiver uses the host UART, enable that serial port and disable any login shell using it. On Raspberry Pi OS, use `sudo raspi-config` → Interface Options → Serial Port, then reboot if required.
2. **gpsd.** Install and configure `gpsd` for your distribution. On Debian-based systems, `install.sh` installs the package. Point gpsd at the GNSS device selected above (for example, `/dev/ttyS0` for a configured Raspberry Pi UART, or the discovered `/dev/ttyUSB*` device for a USB-to-UART connection). In `/etc/default/gpsd`, `GPSD_OPTIONS="-n"` starts polling without waiting for a client. Restart gpsd and confirm with `gpsmon` or `cgps` that it reports a 3D fix and UTC time. Use a clear view of the sky; a first fix can take several minutes.
3. **Meshtastic radio.** Choose the setup matching how the radio connects: a self-contained Meshtastic node with firmware over USB serial, or a supported SPI radio controlled by `meshtasticd`. See [USB/Serial Radios](#usbserial-radios) and [SPI Radios](#spi-radios) below.
4. **Internet access** to `api.weather.gov`.
5. **Time zone and config.** Decide your `time_zone` and whether to set a fixed `location` (see Configuration).

## USB/Serial Radios

Use this setup for a self-contained Meshtastic device that runs Meshtastic firmware and appears as a serial device over USB. The selected Seeed XIAO nRF52840 & Wio-SX1262 kit comes pre-flashed with Meshtastic firmware. **This connection does not require `meshtasticd` or a YAML file under `/etc/meshtasticd/config.d/`.**

Connect the device and list stable USB serial names:

    ls -l /dev/serial/by-id/

If that directory has no matching entry, compare `ls /dev/ttyACM* /dev/ttyUSB*` before and after connecting the device. The Seeed kit was detected as `/dev/ttyACM0` on the current Pi. Prefer a `/dev/serial/by-id/...` path in the config when one is available, since a numbered `/dev/ttyACM*` path can change.

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

The LoRa region, channel key and modem preset must match the other mesh nodes. Set `channel_index` to `0` in this service config to use the primary channel. Check settings with `meshtastic --port /dev/ttyACM0 --info` before testing. Do not use the `CLIENT_MUTE` role solely because it was recommended for the previous Waveshare HAT; that recommendation was specific to its hardware limitation.

## SPI Radios

Use this setup when the Linux host directly controls an SPI-attached radio. In this arrangement `meshtasticd` runs the Meshtastic node software on the host; the radio module itself does not need Meshtastic firmware. Configure this service with `meshtastic.connection: tcp` and the `tcp_host` of the daemon (typically `localhost` when they run on the same Pi). No USB serial port is used by the OZO service.

Enable SPI and configure the radio's SPI, GPIO and other required pins. Install `meshtasticd` for your distribution. Find the preset that matches the exact radio board and pinout in `/etc/meshtasticd/available.d`, then copy that preset to `/etc/meshtasticd/config.d` as described in the [meshtasticd configuration guide](https://meshtastic.org/docs/meshtasticd/usage/). Do not select a preset based only on the radio chip; a mismatched pinout will not work. Start the daemon and confirm its TCP API is reachable on the default port 4403. See Meshtastic's [Raspberry Pi hardware instructions](https://meshtastic.org/docs/meshtasticd/hardware/boards/raspberry-pi/).

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
- `meshtastic`: `connection` (`serial` for a self-contained Meshtastic device or `tcp` for `meshtasticd`; the example uses the selected USB/serial radio), `serial_port` (null = autodetect; set the discovered port for predictable selection), `tcp_host`, `channel_index`, `max_message_bytes` (message is truncated to this many UTF-8 bytes; default 200), and `post_send_delay_seconds` (default 5; a non-negative number of seconds to keep the connection open after the last submitted message; set to 0 for no delay).

  The post-send delay gives the radio time to queue messages before disconnecting; in some setups this may help with delayed delivery. It is a precaution, not a confirmed fix or a guarantee of queueing or delivery. The radio connection remains occupied during the delay. Existing configurations that omit the setting also use the five-second default.
- `gps`: gpsd host/port and fix timeout. The physical GNSS serial device is configured in gpsd, not this file.
- `location`: optional fixed latitude/longitude; if null, the GPS position is used. **Setting both also makes the GPS optional**: if gpsd is unreachable or has no fix, the service still broadcasts, using the system clock (the time-sync field reads `SYSTEM CLOCK-NOT GPS`) and the fixed location for weather and sun times. This allows a low-cost build with just a Pi and a radio. Keep the Pi's clock accurate (NTP) in that case, because there is no GPS time to correct it. `--check` reports the missing GPS as a warning rather than a failure.
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
- `--dry-run`: from an interactive terminal, ignore the schedule, build the message(s) now and print each with its byte count instead of transmitting. It never opens the LoRa radio. If gpsd is unreachable it falls back to the system clock and prints a prominent warning (the time-sync field then reads `SYSTEM CLOCK-NOT GPS`). Failed NWS lookups are reported. It refuses to run when not attached to a terminal.
- `--check`: post-install health check of the config, GPS, LoRa radio and NOAA reachability; exits non-zero on any failure. It briefly opens the radio, like `check_radios.py`.

## Install as a service
    sudo ./install.sh
The installer is for Debian-based Linux systems using `apt` and `systemd`. It first runs `check_radios.py` as the service user and aborts (without installing the service) if gpsd cannot supply a GPS fix/time or the Meshtastic radio cannot be reached. It uses `/etc/ozo-meshtastic-tw/config.json` (created from the example if absent), so a non-default serial port or gpsd host must be set there first; re-run the installer after fixing any problem. You can run the check on its own with `venv/bin/python check_radios.py --config config.json`.
The installed layout is: code and venv in `/opt/ozo-meshtastic-tw`, configuration in `/etc/ozo-meshtastic-tw/config.json` (not in `/opt`). The config is readable only by root and the `ozo-meshtastic` group, and `--config` defaults to a `config.json` in the current directory, so run the installed program as the service user and pass the config path explicitly. With the service stopped (the checks open the radio):

    sudo systemctl stop ozo-meshtastic-tw
    sudo -u ozo-meshtastic /opt/ozo-meshtastic-tw/venv/bin/python /opt/ozo-meshtastic-tw/ozo_meshtastic_tw.py --config /etc/ozo-meshtastic-tw/config.json --check

`--dry-run` needs an interactive terminal and may not work under `sudo -u`; if it refuses, run it with `sudo` instead (`sudo /opt/ozo-meshtastic-tw/venv/bin/python /opt/ozo-meshtastic-tw/ozo_meshtastic_tw.py --config /etc/ozo-meshtastic-tw/config.json --dry-run`).

Edit `/etc/ozo-meshtastic-tw/config.json` (`sudo nano`), then `sudo systemctl start ozo-meshtastic-tw`.
Logs: `journalctl -u ozo-meshtastic-tw`. Each broadcast cycle ends with one summary line you can grep, e.g. `journalctl -u ozo-meshtastic-tw | grep 'broadcast summary'`: `status=ok|partial|skipped|failed` plus message count, byte sizes, time source and any failed NWS lookups (`partial` means some lookups failed). The unit file is `systemd/ozo-meshtastic-tw.service`.

OZO does not wait for an acknowledgment, and `status=ok` does not confirm that another node received the message; verify reception on a second node.

## Upgrading
Pull or copy the new version of the repository and re-run `sudo ./install.sh`. It refreshes the code and Python dependencies in `/opt/ozo-meshtastic-tw` and keeps your existing `/etc/ozo-meshtastic-tw/config.json`, so compare it with the new `config.example.json` for added settings (missing optional keys use defaults). Then `sudo systemctl restart ozo-meshtastic-tw`. The installer runs the radio check again, so stop other radio clients first.

## Checking the radios (`check_radios.py`)
`check_radios.py` verifies that the two radios this service depends on are usable, using the same code paths and settings as the service. It transmits nothing.
- **GPS:** connects to gpsd (`gps.gpsd_host`/`gpsd_port`) and waits up to `gps.timeout_seconds` for a fix with a valid time.
- **LoRa:** opens the Meshtastic radio (serial or `meshtasticd` TCP, per `meshtastic.*`), reads the node's name, and closes it.

Run it from the repo directory, pointing at the config the service will use:

    venv/bin/python check_radios.py --config config.json

It prints `OK` lines on success. For each failure it prints a `FAIL` line with a plain-language hint (for example gpsd not running, no sky view, missing `dialout` group membership, wrong serial port) plus the underlying error, and exits with status 1. Both checks always run, so you see every problem in one pass.

When to use it:
- Before installing or after changing hardware, ports or `config.json`. `install.sh` runs it automatically and stops on failure.
- When broadcasts stop working, to separate a radio problem from a weather or config problem. For a broader check that also tests the config and NOAA, use `ozo_meshtastic_tw.py --check`.

Guidance:
- Stop other radio clients first (Meshtastic CLI or app, monitors); the serial port is locked while the check runs. See "Device access and locking".
- The GPS check needs a real fix. Indoors it may time out; test near a window or raise `gps.timeout_seconds`. `gpsmon` or `cgps` show gpsd's view independently.
- Run it as the service user (the installer does) so a permissions problem shows up here and not at the first broadcast: `sudo -u ozo-meshtastic /opt/ozo-meshtastic-tw/venv/bin/python /opt/ozo-meshtastic-tw/check_radios.py --config /etc/ozo-meshtastic-tw/config.json`.

## Device access and locking
- **GPS:** the service never opens the GPS device. It connects to `gpsd` over TCP (default port 2947) as a client, so other gpsd clients (`cgps`, `gpsmon`, etc.) can run at the same time.
- **LoRa (serial mode):** the Meshtastic library opens the serial port with an exclusive lock (`flock`). The service holds it only while broadcasting: it connects, sends one message, and disconnects. Between broadcasts (hourly by default) the port is free.
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
|[Waveshare SX1262 LoRa/GNSS HAT](https://www.waveshare.com/wiki/SX1262_XXXM_LoRaWAN/GNSS_HAT)|SPI|Yes|No|Meshtastic's [Linux hardware notes](https://meshtastic.org/docs/meshtasticd/hardware/) caution that this HAT is not recommended for deployment because of hardware limitations that may affect longer messages. In testing, `check_radios.py` and `--check` passed and `meshtasticd` accepted broadcasts (`status=ok`), but only a 40-character plain text message reached a second node. Messages of 80 characters or more (including this service's roughly 170-byte broadcasts) and NodeInfo packets did not, and the daemon logged `error=-7` receive failures. The `status=ok` result only means the daemon accepted a packet, so confirm reception on a second node.This HAT is not considered suitable for this service.|
|[Seeed XIAO nRF52840 & Wio-SX1262 Meshtastic Kit](https://wiki.seeedstudio.com/xiao_nrf52840&_wio_SX1262_kit_for_meshtastic/)|USB/Serial|No|Yes||

### GPS
|Device|Connection|Recommended|Comments|
|------|----------|-----------|--------|
|[Waveshare SX1262 LoRa/GNSS HAT](https://www.waveshare.com/wiki/SX1262_XXXM_LoRaWAN/GNSS_HAT)|UART|No|The GNSS radio on this hat works well with `gpsd` but the included LoRa radio is not recommeded for use with this service. See comments in the Meshtastic/LoRa table, above.|
|[Waveshare L76K GPS HAT](https://www.waveshare.com/wiki/L76K_GPS_HAT)|UART|Yes|Use its selector jumper to choose the connection and identify the resulting device path.|

## Logging and troubleshooting
**How logging works.** The service writes plain-text lines to stderr, which systemd captures in the journal. Each line starts with its level (`INFO`, `WARNING` or `ERROR`). The level is part of the message text, not the journal priority, so `journalctl -p warning` will not filter them; use grep instead:

    journalctl -u ozo-meshtastic-tw -f                           # follow live
    journalctl -u ozo-meshtastic-tw --since today | grep -E 'WARNING|ERROR'
    journalctl -u ozo-meshtastic-tw | grep 'broadcast summary'   # one line per cycle

- `INFO`: normal activity: `next broadcast at ...`, `broadcast: <text>` for each message sent, and the `broadcast summary` line.
- `WARNING`: something went wrong but the service carried on: a retried or failed GPS/NWS/radio call, a missing NWS dataset, an omitted stale observation, GPS unavailable, or an unwritable state file.
- `ERROR`: `broadcast failed: ...`: the whole cycle failed. The service keeps running and tries again at the next scheduled time.
- Startup problems (bad config, unknown time zone) end the process with a Python traceback; systemd restarts it every 30 seconds, so the same error repeats in the journal until you fix it. Check `systemctl status ozo-meshtastic-tw`.

**Reading the summary line:** `status=ok` (all sent), `partial` (sent, but some NWS lookups failed; see `failed=`), `skipped` (`reason=duplicate-guard` or `nothing-to-send`; expected, not a fault), `failed` (nothing sent; see `error=`).

**First steps for any problem**
1. `sudo systemctl status ozo-meshtastic-tw` and the recent journal.
2. Stop the service (`sudo systemctl stop ozo-meshtastic-tw`) and run the checks as the service user: `check_radios.py` (GPS and LoRa) and `ozo_meshtastic_tw.py --check` (adds config and NOAA). Both print hints.
3. Run `ozo_meshtastic_tw.py --dry-run` from a terminal to see exactly what would be sent and which lookups fail. It does not use the radio.

**Common symptoms**

| Log message or symptom | Likely cause and fix |
|---|---|
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
- **System clock.** The schedule and duplicate guard use the Pi's system clock. A Pi without a battery-backed RTC that boots offline may have a wrong system clock until it syncs.
- **Sun and moon** are calculated locally with standard approximations: sunrise and sunset are accurate to within about a couple of minutes, moon phase to the nearest eighth of the cycle.
- **Fields are US units** (°F, mph, hPa).
- **End-to-end hardware validation is pending.** The previous Waveshare setup could not deliver full-length broadcasts, and the selected Seeed USB/serial radio has not yet been verified with this service. Run `check_radios.py`, `--check` and `--dry-run`, then confirm a real broadcast on a second node before relying on a new hardware setup.

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