"""Main module for BME280 read and MQTT publish data"""
from machine import Pin, I2C
import uasyncio as asyncio
from utime import sleep
# from umqtt.simple import MQTTClient
from mqtt_as import MQTTClient, config
from bme280_float import *
from picozero import pico_temp_sensor
from pi_pico_cfg import *

# Async functions
# ************************************
# Start main loop
# Load configs
# Init network - handle errors
# Read sensors - handle errors
# Publish to MQTT - handle errors
# ************************************


# ------------------------------------ Congigs
pin_led = Pin("LED", Pin.OUT)
i2c = I2C(0, sda=Pin(4), scl=Pin(5), freq=100000)
bme280 = BME280(i2c=i2c)
last_r = {"temp": None,
          "hum": None,
          "pres": None,
          "cpu_temp": None}  # last reading
# ------------------------------------ WLAN and MQTT Config
config['ssid'] = NET_SSID
config['wifi_pw'] = NET_SSID_PWD
config['server'] = MQTT_BROKER
config['port'] = MQTT_PORT
config['user'] = MQTT_USER
config['password'] = MQTT_USER_PWD
config['client_id'] = MQTT_CLIENT_ID
# ------------------------------------
client = MQTTClient(config)

# ------------------------------------


async def read_bme280(sensor):
    """Read the data from the sensod BME 280"""
    global last_r
    while True:
        # temp, pressure, humidity
        t, p, h = sensor.read_compensated_data()
        last_r["temp"] = t
        last_r["hum"] = h
        last_r["pres"] = p / 100
        last_r["cpu_temp"] = pico_temp_sensor.temp
        await asyncio.sleep(2)


async def callback(topic, msg, retained):
    """Event on message received"""
    print("Mesage received:", topic, msg)


async def blink_led(period=1):
    """Parpadeo independiente del LED cada 'period' segundos"""
    while True:
        pin_led.value(not pin_led.value())
        await asyncio.sleep(period)


async def mqtt_publish(client):
    """Publish to MQTT broker"""

    while last_r["temp"] is None:
        await asyncio.sleep(1)

    m_topic = "pico02"
    while True:

        await client.publish(m_topic + "/temperature",
                             "{:.2f}".format(last_r["temp"]), qos=1)
        await client.publish(m_topic + "/pressure",
                             "{:.2f}".format(last_r["pres"]), qos=1)
        await client.publish(m_topic + "/humidity",
                             "{:.2f}".format(last_r["hum"]), qos=1)
        await client.publish(m_topic + "/cpu_temp",
                             "{:.2f}".format(last_r["cpu_temp"]), qos=1)
        await asyncio.sleep(5)  # espera 5s


async def main(client):
    """Main event loop"""
    await client.connect()

    # MQTT Topic subscriptions
    # await client.subscribe("pico/led", 1)

    # run async tasks
    asyncio.create_task(blink_led(1))  # LED parpadea cada 0.5s
    asyncio.create_task(read_bme280(bme280))  # LED parpadea cada 0.5s
    await mqtt_publish(client)           # sigue con envío periódico

# callbacks
client.on_msg = callback

try:
    asyncio.run(main(client))
finally:
    client.close()
    asyncio.new_event_loop()
