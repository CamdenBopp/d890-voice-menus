#!/usr/bin/env python3
"""Menu item number in Morse, played by the stock tone-playlist engine. On noflip.

How the stock beep works (verified in Ghidra 2026-10-24):
  feedback task FUN_081224f8 -> FUN_0801a420 -> FUN_0801a6d0(code)
  -> FUN_0811cd44(code) sets the playlist ptr _DAT_2000309c = table[code] (RAM 0x20001150)
  -> FUN_08133210 plays u16 {freq, dur} pairs; dur in 10 ms ticks (TIM7 1 ms ISR /10);
     freq 0 = silence, 0xffff = end. Tone = TIM5/TIM4 square wave.
  Stock code 1 (key beep) = {0,15},{800,10},{0,5},{0xffff}.

Patch:
  * display task loop BL mailbox-receive @0x0812263a -> tramp: original, then detect():
    while menu depth DAT_200097e9 is 1..8, a change of {cursor, depth, page} calls
    morse_cue(). (The scroll workers 0x0809878c/0x0809a3dc never ran in the settings menu:
    live probe 2026-10-24.) morse_cue picks the Morse playlist for
    cursor _DAT_20004244 + 1, arms PEND {ptr, tick, magic}, and posts code 0x1c to the
    feedback task (forcing the DAT_2000a479 post gate, then restoring it).
  * FUN_0801a6d0's BL FUN_0811cd44 @0x0801a75c -> play_hook: original, then if PEND is
    armed and < WINDOW ms old, swap _DAT_2000309c to the Morse playlist. A stock key beep
    posted in the same keypress also gets swapped; the restart lands inside the 150 ms
    leading silence, so it is inaudible.
"""
from __future__ import annotations
import argparse, hashlib, struct, subprocess, tempfile
from pathlib import Path
import capstone
from make_d890_morse_firmware import BASE, align_up, package_paths, patch_size_fields, thumb_bl

STUB_ADDR = 0x081A50A0
DISP_SITE = 0x0812263A                     # display task loop: BL mailbox-receive (redraw request)
DISP_RECV = 0x080BF9D8
PLAY_SITE = 0x0801A75C                     # BL FUN_0811cd44 inside FUN_0801a6d0
BUILD_PLAYLIST = 0x0811CD44
POST_FEEDBACK = 0x080D77E8                 # FUN_080d77e8(code), gated on DAT_2000a479==1
BT_APPLY_SITE = 0x08124678                 # BL FUN_0805ae0c inside FUN_08123f28 case 0x12
BT_APPLY_ORIG = 0x0805AE0C                # FUN_0805ae0c (BT mode dispatcher)
BT_SAVE_CODEPLUG = 0x08136474             # FUN_08136474 (save settings to codeplug flash)
BT_LESCAN_START = 0x0801AF18              # FUN_0801af18: sends LESCAN=2 to Jieli (BLE scan)
BT_MODE_VAR = 0x20014995                  # runtime BT mode: 0=off, 1=BLE, 2=classic

CURSOR = 0x20004244
DEPTH = 0x200097E9                         # menu depth: 1 = main menu, 2 = submenu (live)
PAGE = 0x20004248
LAST = 0x2002A80C                          # last {cursor, depth, page} key
MAX_DEPTH = 8
PLAYLIST_PTR = 0x2000309C
TICK_MS = 0x2001AF5C                       # u16, ++ every TIM7 interrupt
POST_GATE = 0x2000A479
PEND = 0x2002A800                          # {u32 ptr, u16 tick, pad, u32 magic}; free SRAM gap
BT_DEFER = 0x2002A810                     # deferred BT apply flag (byte: nonzero = pending)
MAGIC = 0x4D4F5253                         # "MORS"
CODE = 0x1C                                # passes FUN_080d0ad0 unconditionally, stops at end
WINDOW_MS = 250
MAX_N = 199

FREQ = 800                                 # same pitch as the stock key beep
UNIT = 6                                   # 60 ms dot = ~20 WPM
LEAD = 15                                  # stock leading silence (150 ms)
MORSE = {"0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
         "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----."}


def playlist(n: int) -> list[int]:
    out = [0, LEAD]
    for di, ch in enumerate(str(n)):
        for ei, el in enumerate(MORSE[ch]):
            out += [FREQ, UNIT if el == "." else 3 * UNIT]
            last_el = ei == len(MORSE[ch]) - 1
            last_digit = di == len(str(n)) - 1
            out += [0, (5 if last_digit else 3 * UNIT) if last_el else UNIT]
    return out + [0xFFFF, 0]


