#!/usr/bin/env python3
"""Stream captured DMR voice frames off the radio live over USB (rxstream build).

Polls the SRAM ring the firmware fills (WRIDX + 64 slots of 28 bytes) with the stock 'R' read in
NORMAL mode (no PROGRAM, UI not suspended). Emits each 27-byte CHAN_D frame in order, detects
overruns, and writes raw frames to a file.

  .venv/bin/python stream_rxframes.py [seconds] [outfile] [--port P]
"""
import sys, time, serial

PORT = "/dev/cu.usbmodem9A419A4143121"
WRIDX = 0x2002A304
RING = 0x2002A320
NSLOTS = 64
SLOT = 28


def read_available(s, quiet=0.04, timeout=0.6):
    end = time.monotonic() + timeout
    last = None
    buf = bytearray()
    while time.monotonic() < end:
        n = s.in_waiting
        if n:
            buf += s.read(n); last = time.monotonic()
        elif last and time.monotonic() - last >= quiet:
            break
        else:
            time.sleep(0.003)
    return bytes(buf)


def R(s, addr, n):
    cmd = bytes([ord("R"), (addr >> 24) & 255, (addr >> 16) & 255, (addr >> 8) & 255, addr & 255, n])
    for _ in range(4):
        s.reset_input_buffer(); s.write(cmd); s.flush()
        r = read_available(s)
        if len(r) >= 6 + n + 2 and r[0] == ord("W") and int.from_bytes(r[1:5], "big") == addr:
            return r[6:6 + n]
        time.sleep(0.02)
    return None


def main() -> int:
    args = sys.argv[1:]
    port = PORT
    if "--port" in args:
        i = args.index("--port"); port = args[i + 1]; del args[i:i + 2]
    seconds = float(args[0]) if args else 60.0
    outfile = args[1] if len(args) > 1 else "rxframes.bin"

    s = serial.Serial(); s.port = port; s.baudrate = 921600; s.timeout = 0.3
    s.dtr = True; s.rts = True; s.open()

    w = R(s, WRIDX, 2)
    if w is None:
        print("cannot read WRIDX (is the radio booted with rxstream?)"); return 1
    last = int.from_bytes(w, "little")
    print(f"start WRIDX={last}; streaming {seconds:.0f}s to {outfile}")

    total = 0; drops = 0; buzz = 0
    f = open(outfile, "wb")
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            w = R(s, WRIDX, 2)
            if w is None:
                continue
            cur = int.from_bytes(w, "little")
            avail = (cur - last) & 0xFFFF
            if avail == 0:
                time.sleep(0.03); continue
            if avail > NSLOTS:
                drops += avail - NSLOTS
                last = (cur - NSLOTS) & 0xFFFF
                avail = NSLOTS
            for _ in range(avail):
                slot = last & (NSLOTS - 1)
                d = R(s, RING + slot * SLOT, SLOT)
                last = (last + 1) & 0xFFFF
                if d is None:
                    continue
                frame, tag = d[:27], d[27]
                total += 1
                # 3 identical 9-byte AMBE sub-frames = a steady tone (our buzz)
                if frame[0:9] == frame[9:18] == frame[18:27] and any(frame):
                    buzz += 1
                f.write(frame)
            if total and total % 20 == 0:
                print(f"  {total} frames (buzz-pattern {buzz}, drops {drops})  last={d.hex()[:18]}...")
    finally:
        f.close(); s.close()
    print(f"done: {total} frames, {buzz} steady-tone, {drops} dropped -> {outfile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
