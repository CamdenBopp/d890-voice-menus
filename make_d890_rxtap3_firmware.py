#!/usr/bin/env python3
"""DMR RX tap v3 — USB diagnostic (no BLE). On patched_blecps_verified (21e62ebb).

BLE forwarding gave zero and we can't tell capture-failure from BLE-failure that way. This build
exfils a diagnostic struct over the proven USB CPS 'R'/identity path instead, so one read pins the
break with no BLE and no by-ear dependency.

The confirmed host trigger: sending \\x02 in a PROGRAM session returns dispatcher case 0xe (the
identity "ID890UV..V100"). We trampoline the dispatcher call (bl FUN_0812d058 @ 0x0812c4c0) and,
for cmd 0xe only, replace the identity with a 16-byte diagnostic written into the reply buffer
DAT_2001bb16 (safe: first used global is at buffer+0x11, so offsets 0..0x0f are free), length
_DAT_2001c726 = 0x10. Every other command still runs the stock dispatcher.

16-byte diag (little-endian):
  [0]     'D' (0x44) marker
  [1]     HFLAGS   bit0 = PROCESS_MODE hook fired, bit1 = VOCODER_IO hook fired
  [2]     FRAMECNT low byte (CHAN_D frames captured, saturating)
  [3]     SEQ      (last CHAN_D seq)
  [4..7]  TICK u32 (10 ms service-task hook calls; must change between reads if our code runs)
  [8..11] SEEN[0..3]  dispatcher field-id bitmap, ids 0..31 (field 0x01 = byte0 bit1)
  [12..15] SEEN[4..7] ids 32..63

Reading it answers: TICK changing => our code runs; HFLAGS => mode-setup path hit; SEEN bit 0x01 =>
CHAN_D field reached the MCU dispatcher; FRAMECNT>0 => frames captured. Hooks: PROCESS_MODE
(0x081003e8) / VOCODER_IO (0x080ffff8) set DEMOD_OUT bit2 + HFLAGS; CHAN_D copy (0x08146e90) counts
frames + stores the last one; dispatcher (FUN_08146930 sites) sets SEEN; 10 ms task (0x08105d4a)
ticks TICK. Capture the DMR burst in NORMAL mode, THEN PROGRAM + \\x02 to read (scratch SRAM
survives entering program mode; no reboot). Nothing is transmitted. Read with read_rxtap3_diag.py.
"""
from __future__ import annotations
import argparse, hashlib, subprocess, tempfile
from pathlib import Path
import capstone
from make_d890_morse_firmware import BASE, align_up, package_paths, patch_size_fields, thumb_bl

ADD_FIELD = 0x080122EC
MEMCPY = 0x080F3968
DISPATCH = 0x08146930                        # baseband field dispatcher
QXDISP = 0x0812D058                          # QX CPS command dispatcher
TASK_ORIG = 0x081249E0

PM_SITE = 0x081003E8                         # bl ADD_FIELD, r0=0x18 (PROCESS_MODE)
IO_SITE = 0x080FFFF8                         # bl ADD_FIELD, r0=0x3e (VOCODER_IO_SET)
CHAND_SITE = 0x08146E90                      # bl MEMCPY: CHAN_D -> DAT_2000d5e2
DISP_SITES = (0x080DEF56, 0x080E0C50, 0x0810BB5E)  # bl DISPATCH
TASK_SITE = 0x08105D4A                       # bl FUN_081249e0 in the 10 ms service task
CPS_SITE = 0x0812C4C0                        # bl FUN_0812d058 in the CPS RX loop

SETUP = {PM_SITE: (ADD_FIELD, "1820"), IO_SITE: (ADD_FIELD, "0df10701"),
         CHAND_SITE: (MEMCPY, "2422")}

SCRATCH = 0x2002A300                         # unreferenced SRAM gap (checked)
TICK, HFLAGS, SEQ, FRAMECNT = SCRATCH, SCRATCH + 4, SCRATCH + 5, SCRATCH + 6
SEEN, LASTFRAME = SCRATCH + 0x10, SCRATCH + 0x40
REPLY = 0x2001BB16
RLEN = 0x2001C726
CMDID = 0x2001BB0D
DEMOD_OUT = 0x04
FLASH_LIMIT = 0x081E0000

