#!/usr/bin/env python3
"""Raise the UHF receive ceiling to 520 MHz. TX limits stay stock. On verified blecps (21e62ebb).

Mechanism: frequency entry, VFO stepping and channel tuning clamp against per-region
tables indexed by the band/region byte DAT_20015277 (loaded from the factory block at
external flash 0x4f80003; reads 0 on this radio). Values are u32 in 10 Hz units, 19
regions each. The RX tables (users FUN_08042330, FUN_08038390, FUN_080671b8,
FUN_08099ef8, ...) are separate from the TX-inhibit tables read by FUN_080cdd98 and
FUN_080d358c (sets the "out of band" flag that blocks PTT, message 0x12).

So rewriting only the RX UHF max table (0x0814c194) to 520 MHz lets you tune and listen
up to 520 while PTT is still refused above the region's TX max (480 for region 0).
AnyTone ships regions 11 and 14 with 400-520 RX on this same image, so the RF chain and
calibration code already handle 480-520.
"""
from __future__ import annotations
import argparse, hashlib, struct
from pathlib import Path
from make_d890_morse_firmware import BASE, package_paths, patch_size_fields

REGIONS = 19
RX_UHF_MAX = 0x0814C194
NEW_UHF_MAX = 52_000_000                    # 520.000 MHz in 10 Hz units
STOCK_RX_UHF_MAX = (480, 480, 440, 480, 438, 447, 447, 480, 470, 432,
                    480, 520, 490, 480, 520, 520, 440, 440, 480)
# Keypad entry, two layers (key handlers only see digit keys 1..10 = '0'..'9'):
# 1. First-digit filters reject anything but 1/2/4/5 before entry starts.
#    FUN_08066678 (key index, '5' = 6): `cmp r0,#6; beq accept` -> bhs.
#    FUN_080466d4 (digit value), twice: `cmp r0,#5; bne reject` -> bcc.
# 2. Band dispatch in FUN_080671b8 / FUN_08099ef8: `cmp r0,#'5'; bne <VHF/220 path>`
#    -> bcc, so first digits 5-9 clamp against the UHF table instead of VHF.
# {site: (cmp before, original branch, patched branch)}
KEYPAD_SITES = {
    0x0806677C: ("0628", "0ed0", "0ed2"),
    0x0804679C: ("0528", "01d1", "01d3"),
    0x08046810: ("0528", "01d1", "01d3"),
    0x080674B0: ("3528", "0bd1", "0bd3"),
    0x08067526: ("3528", "1dd1", "1dd3"),
    0x08099F2A: ("3528", "0bd1", "0bd3"),
    0x08099FC0: ("3528", "1dd1", "1dd3"),
}
# TX-inhibit tables: must come out byte-identical.
TX_TABLES = (0x0814C278, 0x0814C2C4, 0x0814C310, 0x0814C44C, 0x0814C498, 0x0814C4E4)


def table(cdd: bytes, addr: int) -> list[int]:
    return list(struct.unpack_from(f"<{REGIONS}I", cdd, addr - BASE))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path, default=Path("patched_blecps_verified/D890UV_V1.05_20260521"))
    p.add_argument("--output-dir", type=Path, default=Path("patched_rxwide"))
    p.add_argument("--uhf-max-mhz", type=int, default=NEW_UHF_MAX // 100_000,
                   help="RX ceiling in MHz; above 599 also patches keypad entry so 6xx-9xx digits work")
    args = p.parse_args()
    new_max = args.uhf_max_mhz * 100_000
    in_spi, in_cdi, in_cdd = package_paths(args.input_base)
    spi = bytearray(in_spi.read_bytes()); cdi = bytearray(in_cdi.read_bytes()); cdd = bytearray(in_cdd.read_bytes())
    if int.from_bytes(spi[6:10], "little") != len(cdd) or int.from_bytes(cdi[260:264], "little") != len(cdd):
        raise SystemExit("input size fields disagree with CDD length")

    if table(cdd, RX_UHF_MAX) != [mhz * 100_000 for mhz in STOCK_RX_UHF_MAX]:
        raise SystemExit("RX UHF max table does not match stock")
    tx_before = {a: table(cdd, a) for a in TX_TABLES}

    struct.pack_into(f"<{REGIONS}I", cdd, RX_UHF_MAX - BASE, *([new_max] * REGIONS))
    if args.uhf_max_mhz > 599:
        for site, (cmp, orig, new) in KEYPAD_SITES.items():
            off = site - BASE
            if cdd[off - 2:off] != bytes.fromhex(cmp) or cdd[off:off + 2] != bytes.fromhex(orig):
                raise SystemExit(f"0x{site:x}: expected {cmp} {orig}")
            cdd[off:off + 2] = bytes.fromhex(new)

    if any(table(cdd, a) != tx_before[a] for a in TX_TABLES):
        raise SystemExit("TX tables changed")
    print("RX UHF max:", [v // 100_000 for v in table(cdd, RX_UHF_MAX)])
    print("TX UHF max:", [v // 100_000 for v in table(cdd, 0x0814C278)])

    patch_size_fields(spi, cdi, len(cdd))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base = args.output_dir / in_cdd.stem
    Path(str(base) + ".spi").write_bytes(spi); Path(str(base) + ".CDI").write_bytes(cdi); Path(str(base) + ".CDD").write_bytes(cdd)
    print("CDD sha256", hashlib.sha256(cdd).hexdigest()[:16], "size", hex(len(cdd)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