def asm_source() -> str:
    lists = "\n".join(f"pl_{n}: .hword {', '.join(map(str, playlist(n)))}" for n in range(1, MAX_N + 1))
    words = "\n".join(f"  .word pl_{max(n, 1)}" for n in range(0, MAX_N + 1))
    return f"""
.syntax unified
.thumb
.cpu cortex-m4
.text
.global _start
_start:
tramp_disp:
  push {{r4, lr}}
call_recv: bl call_recv
  push {{r0-r3}}
  bl detect
  pop {{r0-r3}}
  pop {{r4, pc}}

bt_defer_set:
  push {{r0, r1}}
  ldr r0, ={BT_DEFER:#x}
  movs r1, #1
  strb r1, [r0]
  pop {{r0, r1}}
  bx lr

detect:
  push {{r4, lr}}
  @ --- deferred BT mode apply ---
  ldr r0, ={BT_DEFER:#x}
  ldrb r1, [r0]
  cbz r1, 1f
  movs r1, #0
  strb r1, [r0]
call_bt_apply: bl call_bt_apply
call_bt_save: bl call_bt_save
  @ if BLE mode, kick off LESCAN=2 so Jieli starts advertising/scanning
  ldr r0, ={BT_MODE_VAR:#x}
  ldrb r0, [r0]
  cmp r0, #1
  bne 1f
call_bt_lescan: bl call_bt_lescan
1:
  @ --- end deferred BT ---
  ldr r0, ={DEPTH:#x}
  ldrb r0, [r0]
  subs r1, r0, #1
  cmp r1, #{MAX_DEPTH}
  bhs 3f
  ldr r1, ={CURSOR:#x}
  ldrb r1, [r1]
  ldr r2, ={PAGE:#x}
  ldrb r2, [r2]
  orr r1, r1, r0, lsl #8
  orr r1, r1, r2, lsl #16
  ldr r3, ={LAST:#x}
  ldr r2, [r3]
  cmp r1, r2
  beq 4f
  str r1, [r3]
  bl morse_cue
  b 4f
3:
  ldr r3, ={LAST:#x}
  movs r1, #0
  subs r1, #1
  str r1, [r3]
4:
  pop {{r4, pc}}

play_hook:
  push {{r4, lr}}
call_build: bl call_build
  ldr r0, ={PEND:#x}
  ldr r2, [r0, #8]
  ldr r3, ={MAGIC:#x}
  cmp r2, r3
  bne 2f
  ldr r1, [r0]
  cbz r1, 2f
  ldrh r2, [r0, #4]
  ldr r3, ={TICK_MS:#x}
  ldrh r3, [r3]
  subs r3, r3, r2
  uxth r3, r3
  cmp r3, #{WINDOW_MS}
  bhi 1f
  ldr r2, ={PLAYLIST_PTR:#x}
  str r1, [r2]
  b 2f
1:
  movs r1, #0
  str r1, [r0]
2:
  pop {{r4, pc}}

morse_cue:
  push {{r4, r5, r6, lr}}
  ldr r0, ={CURSOR:#x}
  ldr r0, [r0]
  adds r0, #1
  cmp r0, #{MAX_N}
  bls 1f
  movs r0, #{MAX_N}
1:
  ldr r1, =numtab
  ldr r1, [r1, r0, lsl #2]
  ldr r4, ={PEND:#x}
  str r1, [r4]
  ldr r2, ={TICK_MS:#x}
  ldrh r2, [r2]
  strh r2, [r4, #4]
  ldr r2, ={MAGIC:#x}
  str r2, [r4, #8]
  ldr r5, ={POST_GATE:#x}
  ldrb r6, [r5]
  movs r0, #1
  strb r0, [r5]
  movs r0, #{CODE:#x}
call_post: bl call_post
  strb r6, [r5]
  pop {{r4, r5, r6, pc}}
.ltorg
.balign 4
numtab:
{words}
{lists}
"""


