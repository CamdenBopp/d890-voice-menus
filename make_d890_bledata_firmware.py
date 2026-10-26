#!/usr/bin/env python3
"""D890 main-board patch: BLE data reply test.

Like btread, but tries 'DATA=00,' instead of 'ADATA=00,' for the reply,
since +DATA is the BLE receive path and DATA= may be the BLE send path.

Also adds a second command 'T' (test) that sends a short fixed message,
useful for quick round-trip testing without needing a specific address.

Protocol (phone -> radio, written to BLE char FFE3, arrives as +DATA):
    'R' <addr:4 BE> <len:1>            len clamped 1..32
    'T'                                 sends back 'OK' as test
Reply (radio -> phone, via DATA=00 data path):
    'W' <addr:4 BE echo> <data:len>    for R
    'OK'                                for T
"""
from __future__ import annotations
import argparse, hashlib, subprocess, tempfile
from pathlib import Path
import capstone
from make_d890_morse_firmware import BASE, align_up, package_paths, patch_size_fields, thumb_bl

HOOK_SITE = 0x0801E1EE          # bl FUN_080c630c  (state-2 +DATA/+ADATA completion)
DISP = 0x080C630C               # the string/line dispatcher (must still run)
UART_SEND = 0x080D71E8          # FUN_080d71e8(dest_id, buf_ptr, len) — direct UART TX
FLASH_LIMIT = 0x081E0000
IN_DEFAULT = Path("patched_voiceitems/D890UV_V1.05_20260521")
OUT_DEFAULT = Path("patched_bledata")

# Instead of going through board_command_queue (which builds AT+cmd\r\n framing
# via the wire sender), we build the full AT+DATA=00,NNN\r\n<payload>\r\n frame
# ourselves and call FUN_080d71e8 directly with dest_id=0x10. This is exactly
# what the wire sender does, but we control the command string.
ASM = f"""
.syntax unified
.thumb
.cpu cortex-m4
.section .text.bledata,"ax"
.global _start
_start:
stub:
  push {{r4-r7, lr}}
  @ Check if this is a +DATA completion (id == 0x10)
  movw r0,#0x3ce0
  movt r0,#0x2000
  ldrb r0,[r0]
  cmp r0,#0x10
  bne tail

  @ Get payload pointer and length
  movw r4,#0x3cea
  movt r4,#0x2000
  movw r0,#0x8795
  movt r0,#0x2000
  ldrb r1,[r0]
  add r4,r4,r1              @ r4 = payload ptr
  movw r0,#0x3ada
  movt r0,#0x2000
  ldrh r5,[r0]             @ r5 = payload len

  @ Check for 'T' test command (single byte)
  cmp r5,#1
  blo tail
  ldrb r0,[r4]
  cmp r0,#0x54            @ 'T'
  beq do_test

  @ Check for 'R' read command (6+ bytes)
  cmp r5,#6
  blo tail
  ldrb r0,[r4]
  cmp r0,#0x52            @ 'R'
  bne tail

  @ Parse address (big-endian) from payload[1..4]
  ldrb r6,[r4,#1]
  lsls r6,r6,#8
  ldrb r0,[r4,#2]
  orrs r6,r6,r0
  lsls r6,r6,#8
  ldrb r0,[r4,#3]
  orrs r6,r6,r0
  lsls r6,r6,#8
  ldrb r0,[r4,#4]
  orrs r6,r6,r0          @ r6 = addr

  @ Parse requested length
  ldrb r7,[r4,#5]        @ r7 = req len

  @ Validate address range 0x2000xxxx-0x2002xxxx
  lsrs r0,r6,#16
  movw r1,#0x2000
  cmp r0,r1
  blo tail
  movw r1,#0x2002
  cmp r0,r1
  bhi tail
  cmp r7,#32
  bls 1f
  movs r7,#32
1:
  cmp r7,#0
  beq tail

  @ Build reply in scratch buffer at 0x20005201
  @ Format: W <addr:4BE> <data:len>
  movw r2,#0x5201
  movt r2,#0x2000
  movs r0,#0x57          @ 'W'
  strb r0,[r2]
  lsrs r0,r6,#24
  strb r0,[r2,#1]
  lsrs r0,r6,#16
  strb r0,[r2,#2]
  lsrs r0,r6,#8
  strb r0,[r2,#3]
  strb r6,[r2,#4]
  adds r3,r2,#5          @ r3 = data dest
  movs r0,#0
2:
  ldrb r1,[r6,r0]
  strb r1,[r3,r0]
  adds r0,r0,#1
  cmp r0,r7
  blo 2b

  @ r7 = data payload length, total reply = 5+r7
  adds r5,r7,#5          @ r5 = reply payload len
  b send_reply

do_test:
  @ Build 'OK' reply at 0x20005201
  movw r2,#0x5201
  movt r2,#0x2000
  movs r0,#0x4f          @ 'O'
  strb r0,[r2]
  movs r0,#0x4b          @ 'K'
  strb r0,[r2,#1]
  movs r5,#2             @ r5 = reply payload len

send_reply:
  @ Build AT+DATA=00,NNN\\r\\n<payload>\\r\\n in tx buffer at 0x20003ddf
  movw r2,#0x3ddf
  movt r2,#0x2000
  @ "AT+DATA=00,"  = 41 54 2b 44 41 54 41 3d 30 30 2c
  movs r0,#0x41          @ 'A'
  strb r0,[r2]
  movs r0,#0x54          @ 'T'
  strb r0,[r2,#1]
  movs r0,#0x2b          @ '+'
  strb r0,[r2,#2]
  movs r0,#0x44          @ 'D'
  strb r0,[r2,#3]
  movs r0,#0x41          @ 'A'
  strb r0,[r2,#4]
  movs r0,#0x54          @ 'T'
  strb r0,[r2,#5]
  movs r0,#0x41          @ 'A'
  strb r0,[r2,#6]
  movs r0,#0x3d          @ '='
  strb r0,[r2,#7]
  movs r0,#0x30          @ '0'
  strb r0,[r2,#8]
  movs r0,#0x30          @ '0'
  strb r0,[r2,#9]
  movs r0,#0x2c          @ ','
  strb r0,[r2,#10]

  @ Format length as 3-digit decimal
  mov r0,r5
  movs r4,#100
  udiv r3,r0,r4
  mls r0,r3,r4,r0
  adds r3,r3,#0x30
  strb r3,[r2,#11]
  movs r4,#10
  udiv r3,r0,r4
  mls r0,r3,r4,r0
  adds r3,r3,#0x30
  strb r3,[r2,#12]
  adds r0,r0,#0x30
  strb r0,[r2,#13]
  @ \\r\\n
  movs r0,#0x0d
  strb r0,[r2,#14]
  movs r0,#0x0a
  strb r0,[r2,#15]

  @ Copy reply payload
  movw r3,#0x5201
  movt r3,#0x2000
  movs r0,#0
4:
  cmp r0,r5
  bhs 5f
  ldrb r1,[r3,r0]
  adds r4,r2,#16
  strb r1,[r4,r0]
  adds r0,r0,#1
  b 4b
5:
  @ \\r\\n after payload
  adds r4,r0,#16
  adds r4,r4,r2
  movs r1,#0x0d
  strb r1,[r4]
  movs r1,#0x0a
  strb r1,[r4,#1]

  @ Total frame length = 16 (header) + r5 (payload) + 2 (trailing crlf)
  adds r0,r5,#18
  @ Call FUN_080d71e8(0x10, 0x20003ddf, total_len)
  movs r1,r0              @ r1 = len (save)
  movs r0,#0x10           @ dest id
  movw r2,#0x3ddf         @ ptr was r2, but we need: r0=id r1=ptr r2=len
  movt r2,#0x2000
  @ oops, shuffle: r0=0x10, r1=buf, r2=len
  @ r1 currently has total_len, r2 has buf ptr. swap:
  mov r3,r1               @ r3 = total_len
  mov r1,r2               @ r1 = buf ptr
  mov r2,r3               @ r2 = total_len
  movs r0,#0x10           @ r0 = dest_id
c_uart: bl c_uart

tail:
c_disp: bl c_disp
  pop {{r4-r7, pc}}
.ltorg
"""

