#!/usr/bin/env python3
"""Menu item number as recorded DECtalk speech, played by the STM32 itself. On noflip.

The STM32 has a working PWM DAC (found in Ghidra 2026-10-25):
  * TIM4 CH4 (GPIOB pin 9, AF2) is PWM, ARR 0xff, PSC 0 at 120 MHz = 8-bit duty.
    FUN_08125854(v) sets the CH4 pulse to v (it is HAL_TIM_PWM_ConfigChannel).
  * TIM5's update ISR (IRQ 48, 0x08120e70) feeds it one sample per interrupt: in mode 4
    (DAT_2001afa8 == 4) from a RAM buffer, otherwise one step of the 16-point sine table
    at 0x081505c0 (BL FUN_0812587c @0x08120eb2). That sine path is how every beep sounds.
  * The tone producer FUN_08133210 sets the TIM5 rate per playlist item with
    FUN_0811a440(750000/freq - 1, 9) (BL @0x08133444), so the clock is 120 MHz and
    FUN_0811a440(14999, 0) gives exactly 8000 interrupts a second.

Patch (everything from make_d890_morsemenu_firmware.py, plus):
  * tone_hook on the producer's BL @0x08133444: a playlist freq of 0xFF00 + id starts clip
    id at 8 kHz instead of a tone; any other freq clears the clip state and calls through.
  * isr_hook on the ISR's BL @0x08120eb2: while a clip is active, output its next sample;
    when it runs out, output 0 and stay silent until the item ends; otherwise the sine step.
  * The menu cue builds playlists of number words ("forty", "two"). In digital mode
    (DAT_2001afc2 != 0) the producer drives the codec instead of TIM5, so the cue falls
    back to the Morse playlist there.
"""
from __future__ import annotations
import argparse, hashlib, math, struct, subprocess, tempfile, wave
from pathlib import Path
import capstone
from make_d890_morse_firmware import BASE, align_up, package_paths, patch_size_fields, thumb_bl
import make_d890_morsemenu_firmware as mm

TONE_SITE = 0x08133444                     # BL FUN_0811a440 in FUN_08133210 (analog branch)
TIM5_CONFIG = 0x0811A440                   # FUN_0811a440(period, prescaler) on TIM5
PIN_AF = 0x0811A730                        # GPIOB pin 9 -> AF (TIM4_CH4 out)
ISR_SITE = 0x08120EB2                      # BL FUN_0812587c in the TIM5 ISR
SINE_STEP = 0x0812587C
SET_PWM = 0x08125854                       # FUN_08125854(duty 0..255)
FREQ_VAR = 0x2000309A                      # u16 freq of the current playlist item
DIGITAL = 0x2001AFC2                       # nonzero: tones go to the codec, not TIM5
CLIP = 0x2002A820                          # {u32 ptr, u32 end, u32 state}; free SRAM gap
CLIP_FREQ = 0xFF00                         # playlist freq 0xFF00 + id = clip id
RATE = 8000
TIM_CLOCK = 120_000_000
TICK = 80                                  # samples per 10 ms playlist tick
WORD_GAP = 3                               # 30 ms between words
CENTER, SWING = 128, 110                   # PWM duty: idle mid level and peak swing
FADE = 80                                  # 10 ms ramp 0 -> CENTER and back (no clicks)
MAX_N = mm.MAX_N
FLASH_LIMIT = 0x081E0000                   # stay clear of the last 128 KB of flash

WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety", "hundred"]
ID = {w: i for i, w in enumerate(WORDS)}


