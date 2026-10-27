#!/usr/bin/env python3
"""DMR RX voice STREAM v2 — capture the FULL 36-byte CHAN_D (4 AMBE frames), not 27.

The synth-siren instrument showed rxstream dropped every 4th frame (n==3 mod 4). Root cause: the
stock CHAN_D handler FUN_08146930 case 0x01 always copies 0x24 = 36 bytes = 4 on-air AMBE frames,
but rxstream's fwd_stub capped the ring slot at 27 (3 frames). This build captures all 36 bytes.

Ring slot = 40 bytes: 36 payload (4 x 9-byte frames) + [36]=sub byte (0x24/0x1b) + [37]=seq tag.
64 slots at 0x2002a360. Header: INITFLAG@0x2002a300, WRIDX u16@0x2002a304, TICK u32@0x2002a308,
C24 u16@0x2002a30c, C1b u16@0x2002a30e (sub-type counts, confirm all packets are 0x24). One-shot
zero-init on first task tick. Hooks: task(0x08105d4a), pm/io(0x081003e8/0x080ffff8) DEMOD_OUT bit2,
CHAN_D copy(0x08146e90). Read with stream_rxframes2.py. Nothing transmitted.
"""
from __future__ import annotations
import argparse, hashlib, subprocess, tempfile
from pathlib import Path
import capstone
from make_d890_morse_firmware import BASE, align_up, package_paths, patch_size_fields, thumb_bl

ADD_FIELD = 0x080122EC
MEMCPY = 0x080F3968
TASK_ORIG = 0x081249E0

PM_SITE = 0x081003E8
IO_SITE = 0x080FFFF8
CHAND_SITE = 0x08146E90
TASK_SITE = 0x08105D4A

SETUP = {PM_SITE: (ADD_FIELD, "1820"), IO_SITE: (ADD_FIELD, "0df10701"),
         CHAND_SITE: (MEMCPY, "2422")}

SCRATCH = 0x2002A300
INITFLAG, WRIDX, TICK, C24, C1B = SCRATCH, SCRATCH + 4, SCRATCH + 8, SCRATCH + 0xC, SCRATCH + 0xE
RING = SCRATCH + 0x60
MAGIC = 0x52585332                            # 'RXS2' LE
NSLOTS = 64
SLOT = 40
CAPLEN = 36
DEMOD_OUT = 0x04
FLASH_LIMIT = 0x081E0000

ASM = f"""
.syntax unified
.thumb
.cpu cortex-m4
.section .text.rxstream2,"ax"
.global _start
_start:

task_stub:
  push {{r4, lr}}
  ldr r3, =0x{INITFLAG:08x}
  ldr r0, [r3]
  ldr r1, =0x{MAGIC:08x}
  cmp r0, r1
  beq task_ticked
  movs r0, #0
  ldr r2, =0x{WRIDX:08x}
  strh r0, [r2]
  ldr r2, =0x{C24:08x}
  str r0, [r2]                      @ zero C24 (u16) + C1B (u16)
  str r1, [r3]
task_ticked:
  ldr r3, =0x{TICK:08x}
  ldr r0, [r3]
  adds r0, #1
  str r0, [r3]
call_orig:
  bl .
  pop {{r4, pc}}

pm_stub:
  push {{r7, lr}}
call_af1:
  bl .
  movs r0, #0x3e
  ldr r1, =io_byte
call_af2:
  bl .
  pop {{r7, pc}}

io_stub:
  push {{r7, lr}}
  mov r7, r1
  ldrb r2, [r7]
  orr r2, r2, #{DEMOD_OUT}
  strb r2, [r7]
  movs r0, #0x3e
  mov r1, r7
call_af3:
  bl .
  pop {{r7, pc}}

@ CHAN_D copy hook: stock copies 36 B; capture all 36 (4 frames) into a 40-byte slot
fwd_stub:
  push {{r3-r7, lr}}
  mov r7, r1                        @ src = param_1+3
call_memcpy:
  bl .                             @ stock FUN_080f3968(dst, src, 0x24)
  ldrb r5, [r7, #-1]              @ sub = param_1[2] (0x24 or 0x1b)
  ldr r3, =0x{WRIDX:08x}
  ldrh r4, [r3]
  and r0, r4, #63
  movs r1, #{SLOT}
  mul r0, r0, r1
  ldr r6, =0x{RING:08x}
  add r6, r6, r0                   @ slot base
  mov r2, r6
  movs r1, #{CAPLEN}
fwd_cp:
  ldrb r0, [r7], #1
  strb r0, [r2], #1
  subs r1, #1
  bne fwd_cp
  strb r5, [r6, #{CAPLEN}]        @ [36] = sub
  strb r4, [r6, #{CAPLEN}+1]      @ [37] = tag (WRIDX low)
  adds r4, #1
  strh r4, [r3]
  cmp r5, #0x24
  bne fwd_c1b
  ldr r3, =0x{C24:08x}
  ldrh r0, [r3]
  adds r0, #1
  strh r0, [r3]
  b fwd_done
fwd_c1b:
  ldr r3, =0x{C1B:08x}
  ldrh r0, [r3]
  adds r0, #1
  strh r0, [r3]
fwd_done:
  pop {{r3-r7, pc}}

.balign 4
io_byte: .byte {DEMOD_OUT}
.balign 4
.ltorg
"""