ASM = f"""
.syntax unified
.thumb
.cpu cortex-m4
.section .text.rxtap3,"ax"
.global _start
_start:

@ ---- 10 ms service-task hook: run original, then TICK++ ----
task_stub:
  push {{r4, lr}}
call_orig:
  bl .
  ldr r3, =0x{TICK:08x}
  ldr r0, [r3]
  adds r0, #1
  str r0, [r3]
  pop {{r4, pc}}

@ ---- PROCESS_MODE hook: original add, then VOCODER_IO_SET=DEMOD_OUT, HFLAGS bit0 ----
pm_stub:
  push {{r7, lr}}
call_af1:
  bl .
  movs r0, #0x3e
  ldr r1, =io_byte
call_af2:
  bl .
  ldr r3, =0x{HFLAGS:08x}
  ldrb r0, [r3]
  orr r0, r0, #1
  strb r0, [r3]
  pop {{r7, pc}}

@ ---- VOCODER_IO_SET hook: OR DEMOD_OUT into byte, HFLAGS bit1 ----
io_stub:
  push {{r7, lr}}
  mov r7, r1
  ldrb r2, [r7]
  orr r2, r2, #{DEMOD_OUT}
  strb r2, [r7]
  ldr r3, =0x{HFLAGS:08x}
  ldrb r0, [r3]
  orr r0, r0, #2
  strb r0, [r3]
  movs r0, #0x3e
  mov r1, r7
call_af3:
  bl .
  pop {{r7, pc}}

@ ---- CHAN_D copy hook: stock copy, then FRAMECNT++/SEQ++/store last frame ----
fwd_stub:
  push {{r3-r7, lr}}
  mov r7, r1
call_memcpy:
  bl .
  ldrb r5, [r7, #-1]
  cbz r5, fwd_done
  cmp r5, #0x24
  bhi fwd_done
  ldr r3, =0x{FRAMECNT:08x}
  ldrh r0, [r3]
  adds r0, #1
  strh r0, [r3]
  ldr r3, =0x{SEQ:08x}
  ldrb r0, [r3]
  adds r0, #1
  strb r0, [r3]
  ldr r6, =0x{LASTFRAME:08x}
fwd_cp:
  ldrb r0, [r7], #1
  strb r0, [r6], #1
  subs r5, #1
  bne fwd_cp
fwd_done:
  pop {{r3-r7, pc}}

@ ---- Dispatcher hook: set SEEN bit for the field id (ids 0..63), then call original ----
disp_stub:
  push {{r3-r7, lr}}
  mov r4, r0
  ldrb r5, [r4, #1]
  lsrs r0, r5, #3
  cmp r0, #8
  bhs disp_call
  ldr r3, =0x{SEEN:08x}
  ldrb r1, [r3, r0]
  and r2, r5, #7
  movs r6, #1
  lsl r6, r6, r2
  orr r1, r1, r6
  strb r1, [r3, r0]
disp_call:
  mov r0, r4
call_disp:
  bl .
  pop {{r3-r7, pc}}

@ ---- CPS dispatcher trampoline: cmd 0xe -> 16-byte diag; else stock ----
cps_stub:
  push {{r4-r6, lr}}
  ldr r3, =0x{CMDID:08x}
  ldrb r0, [r3]
  cmp r0, #0x0e
  beq cps_diag
call_qx:
  bl .
  b cps_done
cps_diag:
  ldr r6, =0x{REPLY:08x}
  movs r0, #0x44
  strb r0, [r6, #0]
  ldr r3, =0x{HFLAGS:08x}
  ldrb r0, [r3]
  strb r0, [r6, #1]
  ldr r3, =0x{FRAMECNT:08x}
  ldrb r0, [r3]
  strb r0, [r6, #2]
  ldr r3, =0x{SEQ:08x}
  ldrb r0, [r3]
  strb r0, [r6, #3]
  ldr r3, =0x{TICK:08x}
  ldr r0, [r3]
  str r0, [r6, #4]
  ldr r3, =0x{SEEN:08x}
  ldr r0, [r3, #0]
  str r0, [r6, #8]
  ldr r0, [r3, #4]
  str r0, [r6, #12]
  ldr r3, =0x{RLEN:08x}
  movs r0, #0x10
  str r0, [r3]
cps_done:
  pop {{r4-r6, pc}}

.balign 4
io_byte: .byte {DEMOD_OUT}
.balign 4
.ltorg
"""

CALLS = {"call_orig": TASK_ORIG, "call_af1": ADD_FIELD, "call_af2": ADD_FIELD,
         "call_af3": ADD_FIELD, "call_memcpy": MEMCPY, "call_disp": DISPATCH, "call_qx": QXDISP}


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
    p.add_argument("--output-dir", type=Path, default=Path("patched_rxtap3"))
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
    for site in DISP_SITES:
        if bytes(cdd[site - BASE:site - BASE + 4]) != thumb_bl(site, DISPATCH):
            raise SystemExit(f"0x{site:x}: dispatcher BL mismatch")
    if bytes(cdd[TASK_SITE - BASE:TASK_SITE - BASE + 4]) != thumb_bl(TASK_SITE, TASK_ORIG):
        raise SystemExit(f"0x{TASK_SITE:x}: task-site BL mismatch")
    if bytes(cdd[CPS_SITE - BASE:CPS_SITE - BASE + 4]) != thumb_bl(CPS_SITE, QXDISP):
        raise SystemExit(f"0x{CPS_SITE:x}: CPS-site BL mismatch")

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
    for site in DISP_SITES:
        cdd[site - BASE:site - BASE + 4] = thumb_bl(site, sym["disp_stub"])
    cdd[TASK_SITE - BASE:TASK_SITE - BASE + 4] = thumb_bl(TASK_SITE, sym["task_stub"])
    cdd[CPS_SITE - BASE:CPS_SITE - BASE + 4] = thumb_bl(CPS_SITE, sym["cps_stub"])

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    print(f"blob @0x{blob:08x} ({len(stub)} B), end 0x{BASE + len(cdd):08x}")
    for site, name in [(PM_SITE, "pm_stub"), (IO_SITE, "io_stub"), (CHAND_SITE, "fwd_stub"),
                       *[(s, "disp_stub") for s in DISP_SITES], (TASK_SITE, "task_stub"),
                       (CPS_SITE, "cps_stub")]:
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
