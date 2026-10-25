#!/usr/bin/env python3
"""Add a no-PROGRAM (no PC-mode) path for 'R' read and 'S' settings, on top of rampeek.

The stock command decoder FUN_080d4664 rejects commands unless a PROGRAM session
is open (DAT_2001c71b != 0); that session flips the radio into "PC READ" and
freezes input. We bypass the guard for 'R' (read) and 'S' (settings write) so
they work over USB without PROGRAM/END and without taking over the screen.

Entry detour at FUN_080d4664 (0x080d4664, prologue push{r7,lr};sub sp,#8):
  if cmd == 'R':
      push {r7,lr}; sub sp,#8; DAT_2001bb0d = 0; B.W 0x080d475a   (into the switch)
  elif cmd == 'S':
      push {r7,lr}; sub sp,#8; DAT_2001bb0d = 0x23; DAT_2000878c = 5;
      B.W 0x080d4df0  (epilogue — skips the guarded switch entirely)
  else:
      push {r7,lr}; sub sp,#8; B.W 0x080d4668                     (original, post-prologue)
The 'S' case in the stock switch requires DAT_2001c71b == 2 (PROGRAM session).
Our stub bypasses that by directly setting the cmd id and expected length, then
jumping to the epilogue which increments the packet counter and returns.
"""
from __future__ import annotations
import argparse, hashlib, struct
from pathlib import Path
from make_d890_morse_firmware import BASE, align_up, package_paths, patch_size_fields, thumb_bl

HOOK_ADDR     = 0x080D4664
HOOK_ORIGINAL = bytes.fromhex("80b582b0")     # push {r7,lr}; sub sp,#8
SWITCH_ADDR   = 0x080D475A                     # decoder switch entry (guard already skipped)
POST_PROLOGUE = 0x080D4668                     # first insn after the displaced prologue
EPILOGUE_ADDR = 0x080D4DF0                     # after switch: if(bb0d!=0) c724++; return
STUB_ADDR     = 0x081A5040                     # end of patched_rampeek image
CMD_BB16      = 0x2001BB16                      # received command byte
ID_BB0D       = 0x2001BB0D                      # decoded command id
EXPECT_878C   = 0x2000878C                      # expected remaining byte count


def thumb_b_w(src: int, target: int) -> bytes:
    hw1, hw2 = struct.unpack("<HH", thumb_bl(src, target))
    hw2 &= ~0x4000                              # BL -> B.W (clear hw2 bit14)
    return struct.pack("<HH", hw1, hw2)


def movw(rd, imm):
    return struct.pack("<HH", 0xF240 | (((imm >> 11) & 1) << 10) | ((imm >> 12) & 0xF),
                       (((imm >> 8) & 7) << 12) | (rd << 8) | (imm & 0xFF))


def movt(rd, imm):
    return struct.pack("<HH", 0xF2C0 | (((imm >> 11) & 1) << 10) | ((imm >> 12) & 0xF),
                       (((imm >> 8) & 7) << 12) | (rd << 8) | (imm & 0xFF))


def h(x):
    return struct.pack("<H", x)


