"""Chip temperature on Apple Silicon laptops, so long runs can pace themselves.

Nomi runs its model on the same laptop it analyses on. A single analysis is
a short burst, but an evaluation over dozens of issues is sustained GPU load.
This module reads the SoC temperature sensors through the IOHID event API
(no sudo, the same source tools like Stats use) and lets callers wait for the
chip to cool. Everywhere else it reports "unknown" and never blocks.
"""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable
from functools import lru_cache

_UTF8 = 0x08000100
_TEMPERATURE_EVENT = 15
_SOC_HINTS = ("tdie", "tdev", "pmu")


def hottest_celsius() -> float | None:
    """Hottest SoC sensor in °C, or None when it cannot be read."""
    try:
        readings = _read_sensors()
    except Exception:  # sensor access is best effort and must never break a run
        return None
    soc = [v for name, v in readings.items() if any(h in name.lower() for h in _SOC_HINTS)]
    values = soc or list(readings.values())
    return round(max(values), 1) if values else None


def wait_until_cool(
    limit: float,
    *,
    resume_below: float | None = None,
    poll_s: float = 10.0,
    max_wait_s: float = 900.0,
    read: Callable[[], float | None] = hottest_celsius,
    sleep: Callable[[float], None] = time.sleep,
    notify: Callable[[str], None] | None = None,
) -> float:
    """Block while the chip is at or above `limit` °C; returns seconds waited.

    Resumes once it drops below `resume_below` (default: limit - 10), so a
    run does not oscillate right at the threshold. Gives up after
    `max_wait_s` rather than hanging forever on a stuck sensor.
    """
    resume = limit - 10 if resume_below is None else resume_below
    temp = read()
    if temp is None or temp < limit:
        return 0.0
    if notify:
        notify(f"chip at {temp:.0f} °C (limit {limit:.0f} °C); pausing until below {resume:.0f} °C")
    waited = 0.0
    while temp is not None and temp >= resume and waited < max_wait_s:
        sleep(poll_s)
        waited += poll_s
        temp = read()
    return waited


@lru_cache(maxsize=1)
def _libs() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    if sys.platform != "darwin":
        raise OSError("temperature sensors are only read on macOS")
    iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    vp = ctypes.c_void_p
    for fn, res, args in [
        ("CFStringCreateWithCString", vp, [vp, ctypes.c_char_p, ctypes.c_uint32]),
        ("CFNumberCreate", vp, [vp, ctypes.c_int, vp]),
        ("CFDictionaryCreate", vp, [vp, vp, vp, ctypes.c_long, vp, vp]),
        ("CFArrayGetCount", ctypes.c_long, [vp]),
        ("CFArrayGetValueAtIndex", vp, [vp, ctypes.c_long]),
        (
            "CFStringGetCString",
            ctypes.c_bool,
            [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32],
        ),
        ("CFRelease", None, [vp]),
    ]:
        getattr(cf, fn).restype, getattr(cf, fn).argtypes = res, args
    for fn, res, args in [
        ("IOHIDEventSystemClientCreate", vp, [vp]),
        ("IOHIDEventSystemClientSetMatching", ctypes.c_int, [vp, vp]),
        ("IOHIDEventSystemClientCopyServices", vp, [vp]),
        ("IOHIDServiceClientCopyProperty", vp, [vp, vp]),
        ("IOHIDServiceClientCopyEvent", vp, [vp, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64]),
        ("IOHIDEventGetFloatValue", ctypes.c_double, [vp, ctypes.c_int32]),
    ]:
        getattr(iokit, fn).restype, getattr(iokit, fn).argtypes = res, args
    return iokit, cf


def _read_sensors() -> dict[str, float]:
    iokit, cf = _libs()
    vp = ctypes.c_void_p

    def cfstr(text: str) -> int:
        return cf.CFStringCreateWithCString(None, text.encode(), _UTF8)

    page, usage = (
        ctypes.c_int32(0xFF00),
        ctypes.c_int32(5),
    )  # Apple vendor page, temperature sensors
    keys = (vp * 2)(cfstr("PrimaryUsagePage"), cfstr("PrimaryUsage"))
    vals = (vp * 2)(
        cf.CFNumberCreate(None, 3, ctypes.byref(page)),
        cf.CFNumberCreate(None, 3, ctypes.byref(usage)),
    )
    key_cb = vp.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
    val_cb = vp.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
    matching = cf.CFDictionaryCreate(
        None, keys, vals, 2, ctypes.addressof(key_cb), ctypes.addressof(val_cb)
    )
    client = iokit.IOHIDEventSystemClientCreate(None)
    iokit.IOHIDEventSystemClientSetMatching(client, matching)
    services = iokit.IOHIDEventSystemClientCopyServices(client)
    readings: dict[str, float] = {}
    if not services:
        return readings
    product = cfstr("Product")
    name_buf = ctypes.create_string_buffer(128)
    for i in range(cf.CFArrayGetCount(services)):
        service = cf.CFArrayGetValueAtIndex(services, i)
        name = iokit.IOHIDServiceClientCopyProperty(service, product)
        event = iokit.IOHIDServiceClientCopyEvent(service, _TEMPERATURE_EVENT, 0, 0)
        if name and event and cf.CFStringGetCString(name, name_buf, 128, _UTF8):
            value = iokit.IOHIDEventGetFloatValue(event, _TEMPERATURE_EVENT << 16)
            if 0 < value < 150:
                readings[name_buf.value.decode(errors="replace")] = value
        if event:
            cf.CFRelease(event)
        if name:
            cf.CFRelease(name)
    return readings
