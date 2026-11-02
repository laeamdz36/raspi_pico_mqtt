"""Main module for BME280 read and MQTT publish data

Resilient to Wi-Fi and MQTT broker outages:
- The initial connection is retried forever (exponential backoff).
- After the first connection, mqtt_as handles automatic reconnection.
- Hardware watchdog (WDT) resets the board if the event loop freezes.
- If the device stays offline for too long, it is reset.
- Root topic = client_id from config.conf (<client_id>/...).
- Last Will: <client_id>/diagnostics/status = "offline" (retained).
- Internal log on flash (log.txt / log.old, see logger.py) with Wi-Fi
  status transitions, AP scans and broker errors to debug connectivity.
"""
import gc
import os
import errno
import machine
import network
from machine import Pin, I2C
import uasyncio as asyncio
from utime import sleep, ticks_ms, ticks_diff, localtime
from mqtt_as import MQTTClient, config
from bme280_float import BME280
from picozero import pico_temp_sensor
import logger as log

# ------------------------------------ Configurations
# Numeric keys in config.conf (everything else is kept as a string)
INT_KEYS = ("port", "keepalive", "response_time", "max_repubs",
            "ping_interval", "queue_len", "wdt", "offline_reset_s",
            "pub_interval", "diag_interval", "log_debug")

pin_led = Pin("LED", Pin.OUT)
i2c = I2C(0, sda=Pin(4), scl=Pin(5), freq=100000)
bme280 = None   # initialized (and retried) in read_bme280
wdt = None
last_r = {"temp": None,
          "hum": None,
          "pres": None,
          "cpu_temp": None}  # last reading
state = {"online": False,       # broker connection is up
         "outages": 0,          # detected Wi-Fi/broker outages
         "connects": 0,         # successful broker connections
         "offline_since": ticks_ms(),
         "sensor_ok": False,
         "sensor_errors": 0,
         "attempt_ms": ticks_ms()}  # start of the current connect attempt

# cyw43 (Pico W) Wi-Fi link status codes returned by wlan.status()
WIFI_STATUS = {0: "LINK_DOWN", 1: "CONNECTING", 2: "LINK_UP_NO_IP",
               3: "GOT_IP", -1: "CONNECT_FAIL", -2: "NO_AP_FOUND",
               -3: "WRONG_PASSWORD"}
WIFI_HINTS = {
    -1: "join failed: AP rejected the client (WPA3-only security, "
        "MAC filter, client limit) or wrong password",
    -2: "SSID not found: check exact name (case sensitive), that the "
        "2.4 GHz band is enabled, and signal range",
    -3: "authentication failed: wrong Wi-Fi password",
    2: "associated but no IP: check router DHCP"}
BROKER_HINTS = {
    "ECONNREFUSED": "broker not listening on that IP/port",
    "ECONNRESET": "broker closed the connection (TLS/port mismatch?)",
    "ETIMEDOUT": "no answer: wrong broker IP or firewall",
    "EHOSTUNREACH": "broker IP unreachable from this network",
    "CONNACK": "broker rejected login: check user/password/client_id"}


def load_conf(filename="config.conf"):
    """Read and load the local config file"""

    global config
    try:
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if key in INT_KEYS:
                        value = int(value)
                    config[key] = value
    except Exception as e:
        log.error("Reading error config file:", e)

    # client_id is also the device root topic: <client_id>/...
    cid = config["client_id"]
    if isinstance(cid, bytes):  # mqtt_as default: hex of the board unique id
        cid = cid.decode()
    config["client_id"] = cid
    config["topic"] = cid

    # Application defaults (can be overridden in config.conf)
    config.setdefault("wdt", 1)                  # 1 = watchdog enabled
    config.setdefault("offline_reset_s", 900)    # 0 = never reset
    config.setdefault("pub_interval", 5)
    config.setdefault("diag_interval", 30)
    config.setdefault("io_pins", "")             # e.g. io_pins=15,16
    config.setdefault("log_debug", 1)            # 1 = DEBUG lines to flash
    config.setdefault("ntp_host", "pool.ntp.org")  # empty = no NTP


def fmt_mac(mac):
    """bytes -> 'aa:bb:cc:dd:ee:ff'"""
    return ":".join("{:02x}".format(b) for b in mac)


def reset_cause_str():
    """Translate machine.reset_cause() to text"""
    causes = {}
    for name in ("PWRON_RESET", "HARD_RESET", "WDT_RESET",
                 "DEEPSLEEP_RESET", "SOFT_RESET"):
        if hasattr(machine, name):
            causes[getattr(machine, name)] = name
    rc = machine.reset_cause()
    return causes.get(rc, str(rc))


RESET_CAUSE = reset_cause_str()
BOOT_MS = ticks_ms()


