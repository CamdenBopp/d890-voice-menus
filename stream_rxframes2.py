#!/usr/bin/env python3
"""Stream captured DMR voice off the radio (rxstream2: full 36-byte CHAN_D = 4 AMBE frames).

Ring: 64 slots x 40 bytes at 0x2002a360 (36 payload + [36]=sub + [37]=tag), WRIDX u16 @0x2002a304.
A sub of 0x24 = 4 valid frames (36 B), 0x1b = 3 frames (27 B). Reads live with the stock 'R' command
in NORMAL mode. Also reports the C24/C1b sub-type counts.

  .venv/bin/python stream_rxframes2.py [seconds] [outfile] [--port P]
"""
import sys, time, serial

PORT = "/dev/cu.usbmodem9A419A4143121"
WRIDX = 0x2002A304
C24, C1B = 0x2002A30C, 0x2002A30E
RING = 0x2002A360
NSLOTS, SLOT = 64, 40


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


def u16(s, a):
    d = R(s, a, 2)
    return int.from_bytes(d, "little") if d else None


def main() -> int:
    args = sys.argv[1:]
    port = PORT
    if "--port" in args:
        i = args.index("--port"); port = args[i + 1]; del args[i:i + 2]
    seconds = float(args[0]) if args else 30.0
    outfile = args[1] if len(args) > 1 else "rxframes2.bin"

    s = serial.Serial(); s.port = port; s.baudrate = 921600; s.timeout = 0.3
    s.dtr = True; s.rts = True; s.open()

    w = u16(s, WRIDX)
    if w is None:
        print("cannot read WRIDX (radio booted with rxstream2?)"); return 1
    last = w
    print(f"start WRIDX={last}; streaming {seconds:.0f}s -> {outfile}")

    total = 0; frames = 0; drops = 0
    f = open(outfile, "wb")
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            cur = u16(s, WRIDX)
            if cur is None:
                continue
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
                sub = d[36]
                nbytes = 36 if sub == 0x24 else 27 if sub == 0x1b else 36
                f.write(d[:nbytes])
                total += 1; frames += nbytes // 9
            if total and total % 20 == 0:
                print(f"  {total} packets, {frames} frames, drops {drops}")
    finally:
        f.close()
        c24 = u16(s, C24); c1b = u16(s, C1B); s.close()
    print(f"done: {total} packets, {frames} frames, {drops} dropped; C24={c24} C1b={c1b} -> {outfile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
