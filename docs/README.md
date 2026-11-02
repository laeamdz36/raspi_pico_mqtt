# Raspberry Pi Pico W MQTT Sender [Temperature, Humidity, Pressure, CPU Temperature]

## Description

A Raspberry Pi Pico W (RP2040) running MicroPython acts as an MQTT client using the `mqtt_as` library. It continuously reads temperature, humidity and pressure from a BME280 sensor and publishes them, together with device diagnostics, to an MQTT broker (e.g. Eclipse Mosquitto).

The firmware is built for unattended IoT use: it never stops on network errors. It keeps retrying the Wi-Fi and broker connection, recovers from outages and resets itself if it freezes. It also keeps an internal log on flash to diagnose connectivity problems.

## Files

| File              | Purpose                                                                  |
| ----------------- | ------------------------------------------------------------------------ |
| `main.py`         | Main program: sensor reading, MQTT publishing, diagnostics, resilience   |
| `logger.py`       | Persistent internal log on flash (`log.txt` / `log.old`) with rotation   |
| `mqtt_as.py`      | Asynchronous MQTT client with automatic reconnection                     |
| `bme280_float.py` | BME280 sensor driver                                                     |
| `config.conf`     | Device configuration (Wi-Fi, broker, options)                            |

Libraries: `mqtt_as`, `bme280_float`, `picozero` (CPU temperature), plus MicroPython built-ins (`machine`, `network`, `uasyncio`, `rp2`, `ntptime`).

## Hardware

| BME280 | Pico W           |
| ------ | ---------------- |
| SDA    | GP4 (I2C0)       |
| SCL    | GP5 (I2C0)       |
| VCC    | 3V3              |
| GND    | GND              |

I2C runs at 100 kHz. The onboard LED shows the connection state.

## Configuration (`config.conf`)

One `key=value` per line. Lines starting with `#` are comments.

```conf
# configuration for pico02
client_id=pico02
server=192.168.10.15
port=1883
user=mqtt_usr
password=secret_password
ssid=your_ssid
wifi_pw=secret_wifi_password
```

