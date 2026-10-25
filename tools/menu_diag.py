#!/usr/bin/env python3
"""Diagnose the menu-cue hook over USB (cable connected, normal mode).

Reads page id _DAT_20004248, cursor _DAT_20004250, and our shadow byte 0x2002a000
with light invisible R reads while the user navigates the settings menu. If the
cursor changes and the shadow TRACKS it, the hook fires and reads correctly (so the
problem is the beep call); if the shadow never updates, the hook isn't firing.
"""
from __future__ import annotations
import argparse, glob, time
import serial

VARS = [("page", 0x20004248, 4), ("cursor", 0x20004250, 4), ("shadow", 0x2002A000, 4)]


def rd(p, t=1.2):
    dl = time.monotonic() + t; b = bytearray(); last = None
    while time.monotonic() < dl:
        w = p.in_waiting
        if w:
            b.extend(p.read(w)); last = time.monotonic(); continue
        if last and time.monotonic() - last >= 0.1:
            break
        time.sleep(0.005)
    return bytes(b)


def R(p, a, n):
    p.reset_input_buffer()
    p.write(bytes([0x52, (a >> 24) & 255, (a >> 16) & 255, (a >> 8) & 255, a & 255, n])); p.flush()
    r = rd(p)
    return r[6:6 + n] if len(r) >= 6 + n and r[0] == 0x57 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=20)
    ap.add_argument("--gap", type=float, default=2.0)
    a = ap.parse_args()
    prev = None
    for it in range(a.iterations):
        port = (glob.glob("/dev/cu.usbmodem*") or [None])[0]
        tag = time.strftime("%H:%M:%S")
        if not port:
            print(f"[{tag}] no port"); time.sleep(a.gap); continue
        try:
            with serial.Serial(port, 9600, timeout=1) as p:
                p.dtr = True; time.sleep(0.15)
                vals = {}
                for name, addr, n in VARS:
                    d = R(p, addr, n)
                    vals[name] = (d[0] if d else None)  # low byte
            line = f"[{tag}] page={vals['page']} cursor={vals['cursor']} shadow={vals['shadow']}"
            if (vals['cursor'], vals['shadow']) != prev:
                line += "   <-- changed"
                prev = (vals['cursor'], vals['shadow'])
            print(line)
        except (OSError, serial.SerialException) as e:
            print(f"[{tag}] serial error: {e}")
        if it + 1 < a.iterations:
            time.sleep(a.gap)


if __name__ == "__main__":
    raise SystemExit(main())
