#!/usr/bin/env python3
"""Build D890UV V1.05 firmware with a USB "RAM peek" read hook.

Why: the stock CPS 'R' read (anytone_readonly_dump.py: R + 4-byte BE addr + len)
reaches only the external codeplug flash. Physical STM32 SRAM reads back as 0xFF.
This patch makes any SRAM address (top byte 0x20) readable over the existing USB
protocol, so the live audio-board command queue (ids @0x20003424, lens @0x2000318e,
payloads @0x200031ad, counter @0x20003833) and any RAM var can be dumped with the
unchanged anytone_readonly_dump.py.

Mechanism (proven BL-redirect recipe, no boot-critical code touched):
  The read handler FUN_0812efdc copies bytes into the reply via two calls to
  FUN_08037a9c(addr,&buf,len) -- the paged codeplug-flash reader -- at
  0x0812f0f4 and 0x0812f110. We redirect BOTH to a stub appended at the stock
  image end (0x081a5008). The stub re-reads the TRUE requested address from the
  request bytes at 0x2001bb17 (big-endian), and:
    * if (addr >> 24) == 0x20  -> direct byte copy from that SRAM address to the
      reply buffer (len bytes); the stock handler still frames 'W'+checksum.
    * otherwise                -> tail into the original FUN_08037a9c unchanged.
  Only ADDS a call; never removes one. Built from stock (channel beep not included).
"""
from __future__ import annotations
import argparse, hashlib, struct
from pathlib import Path
from make_d890_morse_firmware import (
    BASE, align_up, package_paths, patch_size_fields, thumb_bl,
)

# Two BL->FUN_08037a9c call sites inside the read handler FUN_0812efdc.
HOOK_SITES         = (0x0812F0F4, 0x0812F110)
ORIGINAL_CALL_ADDR = 0x08037A9C   # paged codeplug-flash reader -- still called for non-SRAM
STUB_ADDR          = 0x081A5008   # stock image end
REQ_ADDR_PTR       = 0x2001BB17   # request address bytes (4, big-endian) live here


def movw(rd: int, imm: int) -> bytes:
    i = (imm >> 11) & 1
    imm4 = (imm >> 12) & 0xF
    imm3 = (imm >> 8) & 0x7
    imm8 = imm & 0xFF
    hw1 = 0xF240 | (i << 10) | imm4
    hw2 = (imm3 << 12) | (rd << 8) | imm8
    return struct.pack("<HH", hw1, hw2)


def movt(rd: int, imm: int) -> bytes:
    i = (imm >> 11) & 1
    imm4 = (imm >> 12) & 0xF
    imm3 = (imm >> 8) & 0x7
    imm8 = imm & 0xFF
    hw1 = 0xF2C0 | (i << 10) | imm4
    hw2 = (imm3 << 12) | (rd << 8) | imm8
    return struct.pack("<HH", hw1, hw2)


def h(x: int) -> bytes:
    return struct.pack("<H", x)


