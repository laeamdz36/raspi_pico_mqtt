# Raspberry pi pico W MQTT Sender [Temperature, HUmidity, Pressure, CPU Temperature]

## Description

With Raspberry pi pico W 2022 RP2040 send MQTT data as MQTT cliente with micropython framework within pi pico using library ``mqtt_as``; send data to a MQTT broker as example mosquitto eclipse, data send is from continous readings from a BME280 sensor collection temperature, humidity and pressure data, also including built-in pi pico CPU temperature data

## Libraries

- mqtt_as
- bme280_float
- Built-in library