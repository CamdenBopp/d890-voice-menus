#!/usr/bin/env python3
"""Build a D890UV V1.05 STM32 firmware package with a Morse CQ audio proof.

The patch is intentionally small:
  * append a Thumb stub in the unused flash tail at 0x081a6000
  * replace the stock board-ready ATEST=1 send call with BL morse_stub
  * change the UTF-16 welcome text to "CW CQ   " for visible identification

The stub starts the stock audio-test path, enables the audio route, and gates
SPKEN=1/SPKEN=0 as Morse "CQ" (-.-. --.-). It then returns to the original
stock board-ready flow.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


BASE = 0x0800C000
HOOK_ADDR = 0x0810EE2A
HOOK_ORIGINAL = bytes.fromhex("c8f7d1fb")  # BL 0x080d75d0 from stock ATEST=1 send
STUB_ADDR = 0x081A6000

# Thumb code assembled from the source in docs/morse_stub.S notes below. It uses
# only local relative branches plus BLX through literal absolute Thumb addresses:
#   0x080d75d1 = stock board-command queue function
#   0x0811ec65 = stock millisecond delay helper
# Strings are stock command literals: ATEST=1, AUDIO=1/0, SPKEN=1/0.
STUB = bytes.fromhex(
    "f0b581b000f045f840f2f41000f062f800f044f840f2c80000f05cf8"
    "00f026f800f018f800f022f800f014f800f02bf800f01cf800f01af8"
    "00f00cf800f016f800f03bf840f2c80000f044f800f02bf801b0f0bd"
    "10b500f02bf8642000f03af800f02bf8642000f035f810bd10b500f0"
    "1ff840f22c1000f02df800f01ef8642000f028f810bd10b540f2c800"
    "00f022f810bd192011490722002313e00b201049072200230ee00a20"
    "0e490722002309e00d200d490722002304e00d200b4907220023ffe7"
    "10b582b000240094084ca04702b010bd10b5074ca04710bd3ca41408"
    "e7a3140874a31408f7a3140896a31408d1750d0865ec1108"
)


def package_paths(base: Path) -> tuple[Path, Path, Path]:
    base_s = str(base)
    if base_s.lower().endswith(".spi"):
        base_s = base_s[:-4]
    return Path(base_s + ".spi"), Path(base_s + ".CDI"), Path(base_s + ".CDD")


def thumb_bl(src: int, target: int) -> bytes:
    """Encode a Thumb-2 BL from src to target."""
    offset = (target & ~1) - (src + 4)
    if offset % 2:
        raise ValueError(f"unaligned BL offset {offset}")
    imm = offset >> 1
    if imm < -(1 << 23) or imm >= (1 << 23):
        raise ValueError(f"BL target out of range: src=0x{src:x} target=0x{target:x}")
    if imm < 0:
        imm = (1 << 24) + imm

    s = (imm >> 23) & 1
    i1 = (imm >> 22) & 1
    i2 = (imm >> 21) & 1
    imm10 = (imm >> 11) & 0x3FF
    imm11 = imm & 0x7FF
    j1 = (~(i1 ^ s)) & 1
    j2 = (~(i2 ^ s)) & 1
    hw1 = 0xF000 | (s << 10) | imm10
    hw2 = 0xF800 | (j1 << 13) | (j2 << 11) | imm11
    return hw1.to_bytes(2, "little") + hw2.to_bytes(2, "little")


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def patch_size_fields(spi: bytearray, cdi: bytearray, size: int) -> None:
    spi[6:10] = size.to_bytes(4, "little")
    cdi[260:264] = size.to_bytes(4, "little")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input-base",
        type=Path,
        default=Path("firmware_d890/extracted/D890UV_V1.05_DMR FW/D890UV_V1.05_20260521"),
        help="Input firmware path without extension, or the .spi path.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("modded_firmware/d890_v105_morse_cq"),
    )
    args = parser.parse_args()

    in_spi_path, in_cdi_path, in_cdd_path = package_paths(args.input_base)
    spi = bytearray(in_spi_path.read_bytes())
    cdi = bytearray(in_cdi_path.read_bytes())
    cdd = bytearray(in_cdd_path.read_bytes())

    declared = int.from_bytes(spi[6:10], "little")
    start = int.from_bytes(cdi[256:260], "little")
    cdi_size = int.from_bytes(cdi[260:264], "little")
    if start != BASE:
        raise ValueError(f"unexpected base address 0x{start:08x}")
    if declared != len(cdd) or cdi_size != len(cdd):
        raise ValueError(f"size mismatch: spi={declared} cdi={cdi_size} cdd={len(cdd)}")

    hook_off = HOOK_ADDR - BASE
    if bytes(cdd[hook_off : hook_off + 4]) != HOOK_ORIGINAL:
        got = bytes(cdd[hook_off : hook_off + 4]).hex()
        raise ValueError(f"hook bytes changed at 0x{HOOK_ADDR:08x}: got {got}")
    cdd[hook_off : hook_off + 4] = thumb_bl(HOOK_ADDR, STUB_ADDR)

    welcome = "Welcome!".encode("utf-16le")
    marker = "CW CQ   ".encode("utf-16le")
    welcome_off = cdd.find(welcome)
    if welcome_off < 0:
        raise ValueError("could not find UTF-16 Welcome! marker")
    cdd[welcome_off : welcome_off + len(welcome)] = marker

    stub_off = STUB_ADDR - BASE
    if len(cdd) > stub_off:
        raise ValueError("stock image already overlaps the chosen stub address")
    cdd.extend(b"\xff" * (stub_off - len(cdd)))
    cdd.extend(STUB)
    cdd.extend(b"\xff" * (align_up(len(cdd), 32) - len(cdd)))

    patch_size_fields(spi, cdi, len(cdd))

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    out_base = out / "D890UV_V1.05_MORSE_CQ"
    out_spi = Path(str(out_base) + ".spi")
    out_cdi = Path(str(out_base) + ".CDI")
    out_cdd = Path(str(out_base) + ".CDD")
    out_spi.write_bytes(spi)
    out_cdi.write_bytes(cdi)
    out_cdd.write_bytes(cdd)

    print(f"wrote {out_spi}")
    print(f"wrote {out_cdi}")
    print(f"wrote {out_cdd}")
    print(f"new_size={len(cdd)} / 0x{len(cdd):x}")
    print(f"stub_addr=0x{STUB_ADDR:08x} stub_len={len(STUB)}")
    print(f"hook_addr=0x{HOOK_ADDR:08x} hook_bl={thumb_bl(HOOK_ADDR, STUB_ADDR).hex()}")
    print(f"sha256_cdd={hashlib.sha256(cdd).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
