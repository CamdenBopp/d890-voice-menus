#!/usr/bin/env python3
"""DMR RX voice STREAM: captured CHAN_D frames into a SRAM ring, polled live over USB 'R'.

Follows the confirmed rxtap3 capture (frames reach the MCU; 'R' reads live SRAM in normal mode,
no PROGRAM, UI not suspended). This build streams instead of snapshotting:

  - 10 ms task hook (0x08105d4a): one-shot zero-init of the ring header (fixes the uninit-scratch
    flaw: WRIDX starts at 0), then TICK++ and the original call.
  - PROCESS_MODE / VOCODER_IO hooks (0x081003e8 / 0x080ffff8): OR DEMOD_OUT bit2 into field 0x3e so
    the baseband routes received voice to the MCU (Wall 1).
  - CHAN_D copy hook (0x08146e90): stock copy, then append the frame (<=27 bytes) to the ring at
    slot = WRIDX & 63, tag byte [slot+27] = WRIDX low byte, then WRIDX++ (published after the slot
    is complete, so the host never sees a torn frame).

Host polls WRIDX and reads new slots with the stock 'R' command (stream_rxframes.py). Single
producer (the baseband service task) / single consumer (the host); 64-slot ring (~3.8 s at
16.7 frames/s) absorbs poll jitter, and overrun is detectable via a WRIDX jump > 64.

Scratch (unreferenced SRAM gap 0x20029b06..0x2002b2c1):
  INITFLAG u32 @0x2002a300 (magic 0x52585450 once zero-init done)
  WRIDX    u16 @0x2002a304 (frames written, monotonic 16-bit)
  TICK     u32 @0x2002a308 (task ticks, debug)
  RING         @0x2002a320 : 64 slots x 28 bytes (27 frame + 1 seq tag), ends 0x2002aa20

Nothing is transmitted. Reader: stream_rxframes.py.
"""
from __future__ import annotations
import argparse, hashlib, subprocess, tempfile
from pathlib import Path
import capstone
from make_d890_morse_firmware import BASE, align_up, package_paths, patch_size_fields, thumb_bl

ADD_FIELD = 0x080122EC
MEMCPY = 0x080F3968
TASK_ORIG = 0x081249E0

PM_SITE = 0x081003E8                         # bl ADD_FIELD, r0=0x18 (PROCESS_MODE)
IO_SITE = 0x080FFFF8                         # bl ADD_FIELD, r0=0x3e (VOCODER_IO_SET)
CHAND_SITE = 0x08146E90                      # bl MEMCPY: CHAN_D -> DAT_2000d5e2
TASK_SITE = 0x08105D4A                       # bl FUN_081249e0 in the 10 ms service task

SETUP = {PM_SITE: (ADD_FIELD, "1820"), IO_SITE: (ADD_FIELD, "0df10701"),
         CHAND_SITE: (MEMCPY, "2422")}

SCRATCH = 0x2002A300
INITFLAG, WRIDX, TICK, RING = SCRATCH, SCRATCH + 4, SCRATCH + 8, SCRATCH + 0x20
MAGIC = 0x52585450                           # 'PTXR' LE marker
NSLOTS = 64
SLOT = 28
DEMOD_OUT = 0x04
FLASH_LIMIT = 0x081E0000

ASM = f"""
.syntax unified
.thumb
.cpu cortex-m4
.section .text.rxstream,"ax"
.global _start
_start:

@ ---- 10 ms task hook: one-shot zero-init of ring header, TICK++, then original ----
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
  str r1, [r3]
task_ticked:
  ldr r3, =0x{TICK:08x}
  ldr r0, [r3]
  adds r0, #1
  str r0, [r3]
call_orig:
  bl .
  pop {{r4, pc}}

@ ---- PROCESS_MODE hook: original add, then VOCODER_IO_SET=DEMOD_OUT ----
pm_stub:
  push {{r7, lr}}
call_af1:
  bl .
  movs r0, #0x3e
  ldr r1, =io_byte
call_af2:
  bl .
  pop {{r7, pc}}

@ ---- VOCODER_IO_SET hook: OR DEMOD_OUT into the byte ----
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

@ ---- CHAN_D copy hook: stock copy, then append frame to the ring ----
fwd_stub:
  push {{r3-r7, lr}}
  mov r7, r1
call_memcpy:
  bl .
  ldrb r5, [r7, #-1]            @ frame len
  cbz r5, fwd_done
  cmp r5, #0x24
  bhi fwd_done
  cmp r5, #27
  bls fwd_len_ok
  movs r5, #27
fwd_len_ok:
  ldr r3, =0x{WRIDX:08x}
  ldrh r4, [r3]                 @ r4 = WRIDX
  and r0, r4, #63               @ slot
  movs r1, #{SLOT}
  mul r0, r0, r1                @ slot*28
  ldr r6, =0x{RING:08x}
  add r6, r6, r0                @ r6 = slot base
  mov r2, r6                    @ copy cursor
  mov r1, r5                    @ len counter
fwd_cp:
  ldrb r0, [r7], #1
  strb r0, [r2], #1
  subs r1, #1
  bne fwd_cp
  strb r4, [r6, #27]           @ seq tag = WRIDX low byte
  adds r4, #1
  strh r4, [r3]                @ publish WRIDX after slot complete
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
    p.add_argument("--output-dir", type=Path, default=Path("patched_rxstream"))
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
        raise SystemExit(f"ring overruns the free SRAM gap (ends 0x{ringend:08x})")

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
    print(f"blob @0x{blob:08x} ({len(stub)} B), end 0x{BASE + len(cdd):08x}; ring 0x{RING:08x}..0x{ringend:08x}")
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
