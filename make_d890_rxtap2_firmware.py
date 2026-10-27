#!/usr/bin/env python3
"""DMR RX tap v2 — the three-wall build. On patched_blecps_verified (21e62ebb).

The deep dive (D890_dmr_voice_frames.md) found three independent walls, any one of which made
every earlier tap silent:
  1. plain RX never routes voice to the MCU (field 0x3e / DEMOD_OUT bit2 clear);
  2. the plain-RX CHAN_D consumer FUN_080fbc0c is a no-op stub;
  3. FUN_080d71e8(0x10,...) only ENQUEUES - the TX-to-Jieli drain FUN_08023354 runs only inside
     blocking pump loops, so a frame sent from a passive hook never flushes to BLE.

Wall 3 is the one fully understood offline, and it means the earlier diag traces proved nothing:
a hook could have fired and still shown zero. So this build fixes Wall 3 and keeps the capture +
diagnostics, making a single flash self-diagnosing:

  - pm/io hooks (0x081003e8 / 0x080ffff8): OR DEMOD_OUT (bit2) into field 0x3e whenever stock
    writes it, and report H1/H2 once each (Wall 1 best-effort + probe).
  - CHAN_D copy hook (0x08146e90): snapshot the frame and forward it as V<seq><hex> (capture in the
    dispatcher path, BEFORE the Wall-2 stub consumer).
  - dispatcher hook (FUN_08146930 sites): report F<id> once per distinct field id (probe: F01 means
    CHAN_D reached the MCU).
  - NEW task-drain hook: redirect `bl FUN_081249e0 @ 0x08105d4a` in the 10 ms service task
    (FUN_08105d3c). After the original runs, if we enqueued anything (PEND), spin FUN_08023354 in
    task context until the queue drains (bounded). This is the Wall-3 fix - the drain nothing else
    spins during passive RX.

Reading one buzz DMR call now tells the whole truth: V-lines => the tap works (Walls 1+2 were
already open, Wall 3 was the only blocker). H1/H2 but no F01 => baseband ignores bit2 (need an
active mode apply during RX). No H => stock never writes 0x3e in plain RX (need active assertion).
Nothing is transmitted by this build. Flash gated by the radio lock + base-hash check.
"""
from __future__ import annotations
import argparse, hashlib, subprocess, tempfile
from pathlib import Path
import capstone
from make_d890_morse_firmware import BASE, align_up, package_paths, patch_size_fields, thumb_bl


def thumb_bw(src, target):
    bl = bytearray(thumb_bl(src, target)); hw2 = int.from_bytes(bl[2:4], "little") & ~0x4000
    bl[2:4] = hw2.to_bytes(2, "little"); return bytes(bl)


ADD_FIELD = 0x080122EC
MEMCPY = 0x080F3968
UART_SEND = 0x080D71E8
DISPATCH = 0x08146930
TASK_ORIG = 0x081249E0                      # inner-poll callee in the 10 ms service task
DRAIN = 0x08023354                          # TX-to-Jieli pop+transmit (one frame per call)

PM_SITE = 0x081003E8                        # bl ADD_FIELD, r0=0x18 (PROCESS_MODE)
IO_SITE = 0x080FFFF8                        # bl ADD_FIELD, r0=0x3e (VOCODER_IO_SET)
CHAND_SITE = 0x08146E90                     # bl MEMCPY: CHAN_D frames -> DAT_2000d5e2
DISP_SITES = (0x080DEF56, 0x080E0C50, 0x0810BB5E)  # bl DISPATCH
TASK_SITE = 0x08105D4A                      # bl FUN_081249e0 in FUN_08105d3c inner poll

SETUP = {PM_SITE: (ADD_FIELD, "1820"), IO_SITE: (ADD_FIELD, "0df10701"),
         CHAND_SITE: (MEMCPY, "2422")}

SCRATCH = 0x2002A300                        # unreferenced SRAM gap (checked)
SEQ, FLAGS, PEND, SEEN, PLBUF, TXBUF = (SCRATCH, SCRATCH + 1, SCRATCH + 2,
                                        SCRATCH + 0x10, SCRATCH + 0x40, SCRATCH + 0xC0)
DEMOD_OUT = 0x04
COUNT = 0x20001FC3                          # TX queue pending count (byte)
COOLDOWN = 0x2001AEFA                       # TX inter-frame cooldown (byte; 0 for id 1/0x10)
DRAIN_BOUND = 20
FLASH_LIMIT = 0x081E0000