| Key               | Default         | Description                                                                 |
| ----------------- | --------------- | --------------------------------------------------------------------------- |
| `client_id`       | board unique id | MQTT client id **and root topic** of the device (see [Topics](#topics))    |
| `server`          | (required)      | Broker IP or hostname                                                       |
| `port`            | `1883`          | Broker port                                                                 |
| `user`/`password` | empty           | Broker credentials                                                          |
| `ssid`/`wifi_pw`  | (required)      | Wi-Fi network (2.4 GHz only, the Pico W does not support 5 GHz)             |
| `wdt`             | `1`             | `1` = hardware watchdog enabled, `0` = disabled                             |
| `offline_reset_s` | `900`           | Reset the board after this many seconds offline, `0` = never                |
| `pub_interval`    | `5`             | Seconds between sensor publications                                         |
| `diag_interval`   | `30`            | Seconds between periodic diagnostics publications                           |
| `io_pins`         | empty           | GPIOs to report under `diagnostics/io/`, e.g. `io_pins=15,16`               |
| `log_debug`       | `1`             | `1` = DEBUG lines are written to flash, `0` = only INFO/WARN/ERROR          |
| `ntp_host`        | `pool.ntp.org`  | NTP server to set the clock (UTC) for log timestamps, empty = no NTP        |
| `country`         | not set         | Optional Wi-Fi country code (`rp2.country`), e.g. `country=MX`              |

Other `mqtt_as` options (`keepalive`, `response_time`, `max_repubs`, `ping_interval`, `queue_len`) may also be set here.

> **Note:** write IP addresses without leading zeros (`192.168.10.15`, not `192.168.10.015`). A leading zero may be parsed as octal (`015` = 13). The boot report warns about it.

> **Note:** every device needs a unique `client_id`. If two devices share it, the broker disconnects one each time the other connects.

## Topics

The root topic is the `client_id` from `config.conf`. If `client_id` is not set, the board unique id in hex is used (e.g. `e6614c311b4a2b35`).

### Sensor data (every `pub_interval` s)

| Topic                     | Value                     |
| ------------------------- | ------------------------- |
| `<client_id>/temperature` | BME280 temperature (°C)   |
| `<client_id>/pressure`    | BME280 pressure (hPa)     |
| `<client_id>/humidity`    | BME280 relative humidity (%) |

Sensor values are only published while the sensor is reading correctly.

### Diagnostics `<client_id>/diagnostics/`

| When                                 | Topics                                                                                                                                                       |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| On connect/reconnect (retained)      | `status`, `mac`, `ip`, `netmask`, `gateway`, `dns`, `ssid`, `channel`, `hostname`, `firmware`, `cpu_freq`, `reset_cause`                                     |
| Every `diag_interval` s              | `cpu_temp`, `rssi`, `wifi_quality`, `wifi_status`, `uptime`, `mem_free`, `mem_alloc`, `outages`, `connects`, `sensor_ok`, `sensor_errors`, `i2c_devices`, `io/led`, `io/gpN` |
| Every `diag_interval` s, if changed (retained) | `log`                                                                                                                                              |

| Topic           | Description                                                                                 |
| --------------- | ------------------------------------------------------------------------------------------- |
| `status`        | `online` when connected; `offline` published by the broker (Last Will) if the device drops |
| `reset_cause`   | Cause of the last reset: `PWRON_RESET`, `WDT_RESET` (watchdog, the program froze), ...      |
| `cpu_temp`      | RP2040 internal temperature (°C)                                                            |
| `rssi`          | Wi-Fi signal strength (dBm)                                                                 |
| `wifi_quality`  | Approximate signal quality (%) derived from RSSI                                            |
| `wifi_status`   | Raw `wlan.status()` code (see [Wi-Fi status codes](#wi-fi-status-codes))                     |
| `uptime`        | Seconds since boot                                                                          |
| `mem_free`/`mem_alloc` | Free / used heap (bytes)                                                             |
| `outages`       | Number of connection losses (Wi-Fi or broker)                                               |
| `connects`      | Number of successful broker connections                                                     |
| `sensor_ok`     | `1` if the BME280 is reading correctly, `0` otherwise                                       |
| `sensor_errors` | Number of failed BME280 readings                                                            |
| `i2c_devices`   | I2C addresses found on the bus (BME280 is `0x76` or `0x77`)                                 |
| `io/led`        | Onboard LED state                                                                           |
| `io/gpN`        | State of each GPIO listed in `io_pins`                                                      |
| `log`           | Last 20 lines of the internal log                                                           |

> **Changing `client_id`:** retained topics of the old id stay on the broker. To remove them, publish an empty retained message, e.g. `mosquitto_pub -t pico01/diagnostics/status -r -n`.

## Resilience

### Connection retries

- **Initial connection:** `mqtt_as` only reconnects by itself after the first successful connection, so `connect_forever()` retries the first connection forever with an increasing delay (2 s, 4 s, 8 s ... up to 60 s). The device boots and waits even if the Wi-Fi or the broker is not ready yet.
- **Later outages:** `mqtt_as` reconnects automatically. While offline, publications wait for the connection, and sensor reading and the LED keep running.

### Watchdog

The hardware watchdog (`machine.WDT`, 8 s timeout) is fed by an independent task. If the program freezes, the Pico resets itself. Disable it with `wdt=0`.

### Reset after long time offline

If the device stays offline for `offline_reset_s` seconds (default 15 minutes), it runs `machine.reset()`. This recovers from cases where the Wi-Fi chip gets stuck. `offline_reset_s=0` disables it.

### Unhandled errors

Any unhandled error is logged (with traceback) and the board resets instead of staying halted.

### BME280 failure

If the sensor is disconnected or fails, the program keeps running and retries every 2 s. `diagnostics/sensor_ok` and `sensor_errors` report the state.

### LED

| LED                                  | Meaning                         |
| ------------------------------------ | ------------------------------- |
| Very fast blink during first 3 s     | Safe mode window (see below)    |
| Slow blink (1 s)                     | Connected to the broker         |
| Fast blink (0.2 s)                   | No connection                   |
| Solid on                             | Safe mode, program not started  |

## Development with Thonny (watchdog and safe mode)

Once enabled, the RP2040 watchdog cannot be stopped through the MicroPython API. Without handling, stopping the program from Thonny would reset the board 8 s later, in a loop. The program avoids this in two ways:

1. **Ctrl-C disables the watchdog:** when Thonny connects or you press **Stop** (Ctrl+F2), the program catches the interrupt and turns off the watchdog by writing its control register (`wdt_disable()`). The REPL stays usable even with `wdt=1`.
2. **Safe mode:** during the first 3 s after boot the LED blinks fast. Holding the **BOOTSEL** button (or pressing Ctrl-C) in that window skips the program, the watchdog is never enabled and the LED stays solid on.

> Press BOOTSEL **after** plugging in the USB cable. Holding it while plugging in enters the UF2 bootloader instead.

**Recovering a board stuck in a reset loop** (e.g. an older version without these features):

- Unplug/plug the USB and press **Stop** in Thonny repeatedly to catch the 3 s window, then fix the files.
- Or, right after stopping (you have ~8 s), paste in the REPL:
  ```python
  import machine; machine.mem32[0x40058000] &= ~(1 << 30)
  ```
- Last resort: flash `flash_nuke.uf2` and MicroPython again (**this erases all files**).

## Internal log

`logger.py` writes a persistent log to flash to diagnose why the device does not connect.

### Size

- Each file is limited to **16 KB** (`MAX_SIZE` in `logger.py`). When `log.txt` reaches it, it is renamed to `log.old` (replacing the previous one) and a new `log.txt` starts.
- Maximum flash usage is about **32 KB** (2 files).
- The last 20 lines are also kept in RAM (`RECENT_LEN`) and published to `<client_id>/diagnostics/log`.
- To reduce flash writes, sensor errors are logged only on state changes (OK → error, error → OK), and `log_debug=0` skips DEBUG lines.

### Reading the log

- **Thonny:** open `log.txt` from the Pico file panel.
- **REPL:** stop the program (this also disables the watchdog) and run:
  ```python
  import logger
  logger.show()    # print log.old + log.txt
  logger.clear()   # delete both files
  ```
- **MQTT:** subscribe to `<client_id>/diagnostics/log`.

Each line has a timestamp (UTC date after NTP sync, plus `+Ns` uptime) and a level (`DEBUG`, `INFO`, `WARN`, `ERROR`).

### What is logged

| Event                    | Details                                                                                                         |
| ------------------------ | --------------------------------------------------------------------------------------------------------------- |
| Boot                     | Reset cause, firmware, MAC                                                                                      |
| Config check             | SSID shown quoted (to spot extra spaces) and its length, Wi-Fi password **length only**, broker, client_id; warns on missing SSID, invalid password length or leading zeros in the server IP |
| Wi-Fi status transitions | e.g. `Wi-Fi CONNECTING(1) -> NO_AP_FOUND(-2) after 4200 ms`, with a hint                                        |
| Wi-Fi failure            | Scan of nearby networks: whether the SSID is visible, its RSSI, channel and security; if not visible, the 5 strongest networks and possible case mismatch |
| Broker failure           | Error code (`ECONNREFUSED`, `ETIMEDOUT`, `CONNACK ...`) with the probable cause                                  |
| Successful connection    | Time taken, IP, RSSI, channel                                                                                   |
| Connection lost          | Wi-Fi status at that moment and outage count                                                                    |
| `mqtt_as` debug messages | Reconnections and their errors (when `log_debug=1`)                                                             |
| Fatal error              | Full traceback before the reset                                                                                 |

### Wi-Fi status codes

| Code | Name             | Probable cause                                                                    |
| ---- | ---------------- | --------------------------------------------------------------------------------- |
| 0    | `LINK_DOWN`      | Not connected                                                                     |
| 1    | `CONNECTING`     | Joining; staying here long usually means weak signal or wrong password            |
| 2    | `LINK_UP_NO_IP`  | Associated but no IP: check the router DHCP                                       |
| 3    | `GOT_IP`         | Connected                                                                         |
| -1   | `CONNECT_FAIL`   | AP rejected the client: WPA3-only security, MAC filter, client limit, or password |
| -2   | `NO_AP_FOUND`    | SSID not found: wrong name (case sensitive), 2.4 GHz band disabled, out of range  |
| -3   | `WRONG_PASSWORD` | Wrong Wi-Fi password                                                              |
