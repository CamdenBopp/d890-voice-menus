#!/usr/bin/env python3
"""Read the rxtap3 USB diagnostic: PROGRAM session, send \\x02, parse the 16-byte diag.

cmd 0xe (triggered by \\x02) returns our diag instead of the identity string:
  [0] 'D'  [1] HFLAGS  [2] FRAMECNT_lo  [3] SEQ  [4:8] TICK(LE)  [8:16] SEEN[0..7]

  .venv/bin/python read_rxtap3_diag.py [port] [--poll N]
"""
import sys, time, serial

PORT = "/dev/cu.usbmodem9A419A4143121"


def read_available(s, quiet=0.05, timeout=1.5):
    end = time.monotonic() + timeout
    last = None
    buf = bytearray()
    while time.monotonic() < end:
        n = s.in_waiting
        if n:
            buf += s.read(n)
            last = time.monotonic()
        elif last and time.monotonic() - last >= quiet:
            break
        else:
            time.sleep(0.005)
    return bytes(buf)


def parse(d: bytes) -> str:
    if len(d) < 16 or d[0] != 0x44:
        return f"  no diag marker (got {d.hex()!r})"
    hflags = d[1]; framecnt = d[2]; seq = d[3]
    tick = int.from_bytes(d[4:8], "little")
    seen = d[8:16]
    ids = [i for i in range(64) if seen[i >> 3] & (1 << (i & 7))]
    return (f"  TICK={tick}  HFLAGS={hflags:#04x} "
            f"(pm={'Y' if hflags&1 else 'N'} io={'Y' if hflags&2 else 'N'})  "
            f"FRAMECNT={framecnt}  SEQ={seq}\n"
            f"  dispatcher field ids seen: {[hex(i) for i in ids] or 'none'}  "
            f"(CHAN_D 0x01 {'YES' if 0x01 in ids else 'no'})")


def one_read(s) -> bytes:
    s.reset_input_buffer()
    s.write(b"\x02"); s.flush()
    return read_available(s)


def main() -> int:
    port = PORT
    poll = 1
    args = sys.argv[1:]
    if "--poll" in args:
        i = args.index("--poll"); poll = int(args[i + 1]); del args[i:i + 2]
    if args:
        port = args[0]
    s = serial.Serial(); s.port = port; s.baudrate = 921600; s.timeout = 0.3
    s.dtr = True; s.rts = True
    s.open()
    s.reset_input_buffer()
    s.write(b"PROGRAM"); s.flush()
    r = read_available(s)
    if r not in (b"\x06", b"QX\x06"):
        print(f"PROGRAM failed: {r.hex()!r}"); return 1
    print(f"PROGRAM ok ({r.hex()})")
    try:
        for k in range(poll):
            d = one_read(s)
            print(f"[{k}] {d.hex()}")
            print(parse(d))
            if poll > 1:
                time.sleep(1.0)
    finally:
        s.write(b"END"); s.flush(); read_available(s); s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