def wifi_status_str(st):
    return "{}({})".format(WIFI_STATUS.get(st, "?"), st)


def err_str(e):
    """Readable exception text, decoding errno numbers (e.g. ECONNREFUSED)"""
    code = e.args[0] if e.args else None
    if isinstance(code, int) and code in errno.errorcode:
        return " ".join([errno.errorcode[code]] + [str(a) for a in e.args[1:]])
    return "{}: {}".format(type(e).__name__, e)


def boot_report():
    """Log boot context and sanity checks of the network config"""
    log.info("===== Boot | reset_cause:", RESET_CAUSE,
             "| fw:", os.uname().release, "| mac:",
             fmt_mac(wlan_cfg("mac", b"")))
    ssid = config.get("ssid") or ""
    pw = config.get("wifi_pw") or ""
    log.info("Config: ssid={!r} (len {}) wifi_pw len {} server={}:{} "
             "client_id={}".format(ssid, len(ssid), len(pw),
                                   config.get("server"), config.get("port"),
                                   config.get("client_id")))
    if not ssid:
        log.error("No 'ssid' in config.conf")
    if not 8 <= len(pw) <= 63:
        log.warn("WPA2 passwords must be 8..63 chars")
    server = str(config.get("server"))
    if any(len(o) > 1 and o.startswith("0") for o in server.split(".")):
        log.warn("Server IP with leading zeros ({}) may be parsed as octal"
                 .format(server))


def mqtt_dprint(self, msg, *args):
    """Route mqtt_as debug output to the internal log"""
    text = msg % args if args else msg
    if text.startswith("RAM free"):  # every 20 s: console only
        print(text)
        return
    log.dbg("mqtt_as:", text)


def wifi_scan():
    """Scan nearby APs and check whether the configured SSID is visible"""
    target = (config.get("ssid") or "").encode()
    if wdt:
        wdt.feed()  # scan blocks ~2-3 s
    try:
        nets = wlan.scan()  # (ssid, bssid, channel, rssi, security, hidden)
    except Exception as e:
        log.warn("Wi-Fi scan failed:", err_str(e))
        return
    if wdt:
        wdt.feed()
    nets.sort(key=lambda n: n[3], reverse=True)
    found = [n for n in nets if n[0] == target]
    log.dbg("Scan: {} APs visible".format(len(nets)))
    for n in found:
        log.info("Scan: target SSID found bssid={} ch={} rssi={} dBm "
                 "security={}".format(fmt_mac(n[1]), n[2], n[3], n[4]))
        if n[3] < -80:
            log.warn("Scan: very weak signal, move the device closer")
    if not found:
        similar = [n[0] for n in nets if n[0].lower() == target.lower()]
        if similar:
            log.warn("Scan: SSID not found, but similar name exists:",
                     similar, "(case mismatch?)")
        log.warn("Scan: SSID {!r} NOT visible. Strongest: {}".format(
            target, ", ".join("{}({})".format(n[0], n[3])
                              for n in nets[:5])))


# ------------------------------------ Tasks
async def read_bme280():
    """Read the data from the BME280 sensor (tolerant to I2C failures)"""

    global bme280
    while True:
        try:
            if bme280 is None:
                bme280 = BME280(i2c=i2c)
            # temp, pressure, humidity
            t, p, h = bme280.read_compensated_data()
            last_r["temp"] = t
            last_r["hum"] = h
            last_r["pres"] = p / 100
            if not state["sensor_ok"]:
                log.info("BME280 OK")
            state["sensor_ok"] = True
        except Exception as e:
            if state["sensor_ok"] or state["sensor_errors"] == 0:
                log.error("BME280 error:", err_str(e))
            bme280 = None
            state["sensor_ok"] = False
            state["sensor_errors"] += 1
        try:
            last_r["cpu_temp"] = pico_temp_sensor.temp
        except Exception:
            pass
        await asyncio.sleep(2)


def callback(topic, msg, retained):
    """Event on message received (mqtt_as calls subs_cb synchronously)"""
    print("Message received:", topic, msg)


async def blink_led():
    """LED: slow blink = online, fast blink = no connection"""
    while True:
        pin_led.value(not pin_led.value())
        await asyncio.sleep(1 if state["online"] else 0.2)


async def watchdog():
    """Feed the WDT and reset the board if it has been offline for too long"""
    limit = config["offline_reset_s"] * 1000
    while True:
        if wdt:
            wdt.feed()
        if limit and not state["online"] and \
                ticks_diff(ticks_ms(), state["offline_since"]) > limit:
            log.error("Offline for too long, resetting...")
            sleep(1)
            machine.reset()
        await asyncio.sleep(2)


