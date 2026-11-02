"""Persistent internal log on flash with size-based rotation

Usage from the REPL (e.g. Thonny):
    import logger
    logger.show()     # print log.old + log.txt
    logger.clear()    # delete both files
"""
import io
import os
import sys
import time

LOG_FILE = "log.txt"
OLD_FILE = "log.old"
MAX_SIZE = 16 * 1024    # bytes per file (2 files max on flash)
RECENT_LEN = 20         # last lines kept in RAM (published over MQTT)

debug = True            # write DEBUG lines to flash
recent = []
count = 0               # total lines logged (detect changes)
_boot = time.ticks_ms()


def _stamp():
    """UTC date if the RTC was set via NTP, otherwise uptime"""
    t = time.localtime()
    up = time.ticks_diff(time.ticks_ms(), _boot) // 1000
    if t[0] >= 2024:
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d} +{}s".format(
            t[0], t[1], t[2], t[3], t[4], t[5], up)
    return "+{}s".format(up)


def _rotate():
    try:
        if os.stat(LOG_FILE)[6] < MAX_SIZE:
            return
    except OSError:
        return  # file does not exist yet
    try:
        os.remove(OLD_FILE)
    except OSError:
        pass
    os.rename(LOG_FILE, OLD_FILE)


def log(level, *args):
    """Print a line to the console and append it to the log file"""
    global count
    count += 1
    line = "{} {:5} {}".format(_stamp(), level,
                               " ".join(str(a) for a in args))
    print(line)
    recent.append(line)
    if len(recent) > RECENT_LEN:
        recent.pop(0)
    if level == "DEBUG" and not debug:
        return
    try:
        _rotate()
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print("Log write error:", e)


def info(*args):
    log("INFO", *args)


def warn(*args):
    log("WARN", *args)


def error(*args):
    log("ERROR", *args)


def dbg(*args):
    log("DEBUG", *args)


def exc(msg, e):
    """Log an exception with its full traceback"""
    buf = io.StringIO()
    sys.print_exception(e, buf)
    error(msg, buf.getvalue().strip())


def show():
    for name in (OLD_FILE, LOG_FILE):
        try:
            with open(name) as f:
                print("=====", name)
                for line in f:
                    print(line, end="")
        except OSError:
            pass


def clear():
    for name in (OLD_FILE, LOG_FILE):
        try:
            os.remove(name)
        except OSError:
            pass