CALLS = {"call_orig": TASK_ORIG, "call_af1": ADD_FIELD, "call_af2": ADD_FIELD,
         "call_af3": ADD_FIELD, "call_memcpy": MEMCPY}


def run(cmd, **kw):
    return subprocess.run([str(x) for x in cmd], check=True, **kw)


def build_stub(blob: int) -> tuple[bytes, dict[str, int]]:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "s.S").write_text(ASM)
        run(["arm-none-eabi-as", "-mcpu=cortex-m4", "-mthumb", "-o", t / "s.o", t / "s.S"])
        (t / "link.ld").write_text(
            f"ENTRY(_start)\nSECTIONS {{ .text 0x{blob:08x} : {{ *(.text*) }} "
            f"/DISCARD/ : {{ *(.ARM.*) *(.comment) }} }}\n")
        run(["arm-none-eabi-ld", "-T", t / "link.ld", "-o", t / "s.elf", t / "s.o"])
        run(["arm-none-eabi-objcopy", "-O", "binary", "-j", ".text", t / "s.elf", t / "s.bin"])
        nm = run(["arm-none-eabi-nm", t / "s.elf"], capture_output=True, text=True).stdout
        addr = {l.split()[2]: int(l.split()[0], 16) for l in nm.splitlines() if len(l.split()) == 3}
        stub = bytearray((t / "s.bin").read_bytes())
    for name, target in CALLS.items():
        o = addr[name] - blob
        stub[o:o + 4] = thumb_bl(addr[name], target)
    return bytes(stub), addr


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path, default=Path("patched_blecps_verified/D890UV_V1.05_20260521"))
    p.add_argument("--output-dir", type=Path, default=Path("patched_rxstream2"))
    args = p.parse_args()
    in_spi, in_cdi, in_cdd = package_paths(args.input_base)
    spi = bytearray(in_spi.read_bytes()); cdi = bytearray(in_cdi.read_bytes()); cdd = bytearray(in_cdd.read_bytes())
    if int.from_bytes(spi[6:10], "little") != len(cdd) or int.from_bytes(cdi[260:264], "little") != len(cdd):
        raise SystemExit("input size fields disagree with CDD length")
    print("base CDD sha256", hashlib.sha256(cdd).hexdigest()[:16])

    for site, (target, before) in SETUP.items():
        off = site - BASE
        if bytes(cdd[off - len(bytes.fromhex(before)):off]) != bytes.fromhex(before):
            raise SystemExit(f"0x{site:x}: unexpected setup bytes")
        if bytes(cdd[off:off + 4]) != thumb_bl(site, target):
            raise SystemExit(f"0x{site:x}: BL mismatch")
    if bytes(cdd[TASK_SITE - BASE:TASK_SITE - BASE + 4]) != thumb_bl(TASK_SITE, TASK_ORIG):
        raise SystemExit(f"0x{TASK_SITE:x}: task-site BL mismatch")

    ringend = RING + NSLOTS * SLOT
    if ringend > 0x2002B2C1:
        raise SystemExit(f"ring overruns free gap (ends 0x{ringend:08x})")

    blob = align_up(BASE + len(cdd), 32)
    cdd.extend(b"\xff" * (blob - BASE - len(cdd)))
    stub, sym = build_stub(blob)
    cdd.extend(stub)
    cdd.extend(b"\xff" * (align_up(len(cdd), 32) - len(cdd)))
    if BASE + len(cdd) > FLASH_LIMIT:
        raise SystemExit("image overruns flash limit")
    cdd[PM_SITE - BASE:PM_SITE - BASE + 4] = thumb_bl(PM_SITE, sym["pm_stub"])
    cdd[IO_SITE - BASE:IO_SITE - BASE + 4] = thumb_bl(IO_SITE, sym["io_stub"])
    cdd[CHAND_SITE - BASE:CHAND_SITE - BASE + 4] = thumb_bl(CHAND_SITE, sym["fwd_stub"])
    cdd[TASK_SITE - BASE:TASK_SITE - BASE + 4] = thumb_bl(TASK_SITE, sym["task_stub"])

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    print(f"blob @0x{blob:08x} ({len(stub)} B); ring 0x{RING:08x}..0x{ringend:08x}")
    for site, name in [(PM_SITE, "pm_stub"), (IO_SITE, "io_stub"), (CHAND_SITE, "fwd_stub"), (TASK_SITE, "task_stub")]:
        i = next(md.disasm(bytes(cdd[site - BASE:site - BASE + 4]), site))
        tgt = int(i.op_str.lstrip("#"), 16)
        assert tgt == sym[name], (hex(site), hex(tgt), hex(sym[name]))
        print(f"  site {site:08x} -> {name} {i.op_str}")

    patch_size_fields(spi, cdi, len(cdd))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base = args.output_dir / in_cdd.stem
    Path(str(base) + ".spi").write_bytes(spi); Path(str(base) + ".CDI").write_bytes(cdi); Path(str(base) + ".CDD").write_bytes(cdd)
    print("CDD sha256", hashlib.sha256(cdd).hexdigest()[:16], "size", hex(len(cdd)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