def assemble() -> bytes:
    """Two-pass assembler. Stub ABI matches FUN_08037a9c(r0=addr, r1=dst, r2=len)."""
    prog = [
        ("raw", h(0xB510)),                       # push {r4,lr}
        ("raw", movw(4, REQ_ADDR_PTR & 0xFFFF)),  # movw r4,#lo(0x2001bb17)
        ("raw", movt(4, REQ_ADDR_PTR >> 16)),     # movt r4,#hi
        ("raw", h(0x6823)),                       # ldr  r3,[r4]      ; 4 req addr bytes (LE word)
        ("raw", h(0xBA1B)),                       # rev  r3,r3        ; -> true big-endian addr
        ("raw", h(0x0E1C)),                       # lsr  r4,r3,#24    ; top byte
        ("raw", h(0x2C20)),                       # cmp  r4,#0x20     ; SRAM?
        ("b", "eq", "magic"),
        # non-SRAM: original behaviour, r0/r1/r2 untouched since entry
        ("bl", ORIGINAL_CALL_ADDR),               # bl FUN_08037a9c
        ("raw", h(0xBD10)),                       # pop {r4,pc}
        ("label", "magic"),                       # r3=src addr, r1=dst, r2=len
        ("raw", h(0x2400)),                       # movs r4,#0        ; i
        ("label", "cp"),
        ("raw", h(0x4294)),                       # cmp  r4,r2
        ("b", "hs", "cpd"),                       # if i>=len -> done
        ("raw", h(0x5D18)),                       # ldrb r0,[r3,r4]
        ("raw", h(0x5508)),                       # strb r0,[r1,r4]
        ("raw", h(0x3401)),                       # adds r4,#1
        ("b", "al", "cp"),
        ("label", "cpd"),
        ("raw", h(0xBD10)),                       # pop {r4,pc}
    ]
    # pass 1: offsets
    off, offsets, sizes = 0, {}, []
    for e in prog:
        if e[0] == "label":
            offsets[e[1]] = off
            sizes.append(0)
            continue
        n = len(e[1]) if e[0] == "raw" else (2 if e[0] == "b" else 4)
        sizes.append(n)
        off += n
    # pass 2: emit
    COND = {"eq": 0x0, "hs": 0x2, "al": None}
    out = bytearray()
    pos = 0
    for e, n in zip(prog, sizes):
        if e[0] == "label":
            continue
        if e[0] == "raw":
            out += e[1]
        elif e[0] == "b":
            cond, label = e[1], e[2]
            imm = (offsets[label] - (pos + 4)) >> 1
            if cond == "al":
                if not (-1024 <= imm < 1024):
                    raise ValueError("B out of T1 range")
                out += h(0xE000 | (imm & 0x7FF))
            else:
                if not (-128 <= imm < 128):
                    raise ValueError("Bcond out of T1 range")
                out += h(0xD000 | (COND[cond] << 8) | (imm & 0xFF))
        elif e[0] == "bl":
            out += thumb_bl(STUB_ADDR + pos, e[1])
        pos += n
    return bytes(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path,
                   default=Path("firmware_d890/extracted/D890UV_V1.05_DMR FW/D890UV_V1.05_20260521"))
    p.add_argument("--output-dir", type=Path, default=Path("patched_rampeek"))
    args = p.parse_args()

    in_spi, in_cdi, in_cdd = package_paths(args.input_base)
    spi = bytearray(in_spi.read_bytes())
    cdi = bytearray(in_cdi.read_bytes())
    cdd = bytearray(in_cdd.read_bytes())

    declared = int.from_bytes(spi[6:10], "little")
    start = int.from_bytes(cdi[256:260], "little")
    cdi_size = int.from_bytes(cdi[260:264], "little")
    if start != BASE:
        raise ValueError(f"unexpected base 0x{start:08x}")
    if declared != len(cdd) or cdi_size != len(cdd):
        raise ValueError(f"size mismatch spi={declared} cdi={cdi_size} cdd={len(cdd)}")
    if BASE + len(cdd) != STUB_ADDR:
        raise ValueError(f"image end 0x{BASE+len(cdd):08x} != stub 0x{STUB_ADDR:08x} "
                         f"(not the stock image?)")

    # verify + rewrite both hook sites (each must currently be BL FUN_08037a9c)
    for site in HOOK_SITES:
        off = site - BASE
        expect = thumb_bl(site, ORIGINAL_CALL_ADDR)
        have = bytes(cdd[off:off + 4])
        if have != expect:
            raise ValueError(f"hook mismatch @0x{site:08x}: have {have.hex()} "
                             f"expect {expect.hex()}")

    stub = assemble()
    # self-check: the non-SRAM BL in the stub must target FUN_08037a9c
    bl_off = None
    for i in range(0, len(stub) - 3, 2):
        hw1, hw2 = struct.unpack_from("<HH", stub, i)
        if (hw1 & 0xF800) == 0xF000 and (hw2 & 0xD000) == 0xD000:
            S = (hw1 >> 10) & 1
            imm10 = hw1 & 0x3FF
            j1 = (hw2 >> 13) & 1
            j2 = (hw2 >> 11) & 1
            imm11 = hw2 & 0x7FF
            i1 = 1 - (j1 ^ S)
            i2 = 1 - (j2 ^ S)
            imm = (S << 24) | (i1 << 23) | (i2 << 22) | (imm10 << 12) | (imm11 << 1)
            if S:
                imm -= (1 << 25)
            tgt = (STUB_ADDR + i + 4) + imm
            if tgt == ORIGINAL_CALL_ADDR:
                bl_off = i
            break
    if bl_off is None:
        raise ValueError("stub self-check failed: no BL->FUN_08037a9c found")

    for site in HOOK_SITES:
        off = site - BASE
        cdd[off:off + 4] = thumb_bl(site, STUB_ADDR)
    cdd.extend(stub)
    cdd.extend(b"\xff" * (align_up(len(cdd), 32) - len(cdd)))
    patch_size_fields(spi, cdi, len(cdd))

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    base = out / "D890UV_V1.05_20260521"
    Path(str(base) + ".spi").write_bytes(spi)
    Path(str(base) + ".CDI").write_bytes(cdi)
    Path(str(base) + ".CDD").write_bytes(cdd)

    print(f"stub @0x{STUB_ADDR:08x} len={len(stub)} bytes={stub.hex()}")
    for site in HOOK_SITES:
        print(f"hook @0x{site:08x} new_bl={thumb_bl(site, STUB_ADDR).hex()} "
              f"(was {thumb_bl(site, ORIGINAL_CALL_ADDR).hex()})")
    print(f"new_size={len(cdd)} / 0x{len(cdd):x}")
    print(f"sha256_cdd={hashlib.sha256(cdd).hexdigest()}")
    print(f"wrote {base}.spi/.CDI/.CDD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
