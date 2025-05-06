"""Function to blink led PIN"""
from umqtt.simple import MQTTClient
from machine import Pin
from utime import sleep
import network


# Network connection
def conectar_wifi(ssid, password):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(ssid, password)

    while not wlan.isconnected():
        print("Conectando al WiFi...")
        sleep(1)

    print("Conectado:", wlan.ifconfig())


def get_mqtt_client():
    """Build MQTT CLient to connect"""

    _srvr = '192.168.68.109'
    _p = 1883
    _usr = 'mqtt_usr'
    _pass = 'luismdz366'
    _id = 'pico_client_1'

    _client = MQTTClient(_id,
                         _srvr,
                         _p,
                         _usr,
                         _pass)
    return _client


# Led to wathc activity loop
def blink_pin(pin, timing):
    """File version to blink led RP PICO W"""
    pin.toggle()
    sleep(timing)


# main loop
conectar_wifi("Skynet_366", "Copper_Skynet!")
client = get_mqtt_client()
client.connect()
topic_pub = "pico1/status"
while True:
    pin = Pin("LED", Pin.OUT)
    msg = "ON"
    client.publish(topic_pub, msg)
    blink_pin(pin, 3)
