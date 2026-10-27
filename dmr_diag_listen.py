#!/usr/bin/env python3
"""Print every AT+DATA line the diagnostic build sends on FFE2, with a timestamp.

Lines: H1/H2 (DEMOD_OUT hooks fired), F<id> (baseband field seen), V<seq><hex> (CHAN_D frames).
Run from the Claude desktop Terminal panel (Bluetooth access). Ctrl-C to stop.

  .venv/bin/python dmr_diag_listen.py [log.txt] [seconds]
"""
import asyncio, sys, time
from bleak import BleakClient, BleakScanner

NAME_HINT = "ET25SE"
FFE2 = "0000ffe2-0000-1000-8000-00805f9b34fb"


async def main() -> None:
    log_path = sys.argv[1] if len(sys.argv) > 1 else "dmr_diag.log"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 300
    print(f"Scanning for {NAME_HINT}...")
    found = await BleakScanner.discover(timeout=15, return_adv=True)
    target = next((d for d, a in found.values() if NAME_HINT in (d.name or a.local_name or "")), None)
    if not target:
        raise SystemExit("Radio not found over BLE")

    buf = bytearray()
    log = open(log_path, "a")

    def on_notify(_h, data: bytearray) -> None:
        nonlocal buf
        buf += data
        while b"\n" in buf:
            line, _, rest = bytes(buf).partition(b"\n")
            buf = bytearray(rest)
            s = line.strip()
            if s:
                txt = s.decode("ascii", "replace")
                stamp = f"{time.time():.2f} {txt}"
                # V lines can be long; print a short tag, full text to the log
                print(stamp if len(txt) < 40 else f"{time.time():.2f} {txt[:16]}... ({len(txt)} ch)")
                log.write(stamp + "\n")
                log.flush()

    async with BleakClient(target) as c:
        await c.start_notify(FFE2, on_notify)
        print("Connected. Key up the DMR test now.")
        await asyncio.sleep(seconds)
    log.close()
    print("done")


asyncio.run(main())
