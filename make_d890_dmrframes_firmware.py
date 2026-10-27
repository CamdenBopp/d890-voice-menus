#!/usr/bin/env python3
"""DMR hotspot groundwork: forward every received voice burst over BLE. Test build, on blecps.

Hypothesis: the SCT3288 hands received DMR voice to the MCU as CHAN_D fields (field 0x01,
27 bytes = three 9-byte AMBE+2 frames per 60 ms burst) once VOCODER_IO_SET (field 0x3E) has
DEMOD_OUT (bit 2) set. The firmware already parses them: FUN_08146930 case 0x01 copies the
frames to DAT_2000d5e2 (stock never enables DEMOD_OUT, so this never fires). See
D890_dmr_voice_frames.md.

Mechanism, three redirected BLs:
1. 0x081003e8, PROCESS_MODE (0x18) field add: after it, also add 0x3E = DEMOD_OUT to the same
   packet, so it goes out whenever the radio sets the baseband's process mode.
2. 0x080ffff8, the only 0x3E sender (FUN_080fffe4): OR DEMOD_OUT into its byte so the stock
   voice-mode switch never clears it. DEMOD_OUT also keeps feeding the radio's own decoder, so
   normal receive audio is unchanged.
3. 0x08146e90, the CHAN_D copy in the dispatcher: after the stock memcpy, send
   "AT+DATA=00,NNN\\r\\nV<seq><frames hex>\\r\\n" to the BLE client via FUN_080d71e8(0x10, ...),
   the proven BLE notify path. seq is a wrapping byte, so the client can count drops.

Pass: while a DMR radio transmits on the D890's channel, a BLE client sees V lines at ~16.7/s
with 54 hex chars of frames each. Nothing is transmitted by this build.
"""
from __future__ import annotations
import argparse, hashlib, subprocess, tempfile
from pathlib import Path
import capstone
from make_d890_morse_firmware import BASE, align_up, package_paths, patch_size_fields, thumb_bl

ADD_FIELD = 0x080122EC                     # FUN_080122ec(tag, ptr): append a field to the packet
MEMCPY = 0x080F3968                        # FUN_080f3968(dst, src, len)
UART_SEND = 0x080D71E8                     # FUN_080d71e8(id, ptr, len); id 0x10 = BLE AT+DATA

PM_SITE = 0x081003E8                       # bl ADD_FIELD with r0 = 0x18 (PROCESS_MODE)
IO_SITE = 0x080FFFF8                       # bl ADD_FIELD with r0 = 0x3E (VOCODER_IO_SET)
CHAND_SITE = 0x08146E90                    # bl MEMCPY: CHAN_D frames -> DAT_2000d5e2
SITES = {PM_SITE: (ADD_FIELD, "1820"), IO_SITE: (ADD_FIELD, "0df10701"), CHAND_SITE: (MEMCPY, "2422")}

DEMOD_OUT = 0x04
SCRATCH = 0x2002A300                       # unreferenced in the image (checked)
SEQ = SCRATCH                              # 1 byte burst counter
TXBUF = SCRATCH + 0x10                     # AT+DATA line, max 11+3+2+3+72+2 = 93 bytes
MAX_FRAME = 0x24                           # dPMR 36; DMR is 27
FLASH_LIMIT = 0x081E0000

ASM = f"""
.syntax unified
.thumb
.cpu cortex-m4
.section .text.dmr,"ax"
.global _start
_start:

@ 1. PROCESS_MODE field, then VOCODER_IO_SET = DEMOD_OUT in the same packet.
pm_stub:
  push {{r4, lr}}
call_pm_orig:
  bl .                          @ -> ADD_FIELD(0x18, ptr), args untouched
  movs r0, #0x3e
  ldr r1, =io_byte
call_pm_io:
  bl .                          @ -> ADD_FIELD(0x3e, &io_byte)
  pop {{r4, pc}}

@ 2. Every VOCODER_IO_SET keeps DEMOD_OUT. r1 -> the byte on the caller's stack.
io_stub:
  push {{r4, lr}}
  ldrb r2, [r1]
  orr r2, r2, #{DEMOD_OUT}
  strb r2, [r1]
call_io_orig:
  bl .                          @ -> ADD_FIELD(0x3e, ptr)
  pop {{r4, pc}}

@ 3. CHAN_D received: stock copy, then forward the frames over BLE.
fwd_stub:
  push {{r3-r7, lr}}
  mov r4, r1                    @ src: frames; length byte sits just before
call_memcpy:
  bl .                          @ -> MEMCPY(dst, src, 0x24)
  ldrb r5, [r4, #-1]            @ frame byte count (0x1b DMR, 0x24 dPMR)
  cbz r5, fwd_done
  cmp r5, #{MAX_FRAME}
  bhi fwd_done
  ldr r7, =0x{TXBUF:08x}
  adr r1, hdr                   @ "AT+DATA=00,"
  movs r2, #11
1:
  ldrb r0, [r1], #1
  strb r0, [r7], #1
  subs r2, #1
  bne 1b
  lsls r6, r5, #1
  adds r6, #3                   @ payload = 'V' + 2 seq hex + 2*n frame hex
  movs r1, #100
  udiv r0, r6, r1
  mls r6, r0, r1, r6
  adds r0, #0x30
  strb r0, [r7], #1
  movs r1, #10
  udiv r0, r6, r1
  mls r6, r0, r1, r6
  adds r0, #0x30
  strb r0, [r7], #1
  adds r0, r6, #0x30
  strb r0, [r7], #1
  movw r0, #0x0a0d              @ "\\r\\n"
  strh r0, [r7], #2
  movs r0, #0x56                @ 'V'
  strb r0, [r7], #1
  ldr r1, =0x{SEQ:08x}
  ldrb r0, [r1]
  adds r2, r0, #1
  strb r2, [r1]
  bl puthex
2:
  ldrb r0, [r4], #1
  bl puthex
  subs r5, #1
  bne 2b
  movw r0, #0x0a0d
  strh r0, [r7], #2
  ldr r1, =0x{TXBUF:08x}
  subs r2, r7, r1
  movs r0, #0x10
call_send:
  bl .                          @ -> UART_SEND(0x10, TXBUF, len)
fwd_done:
  pop {{r3-r7, pc}}

@ r0 = byte -> two lowercase hex chars at r7, r7 += 2. Clobbers r0-r3.
puthex:
  adr r3, hexdigits
  lsrs r1, r0, #4
  ldrb r1, [r3, r1]
  strb r1, [r7], #1
  and r0, r0, #0xf
  ldrb r0, [r3, r0]
  strb r0, [r7], #1
  bx lr

.balign 4
hdr: .ascii "AT+DATA=00,"
.balign 4
hexdigits: .ascii "0123456789abcdef"
io_byte: .byte {DEMOD_OUT}
.balign 4
.ltorg
"""