async def wifi_han(up):
    """mqtt_as: called with True on connect and False on connection loss"""
    state["online"] = up
    if not up:
        state["outages"] += 1
        state["offline_since"] = ticks_ms()
        state["attempt_ms"] = ticks_ms()
        log.warn("Connection DOWN | wifi", wifi_status_str(wlan.status()),
                 "| outages:", state["outages"])
    else:
        log.info("Connection UP")


async def wifi_monitor():
    """Log every Wi-Fi status transition (why the SSID does not connect)"""
    last = None
    t0 = ticks_ms()
    while True:
        st = wlan.status()
        if st != last:
            msg = "Wi-Fi {} -> {} after {} ms".format(
                wifi_status_str(last), wifi_status_str(st),
                ticks_diff(ticks_ms(), t0))
            if st < 0:
                log.warn(msg, "|", WIFI_HINTS.get(st, ""))
            elif st == 3:
                log.info(msg, "| ip:", wlan.ifconfig()[0],
                         "rssi:", wlan.status("rssi"))
            else:
                log.dbg(msg, "|", WIFI_HINTS.get(st, ""))
            last = st
            t0 = ticks_ms()
        await asyncio.sleep_ms(200)


def ntp_sync():
    """Set the RTC (UTC) so log lines carry a real date"""
    if not config["ntp_host"] or localtime()[0] >= 2024:
        return
    try:
        import ntptime
        ntptime.host = config["ntp_host"]
        ntptime.settime()
        log.info("NTP time set (UTC)")
    except Exception as e:
        log.warn("NTP failed:", err_str(e))


async def conn_han(client):
    """mqtt_as: called after every connection (initial and reconnections)"""
    state["connects"] += 1
    log.info("MQTT connected #{} in {} ms | ip {} | rssi {} | ch {}".format(
        state["connects"], ticks_diff(ticks_ms(), state["attempt_ms"]),
        wlan.ifconfig()[0], wlan.status("rssi"), wlan_cfg("channel", "?")))
    ntp_sync()
    # MQTT Topic subscriptions (must be repeated on every reconnection)
    # await client.subscribe("pico/led", 1)
    diag = config["topic"] + "/diagnostics"
    await client.publish(diag + "/status", "online", retain=True, qos=1)
    await publish_static(client, diag)


async def connect_forever(client):
    """Initial connection: retry until it succeeds, never give up.
    After the first connection mqtt_as handles reconnections."""
    delay = 2
    attempt = 0
    while True:
        attempt += 1
        state["attempt_ms"] = ticks_ms()
        try:
            log.info("Connect attempt", attempt, "to", config.get("ssid"))
            await client.connect()
            return
        except Exception as e:  # OSError, MQTTException, timeouts...
            st = wlan.status()
            if wlan.isconnected():
                text = err_str(e)
                hint = [h for k, h in BROKER_HINTS.items() if k in text]
                log.error("Broker {}:{} failed: {} | {}".format(
                    config["server"], config.get("port"), text,
                    hint[0] if hint else "check broker is running"))
            else:
                log.error("Wi-Fi failed:", err_str(e), "| status",
                          wifi_status_str(st), "|",
                          WIFI_HINTS.get(st, "timeout while connecting: "
                                         "weak signal or wrong password"))
                try:
                    wlan.disconnect()  # stop a pending join before scanning
                except OSError:
                    pass
                wifi_scan()
            log.info("Retrying in", delay, "s")
            if not wlan.isconnected():
                try:
                    wlan.disconnect()
                except OSError:
                    pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def mqtt_publish(client):
    """Publish sensor data to MQTT broker"""

    while last_r["temp"] is None:
        await asyncio.sleep(1)

    m_topic = config["topic"]
    while True:
        try:
            if state["sensor_ok"]:
                await client.publish(m_topic + "/temperature",
                                     "{:.2f}".format(last_r["temp"]), qos=1)
                await client.publish(m_topic + "/pressure",
                                     "{:.2f}".format(last_r["pres"]), qos=1)
                await client.publish(m_topic + "/humidity",
                                     "{:.2f}".format(last_r["hum"]), qos=1)
        except Exception as e:
            log.error("Publish error:", err_str(e))
        await asyncio.sleep(config["pub_interval"])


# ------------------------------------ Diagnostics
def io_pins():
    """List of GPIOs to report (config: io_pins=15,16)"""
    pins = []
    for p in config["io_pins"].split(","):
        p = p.strip()
        if p.isdigit():
            pins.append(int(p))
    return pins


def wlan_cfg(param, default=None):
    """Safe wlan.config() (not every parameter exists in every firmware)"""
    try:
        return wlan.config(param)
    except Exception:
        return default