ASM = f"""
.syntax unified
.thumb
.cpu cortex-m4
.section .text.rxtap2,"ax"
.global _start
_start:

@ ---- PROCESS_MODE hook: original add, then VOCODER_IO_SET=DEMOD_OUT, report H1 once ----
pm_stub:
  push {{r7, lr}}
call_af1:
  bl .
  movs r0, #0x3e
  ldr r1, =io_byte
call_af2:
  bl .
  ldr r3, =0x{FLAGS:08x}
  ldrb r0, [r3]
  cbnz r0, pm_done
  movs r0, #1
  strb r0, [r3]
  adr r0, txt_h1
  movs r5, #2
  bl send_payload
pm_done:
  pop {{r7, pc}}

@ ---- VOCODER_IO_SET hook: OR DEMOD_OUT into the byte, report H2 once ----
io_stub:
  push {{r7, lr}}
  mov r7, r1
  ldrb r2, [r7]
  orr r2, r2, #{DEMOD_OUT}
  strb r2, [r7]
  ldr r3, =0x{FLAGS:08x}
  ldrb r0, [r3, #1]
  cbnz r0, io_call
  movs r0, #1
  strb r0, [r3, #1]
  adr r0, txt_h2
  movs r5, #2
  bl send_payload
io_call:
  movs r0, #0x3e
  mov r1, r7
call_af3:
  bl .
  pop {{r7, pc}}

@ ---- CHAN_D copy hook: stock copy, then forward frames as V<seq><hex> ----
fwd_stub:
  push {{r3-r7, lr}}
  mov r7, r1
call_memcpy:
  bl .
  ldrb r5, [r7, #-1]
  cbz r5, fwd_done
  cmp r5, #0x24
  bhi fwd_done
  ldr r6, =0x{PLBUF:08x}
  movs r0, #0x56                    @ 'V'
  strb r0, [r6], #1
  ldr r1, =0x{SEQ:08x}
  ldrb r0, [r1]
  adds r2, r0, #1
  strb r2, [r1]
  bl puthex
fwd_loop:
  ldrb r0, [r7], #1
  bl puthex
  subs r5, #1
  bne fwd_loop
  ldr r0, =0x{PLBUF:08x}
  subs r5, r6, r0
  bl send_payload
fwd_done:
  pop {{r3-r7, pc}}

@ ---- Dispatcher trace: report F<id> once per distinct field, then call the original ----
disp_stub:
  push {{r3-r7, lr}}
  mov r4, r0
  ldrb r5, [r4, #1]
  ldr r3, =0x{SEEN:08x}
  lsrs r0, r5, #3
  ldrb r1, [r3, r0]
  and r2, r5, #7
  movs r6, #1
  lsl r6, r6, r2
  tst r1, r6
  bne disp_call
  orr r1, r1, r6
  strb r1, [r3, r0]
  ldr r6, =0x{PLBUF:08x}
  movs r0, #0x46                    @ 'F'
  strb r0, [r6], #1
  mov r0, r5
  bl puthex
  ldr r0, =0x{PLBUF:08x}
  subs r5, r6, r0
  bl send_payload
disp_call:
  mov r0, r4
call_disp:
  bl .
  pop {{r3-r7, pc}}

@ ---- Task drain hook (Wall 3): run the original inner-poll call, then flush our TX frames ----
task_stub:
  push {{r4, lr}}
call_orig:
  bl .                             @ FUN_081249e0()
  ldr r3, =0x{PEND:08x}
  ldrb r0, [r3]
  cbz r0, task_done
  movs r0, #0
  strb r0, [r3]                    @ clear pending
  movs r4, #{DRAIN_BOUND}
task_drain:
  ldr r0, =0x{COOLDOWN:08x}
  ldrb r0, [r0]
  cbnz r0, task_done               @ cooldown active -> let the 1 ms tick clear it
  ldr r0, =0x{COUNT:08x}
  ldrb r0, [r0]
  cbz r0, task_done                @ queue empty
call_drain:
  bl .                             @ FUN_08023354()
  subs r4, #1
  bne task_drain
task_done:
  pop {{r4, pc}}

@ ---- send_payload(r0 = src, r5 = len): "AT+DATA=00,NNN\\r\\n<payload>\\r\\n", then arm PEND ----
send_payload:
  push {{r4-r6, lr}}
  mov r4, r0
  ldr r6, =0x{TXBUF:08x}
  adr r1, hdr
  movs r2, #11
sp_hdr:
  ldrb r0, [r1], #1
  strb r0, [r6], #1
  subs r2, #1
  bne sp_hdr
  movs r1, #100
  udiv r0, r5, r1
  mls r3, r0, r1, r5
  adds r0, #0x30
  strb r0, [r6], #1
  movs r1, #10
  udiv r0, r3, r1
  mls r3, r0, r1, r3
  adds r0, #0x30
  strb r0, [r6], #1
  adds r0, r3, #0x30
  strb r0, [r6], #1
  movw r0, #0x0a0d
  strh r0, [r6], #2
  movs r2, #0
sp_cp:
  cmp r2, r5
  bge sp_tail
  ldrb r0, [r4, r2]
  strb r0, [r6], #1
  adds r2, #1
  b sp_cp
sp_tail:
  movw r0, #0x0a0d
  strh r0, [r6], #2
  ldr r1, =0x{TXBUF:08x}
  subs r2, r6, r1
  movs r0, #0x10
call_uart:
  bl .                             @ UART_SEND(0x10, TXBUF, len)
  ldr r3, =0x{PEND:08x}
  movs r0, #1
  strb r0, [r3]                    @ arm drain for the next task tick
  pop {{r4-r6, pc}}

@ ---- puthex(r0 = byte, r6 = write cursor) ----
puthex:
  adr r3, hexd
  lsrs r1, r0, #4
  ldrb r1, [r3, r1]
  strb r1, [r6], #1
  and r0, r0, #0xf
  ldrb r0, [r3, r0]
  strb r0, [r6], #1
  bx lr

.balign 4
hdr: .ascii "AT+DATA=00,"
.balign 4
hexd: .ascii "0123456789abcdef"
txt_h1: .ascii "H1"
txt_h2: .ascii "H2"
io_byte: .byte {DEMOD_OUT}
.balign 4
.ltorg
"""

CALLS = {"call_af1": ADD_FIELD, "call_af2": ADD_FIELD, "call_af3": ADD_FIELD,
         "call_memcpy": MEMCPY, "call_disp": DISPATCH, "call_uart": UART_SEND,
         "call_orig": TASK_ORIG, "call_drain": DRAIN}


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
    p.add_argument("--output-dir", type=Path, default=Path("patched_rxtap2"))
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

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    print(f"blob @0x{blob:08x} ({len(stub)} B), end 0x{BASE + len(cdd):08x}")
    for site, name in [(PM_SITE, "pm_stub"), (IO_SITE, "io_stub"), (CHAND_SITE, "fwd_stub"),
                       *[(s, "disp_stub") for s in DISP_SITES], (TASK_SITE, "task_stub")]:
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
