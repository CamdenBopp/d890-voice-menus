#!/usr/bin/env python3
"""Usage: item_probe.py SECONDS [image.CDD]

Live: print menu depth/page/cursor and the string b40[cursor] points at (resolved offline)."""
import glob, struct, sys, time
from pathlib import Path
import serial
sys.path.insert(0, str(Path(__file__).resolve().parent))
from menu_diag import R
d = open(sys.argv[2] if len(sys.argv) > 2 else "build/voiceitems/D890UV_V1.05_20260521.CDD", "rb").read(); B = 0x0800C000
def u32(a): return struct.unpack_from("<I", d, a - B)[0] if B <= a < B + len(d) - 4 else None
def s16(a):
    if a is None or not (B <= a < B + len(d)): return "?"
    o, out = a - B, ""
    while len(out) < 40:
        c = struct.unpack_from("<H", d, o)[0]
        if c == 0: break
        out += chr(c) if c < 0x80 else "~"; o += 2
    return out
end = time.monotonic() + float(sys.argv[1]); prev = None
while time.monotonic() < end:
    port = (glob.glob("/dev/cu.usbmodem*") or [None])[0]
    if port:
        try:
            with serial.Serial(port, 9600, timeout=1) as p:
                p.dtr = True; time.sleep(0.15)
                raw = {n: R(p, a, k) for n, a, k in [("depth", 0x200097E9, 1), ("page", 0x20004248, 1),
                       ("cur", 0x20004244, 1), ("cnt", 0x20008790, 1), ("b40", 0x20004B40, 4)]}
            if None in raw.values(): continue
            dep, pg, cur, cnt = (raw[k][0] for k in ("depth", "page", "cur", "cnt"))
            b40 = int.from_bytes(raw["b40"], "little")
            item = s16(u32(b40 + 4 * cur)) if u32(b40) else "-"
            s = f"depth={dep} page={pg} cur={cur} cnt={cnt} b40={b40:#x} item={item!r}"
            if s != prev: print(time.strftime("%H:%M:%S"), s, flush=True); prev = s
        except (OSError, serial.SerialException) as e:
            print("err", e, flush=True)
    time.sleep(1.5)