async def publish_static(client, diag):
    """Data that only changes on (re)connect: published as retained"""
    ip, mask, gw, dns = wlan.ifconfig()
    try:
        hostname = network.hostname()
    except Exception:
        hostname = config["client_id"]
    data = (("mac", fmt_mac(wlan_cfg("mac", b""))),
            ("ip", ip),
            ("netmask", mask),
            ("gateway", gw),
            ("dns", dns),
            ("ssid", wlan_cfg("ssid", config.get("ssid", ""))),
            ("channel", wlan_cfg("channel", "")),
            ("hostname", hostname),
            ("firmware", os.uname().release),
            ("cpu_freq", machine.freq() // 1000000),
            ("reset_cause", RESET_CAUSE))
    for key, value in data:
        await client.publish(diag + "/" + key, str(value), retain=True, qos=1)


async def mqtt_diagnostics(client):
    """Publish periodic telemetry under <client_id>/diagnostics/"""

    diag = config["topic"] + "/diagnostics"
    pins = io_pins()
    log_sent = -1
    while True:
        try:
            gc.collect()
            rssi = wlan.status("rssi") if wlan.isconnected() else -100
            quality = min(max(2 * (rssi + 100), 0), 100)  # approximate %
            data = [("cpu_temp", "{:.2f}".format(last_r["cpu_temp"] or 0)),
                    ("rssi", rssi),
                    ("wifi_quality", quality),
                    ("wifi_status", wlan.status()),
                    ("uptime", ticks_diff(ticks_ms(), BOOT_MS) // 1000),
                    ("mem_free", gc.mem_free()),
                    ("mem_alloc", gc.mem_alloc()),
                    ("outages", state["outages"]),
                    ("connects", state["connects"]),
                    ("sensor_ok", int(state["sensor_ok"])),
                    ("sensor_errors", state["sensor_errors"]),
                    ("i2c_devices", ",".join(hex(a) for a in i2c.scan())),
                    ("io/led", pin_led.value())]
            if log.count != log_sent:  # last log lines, only when changed
                data.append(("log", "\n".join(log.recent)))
                log_sent = log.count
            for p in pins:
                data.append(("io/gp{}".format(p), Pin(p).value()))
            for key, value in data:
                await client.publish(diag + "/" + key, str(value),
                                     retain=(key == "log"), qos=1)
        except Exception as e:
            log.error("Diagnostics error:", err_str(e))
        await asyncio.sleep(config["diag_interval"])


async def main(client):
    """Main event loop"""

    # Local tasks: run even without network
    asyncio.create_task(watchdog())
    asyncio.create_task(blink_led())
    asyncio.create_task(read_bme280())
    asyncio.create_task(wifi_monitor())

    await connect_forever(client)

    # publish waits internally for the connection if the network drops
    asyncio.create_task(mqtt_diagnostics(client))
    await mqtt_publish(client)


def wdt_disable():
    """Stop the hardware watchdog (MicroPython has no API for this).
    Clears the ENABLE bit (30) of the WATCHDOG CTRL register."""
    base = 0x40058000 if "RP2040" in os.uname().machine else 0x400D8000
    machine.mem32[base] &= ~(1 << 30)


def safe_mode(window_s=3):
    """Startup window: holding BOOTSEL (or Ctrl-C) skips the program
    and leaves the REPL free for development, without the WDT."""
    import rp2
    print("Hold BOOTSEL or press Ctrl-C within {} s for safe mode".format(
        window_s))
    for _ in range(window_s * 10):
        if rp2.bootsel_button():
            return True
        pin_led.toggle()
        sleep(0.1)
    pin_led.off()
    return False


# ------------------------------------ Startup
if safe_mode():
    pin_led.on()  # solid LED = safe mode
    print("Safe mode: program not started, WDT disabled")
    raise SystemExit
load_conf()
log.debug = bool(config["log_debug"])

try:
    network.hostname(config["client_id"])  # before the interface is activated
except Exception:
    pass
if "country" in config:
    import rp2
    rp2.country(config["country"])

config["subs_cb"] = callback
config["wifi_coro"] = wifi_han
config["connect_coro"] = conn_han
config["will"] = (config["topic"] + "/diagnostics/status",
                  "offline", True, 1)

if config["log_debug"]:
    MQTTClient.DEBUG = True
    MQTTClient.dprint = mqtt_dprint
client = MQTTClient(config)
wlan = network.WLAN(network.STA_IF)
boot_report()

if config["wdt"]:
    wdt = machine.WDT(timeout=8000)  # RP2040: max ~8.3 s

try:
    asyncio.run(main(client))
except KeyboardInterrupt:
    # Ctrl-C (Thonny Stop/connect): disable the WDT so the REPL stays usable
    wdt_disable()
    log.info("Stopped by user, WDT disabled")
except Exception as e:
    # Any unhandled error: reset instead of staying halted
    log.exc("Fatal error:", e)
    sleep(5)
    machine.reset()
finally:
    client.close()
    asyncio.new_event_loop()
