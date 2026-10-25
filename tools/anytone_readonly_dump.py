#!/usr/bin/env python3
"""Read-only AnyTone memory dumper/prober.

This is based on the public AT-D878UV programming protocol and the newer
anytone-cps-py read path. It intentionally implements no write commands.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import serial
    import serial.tools.list_ports
except ImportError as exc:
    raise SystemExit("pyserial is required: python3 -m pip install pyserial") from exc


ACK = b"\x06"


@dataclass(frozen=True)
class Range:
    start: int
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length


PRESETS: dict[str, list[Range]] = {
    # Small device-identifying block used by anytone-cps-py.
    "local-info": [Range(0x02FA0000, 0x100)],
    # Larger local-info window used by anytone-cps-py for "extended" reads.
    "local-info-extended": [Range(0x02FA0000, 0x40000)],
}


def parse_int(value: str) -> int:
    return int(value.replace("_", ""), 0)


def parse_range(value: str) -> Range:
    if ":" not in value:
        raise argparse.ArgumentTypeError("range must be START:LENGTH, e.g. 0x2fa0000:0x100")
    start_s, length_s = value.split(":", 1)
    start = parse_int(start_s)
    length = parse_int(length_s)
    if start < 0 or length <= 0:
        raise argparse.ArgumentTypeError("range start must be >= 0 and length must be > 0")
    return Range(start, length)


def read_available(port: serial.Serial, quiet_time: float = 0.05, timeout: float = 2.0) -> bytes:
    deadline = time.monotonic() + timeout
    last_data_at = None
    data = bytearray()

    while time.monotonic() < deadline:
        waiting = port.in_waiting
        if waiting:
            data.extend(port.read(waiting))
            last_data_at = time.monotonic()
            continue
        if last_data_at is not None and time.monotonic() - last_data_at >= quiet_time:
            break
        time.sleep(0.005)

    return bytes(data)


def start_programming(port: serial.Serial, timeout: float) -> bytes:
    port.reset_input_buffer()
    port.write(b"PROGRAM")
    port.flush()
    response = read_available(port, timeout=timeout)
    if response not in (ACK, b"QX\x06"):
        raise RuntimeError(f"unexpected PROGRAM response: {response.hex()} ({response!r})")
    return response


def end_programming(port: serial.Serial, timeout: float) -> bytes:
    port.write(b"END")
    port.flush()
    return read_available(port, timeout=timeout)


def read_identity(port: serial.Serial, timeout: float) -> bytes:
    port.write(b"\x02")
    port.flush()
    response = read_available(port, timeout=timeout)
    if not response.endswith(ACK):
        raise RuntimeError(f"identity response did not end in ACK: {response.hex()} ({response!r})")
    return response


def read_memory(
    port: serial.Serial,
    address: int,
    length: int,
    chunk_size: int,
    retries: int,
    timeout: float,
) -> bytes:
    result = bytearray()
    cursor = address
    remaining = length

    while remaining:
        count = min(chunk_size, remaining)
        command = bytes(
            [
                ord("R"),
                (cursor >> 24) & 0xFF,
                (cursor >> 16) & 0xFF,
                (cursor >> 8) & 0xFF,
                cursor & 0xFF,
                count,
            ]
        )

        for attempt in range(1, retries + 1):
            port.write(command)
            port.flush()
            response = read_available(port, timeout=timeout)
            expected_len = 1 + 4 + 1 + count + 1 + 1

            if len(response) != expected_len:
                last_error = f"expected {expected_len} bytes, got {len(response)}: {response.hex()}"
                continue
            if response[0] != ord("W"):
                last_error = f"response command was not W: {response.hex()}"
                continue
            echoed_address = int.from_bytes(response[1:5], "big")
            if echoed_address != cursor:
                last_error = f"address echo mismatch: expected 0x{cursor:08x}, got 0x{echoed_address:08x}"
                continue
            if response[5] != count:
                last_error = f"length echo mismatch: expected {count}, got {response[5]}"
                continue
            checksum = sum(response[1:-2]) & 0xFF
            if response[-2] != checksum:
                last_error = f"checksum mismatch at 0x{cursor:08x}: expected 0x{checksum:02x}, got 0x{response[-2]:02x}"
                continue
            if response[-1] != ACK[0]:
                last_error = f"response did not end in ACK: {response.hex()}"
                continue

            result.extend(response[6:-2])
            break
        else:
            raise RuntimeError(f"read failed at 0x{cursor:08x}: {last_error}")

        cursor += count
        remaining -= count

    return bytes(result)


def list_ports() -> None:
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return
    for item in ports:
        print(f"{item.device}\t{item.description}\t{item.hwid}")


def parse_chunk_size(value: str) -> int:
    chunk_size = parse_int(value)
    if not 1 <= chunk_size <= 255:
        raise argparse.ArgumentTypeError("chunk size must be between 1 and 255")
    return chunk_size


def write_range(output_dir: Path, index: int, memory_range: Range, data: bytes) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"dump_{index:02d}_0x{memory_range.start:08x}_0x{memory_range.length:x}.bin"
    path.write_bytes(data)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe and dump AnyTone memory over the programming serial port.")
    parser.add_argument("--list-ports", action="store_true", help="List detected serial ports and exit.")
    parser.add_argument("--port", help="Serial device, e.g. /dev/cu.usbmodemXXXX, COM7, or /dev/ttyACM0.")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate. AnyTone CDC devices often ignore this.")
    parser.add_argument("--timeout", type=float, default=2.0, help="Serial timeout in seconds.")
    parser.add_argument("--chunk-size", type=parse_chunk_size, default=0xF0)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--range", action="append", type=parse_range, dest="ranges", help="Read START:LENGTH.")
    parser.add_argument("--preset", choices=sorted(PRESETS), action="append", help="Named range preset.")
    parser.add_argument("--out-dir", default="dumps", help="Directory for binary dump files.")
    args = parser.parse_args()

    if args.list_ports:
        list_ports()
        return 0

    if not args.port:
        parser.error("--port is required unless --list-ports is used")

    ranges: list[Range] = []
    for preset in args.preset or []:
        ranges.extend(PRESETS[preset])
    ranges.extend(args.ranges or [])

    with serial.Serial(args.port, args.baud, timeout=args.timeout, write_timeout=args.timeout) as port:
        program_response = start_programming(port, args.timeout)
        print(f"PROGRAM response: {program_response.hex()} ({program_response!r})")
        try:
            identity = read_identity(port, args.timeout)
            printable = identity[:-1].replace(b"\x00", b".")
            print(f"Identity: {identity.hex()} ({printable!r})")

            for index, memory_range in enumerate(ranges):
                print(f"Reading 0x{memory_range.start:08x}..0x{memory_range.end - 1:08x} ({memory_range.length} bytes)")
                data = read_memory(
                    port,
                    memory_range.start,
                    memory_range.length,
                    args.chunk_size,
                    args.retries,
                    args.timeout,
                )
                path = write_range(Path(args.out_dir), index, memory_range, data)
                print(f"Wrote {len(data)} bytes to {path}")
        finally:
            end_response = end_programming(port, args.timeout)
            print(f"END response: {end_response.hex()} ({end_response!r})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
