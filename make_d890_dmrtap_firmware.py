#!/usr/bin/env python3
"""DMR hotspot RX-tap step 1: also report the call-start voice mode over BLE (C<mode>).

Built on patched_blecps (what the radio runs). Three things, all reported on FFE2 via
FUN_080d71e8(0x10, ...) as AT+DATA lines:

  H1  - the PROCESS_MODE hook (0x081003e8) fired and appended VOCODER_IO_SET=DEMOD_OUT.
  H2  - the stock VOCODER_IO_SET sender (0x080ffff8) fired; we OR DEMOD_OUT into its byte.
        H1/H2 are each sent once (so we learn whether the radio ever enables DEMOD_OUT).
  F<id> - the baseband field dispatcher FUN_08146930 was called with field id <id>; one line
        per distinct id seen (256-bit dedup bitmap). If DEMOD_OUT works in DMR we should see
        F01 (CHAN_D) during a decoded call.
  V<seq><hex> - a CHAN_D field actually reached the case-0x01 copy; the voice frames, as before.

So one buzz call tells us which link breaks: no H1/H2 -> the radio never sets DEMOD_OUT; H's but
no F01 -> baseband ignores DEMOD_OUT in DMR; F01 but no V -> the copy-site hook/path is wrong.

Nothing is transmitted by this build. See D890_dmr_voice_frames.md.
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

PM_SITE = 0x081003E8                       # bl ADD_FIELD, r0=0x18 (PROCESS_MODE)
IO_SITE = 0x080FFFF8                       # bl ADD_FIELD, r0=0x3e (VOCODER_IO_SET)
CHAND_SITE = 0x08146E90                    # bl MEMCPY: CHAN_D frames -> DAT_2000d5e2
DISP_SITES = (0x080DEF56, 0x080E0C50, 0x0810BB5E)  # bl DISPATCH
MODE_SW = 0x080FAD68
MODE_RESUME = 0x080FAD78

SETUP = {PM_SITE: (ADD_FIELD, "1820"), IO_SITE: (ADD_FIELD, "0df10701"),
         CHAND_SITE: (MEMCPY, "2422")}

SCRATCH = 0x2002A300                       # unreferenced (checked)
SEQ, FLAGS, SEEN, CSEEN, PLBUF, TXBUF = SCRATCH, SCRATCH + 1, SCRATCH + 0x10, SCRATCH + 0x38, SCRATCH + 0x40, SCRATCH + 0xC0
DEMOD_OUT = 0x04
FLASH_LIMIT = 0x081E0000

ASM = f"""
.syntax unified
.thumb
.cpu cortex-m4
.section .text.diag,"ax"
.global _start
_start:

@ ---- PROCESS_MODE hook: original add, then VOCODER_IO_SET=DEMOD_OUT, report H1 once ----
pm_stub:
  push {{r7, lr}}
call_af1:
  bl .                              @ ADD_FIELD(0x18, ptr), args as entered
  movs r0, #0x3e
  ldr r1, =io_byte
call_af2:
  bl .                              @ ADD_FIELD(0x3e, &io_byte)
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

@ ---- VOCODER_IO_SET hook: keep DEMOD_OUT set, report H2 once ----
io_stub:
  push {{r7, lr}}
  mov r7, r1                        @ ptr (survives send_payload)
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
  bl .                              @ ADD_FIELD(0x3e, ptr)
  pop {{r7, pc}}

@ ---- CHAN_D copy hook: stock copy, then forward the frames as V<seq><hex> ----
fwd_stub:
  push {{r3-r7, lr}}
  mov r7, r1                        @ frames src
call_memcpy:
  bl .                              @ MEMCPY(dst, src, 0x24)
  ldrb r5, [r7, #-1]               @ frame byte count
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
  mov r4, r0                        @ param_1
  ldrb r5, [r4, #1]                @ field id
  ldr r3, =0x{SEEN:08x}
  lsrs r0, r5, #3                   @ byte index (kept in r0)
  ldrb r1, [r3, r0]
  and r2, r5, #7
  movs r6, #1
  lsl r6, r6, r2                    @ bit mask
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
  bl .                              @ FUN_08146930(param_1)
  pop {{r3-r7, pc}}


@ ---- Central voice-mode detour: report C<mode> once per distinct mode, then resume the body ----
tap_mode:
  push {{r7, lr}}
  sub sp, #0x68
  strb.w r0, [sp, #0x67]
  strb.w r1, [sp, #0x66]
  strh.w r2, [sp, #0x64]
  ldrb r5, [sp, #0x67]
  ldr r3, =0x2002a338
  lsrs r0, r5, #3
  ldrb r1, [r3, r0]
  and r2, r5, #7
  movs r6, #1
  lsl r6, r6, r2
  tst r1, r6
  bne tap_resume
  orr r1, r1, r6
  strb r1, [r3, r0]
  ldr r6, =0x2002a340
  movs r0, #0x43
  strb r0, [r6], #1
  mov r0, r5
  bl puthex
  ldr r0, =0x2002a340
  subs r5, r6, r0
  bl send_payload
tap_resume:
call_resume:
  b.w resume_pad
resume_pad:
  nop

@ ---- send_payload(r0 = src, r5 = len): wrap as "AT+DATA=00,NNN\\r\\n<payload>\\r\\n" ----
send_payload:
  push {{r4-r6, lr}}
  mov r4, r0                        @ src
  ldr r6, =0x{TXBUF:08x}
  adr r1, hdr
  movs r2, #11
sp_hdr:
  ldrb r0, [r1], #1
  strb r0, [r6], #1
  subs r2, #1
  bne sp_hdr
  movs r1, #100                     @ 3-digit decimal length
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
  bl .                              @ UART_SEND(0x10, TXBUF, len)
  pop {{r4-r6, pc}}

@ ---- puthex(r0 = byte, r6 = write cursor): two lowercase hex chars ----
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
         "call_memcpy": MEMCPY, "call_disp": DISPATCH, "call_uart": UART_SEND}


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
    o = addr["call_resume"] - blob
    stub[o:o + 4] = thumb_bw(addr["call_resume"], MODE_RESUME)
    return bytes(stub), addr


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path, default=Path("patched_blecps/D890UV_V1.05_20260521"))
    p.add_argument("--output-dir", type=Path, default=Path("patched_dmrtap"))
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
    if bytes(cdd[MODE_SW - BASE:MODE_SW - BASE + 4]) != bytes.fromhex("80b59ab0"):
        raise SystemExit("MODE_SW prologue mismatch")

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
    cdd[MODE_SW - BASE:MODE_SW - BASE + 4] = thumb_bw(MODE_SW, sym["tap_mode"])

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    print(f"blob @0x{blob:08x} ({len(stub)} B), end 0x{BASE + len(cdd):08x}")
    for site, name in [(PM_SITE, "pm_stub"), (IO_SITE, "io_stub"), (CHAND_SITE, "fwd_stub"),
                       *[(s, "disp_stub") for s in DISP_SITES]]:
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
