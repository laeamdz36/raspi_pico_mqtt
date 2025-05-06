"""Initial TEST to blink uPi"""
from machine import Pin
from utime import sleep


def blink_led():
    """Execute function to blink led"""


pin = Pin("LED", Pin.OUT)

print("LED starts flashing...")
while True:
    try:
        pin.toggle()
        sleep(1)  # sleep 1sec
        print("{:^5}".format("Function LED"))
    except KeyboardInterrupt:
        break
pin.off()
print("Finished.")
