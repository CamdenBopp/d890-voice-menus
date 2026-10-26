#!/usr/bin/env python3
"""D890 main-board patch: wireless CPS over BLE (self-contained, no PC mode).

Self-contained CPS handler in the BLE receive hook — does NOT call any
stock CPS functions, so the radio never shows "PC mode" on the display.

Handles:
  PROGRAM  -> ACK (0x06)
  \\x02     -> 16-byte identity response
  R+addr+len -> W+addr+len+data+checksum+ACK (via SPI flash read)
  END      -> ignored

Built on patched_voiceitems (voice menu + Morse).
"""
from __future__ import annotations
import argparse, hashlib, subprocess, tempfile
from pathlib import Path
import capstone
from make_d890_morse_firmware import BASE, align_up, package_paths, patch_size_fields, thumb_bl

# --- Hook site ---
RX_HOOK = 0x0801E1EE
RX_ORIG = 0x080C630C

# --- External functions ---
SPI_READ = 0x08037A9C       # FUN_08037a9c(addr, buf, len) — SPI flash read
UART_SEND = 0x080D71E8      # FUN_080d71e8(id, ptr, len) — STM32→BT/BLE

# --- RAM vars ---
BAND_VAR = 0x20015277       # band byte for identity response
DISP_ID = 0x20003CE0        # +DATA dispatcher id
PAYLOAD_BUF = 0x20003CEA    # +DATA payload buffer
HDR_LEN = 0x20008795        # +DATA header length
PAYLOAD_LEN = 0x20003ADA    # +DATA payload length

# --- Scratch RAM ---
SCRATCH = 0x2002A000        # TX buffer (256 B)
READ_BUF = 0x2002A100       # reply data buffer (256 B)

FLASH_LIMIT = 0x081E0000
IN_DEFAULT = Path("patched_voiceitems/D890UV_V1.05_20260521")
OUT_DEFAULT = Path("patched_blecps")

