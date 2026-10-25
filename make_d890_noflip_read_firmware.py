#!/usr/bin/env python3
"""Make the 'R' read fully invisible: skip the PC-READ mode flip. On top of nopcm.

In the read handler FUN_0812efdc, the first read flips DAT_20004b98 to 0x2b ('+'),
which the screen dispatcher renders as the "PC READ" overlay and which freezes
input (nothing resets it, since we never send END). The handler already branches
PAST that flip block when it's redundant:
    0x0812eff8: beq.n 0x0812f044   (d0 24)   ; if DAT_20004b98 already 0x2b, skip flip
Making that branch UNCONDITIONAL skips the flip on every read:
    0x0812eff8: b.n   0x0812f044   (e0 24)
No call is removed (the flip block, incl. its BLs, simply isn't executed at read
time; it never runs at boot). 2-byte in-place edit, no size change.
"""
from __future__ import annotations
import argparse, hashlib
from pathlib import Path
from make_d890_morse_firmware import BASE, package_paths, patch_size_fields

PATCH_ADDR = 0x0812EFF8
ORIGINAL   = bytes.fromhex("24d0")   # beq.n 0x0812f044  (little-endian d0 24)
REPLACED   = bytes.fromhex("24e0")   # b.n   0x0812f044


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path, default=Path("patched_nopcm_read/D890UV_V1.05_20260521"))
    p.add_argument("--output-dir", type=Path, default=Path("patched_noflip_read"))
    args = p.parse_args()

    in_spi, in_cdi, in_cdd = package_paths(args.input_base)
    spi = bytearray(in_spi.read_bytes()); cdi = bytearray(in_cdi.read_bytes()); cdd = bytearray(in_cdd.read_bytes())
    if int.from_bytes(cdi[256:260], "little") != BASE:
        raise ValueError("unexpected base")
    if int.from_bytes(spi[6:10], "little") != len(cdd) or int.from_bytes(cdi[260:264], "little") != len(cdd):
        raise ValueError("size field mismatch")

    off = PATCH_ADDR - BASE
    have = bytes(cdd[off:off + 2])
    if have != ORIGINAL:
        raise ValueError(f"patch mismatch @0x{PATCH_ADDR:08x}: have {have.hex()} expect {ORIGINAL.hex()}")
    cdd[off:off + 2] = REPLACED
    patch_size_fields(spi, cdi, len(cdd))   # unchanged size; keeps fields consistent

    out = args.output_dir; out.mkdir(parents=True, exist_ok=True)
    base = out / "D890UV_V1.05_20260521"
    Path(str(base) + ".spi").write_bytes(spi); Path(str(base) + ".CDI").write_bytes(cdi); Path(str(base) + ".CDD").write_bytes(cdd)
    print(f"patched @0x{PATCH_ADDR:08x}: {have.hex()} -> {REPLACED.hex()} (beq -> b)")
    print(f"size={len(cdd)}/0x{len(cdd):x}  sha256_cdd={hashlib.sha256(cdd).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
