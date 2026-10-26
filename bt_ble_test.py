#!/usr/bin/env python3
"""BLE round-trip test: send 'T' to FFE3, listen for reply on FFE2.

Usage: bt_ble_test.py [T|R <addr_hex> <len>]
  bt_ble_test.py            → sends 'T' test command
  bt_ble_test.py R 0x2000420c 2  → sends R-command
"""
import asyncio
import struct
import sys

from bleak import BleakClient, BleakScanner

NAME_HINT = "ET25SE"
FFE3 = "0000ffe3-0000-1000-8000-00805f9b34fb"   # write
FFE2 = "0000ffe2-0000-1000-8000-00805f9b34fb"   # notify

frames = []


async def main():
    # Parse command
    if len(sys.argv) < 2 or sys.argv[1] == "T":
        cmd = b"T"
        print("Command: T (test echo)")
    elif sys.argv[1] == "R":
        addr = int(sys.argv[2], 0)
        length = int(sys.argv[3]) if len(sys.argv) > 3 else 4
        cmd = b"R" + struct.pack(">I", addr) + bytes([length])
        print(f"Command: R (read {length} B @ {addr:#010x})")
    else:
        sys.exit(f"Unknown command: {sys.argv[1]}")

    print(f"Scanning for {NAME_HINT}...")
    found = await BleakScanner.discover(timeout=15, return_adv=True)
    target = next((d for d, a in found.values() if NAME_HINT in (d.name or a.local_name or "")), None)
    if not target:
        raise SystemExit("Radio not found over BLE")

    print(f"Connecting to {target.name}...")
    async with BleakClient(target) as c:
        def on_notify(_h, data):
            frames.append(bytes(data))
            print(f"  FFE2 notify ({len(data)} B): {bytes(data).hex()}")
            try:
                print(f"    ascii: {bytes(data)!r}")
            except Exception:
                pass

        await c.start_notify(FFE2, on_notify)
        await asyncio.sleep(0.3)

        print(f"Writing to FFE3: {cmd.hex()}")
        await c.write_gatt_char(FFE3, cmd, response=True)

        print("Waiting 5s for reply...")
        await asyncio.sleep(5)

    if not frames:
        print("\nNo reply on FFE2.")
    else:
        payload = b"".join(frames)
        print(f"\nTotal received: {len(payload)} bytes")
        print(f"  hex: {payload.hex()}")
        print(f"  ascii: {payload!r}")


asyncio.run(main())