RX_ASM = f"""
.syntax unified
.thumb
.cpu cortex-m4
.section .text.blecps,"ax"
.global rx_stub

rx_stub:
  push {{r4-r7, lr}}

  @ ---- Check dispatcher id == 0x10 (+DATA) ----
  movw r0, #0x{DISP_ID & 0xFFFF:04x}
  movt r0, #0x{DISP_ID >> 16:04x}
  ldrb r0, [r0]
  cmp r0, #0x10
  bne tail

  @ ---- Get payload ptr (r4) and len (r5) ----
  movw r4, #0x{PAYLOAD_BUF & 0xFFFF:04x}
  movt r4, #0x{PAYLOAD_BUF >> 16:04x}
  movw r0, #0x{HDR_LEN & 0xFFFF:04x}
  movt r0, #0x{HDR_LEN >> 16:04x}
  ldrb r1, [r0]
  add r4, r4, r1
  movw r0, #0x{PAYLOAD_LEN & 0xFFFF:04x}
  movt r0, #0x{PAYLOAD_LEN >> 16:04x}
  ldrh r5, [r0]
  cmp r5, #0
  beq tail

  @ ---- Dispatch on first byte ----
  ldrb r0, [r4]
  cmp r0, #0x50
  beq handle_program
  cmp r0, #0x02
  beq handle_identity
  cmp r0, #0x52
  beq handle_read
  b tail

@ ============================================================
@ PROGRAM -> single ACK byte
@ ============================================================
handle_program:
  movw r5, #0x{READ_BUF & 0xFFFF:04x}
  movt r5, #0x{READ_BUF >> 16:04x}
  movs r0, #0x06
  strb r0, [r5]
  movs r4, #1
  b common_send

@ ============================================================
@ Identity request (0x02) -> 16-byte identity string
@ I D890UV \\0 <band> V100 \\0 ACK \\0
@ ============================================================
handle_identity:
  movw r5, #0x{READ_BUF & 0xFFFF:04x}
  movt r5, #0x{READ_BUF >> 16:04x}
  movs r0, #0x49
  strb r0, [r5, #0]
  movs r0, #0x44
  strb r0, [r5, #1]
  movs r0, #0x38
  strb r0, [r5, #2]
  movs r0, #0x39
  strb r0, [r5, #3]
  movs r0, #0x30
  strb r0, [r5, #4]
  movs r0, #0x55
  strb r0, [r5, #5]
  movs r0, #0x56
  strb r0, [r5, #6]
  movs r0, #0
  strb r0, [r5, #7]
  movw r0, #0x{BAND_VAR & 0xFFFF:04x}
  movt r0, #0x{BAND_VAR >> 16:04x}
  ldrb r0, [r0]
  strb r0, [r5, #8]
  movs r0, #0x56
  strb r0, [r5, #9]
  movs r0, #0x31
  strb r0, [r5, #10]
  movs r0, #0x30
  strb r0, [r5, #11]
  strb r0, [r5, #12]
  movs r0, #0
  strb r0, [r5, #13]
  movs r0, #0x06
  strb r0, [r5, #14]
  movs r0, #0
  strb r0, [r5, #15]
  movs r4, #16
  b common_send

@ ============================================================
@ R-command -> SPI flash read, W+data reply
@ Payload: R + addr(4 BE) + len(1)
@ Reply:   W + addr(4 BE) + len(1) + data + checksum + ACK
@ ============================================================
handle_read:
  cmp r5, #6
  blo tail
  @ Parse big-endian address from payload[1..4]
  ldrb r6, [r4, #1]
  lsls r6, r6, #24
  ldrb r0, [r4, #2]
  orr r6, r6, r0, lsl #16
  ldrb r0, [r4, #3]
  orr r6, r6, r0, lsl #8
  ldrb r0, [r4, #4]
  orr r6, r6, r0
  @ Read length
  ldrb r7, [r4, #5]
  cmp r7, #0
  beq tail
  cmp r7, #200
  bls 1f
  movs r7, #200
1:
  @ SPI flash read: FUN_08037a9c(addr, buf, len)
  @ Read data into READ_BUF+6 (after W+addr+len header)
  mov r0, r6
  movw r1, #0x{(READ_BUF + 6) & 0xFFFF:04x}
  movt r1, #0x{(READ_BUF + 6) >> 16:04x}
  mov r2, r7
c_spi_read: bl c_spi_read

  @ Build reply header at READ_BUF
  movw r4, #0x{READ_BUF & 0xFFFF:04x}
  movt r4, #0x{READ_BUF >> 16:04x}
  movs r0, #0x57
  strb r0, [r4, #0]
  lsrs r0, r6, #24
  strb r0, [r4, #1]
  ubfx r0, r6, #16, #8
  strb r0, [r4, #2]
  ubfx r0, r6, #8, #8
  strb r0, [r4, #3]
  uxtb r0, r6
  strb r0, [r4, #4]
  strb r7, [r4, #5]

  @ Checksum: sum of bytes [1..5+r7], masked to 8 bits
  movs r0, #0
  movs r1, #1
  adds r2, r7, #6
2:
  cmp r1, r2
  bhs 3f
  ldrb r3, [r4, r1]
  adds r0, r0, r3
  adds r1, r1, #1
  b 2b
3:
  uxtb r0, r0
  strb r0, [r4, r2]
  adds r2, r2, #1
  movs r0, #0x06
  strb r0, [r4, r2]

  @ Set up r4=total_len, r5=buf for common_send
  mov r5, r4
  adds r4, r7, #8
  b common_send

@ ============================================================
@ common_send: build AT+DATA=00,NNN and send via BLE
@ r4 = data length, r5 = data pointer
@ ============================================================
common_send:
  movw r6, #0x{SCRATCH & 0xFFFF:04x}
  movt r6, #0x{SCRATCH >> 16:04x}

  @ Write "AT+DATA=00,"
  movs r0, #0x41
  strb r0, [r6, #0]
  movs r0, #0x54
  strb r0, [r6, #1]
  movs r0, #0x2b
  strb r0, [r6, #2]
  movs r0, #0x44
  strb r0, [r6, #3]
  movs r0, #0x41
  strb r0, [r6, #4]
  movs r0, #0x54
  strb r0, [r6, #5]
  movs r0, #0x41
  strb r0, [r6, #6]
  movs r0, #0x3d
  strb r0, [r6, #7]
  movs r0, #0x30
  strb r0, [r6, #8]
  strb r0, [r6, #9]
  movs r0, #0x2c
  strb r0, [r6, #10]

  @ Format r4 as 3 decimal digits
  mov r0, r4
  movs r1, #100
  udiv r2, r0, r1
  mls r0, r2, r1, r0
  adds r2, r2, #0x30
  strb r2, [r6, #11]
  movs r1, #10
  udiv r2, r0, r1
  mls r0, r2, r1, r0
  adds r2, r2, #0x30
  strb r2, [r6, #12]
  adds r0, r0, #0x30
  strb r0, [r6, #13]

  @ \\r\\n
  movs r0, #0x0d
  strb r0, [r6, #14]
  movs r0, #0x0a
  strb r0, [r6, #15]

  @ Copy r4 bytes from r5 to TX_BUF+16
  add r2, r6, #16
  movs r0, #0
4:
  cmp r0, r4
  bhs 5f
  ldrb r1, [r5, r0]
  strb r1, [r2, r0]
  adds r0, r0, #1
  b 4b
5:
  @ Trailing \\r\\n
  add r2, r2, r0
  movs r1, #0x0d
  strb r1, [r2]
  movs r1, #0x0a
  strb r1, [r2, #1]

  @ UART_SEND(0x10, TX_BUF, 16 + datalen + 2)
  adds r2, r4, #18
  movs r0, #0x10
  mov r1, r6
c_uart: bl c_uart
  b tail

tail:
c_rx_orig: bl c_rx_orig
  pop {{r4-r7, pc}}
.ltorg
"""

