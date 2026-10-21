import json
import os
import random

_HERE = os.path.dirname(__file__)
_POOL = None

CHROME_COLOR_DEPTH = 24


def _pool():
    global _POOL
    if _POOL is None:
        def _load(fn):
            with open(os.path.join(_HERE, fn), encoding="utf-8") as fh:
                return json.load(fh)
        misc = _load("devices_misc.json")
        _POOL = {
            "gpus": (_load("devices_gpus_1.json") + _load("devices_gpus_2.json") +
                     _load("devices_gpus_3.json") + _load("devices_gpus_4.json")),
            "screens": _load("devices_screens.json"),
            "cores": misc["cores"],
            "memory": misc["memory"],
            "network": misc["network"],
            "base": misc.get("base", {}),
        }
        for name in ("gpus", "screens", "cores", "memory", "network"):
            if not _POOL.get(name):
                raise RuntimeError("devices split files are missing pool %r" % name)
    return _POOL


def _pick(pool):
    total = sum(e["weight"] for e in pool)
    r = random.uniform(0, total)
    for entry in pool:
        r -= entry["weight"]
        if r <= 0:
            return entry
    return pool[-1]


def js_heap_size_limit(memory_gb):
    if memory_gb <= 4:
        return 2172649472
    elif memory_gb <= 8:
        return 4294705152
    else:
        return 8589934592


def _window_on(base, screen):
    dpr = screen.get("dpr", 1)
    sw = screen["width"]
    sh = screen["height"]
    aw = screen.get("availWidth", sw)
    ah = screen.get("availHeight", sh)
    taskbar = sh - ah
    iw = int(aw * 0.5)
    ih = ah - taskbar
    ow = iw + 16
    oh = ah
    return {
        "screen": {
            "width": sw,
            "height": sh,
            "availWidth": aw,
            "availHeight": ah,
            "colorDepth": screen.get("colorDepth", 24),
            "pixelDepth": screen.get("colorDepth", 24),
        },
        "devicePixelRatio": dpr,
        "innerWidth": iw,
        "innerHeight": ih,
        "outerWidth": ow,
        "outerHeight": oh,
    }


def generate_device():
    p = _pool()
    gpu = _pick(p["gpus"])
    screen = _pick(p["screens"])
    cores = _pick(p["cores"])["value"]
    memory = _pick(p["memory"])["value"]
    net = _pick(p["network"])
    win = _window_on(p["base"], screen)
    color_depth = win["screen"].get("colorDepth", CHROME_COLOR_DEPTH)
    return {
        **win,
        "color_depth": color_depth,
        "hardwareConcurrency": cores,
        "deviceMemory": memory,
        "device_memory": memory,
        "jsHeapSizeLimit": js_heap_size_limit(memory),
        "connection": {
            "effectiveType": "4g",
            "downlink": net["downlink"],
            "rtt": net["rtt"],
            "saveData": net["saveData"],
        },
        "glVendor": gpu.get("glVendor") or gpu.get("vendor", ""),
        "glRenderer": gpu.get("glRenderer") or gpu.get("renderer", ""),
    }


def pool_sizes():
    p = _pool()
    return {k: len(v) if isinstance(v, list) else 1 for k, v in p.items()}
