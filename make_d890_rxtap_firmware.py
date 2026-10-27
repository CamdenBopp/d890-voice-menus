#!/usr/bin/env python3
"""DMR RX tap at the true async sink: forward baseband->MCU packets over BLE. On blecps.

The earlier diag hooked the synchronous reply-matcher (FUN_08146930), which is silent in passive
RX. The real path is FUN_0802de84 (UART RX demux, sync 84 A9 61) -> FUN_080f335c for every
unsolicited baseband packet. Packet layout: [84 a9 61 00 len_lo len_hi id payload...]; field id at
offset 6, payload at offset 7, total length passed as the 2nd arg.

This build:
  - keeps DEMOD_OUT set (pm/io hooks) so the baseband routes received voice to the MCU;
  - trampolines FUN_080f335c and, for every packet it receives, sends over BLE (FUN_080d71e8 0x10):
      R<seq><hex payload>  for field 0x01 (CHAN_D voice frames) - one per packet, seq to count drops
      P<id>                for any other field id - once per distinct id (dedup bitmap)
    then runs the stock FUN_080f335c body unchanged.

If a decoded DMR call produces R-lines, the hotspot RX tap lives here, with no record-mode state
machine needed. Nothing is transmitted by this build.
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
UART_SEND = 0x080D71E8
PM_SITE = 0x081003E8
IO_SITE = 0x080FFFF8
SINK = 0x080F335C                          # FUN_080f335c; detour first 4 bytes
SINK_RESUME = 0x080F3366                   # after the prologue arg stores
SETUP = {PM_SITE: (ADD_FIELD, "1820"), IO_SITE: (ADD_FIELD, "0df10701")}

SCRATCH = 0x2002A300
SEQ, FLAGS, PSEEN, PLBUF, TXBUF = SCRATCH, SCRATCH + 1, SCRATCH + 0x10, SCRATCH + 0x40, SCRATCH + 0xC0
DEMOD_OUT = 0x04
MAXPL = 0x2A                                # cap payload hex per R line
FLASH_LIMIT = 0x081E0000

ASM = f"""
.syntax unified
.thumb
.cpu cortex-m4
.section .text.rxtap,"ax"
.global _start
_start:

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

@ ---- RX sink detour: forward every baseband packet, then resume the stock body ----
rx_stub:
  push {{r7, lr}}
  sub sp, #8
  str r0, [sp, #4]                   @ re-do the relocated prologue stores
  strh.w r1, [sp, #2]
  ldr r7, [sp, #4]                   @ packet ptr (survives send_payload)
  ldrb r0, [r7, #6]                  @ field id
  cmp r0, #1
  beq rx_voice
  @ other field: dedup, send "P<id>"
  ldr r3, =0x{PSEEN:08x}
  lsrs r1, r0, #3
  ldrb r2, [r3, r1]
  and r4, r0, #7
  movs r6, #1
  lsl r6, r6, r4
  tst r2, r6
  bne rx_resume
  orr r2, r2, r6
  strb r2, [r3, r1]
  ldr r6, =0x{PLBUF:08x}
  movs r1, #0x50                     @ 'P'
  strb r1, [r6], #1
  bl puthex
  ldr r0, =0x{PLBUF:08x}
  subs r5, r6, r0
  bl send_payload
  b rx_resume
rx_voice:
  @ "R" + seq(2 hex) + payload hex (payload at ptr+7, len = arg2 - 7, capped)
  ldrh r5, [sp, #2]
  subs r5, #7
  bls rx_resume
  cmp r5, #{MAXPL}
  bls rx_len_ok
  movs r5, #{MAXPL}
rx_len_ok:
  ldr r6, =0x{PLBUF:08x}
  movs r0, #0x52                     @ 'R'
  strb r0, [r6], #1
  ldr r1, =0x{SEQ:08x}
  ldrb r0, [r1]
  adds r2, r0, #1
  strb r2, [r1]
  bl puthex
  adds r4, r7, #7                    @ payload start
rx_vloop:
  ldrb r0, [r4], #1
  bl puthex
  subs r5, #1
  bne rx_vloop
  ldr r0, =0x{PLBUF:08x}
  subs r5, r6, r0
  bl send_payload
rx_resume:
call_resume:
  b.w resume_pad
resume_pad:
  nop

@ ---- send_payload(r0 = src, r5 = len) ----
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
  bl .
  pop {{r4-r6, pc}}

@ ---- puthex(r0 = byte, r6 = cursor) ----
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

CALLS = {"call_af1": ADD_FIELD, "call_af2": ADD_FIELD, "call_af3": ADD_FIELD, "call_uart": UART_SEND}


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
    stub[o:o + 4] = thumb_bw(addr["call_resume"], SINK_RESUME)
    return bytes(stub), addr


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path, default=Path("patched_blecps/D890UV_V1.05_20260521"))
    p.add_argument("--output-dir", type=Path, default=Path("patched_rxtap"))
    args = p.parse_args()
    in_spi, in_cdi, in_cdd = package_paths(args.input_base)
    spi = bytearray(in_spi.read_bytes()); cdi = bytearray(in_cdi.read_bytes()); cdd = bytearray(in_cdd.read_bytes())
    if int.from_bytes(spi[6:10], "little") != len(cdd) or int.from_bytes(cdi[260:264], "little") != len(cdd):
        raise SystemExit("input size fields disagree with CDD length")
    print("base CDD sha256", hashlib.sha256(cdd).hexdigest()[:16])

    for site, (target, before) in SETUP.items():
        off = site - BASE
        if bytes(cdd[off - len(bytes.fromhex(before)):off]) != bytes.fromhex(before):
            raise SystemExit(f"0x{site:x}: setup mismatch")
        if bytes(cdd[off:off + 4]) != thumb_bl(site, target):
            raise SystemExit(f"0x{site:x}: BL mismatch")
    if bytes(cdd[SINK - BASE:SINK - BASE + 4]) != bytes.fromhex("80b582b0"):
        raise SystemExit("SINK prologue mismatch")

    blob = align_up(BASE + len(cdd), 32)
    cdd.extend(b"\xff" * (blob - BASE - len(cdd)))
    stub, sym = build_stub(blob)
    cdd.extend(stub)
    cdd.extend(b"\xff" * (align_up(len(cdd), 32) - len(cdd)))
    if BASE + len(cdd) > FLASH_LIMIT:
        raise SystemExit("overruns flash")
    cdd[PM_SITE - BASE:PM_SITE - BASE + 4] = thumb_bl(PM_SITE, sym["pm_stub"])
    cdd[IO_SITE - BASE:IO_SITE - BASE + 4] = thumb_bl(IO_SITE, sym["io_stub"])
    cdd[SINK - BASE:SINK - BASE + 4] = thumb_bw(SINK, sym["rx_stub"])

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    print(f"blob @0x{blob:08x} ({len(stub)} B), end 0x{BASE + len(cdd):08x}")
    for site, name in [(PM_SITE, "pm_stub"), (IO_SITE, "io_stub"), (SINK, "rx_stub")]:
        i = next(md.disasm(bytes(cdd[site - BASE:site - BASE + 4]), site))
        print(f"  site {site:08x} -> {name} {i.mnemonic} {i.op_str}")

    patch_size_fields(spi, cdi, len(cdd))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base = args.output_dir / in_cdd.stem
    Path(str(base) + ".spi").write_bytes(spi); Path(str(base) + ".CDI").write_bytes(cdi); Path(str(base) + ".CDD").write_bytes(cdd)
    print("CDD sha256", hashlib.sha256(cdd).hexdigest()[:16], "size", hex(len(cdd)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