CALLS = {
    "c_spi_read": SPI_READ,
    "c_uart": UART_SEND,
    "c_rx_orig": RX_ORIG,
}


def run(cmd, **kw):
    return subprocess.run([str(x) for x in cmd], check=True, **kw)


def build_stub(blob_addr: int) -> tuple[bytes, dict[str, int]]:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "s.S").write_text(RX_ASM)
        run(["arm-none-eabi-as", "-mcpu=cortex-m4", "-mthumb", "-o", t / "s.o", t / "s.S"])
        (t / "link.ld").write_text(
            f"ENTRY(_start)\nSECTIONS {{ .text 0x{blob_addr:08x} : {{ *(.text*) }} "
            f"/DISCARD/ : {{ *(.ARM.*) *(.comment) }} }}\n")
        run(["arm-none-eabi-ld", "-T", t / "link.ld", "-o", t / "s.elf", t / "s.o"])
        run(["arm-none-eabi-objcopy", "-O", "binary", "-j", ".text", t / "s.elf", t / "s.bin"])
        syms_out = run(["arm-none-eabi-nm", t / "s.elf"], capture_output=True, text=True).stdout
        addr = {l.split()[2]: int(l.split()[0], 16) for l in syms_out.splitlines() if len(l.split()) == 3}
        stub = bytearray((t / "s.bin").read_bytes())
    for name, target in CALLS.items():
        o = addr[name] - blob_addr
        stub[o:o + 4] = thumb_bl(addr[name], target)
    return bytes(stub), addr


def main() -> int:
    p = argparse.ArgumentParser(description="Build D890 wireless CPS firmware (no PC mode)")
    p.add_argument("--input-base", type=Path, default=IN_DEFAULT)
    p.add_argument("--output-dir", type=Path, default=OUT_DEFAULT)
    a = p.parse_args()

    in_spi, in_cdi, in_cdd = package_paths(a.input_base)
    spi = bytearray(in_spi.read_bytes())
    cdi = bytearray(in_cdi.read_bytes())
    cdd = bytearray(in_cdd.read_bytes())
    if int.from_bytes(spi[6:10], "little") != len(cdd) or int.from_bytes(cdi[260:264], "little") != len(cdd):
        raise ValueError("base size fields inconsistent")

    rx_have = bytes(cdd[RX_HOOK - BASE:RX_HOOK - BASE + 4])
    rx_expect = thumb_bl(RX_HOOK, RX_ORIG)
    if rx_have != rx_expect:
        raise ValueError(f"RX hook 0x{RX_HOOK:08x}: have {rx_have.hex()}, want bl 0x{RX_ORIG:08x}")

    blob_addr = BASE + len(cdd)
    stub, sym = build_stub(blob_addr)
    end_addr = blob_addr + len(stub)
    if end_addr > FLASH_LIMIT:
        raise ValueError(f"stub ends 0x{end_addr:08x}, past limit")

    # Verify all BL targets are in CALLS
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    bls = sorted(int(i.op_str.lstrip("#"), 16) for i in md.disasm(stub, blob_addr) if i.mnemonic == "bl")
    expected = sorted(CALLS.values())
    if bls != expected:
        raise ValueError(f"BLs {[hex(b) for b in bls]} != {[hex(x) for x in expected]}")

    cdd[RX_HOOK - BASE:RX_HOOK - BASE + 4] = thumb_bl(RX_HOOK, sym["rx_stub"])
    cdd.extend(stub)
    cdd.extend(b"\xff" * (align_up(len(cdd), 32) - len(cdd)))
    patch_size_fields(spi, cdi, len(cdd))

    a.output_dir.mkdir(parents=True, exist_ok=True)
    base = a.output_dir / "D890UV_V1.05_20260521"
    Path(str(base) + ".spi").write_bytes(spi)
    Path(str(base) + ".CDI").write_bytes(cdi)
    Path(str(base) + ".CDD").write_bytes(cdd)

    print(f"stub @0x{sym['rx_stub']:08x} ({len(stub)} B), hook 0x{RX_HOOK:08x}")
    print(f"end=0x{end_addr:08x} sha256={hashlib.sha256(cdd).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