def words(n: int) -> list[str]:
    if n >= 100:
        return ["one", "hundred"] + (words(n - 100) if n > 100 else [])
    if n < 20:
        return [WORDS[n - 1]]
    tens = WORDS[19 + n // 10 - 2]
    return [tens] + ([WORDS[n % 10 - 1]] if n % 10 else [])


def render(word: str, say: Path, cache: Path) -> bytes:
    """DECtalk -> trimmed 8 kHz -> unsigned PWM duty bytes with fades."""
    raw, wav8 = cache / f"{word}.wav", cache / f"{word}.8k.wav"
    with raw.open("wb") as f:
        subprocess.run([str(say), "-p"], input=f"[:rate 220] {word}.".encode(), stdout=f,
                       stderr=subprocess.DEVNULL, check=True)
    subprocess.run(["sox", "-D", raw, "-r", str(RATE), "-c", "1", "-b", "16", "-e", "signed", wav8,
                    "silence", "1", "0.005", "0.5%", "reverse", "silence", "1", "0.005", "0.5%",
                    "reverse", "compand", "0.005,0.1", "-60,-60,-30,-12,0,-3", "norm", "-0.5"],
                   check=True, capture_output=True)
    with wave.open(str(wav8)) as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (RATE, 2, 1):
            raise ValueError(f"{wav8}: unexpected format")
        pcm = struct.unpack(f"<{w.getnframes()}h", w.readframes(w.getnframes()))
    if len(pcm) < 400:
        raise ValueError(f"{word}: clip only {len(pcm)} samples, DECtalk render failed")
    body = [CENTER + round(s * SWING / 32768) for s in pcm]
    ramp = [round(CENTER * i / FADE) for i in range(FADE)]
    out = bytes(ramp + body + ramp[::-1])
    if min(out) < 0 or max(out) > 255:
        raise ValueError("duty out of range")
    return out


def voice_playlist(n: int, clips: dict[str, bytes]) -> list[int]:
    out = [0, mm.LEAD]
    for w in words(n):
        dur = math.ceil(len(clips[w]) / TICK) + 1
        if dur > 255:
            raise ValueError(f"{w}: {dur} ticks exceeds the u8 duration")
        out += [CLIP_FREQ + ID[w], dur, 0, WORD_GAP]
    return out + [0xFFFF, 0]


def asm_source(clips: dict[str, bytes]) -> str:
    morse = "\n".join(f"mpl_{n}: .hword {', '.join(map(str, mm.playlist(n)))}" for n in range(1, MAX_N + 1))
    voice = "\n".join(f"vpl_{n}: .hword {', '.join(map(str, voice_playlist(n, clips)))}" for n in range(1, MAX_N + 1))
    mtab = "\n".join(f"  .word mpl_{max(n, 1)}" for n in range(0, MAX_N + 1))
    vtab = "\n".join(f"  .word vpl_{max(n, 1)}" for n in range(0, MAX_N + 1))
    ctab = "\n".join(f"  .word clip_{w}, clip_{w}_end" for w in WORDS)
    cdata = "\n".join(f"clip_{w}: .byte {', '.join(map(str, clips[w]))}\nclip_{w}_end:" for w in WORDS)
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
  ldr r0, ={mm.BT_DEFER:#x}
  movs r1, #1
  strb r1, [r0]
  pop {{r0, r1}}
  bx lr

detect:
  push {{r4, lr}}
  @ --- deferred BT mode apply ---
  ldr r0, ={mm.BT_DEFER:#x}
  ldrb r1, [r0]
  cbz r1, 1f
  movs r1, #0
  strb r1, [r0]
call_bt_apply: bl call_bt_apply
call_bt_save: bl call_bt_save
  @ if BLE mode, kick off LESCAN=2 so Jieli starts advertising/scanning
  ldr r0, ={mm.BT_MODE_VAR:#x}
  ldrb r0, [r0]
  cmp r0, #1
  bne 1f
call_bt_lescan: bl call_bt_lescan
1:
  @ --- end deferred BT ---
  ldr r0, ={mm.DEPTH:#x}
  ldrb r0, [r0]
  subs r1, r0, #1
  cmp r1, #{mm.MAX_DEPTH}
  bhs 3f
  ldr r1, ={mm.CURSOR:#x}
  ldrb r1, [r1]
  ldr r2, ={mm.PAGE:#x}
  ldrb r2, [r2]
  orr r1, r1, r0, lsl #8
  orr r1, r1, r2, lsl #16
  ldr r3, ={mm.LAST:#x}
  ldr r2, [r3]
  cmp r1, r2
  beq 4f
  str r1, [r3]
  bl menu_cue
  b 4f
3:
  ldr r3, ={mm.LAST:#x}
  movs r1, #0
  subs r1, #1
  str r1, [r3]
4:
  pop {{r4, pc}}

play_hook:
  push {{r4, lr}}
call_build: bl call_build
  ldr r0, ={mm.PEND:#x}
  ldr r2, [r0, #8]
  ldr r3, ={mm.MAGIC:#x}
  cmp r2, r3
  bne 2f
  ldr r1, [r0]
  cbz r1, 2f
  ldrh r2, [r0, #4]
  ldr r3, ={mm.TICK_MS:#x}
  ldrh r3, [r3]
  subs r3, r3, r2
  uxth r3, r3
  cmp r3, #{mm.WINDOW_MS}
  bhi 1f
  ldr r2, ={mm.PLAYLIST_PTR:#x}
  str r1, [r2]
  b 2f
1:
  movs r1, #0
  str r1, [r0]
2:
  pop {{r4, pc}}

menu_cue:
  push {{r4, r5, r6, lr}}
  ldr r0, ={mm.CURSOR:#x}
  ldr r0, [r0]
  adds r0, #1
  cmp r0, #{MAX_N}
  bls 1f
  movs r0, #{MAX_N}
1:
  ldr r1, =vtab
  ldr r2, ={DIGITAL:#x}
  ldrb r2, [r2]
  cbz r2, 2f
  ldr r1, =mtab
2:
  ldr r1, [r1, r0, lsl #2]
  ldr r4, ={mm.PEND:#x}
  str r1, [r4]
  ldr r2, ={mm.TICK_MS:#x}
  ldrh r2, [r2]
  strh r2, [r4, #4]
  ldr r2, ={mm.MAGIC:#x}
  str r2, [r4, #8]
  ldr r5, ={mm.POST_GATE:#x}
  ldrb r6, [r5]
  movs r0, #1
  strb r0, [r5]
  movs r0, #{mm.CODE:#x}
call_post: bl call_post
  strb r6, [r5]
  pop {{r4, r5, r6, pc}}

@ Producer: r0 = TIM5 period, r1 = prescaler for the current item. Keep r0/r1 on pass-through.
tone_hook:
  push {{r4, r5, r6, lr}}
  ldr r2, ={FREQ_VAR:#x}
  ldrh r2, [r2]
  ldr r3, ={CLIP:#x}
  movw r4, #{CLIP_FREQ:#x}
  subs r2, r2, r4
  cmp r2, #{len(WORDS)}
  bhs 1f
  ldr r4, =ctab
  add r4, r4, r2, lsl #3
  ldr r5, [r4]
  str r5, [r3]
  ldr r5, [r4, #4]
  str r5, [r3, #4]
  movs r5, #1
  str r5, [r3, #8]
  movw r0, #{TIM_CLOCK // RATE - 1}
  movs r1, #0
call_tim5_clip: bl call_tim5_clip
call_pin_af: bl call_pin_af
  pop {{r4, r5, r6, pc}}
1:
  movs r4, #0
  str r4, [r3, #8]
call_tim5_tone: bl call_tim5_tone
  pop {{r4, r5, r6, pc}}

@ TIM5 ISR, one call per update. state 1 = playing, 2 = finished (silent), 0 = tone.
isr_hook:
  push {{r4, lr}}
  ldr r3, ={CLIP:#x}
  ldr r2, [r3, #8]
  cbz r2, 3f
  cmp r2, #1
  bne 2f
  ldr r0, [r3]
  ldr r1, [r3, #4]
  cmp r0, r1
  bhs 1f
  ldrb r2, [r0], #1
  str r0, [r3]
  mov r0, r2
call_pwm_sample: bl call_pwm_sample
  pop {{r4, pc}}
1:
  movs r2, #2
  str r2, [r3, #8]
  movs r0, #0
call_pwm_off: bl call_pwm_off
2:
  pop {{r4, pc}}
3:
call_sine: bl call_sine
  pop {{r4, pc}}
.ltorg
.balign 4
vtab:
{vtab}
mtab:
{mtab}
ctab:
{ctab}
{voice}
{morse}
{cdata}
"""


CALLS = {"call_recv": mm.DISP_RECV, "call_build": mm.BUILD_PLAYLIST, "call_post": mm.POST_FEEDBACK,
         "call_bt_apply": mm.BT_APPLY_ORIG, "call_bt_save": mm.BT_SAVE_CODEPLUG,
         "call_bt_lescan": mm.BT_LESCAN_START, "call_tim5_clip": TIM5_CONFIG, "call_pin_af": PIN_AF,
         "call_tim5_tone": TIM5_CONFIG, "call_pwm_sample": SET_PWM, "call_pwm_off": SET_PWM,
         "call_sine": SINE_STEP}


def build_stub(clips: dict[str, bytes]) -> tuple[bytes, dict[str, int]]:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "s.S").write_text(asm_source(clips))
        subprocess.run(["arm-none-eabi-as", "-o", t / "s.o", t / "s.S"], check=True)
        subprocess.run(["arm-none-eabi-ld", f"-Ttext={mm.STUB_ADDR:#x}", "-o", t / "s.elf", t / "s.o"], check=True)
        subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", t / "s.elf", t / "s.bin"], check=True)
        syms = subprocess.run(["arm-none-eabi-nm", t / "s.elf"], check=True, capture_output=True, text=True).stdout
        addr = {l.split()[2]: int(l.split()[0], 16) for l in syms.splitlines() if len(l.split()) == 3}
        stub = bytearray((t / "s.bin").read_bytes())
        for name, target in CALLS.items():
            o = addr[name] - mm.STUB_ADDR
            stub[o:o + 4] = thumb_bl(addr[name], target)
        return bytes(stub), addr


def preview(stub: bytes, sym: dict[str, int], n: int, path: Path) -> None:
    """What the radio's PWM pin would carry for menu item n, read back from the stub bytes."""
    ptr = struct.unpack_from("<I", stub, sym["vtab"] - mm.STUB_ADDR + 4 * n)[0] - mm.STUB_ADDR
    duty: list[int] = []
    while True:
        freq, dur = struct.unpack_from("<HH", stub, ptr)
        ptr += 4
        if freq == 0xFFFF:
            break
        span = dur * TICK
        if freq >= CLIP_FREQ:
            start, end = struct.unpack_from("<II", stub, sym["ctab"] - mm.STUB_ADDR + 8 * (freq - CLIP_FREQ))
            data = list(stub[start - mm.STUB_ADDR:end - mm.STUB_ADDR])
            duty += (data + [0] * span)[:span]
        else:
            duty += [0] * span
    mean = sum(duty) / len(duty)           # the speaker path is AC-coupled; drop the DC
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(duty)}h", *[round((d - mean) * 200) for d in duty]))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path, default=Path("patched_noflip_read/D890UV_V1.05_20260521"))
    p.add_argument("--output-dir", type=Path, default=Path("patched_voicemenu"))
    p.add_argument("--say", type=Path, default=Path("DECtalkMini/build/say"))
    args = p.parse_args()
    out = args.output_dir; out.mkdir(parents=True, exist_ok=True)
    cache = out / "clips"; cache.mkdir(exist_ok=True)
    clips = {w: render(w, args.say, cache) for w in WORDS}

    in_spi, in_cdi, in_cdd = package_paths(args.input_base)
    spi = bytearray(in_spi.read_bytes()); cdi = bytearray(in_cdi.read_bytes()); cdd = bytearray(in_cdd.read_bytes())
    if int.from_bytes(spi[6:10], "little") != len(cdd) or int.from_bytes(cdi[260:264], "little") != len(cdd):
        raise ValueError("size fields")
    if BASE + len(cdd) != mm.STUB_ADDR:
        raise ValueError(f"image end 0x{BASE + len(cdd):08x} != stub 0x{mm.STUB_ADDR:08x}")
    hooks = [(mm.DISP_SITE, mm.DISP_RECV, "tramp_disp"), (mm.PLAY_SITE, mm.BUILD_PLAYLIST, "play_hook"),
             (mm.BT_APPLY_SITE, mm.BT_APPLY_ORIG, "bt_defer_set"), (TONE_SITE, TIM5_CONFIG, "tone_hook"),
             (ISR_SITE, SINE_STEP, "isr_hook")]
    for site, orig, _ in hooks:
        have = bytes(cdd[site - BASE:site - BASE + 4])
        if have != thumb_bl(site, orig):
            raise ValueError(f"hook mismatch @0x{site:08x}: {have.hex()}")
    stub, sym = build_stub(clips)
    if mm.STUB_ADDR + len(stub) > FLASH_LIMIT:
        raise ValueError(f"stub ends 0x{mm.STUB_ADDR + len(stub):08x}, past 0x{FLASH_LIMIT:08x}")

    # Self-check: every BL in the code part lands on the intended function, in order.
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    code_len = sym["vtab"] - mm.STUB_ADDR
    code = list(md.disasm(stub[:code_len], mm.STUB_ADDR))
    if sum(i.size for i in code) != code_len:
        raise ValueError("stub code did not disassemble cleanly")
    bls = [int(i.op_str.lstrip("#"), 16) for i in code if i.mnemonic == "bl"]
    want = [mm.DISP_RECV, sym["detect"], mm.BT_APPLY_ORIG, mm.BT_SAVE_CODEPLUG, mm.BT_LESCAN_START,
            sym["menu_cue"], mm.BUILD_PLAYLIST, mm.POST_FEEDBACK, TIM5_CONFIG, PIN_AF, TIM5_CONFIG,
            SET_PWM, SET_PWM, SINE_STEP]
    if bls != want:
        raise ValueError(f"stub BLs {[hex(b) for b in bls]} != {[hex(w) for w in want]}")
    for n in (1, 7, 42, 100, MAX_N):
        for tab, fn in (("vtab", lambda k: voice_playlist(k, clips)), ("mtab", mm.playlist)):
            ptr = struct.unpack_from("<I", stub, sym[tab] - mm.STUB_ADDR + 4 * n)[0]
            got = struct.unpack_from(f"<{len(fn(n))}H", stub, ptr - mm.STUB_ADDR)
            if list(got) != fn(n):
                raise ValueError(f"{tab} playlist {n} mismatch")
    for i, w in enumerate(WORDS):
        start, end = struct.unpack_from("<II", stub, sym["ctab"] - mm.STUB_ADDR + 8 * i)
        if stub[start - mm.STUB_ADDR:end - mm.STUB_ADDR] != clips[w]:
            raise ValueError(f"clip {w} mismatch")

    for site, _, target in hooks:
        cdd[site - BASE:site - BASE + 4] = thumb_bl(site, sym[target])
    cdd.extend(stub)
    cdd.extend(b"\xff" * (align_up(len(cdd), 32) - len(cdd)))
    patch_size_fields(spi, cdi, len(cdd))
    base = out / "D890UV_V1.05_20260521"
    Path(str(base) + ".spi").write_bytes(spi); Path(str(base) + ".CDI").write_bytes(cdi); Path(str(base) + ".CDD").write_bytes(cdd)
    for n in (3, 42):
        preview(stub, sym, n, out / f"preview_{n}.wav")
    for i in md.disasm(stub[sym["tone_hook"] - mm.STUB_ADDR:code_len], sym["tone_hook"]):
        print(f"  {i.address:08x} {i.mnemonic} {i.op_str}")
    for site, _, target in hooks:
        print(f"hook @0x{site:08x} -> {target} 0x{sym[target]:08x}")
    print(f"clips {sum(map(len, clips.values()))} B, playlist 42 = {voice_playlist(42, clips)}")
    print(f"new_size={len(cdd)}/0x{len(cdd):x} end=0x{BASE + len(cdd):08x}  sha256={hashlib.sha256(cdd).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