def assemble() -> bytes:
    prog = [
        # --- dispatch on cmd byte ---
        ("raw", movw(0, CMD_BB16 & 0xFFFF)),   # movw r0,#lo(cmd)
        ("raw", movt(0, CMD_BB16 >> 16)),      # movt r0,#hi
        ("raw", h(0x7800)),                    # ldrb r0,[r0]
        ("raw", h(0x2852)),                    # cmp  r0,#0x52 ('R')
        ("b", "eq", "isR"),
        ("raw", h(0x2853)),                    # cmp  r0,#0x53 ('S')
        ("b", "eq", "isS"),
        # --- fallthrough: not R or S, run original prologue + continue ---
        ("raw", h(0xB580)),                    # push {r7,lr}
        ("raw", h(0xB082)),                    # sub  sp,#8
        ("bw", POST_PROLOGUE),                 # B.W 0x080d4668
        # --- 'R' path: into the switch (no guard on R case) ---
        ("label", "isR"),
        ("raw", h(0xB580)),                    # push {r7,lr}
        ("raw", h(0xB082)),                    # sub  sp,#8
        ("raw", movw(0, ID_BB0D & 0xFFFF)),    # movw r0,#lo(bb0d)
        ("raw", movt(0, ID_BB0D >> 16)),       # movt r0,#hi
        ("raw", h(0x2100)),                    # movs r1,#0
        ("raw", h(0x7001)),                    # strb r1,[r0]   ; DAT_2001bb0d = 0
        ("bw", SWITCH_ADDR),                   # B.W 0x080d475a
        # --- 'S' path: bypass guarded switch, set cmd id + length directly ---
        ("label", "isS"),
        ("raw", h(0xB580)),                    # push {r7,lr}
        ("raw", h(0xB082)),                    # sub  sp,#8
        ("raw", movw(0, ID_BB0D & 0xFFFF)),    # movw r0,#lo(bb0d)
        ("raw", movt(0, ID_BB0D >> 16)),       # movt r0,#hi
        ("raw", h(0x2123)),                    # movs r1,#0x23
        ("raw", h(0x7001)),                    # strb r1,[r0]   ; DAT_2001bb0d = 0x23
        ("raw", movw(0, EXPECT_878C & 0xFFFF)),# movw r0,#lo(878c)
        ("raw", movt(0, EXPECT_878C >> 16)),   # movt r0,#hi
        ("raw", h(0x2105)),                    # movs r1,#5
        ("raw", h(0x7001)),                    # strb r1,[r0]   ; DAT_2000878c = 5
        ("bw", EPILOGUE_ADDR),                 # B.W 0x080d4df0 (counter++ and return)
    ]
    off, offsets, sizes = 0, {}, []
    for e in prog:
        if e[0] == "label":
            offsets[e[1]] = off; sizes.append(0); continue
        n = len(e[1]) if e[0] == "raw" else (2 if e[0] == "b" else 4)
        sizes.append(n); off += n
    COND = {"eq": 0x0}
    out, pos = bytearray(), 0
    for e, n in zip(prog, sizes):
        if e[0] == "label":
            continue
        if e[0] == "raw":
            out += e[1]
        elif e[0] == "b":
            imm = (offsets[e[2]] - (pos + 4)) >> 1
            if not (-128 <= imm < 128):
                raise ValueError("Bcond range")
            out += h(0xD000 | (COND[e[1]] << 8) | (imm & 0xFF))
        elif e[0] == "bw":
            out += thumb_b_w(STUB_ADDR + pos, e[1])
        pos += n
    return bytes(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path, default=Path("patched_rampeek/D890UV_V1.05_20260521"))
    p.add_argument("--output-dir", type=Path, default=Path("patched_nopcm_read"))
    args = p.parse_args()

    in_spi, in_cdi, in_cdd = package_paths(args.input_base)
    spi = bytearray(in_spi.read_bytes()); cdi = bytearray(in_cdi.read_bytes()); cdd = bytearray(in_cdd.read_bytes())
    if int.from_bytes(cdi[256:260], "little") != BASE:
        raise ValueError("unexpected base")
    if int.from_bytes(spi[6:10], "little") != len(cdd) or int.from_bytes(cdi[260:264], "little") != len(cdd):
        raise ValueError("size field mismatch")
    if BASE + len(cdd) != STUB_ADDR:
        raise ValueError(f"image end 0x{BASE+len(cdd):08x} != stub 0x{STUB_ADDR:08x} (wrong input base?)")

    off = HOOK_ADDR - BASE
    have = bytes(cdd[off:off + 4])
    if have != HOOK_ORIGINAL:
        raise ValueError(f"hook mismatch @0x{HOOK_ADDR:08x}: have {have.hex()} expect {HOOK_ORIGINAL.hex()}")

    stub = assemble()
    # self-check: both B.W targets present and correct
    def bw_target(addr, b):
        hw1, hw2 = struct.unpack_from("<HH", b, 0)
        S = (hw1 >> 10) & 1; imm10 = hw1 & 0x3FF
        j1 = (hw2 >> 13) & 1; j2 = (hw2 >> 11) & 1; imm11 = hw2 & 0x7FF
        i1 = 1 - (j1 ^ S); i2 = 1 - (j2 ^ S)
        imm = (S << 24) | (i1 << 23) | (i2 << 22) | (imm10 << 12) | (imm11 << 1)
        if S: imm -= (1 << 25)
        return addr + 4 + imm
    targets = []
    for i in range(0, len(stub) - 3, 2):
        hw1, hw2 = struct.unpack_from("<HH", stub, i)
        if (hw1 & 0xF800) == 0xF000 and (hw2 & 0xD000) == 0x9000:   # B.W T4
            targets.append(bw_target(STUB_ADDR + i, stub[i:i + 4]))
    expected = {SWITCH_ADDR, POST_PROLOGUE, EPILOGUE_ADDR}
    if not expected.issubset(set(targets)):
        raise ValueError(f"stub self-check failed: B.W targets {[hex(t) for t in targets]}, need {[hex(t) for t in expected]}")

    cdd[off:off + 4] = thumb_b_w(HOOK_ADDR, STUB_ADDR)
    cdd.extend(stub)
    cdd.extend(b"\xff" * (align_up(len(cdd), 32) - len(cdd)))
    patch_size_fields(spi, cdi, len(cdd))

    out = args.output_dir; out.mkdir(parents=True, exist_ok=True)
    base = out / "D890UV_V1.05_20260521"
    Path(str(base) + ".spi").write_bytes(spi); Path(str(base) + ".CDI").write_bytes(cdi); Path(str(base) + ".CDD").write_bytes(cdd)
    print(f"stub @0x{STUB_ADDR:08x} len={len(stub)} bytes={stub.hex()}")
    print(f"entry @0x{HOOK_ADDR:08x} new={thumb_b_w(HOOK_ADDR, STUB_ADDR).hex()} (was {HOOK_ORIGINAL.hex()})")
    print(f"B.W targets ok: {[hex(t) for t in targets]}")
    print(f"new_size={len(cdd)} / 0x{len(cdd):x}  sha256_cdd={hashlib.sha256(cdd).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