def build_stub() -> tuple[bytes, dict[str, int]]:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "s.S").write_text(asm_source())
        subprocess.run(["arm-none-eabi-as", "-o", t / "s.o", t / "s.S"], check=True)
        subprocess.run(["arm-none-eabi-ld", f"-Ttext={STUB_ADDR:#x}", "-o", t / "s.elf", t / "s.o"], check=True)
        subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", t / "s.elf", t / "s.bin"], check=True)
        syms = subprocess.run(["arm-none-eabi-nm", t / "s.elf"], check=True, capture_output=True, text=True).stdout
        addr = {l.split()[2]: int(l.split()[0], 16) for l in syms.splitlines() if len(l.split()) == 3}
        stub = bytearray((t / "s.bin").read_bytes())
        for name, target in (("call_recv", DISP_RECV), ("call_build", BUILD_PLAYLIST),
                              ("call_post", POST_FEEDBACK),
                              ("call_bt_apply", BT_APPLY_ORIG), ("call_bt_save", BT_SAVE_CODEPLUG),
                              ("call_bt_lescan", BT_LESCAN_START)):
            o = addr[name] - STUB_ADDR
            stub[o:o + 4] = thumb_bl(addr[name], target)
        return bytes(stub), addr


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path, default=Path("patched_noflip_read/D890UV_V1.05_20260521"))
    p.add_argument("--output-dir", type=Path, default=Path("patched_morsemenu"))
    args = p.parse_args()
    in_spi, in_cdi, in_cdd = package_paths(args.input_base)
    spi = bytearray(in_spi.read_bytes()); cdi = bytearray(in_cdi.read_bytes()); cdd = bytearray(in_cdd.read_bytes())
    if int.from_bytes(spi[6:10], "little") != len(cdd) or int.from_bytes(cdi[260:264], "little") != len(cdd):
        raise ValueError("size fields")
    if BASE + len(cdd) != STUB_ADDR:
        raise ValueError(f"image end 0x{BASE + len(cdd):08x} != stub 0x{STUB_ADDR:08x}")
    hooks = [(DISP_SITE, DISP_RECV, "tramp_disp"), (PLAY_SITE, BUILD_PLAYLIST, "play_hook"),
             (BT_APPLY_SITE, BT_APPLY_ORIG, "bt_defer_set")]
    for site, orig, _ in hooks:
        have = bytes(cdd[site - BASE:site - BASE + 4])
        if have != thumb_bl(site, orig):
            raise ValueError(f"hook mismatch @0x{site:08x}: {have.hex()}")
    stub, sym = build_stub()

    # Self-check: every BL in the code part lands on the intended stock function.
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    code_len = sym["numtab"] - STUB_ADDR
    bls = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(stub[:code_len], STUB_ADDR) if i.mnemonic == "bl"]
    want = [DISP_RECV, sym["detect"], BT_APPLY_ORIG, BT_SAVE_CODEPLUG, BT_LESCAN_START, sym["morse_cue"], BUILD_PLAYLIST, POST_FEEDBACK]
    if bls != want:
        raise ValueError(f"stub BLs {[hex(b) for b in bls]} != {[hex(w) for w in want]}")
    for n in (1, 7, 42, MAX_N):
        ptr = struct.unpack_from("<I", stub, sym["numtab"] - STUB_ADDR + 4 * n)[0]
        got = struct.unpack_from(f"<{len(playlist(n))}H", stub, ptr - STUB_ADDR)
        if list(got) != playlist(n):
            raise ValueError(f"playlist {n} mismatch")

    for site, _, target in hooks:
        cdd[site - BASE:site - BASE + 4] = thumb_bl(site, sym[target])
    cdd.extend(stub)
    cdd.extend(b"\xff" * (align_up(len(cdd), 32) - len(cdd)))
    patch_size_fields(spi, cdi, len(cdd))
    out = args.output_dir; out.mkdir(parents=True, exist_ok=True)
    base = out / "D890UV_V1.05_20260521"
    Path(str(base) + ".spi").write_bytes(spi); Path(str(base) + ".CDI").write_bytes(cdi); Path(str(base) + ".CDD").write_bytes(cdd)
    for i in md.disasm(stub[:code_len], STUB_ADDR):
        print(f"  {i.address:08x} {i.mnemonic} {i.op_str}")
    for site, _, target in hooks:
        print(f"hook @0x{site:08x} -> {target} 0x{sym[target]:08x}")
    print(f"playlist 7 = {playlist(7)}")
    print(f"new_size={len(cdd)}/0x{len(cdd):x}  sha256={hashlib.sha256(cdd).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