CALLS = {"c_uart": UART_SEND, "c_disp": DISP}


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
        syms = run(["arm-none-eabi-nm", t / "s.elf"], capture_output=True, text=True).stdout
        addr = {l.split()[2]: int(l.split()[0], 16) for l in syms.splitlines() if len(l.split()) == 3}
        stub = bytearray((t / "s.bin").read_bytes())
    for name, target in CALLS.items():
        o = addr[name] - blob_addr
        stub[o:o + 4] = thumb_bl(addr[name], target)
    return bytes(stub), addr


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path, default=IN_DEFAULT)
    p.add_argument("--output-dir", type=Path, default=OUT_DEFAULT)
    a = p.parse_args()
    in_spi, in_cdi, in_cdd = package_paths(a.input_base)
    spi = bytearray(in_spi.read_bytes()); cdi = bytearray(in_cdi.read_bytes()); cdd = bytearray(in_cdd.read_bytes())
    if int.from_bytes(spi[6:10], "little") != len(cdd) or int.from_bytes(cdi[260:264], "little") != len(cdd):
        raise ValueError("base size fields inconsistent")

    have = bytes(cdd[HOOK_SITE - BASE:HOOK_SITE - BASE + 4])
    if have != thumb_bl(HOOK_SITE, DISP):
        raise ValueError(f"hook site 0x{HOOK_SITE:08x} is {have.hex()}, not bl 0x{DISP:08x}")

    blob_addr = BASE + len(cdd)
    stub, sym = build_stub(blob_addr)
    end = blob_addr + len(stub)
    if end > FLASH_LIMIT:
        raise ValueError(f"stub ends 0x{end:08x} past flash limit")

    # verify BLs resolve correctly
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    bls = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(stub, blob_addr) if i.mnemonic == "bl"]
    if sorted(bls) != sorted([UART_SEND, DISP]):
        raise ValueError(f"stub BLs {[hex(b) for b in bls]} != {[hex(UART_SEND), hex(DISP)]}")

    cdd[HOOK_SITE - BASE:HOOK_SITE - BASE + 4] = thumb_bl(HOOK_SITE, sym["stub"])
    cdd.extend(stub)
    cdd.extend(b"\xff" * (align_up(len(cdd), 32) - len(cdd)))
    patch_size_fields(spi, cdi, len(cdd))
    a.output_dir.mkdir(parents=True, exist_ok=True)
    base = a.output_dir / "D890UV_V1.05_20260521"
    Path(str(base) + ".spi").write_bytes(spi)
    Path(str(base) + ".CDI").write_bytes(cdi)
    Path(str(base) + ".CDD").write_bytes(cdd)
    print(f"stub @0x{sym['stub']:08x} ({len(stub)} B), hook 0x{HOOK_SITE:08x} -> stub")
    print(f"new_size={len(cdd)} end=0x{blob_addr + len(stub):08x} sha256={hashlib.sha256(cdd).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
