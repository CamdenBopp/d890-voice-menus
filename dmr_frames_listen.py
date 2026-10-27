#!/usr/bin/env python3
"""Listen for DMR voice bursts forwarded over BLE by the patched_dmrframes build.

Each burst arrives on FFE2 as "V<seq hex2><frames hex>\\r\\n" (27 frame bytes = three AMBE+2
frames per 60 ms). Notifications may split or merge lines, so they are reassembled here.
Prints one line per second: bursts, rate, dropped seqs. Logs every burst to a file and
speaks the first burst and a summary with Samantha. Run from Terminal.app (Bluetooth TCC).

  .venv/bin/python dmr_frames_listen.py [log.txt] [seconds]
"""
import asyncio, subprocess, sys, time
from bleak import BleakClient, BleakScanner

NAME_HINT = "ET25SE"
FFE2 = "0000ffe2-0000-1000-8000-00805f9b34fb"


def say(text: str) -> None:
    subprocess.Popen(["say", "-v", "Samantha", text])


async def main() -> None:
    log_path = sys.argv[1] if len(sys.argv) > 1 else "dmr_frames.log"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 600
    print(f"Scanning for {NAME_HINT}...")
    found = await BleakScanner.discover(timeout=15, return_adv=True)
    target = next((d for d, a in found.values() if NAME_HINT in (d.name or a.local_name or "")), None)
    if not target:
        say("Radio not found over Bluetooth")
        raise SystemExit("Radio not found over BLE")

    buf = bytearray()
    total = drops = window = 0
    last_seq = None
    log = open(log_path, "a")

    def on_notify(_h, data: bytearray) -> None:
        nonlocal buf, total, drops, window, last_seq
        buf += data
        while b"\n" in buf:
            line, _, buf = bytes(buf).partition(b"\n")
            buf = bytearray(buf)
            line = line.strip()
            if not line.startswith(b"V") or len(line) < 3:
                continue
            seq = int(line[1:3], 16)
            if last_seq is not None and seq != (last_seq + 1) & 0xFF:
                drops += (seq - last_seq - 1) & 0xFF
            last_seq = seq
            if total == 0:
                say("First voice frame received")
            total += 1
            window += 1
            log.write(f"{time.time():.3f} {line.decode()}\n")

    async with BleakClient(target) as c:
        await c.start_notify(FFE2, on_notify)
        say("Listening for D M R frames")
        print("Listening. Key up a DMR radio on the D890's channel.")
        end = time.time() + seconds
        while time.time() < end:
            await asyncio.sleep(1)
            if window:
                print(f"{window:3d} bursts/s  total {total}  dropped {drops}")
            window = 0
    log.close()
    summary = f"{total} bursts, {drops} dropped"
    print(summary)
    say(summary)


asyncio.run(main())