CALLS = {"call_pm_orig": ADD_FIELD, "call_pm_io": ADD_FIELD, "call_io_orig": ADD_FIELD,
         "call_memcpy": MEMCPY, "call_send": UART_SEND}


def run(cmd, **kw):
    return subprocess.run([str(x) for x in cmd], check=True, **kw)


def build_stub(blob_addr: int) -> tuple[bytes, dict[str, int]]:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "s.S").write_text(ASM)
        run(["arm-none-eabi-as", "-mcpu=cortex-m4", "-mthumb", "-o", t / "s.o", t / "s.S"])
        (t / "link.ld").write_text(
            f"ENTRY(_start)\nSECTIONS {{ .text 0x{blob_addr:08x} : {{ *(.text*) }} "
            f"/DISCARD/ : {{ *(.ARM.*) *(.comment) }} }}\n")
        run(["arm-none-eabi-ld", "-T", t / "link.ld", "-o", t / "s.elf", t / "s.o"])
        run(["arm-none-eabi-objcopy", "-O", "binary", "-j", ".text", t / "s.elf", t / "s.bin"])
        nm = run(["arm-none-eabi-nm", t / "s.elf"], capture_output=True, text=True).stdout
        addr = {l.split()[2]: int(l.split()[0], 16) for l in nm.splitlines() if len(l.split()) == 3}
        stub = bytearray((t / "s.bin").read_bytes())
    for name, target in CALLS.items():
        o = addr[name] - blob_addr
        stub[o:o + 4] = thumb_bl(addr[name], target)
    return bytes(stub), addr


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path, default=Path("patched_blecps/D890UV_V1.05_20260521"))
    p.add_argument("--output-dir", type=Path, default=Path("patched_dmrframes"))
    args = p.parse_args()
    in_spi, in_cdi, in_cdd = package_paths(args.input_base)
    spi = bytearray(in_spi.read_bytes()); cdi = bytearray(in_cdi.read_bytes()); cdd = bytearray(in_cdd.read_bytes())
    if int.from_bytes(spi[6:10], "little") != len(cdd) or int.from_bytes(cdi[260:264], "little") != len(cdd):
        raise SystemExit("input size fields disagree with CDD length")
    print("base CDD sha256", hashlib.sha256(cdd).hexdigest()[:16])

    for site, (target, before) in SITES.items():
        off = site - BASE
        b = bytes.fromhex(before)
        if bytes(cdd[off - len(b):off]) != b:
            raise SystemExit(f"0x{site:x}: unexpected setup before the BL")
        if bytes(cdd[off:off + 4]) != thumb_bl(site, target):
            raise SystemExit(f"0x{site:x}: original BL does not match encoder")

    blob = align_up(BASE + len(cdd), 32)
    cdd.extend(b"\xff" * (blob - BASE - len(cdd)))
    stub, sym = build_stub(blob)
    cdd.extend(stub)
    cdd.extend(b"\xff" * (align_up(len(cdd), 32) - len(cdd)))
    if BASE + len(cdd) > FLASH_LIMIT:
        raise SystemExit("image overruns flash limit")
    for site, name in ((PM_SITE, "pm_stub"), (IO_SITE, "io_stub"), (CHAND_SITE, "fwd_stub")):
        cdd[site - BASE:site - BASE + 4] = thumb_bl(site, sym[name])

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    print(f"stub @0x{blob:08x} ({len(stub)} B), end 0x{BASE + len(cdd):08x}")
    for i in md.disasm(stub, blob):
        if i.address >= sym["hdr"]:
            break
        print(f"  {i.address:08x} {i.bytes.hex():10} {i.mnemonic} {i.op_str}")
    for site in (PM_SITE, IO_SITE, CHAND_SITE):
        i = next(md.disasm(bytes(cdd[site - BASE:site - BASE + 4]), site))
        print(f"  site {site:08x} {i.mnemonic} {i.op_str}")

    patch_size_fields(spi, cdi, len(cdd))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base = args.output_dir / in_cdd.stem
    Path(str(base) + ".spi").write_bytes(spi); Path(str(base) + ".CDI").write_bytes(cdi); Path(str(base) + ".CDD").write_bytes(cdd)
    print("CDD sha256", hashlib.sha256(cdd).hexdigest()[:16], "size", hex(len(cdd)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
