#!/usr/bin/env python3
"""BLE-to-CPS bridge: runs the AnyTone read protocol over BLE.

Connects to the radio over BLE (FFE3 write, FFE2 notify), then runs the
same CPS protocol as anytone_readonly_dump.py — PROGRAM, identity, R-commands.

Usage (from Terminal.app for BT TCC):
  ble_cps_bridge.py read 0x02FA0000:0x100
  ble_cps_bridge.py read 0x02FA0000:0x100 --out dump.bin
  ble_cps_bridge.py identity
  ble_cps_bridge.py read-preset local-info

The radio must be running the blecps firmware (patched_blecps/).
"""
from __future__ import annotations
import asyncio
import argparse
import struct
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

NAME_HINT = "ET25SE"
FFE3 = "0000ffe3-0000-1000-8000-00805f9b34fb"   # write char
FFE2 = "0000ffe2-0000-1000-8000-00805f9b34fb"   # notify char
ACK = 0x06
CHUNK_SIZE = 0x10  # 16 bytes per read (BLE MTU safe)
REPLY_TIMEOUT = 5.0

PRESETS = {
    "local-info": (0x02FA0000, 0x100),
    "local-info-extended": (0x02FA0000, 0x40000),
}


class BLERadio:
    def __init__(self, client: BleakClient):
        self.client = client
        self.rx_data = bytearray()
        self.rx_event = asyncio.Event()

    def on_notify(self, _handle, data: bytes):
        self.rx_data.extend(data)
        self.rx_event.set()

    async def send(self, data: bytes):
        self.rx_data.clear()
        self.rx_event.clear()
        await self.client.write_gatt_char(FFE3, data, response=True)

    async def recv(self, expected_len: int, timeout: float = REPLY_TIMEOUT) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self.rx_data) < expected_len:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.rx_event.clear()
            try:
                await asyncio.wait_for(self.rx_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
        result = bytes(self.rx_data[:expected_len])
        self.rx_data = self.rx_data[expected_len:]
        return result

    async def send_program(self):
        await self.send(b"PROGRAM")
        resp = await self.recv(1, timeout=3.0)
        if resp == bytes([ACK]):
            return resp
        resp2 = await self.recv(2, timeout=1.0)
        full = resp + resp2
        if ACK in full:
            return full
        raise RuntimeError(f"PROGRAM: unexpected response {full.hex()}")

    async def send_identity(self) -> bytes:
        await self.send(b"\x02")
        resp = await self.recv(16, timeout=REPLY_TIMEOUT)
        if len(resp) >= 16 and resp[14] == ACK:
            return bytes(resp[:16])
        if len(resp) >= 1 and ACK in resp:
            return bytes(resp)
        raise RuntimeError(f"identity: no ACK in {len(resp)} B: {resp.hex()}")

    async def send_end(self):
        await self.send(b"END")
        await asyncio.sleep(0.3)

    async def read_memory(self, address: int, length: int, chunk_size: int = CHUNK_SIZE) -> bytes:
        result = bytearray()
        cursor = address
        remaining = length
        total = length
        while remaining > 0:
            count = min(chunk_size, remaining)
            cmd = bytes([
                ord("R"),
                (cursor >> 24) & 0xFF,
                (cursor >> 16) & 0xFF,
                (cursor >> 8) & 0xFF,
                cursor & 0xFF,
                count,
            ])
            await self.send(cmd)
            expected_len = 1 + 4 + 1 + count + 1 + 1  # W + addr + len + data + checksum + ACK
            resp = await self.recv(expected_len)

            if len(resp) < expected_len:
                raise RuntimeError(
                    f"read @0x{cursor:08x}: expected {expected_len} B, got {len(resp)}: {resp.hex()}"
                )
            if resp[0] != ord("W"):
                raise RuntimeError(f"read @0x{cursor:08x}: not W response: {resp.hex()}")
            echoed = int.from_bytes(resp[1:5], "big")
            if echoed != cursor:
                raise RuntimeError(f"read @0x{cursor:08x}: addr echo 0x{echoed:08x}")
            if resp[5] != count:
                raise RuntimeError(f"read @0x{cursor:08x}: len echo {resp[5]} != {count}")
            checksum = sum(resp[1:-2]) & 0xFF
            if resp[-2] != checksum:
                raise RuntimeError(
                    f"read @0x{cursor:08x}: checksum 0x{resp[-2]:02x} != 0x{checksum:02x}"
                )
            if resp[-1] != ACK:
                raise RuntimeError(f"read @0x{cursor:08x}: no ACK")

            data = resp[6:-2]
            result.extend(data)
            cursor += count
            remaining -= count
            done = total - remaining
            pct = done * 100 // total
            print(f"\r  {done}/{total} bytes ({pct}%)", end="", flush=True)

        print()
        return bytes(result)


async def main_async(args):
    print(f"Scanning for {NAME_HINT}...")
    found = await BleakScanner.discover(timeout=15, return_adv=True)
    target = next(
        (d for d, a in found.values() if NAME_HINT in (d.name or a.local_name or "")),
        None,
    )
    if not target:
        raise SystemExit("Radio not found over BLE")

    print(f"Connecting to {target.name}...")
    async with BleakClient(target) as client:
        radio = BLERadio(client)
        await client.start_notify(FFE2, radio.on_notify)
        await asyncio.sleep(0.3)

        if args.command == "identity":
            print("Sending PROGRAM...")
            await radio.send_program()
            print("Requesting identity...")
            ident = await radio.send_identity()
            print(f"Identity ({len(ident)} B): {ident!r}")
            print(f"  hex: {ident.hex()}")
            await radio.send_end()
            return

        # read or read-preset
        if args.command == "read-preset":
            if args.preset not in PRESETS:
                raise SystemExit(f"Unknown preset: {args.preset}. Available: {list(PRESETS)}")
            address, length = PRESETS[args.preset]
        else:
            parts = args.range.split(":")
            address = int(parts[0], 0)
            length = int(parts[1], 0)

        chunk = args.chunk_size if hasattr(args, "chunk_size") and args.chunk_size else CHUNK_SIZE

        print("Sending PROGRAM...")
        await radio.send_program()
        print(f"Reading 0x{address:08x}:{length} ({length} bytes, chunk={chunk})...")
        data = await radio.read_memory(address, length, chunk_size=chunk)
        await radio.send_end()
        print(f"Read {len(data)} bytes")

        if args.out:
            Path(args.out).write_bytes(data)
            print(f"Saved to {args.out}")
        else:
            # Hex dump first 256 bytes
            for i in range(0, min(len(data), 256), 16):
                row = data[i:i+16]
                hex_part = " ".join(f"{b:02x}" for b in row)
                ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
                print(f"  {address + i:08x}: {hex_part:<48s} {ascii_part}")
            if len(data) > 256:
                print(f"  ... ({len(data) - 256} more bytes, use --out to save)")


def main():
    p = argparse.ArgumentParser(description="BLE CPS bridge for AnyTone D890UV")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("identity", help="Read radio identity string")

    rp = sub.add_parser("read", help="Read memory range")
    rp.add_argument("range", help="START:LENGTH in hex, e.g. 0x02FA0000:0x100")
    rp.add_argument("--out", help="Output file")
    rp.add_argument("--chunk-size", type=lambda x: int(x, 0), default=CHUNK_SIZE)

    pp = sub.add_parser("read-preset", help="Read a named preset range")
    pp.add_argument("preset", choices=sorted(PRESETS))
    pp.add_argument("--out", help="Output file")

    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
