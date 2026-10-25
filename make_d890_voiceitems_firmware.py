#!/usr/bin/env python3
"""Menu items spoken by name (DECtalk words, LPC-coded), played by the STM32. On noflip.

Builds on make_d890_voicemenu_firmware.py (number speech, verified by ear 2026-10-25):
same display-task cue, playlist swap, tone-producer and TIM5-ISR hooks, same BT defer.
What changes:
  * The highlighted item is read live: list = _DAT_20004b40, count = _DAT_20008790, item
    string = list[cursor] (confirmed with item_probe.py on the main menu and submenus).
    A sorted table maps the string address to its words; unknown strings and dynamic
    lists (count 0) fall back to speaking the item number.
  * Words are 20 ms LPC frames (voice_items/voicegen.py), about 0.7 KB/s, decoded in the
    TIM5 ISR by voice_items/voice_lpc.c at 8 kHz. Raw 8-bit clips for the ~440-word
    vocabulary would need ~1.8 MB; internal flash has ~240 KB free.
  * Playlist freq 0xF000 + id = word id. Digital mode keeps the Morse fallback.
"""
from __future__ import annotations
import argparse, hashlib, struct, subprocess, sys, tempfile, wave
from pathlib import Path
import capstone
sys.path.insert(0, str(Path(__file__).resolve().parent / "voice_items"))
import voicegen as vg
from make_d890_morse_firmware import BASE, align_up, package_paths, patch_size_fields, thumb_bl
import make_d890_morsemenu_firmware as mm
import make_d890_voicemenu_firmware as vm

SRC = Path(__file__).resolve().parent / "voice_items"
STOCK_END = 0x081A5008                     # end of the stock image (menu lists live below this)
BLOB = mm.STUB_ADDR                        # after the RAM-peek stub already in the noflip base
VLRAM = 0x2002A900                         # decoder state + playlists (free SRAM gap)
VLRAM_LIMIT = 0x2002B2C0
CLIP_FREQ = 0xF000
LEAD, WORD_GAP = 15, 5
MAX_WORDS = 16


def asm_source() -> str:
    morse = "\n".join(f"mpl_{n}: .hword {', '.join(map(str, mm.playlist(n)))}" for n in range(1, mm.MAX_N + 1))
    mtab = "\n".join(f"  .word mpl_{max(n, 1)}" for n in range(0, mm.MAX_N + 1))
    return f"""
.syntax unified
.thumb
.cpu cortex-m4
.section .text.glue,"ax"
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
  ldr r0, ={mm.BT_DEFER:#x}
  ldrb r1, [r0]
  cbz r1, 1f
  movs r1, #0
  strb r1, [r0]
call_bt_apply: bl call_bt_apply
call_bt_save: bl call_bt_save
  ldr r0, ={mm.BT_MODE_VAR:#x}
  ldrb r0, [r0]
  cmp r0, #1
  bne 1f
call_bt_lescan: bl call_bt_lescan
1:
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
  ldr r2, ={vm.DIGITAL:#x}
  ldrb r2, [r2]
  cbz r2, 1f
  adds r0, #1
  cmp r0, #{mm.MAX_N}
  bls 2f
  movs r0, #{mm.MAX_N}
2:
  ldr r1, =mtab
  ldr r1, [r1, r0, lsl #2]
  b 3f
1:
  bl vl_pick
  mov r1, r0
3:
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

@ Producer: r0 = TIM5 period, r1 = prescaler. A word item reprograms TIM5 to 8 kHz.
tone_hook:
  push {{r0, r1, r4, lr}}
  bl vl_tone
  mov r4, r0
  pop {{r0, r1}}
  cbz r4, 1f
  movw r0, #{vm.TIM_CLOCK // vm.RATE - 1}
  movs r1, #0
call_tim5_clip: bl call_tim5_clip
call_pin_af: bl call_pin_af
  pop {{r4, pc}}
1:
call_tim5_tone: bl call_tim5_tone
  pop {{r4, pc}}

@ TIM5 ISR sample step: the word decoder, or the stock sine step.
isr_hook:
  push {{r4, lr}}
  bl vl_isr
  cbnz r0, 1f
call_sine: bl call_sine
1:
  pop {{r4, pc}}
.ltorg

.section .rodata.morse,"a"
.balign 4
mtab:
{mtab}
{morse}
"""


def linker_script() -> str:
    return f"""
ENTRY(_start)
SECTIONS
{{
  .text 0x{BLOB:08x} : {{ *(.text.glue) *(.text*) *(.rodata*) }}
  .vlram 0x{VLRAM:08x} (NOLOAD) : {{ *(.vlram) }}
  .data : {{ *(.data*) }}
  .bss : {{ *(.bss*) *(COMMON) }}
  /DISCARD/ : {{ *(.ARM.exidx*) *(.comment) *(.ARM.attributes) }}
}}
"""


CALLS = {"call_recv": mm.DISP_RECV, "call_build": mm.BUILD_PLAYLIST, "call_post": mm.POST_FEEDBACK,
         "call_bt_apply": mm.BT_APPLY_ORIG, "call_bt_save": mm.BT_SAVE_CODEPLUG,
         "call_bt_lescan": mm.BT_LESCAN_START, "call_tim5_clip": vm.TIM5_CONFIG, "call_pin_af": vm.PIN_AF,
         "call_tim5_tone": vm.TIM5_CONFIG, "call_sine": vm.SINE_STEP}


def make_data(img: bytes, say: Path, cache: Path):
    """Phrases + vocabulary + LPC frames. Returns (data_c, data_h, phrases, vocab, wordmeta)."""
    strings = vg.item_strings(img, STOCK_END)
    phrases = {}
    for addr, text in strings.items():
        w = vg.words(text)
        if w:
            phrases[addr] = w[:MAX_WORDS]
    vocab = sorted({w for ws in phrases.values() for w in ws} | set(vg.ONES) |
                   {t for t in vg.TENS if t != "_"} | {"hundred", "thousand", "point"})
    wid = {w: i for i, w in enumerate(vocab)}
    frames, meta = bytearray(), []
    for w in vocab:
        enc = cache / (w.replace(" ", "_") + ".lpc")
        if not enc.exists():
            enc.write_bytes(vg.encode(vg.render(w, say, cache)))
        f = enc.read_bytes()
        meta.append((len(frames) // vg.FRAME_BYTES, len(f) // vg.FRAME_BYTES))
        frames += f
    longest = max(n for _, n in meta)
    if 2 * longest + 3 > 255:
        raise ValueError(f"a word has {longest} frames: its playlist duration overflows a u8")
    addrs = sorted(phrases)
    stream, offs = [], []
    for a in addrs:
        offs.append(len(stream))
        stream += [len(phrases[a])] + [wid[w] for w in phrases[a]]
    sqrt_q8 = [round((i ** 0.5) * 256) for i in range(256)]
    h = f"""/* generated by make_d890_voiceitems_firmware.py */
#include <stdint.h>
#define FRAME {vg.FRAME}
#define ORDER {vg.ORDER}
#define FRAME_BYTES {vg.FRAME_BYTES}
#define NWORDS {len(vocab)}
#define NSTR {len(addrs)}
#define IMAGE_START 0x{BASE:08x}u
#define IMAGE_END 0x{STOCK_END:08x}u
#define W_HUNDRED {wid['hundred']}
#define W_THOUSAND {wid['thousand']}
extern const uint8_t vd_frames[];
extern const uint32_t vd_word[][2];
extern const int16_t vd_ktab[256];
extern const int32_t vd_gtab[256];
extern const uint16_t vd_sqrt_q8[256];
extern const uint32_t vd_str_addr[];
extern const uint16_t vd_str_off[];
extern const uint16_t vd_phrase[];
extern const uint16_t vd_num_word[20];
extern const uint16_t vd_tens_word[10];
"""
    c = '#include "voice_data.h"\n'
    c += vg.c_array("uint8_t", "vd_frames", frames)
    c += f"const uint32_t vd_word[{len(meta)}][2] = {{" + ", ".join(f"{{{a}, {n}}}" for a, n in meta) + "};\n"
    c += vg.c_array("int16_t", "vd_ktab", vg.k_table())
    c += vg.c_array("int32_t", "vd_gtab", vg.g_table())
    c += vg.c_array("uint16_t", "vd_sqrt_q8", sqrt_q8)
    c += vg.c_array("uint32_t", "vd_str_addr", [f"0x{a:08x}u" for a in addrs])
    c += vg.c_array("uint16_t", "vd_str_off", offs)
    c += vg.c_array("uint16_t", "vd_phrase", stream)
    c += vg.c_array("uint16_t", "vd_num_word", [wid[w] for w in vg.ONES])
    c += vg.c_array("uint16_t", "vd_tens_word", [0, 0] + [wid[t] for t in vg.TENS[2:]])
    return c, h, phrases, strings, vocab, wid, meta


def run(cmd, **kw):
    return subprocess.run([str(x) for x in cmd], check=True, **kw)


def build_target(t: Path) -> tuple[bytes, dict[str, int]]:
    cflags = ["-mcpu=cortex-m4", "-mthumb", "-O2", "-ffreestanding", "-fno-builtin", "-nostdlib",
              "-fno-common", "-ffunction-sections", "-Wall", "-Werror", f"-I{t}"]
    run(["arm-none-eabi-gcc", *cflags, "-c", SRC / "voice_lpc.c", "-o", t / "voice_lpc.o"])
    run(["arm-none-eabi-gcc", *cflags, "-c", t / "voice_data.c", "-o", t / "voice_data.o"])
    run(["arm-none-eabi-as", "-o", t / "glue.o", t / "glue.S"])
    (t / "link.ld").write_text(linker_script())
    run(["arm-none-eabi-ld", "-T", t / "link.ld", "-o", t / "s.elf", t / "glue.o", t / "voice_lpc.o", t / "voice_data.o"])
    hdr = run(["arm-none-eabi-objdump", "-h", t / "s.elf"], capture_output=True, text=True).stdout
    for sec in (".data", ".bss"):
        for line in hdr.splitlines():
            parts = line.split()
            if len(parts) > 2 and parts[1] == sec and int(parts[2], 16):
                raise ValueError(f"{sec} is not empty: nothing would initialise it")
    run(["arm-none-eabi-objcopy", "-O", "binary", "-j", ".text", t / "s.elf", t / "s.bin"])
    syms = run(["arm-none-eabi-nm", t / "s.elf"], capture_output=True, text=True).stdout
    addr = {l.split()[2]: int(l.split()[0], 16) for l in syms.splitlines() if len(l.split()) == 3}
    undef = run(["arm-none-eabi-nm", "-u", t / "s.elf"], capture_output=True, text=True).stdout.strip()
    if undef:
        raise ValueError(f"undefined symbols: {undef}")
    stub = bytearray((t / "s.bin").read_bytes())
    for name, target in CALLS.items():
        o = addr[name] - BLOB
        stub[o:o + 4] = thumb_bl(addr[name], target)
    return bytes(stub), addr


def build_host(t: Path) -> Path:
    exe = t / "host_preview"
    run(["cc", "-O2", "-DHOST", f"-I{t}", "-Wall", "-Werror", SRC / "voice_lpc.c", t / "voice_data.c",
         SRC / "host_preview.c", "-o", exe])
    return exe


def preview(exe: Path, ids: list[int], path: Path) -> None:
    duty = run([exe, *ids], capture_output=True).stdout
    mean = sum(duty) / len(duty)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(vg.RATE)
        w.writeframes(struct.pack(f"<{len(duty)}h", *[round((d - mean) * 200) for d in duty]))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input-base", type=Path, default=Path("patched_noflip_read/D890UV_V1.05_20260521"))
    p.add_argument("--output-dir", type=Path, default=Path("patched_voiceitems"))
    p.add_argument("--say", type=Path, default=Path("DECtalkMini/build/say"))
    p.add_argument("--preview", nargs="*", default=["Talk Group", "Talker Alias", "Settings",
                                                    "Auto Rptr A", "162.475MHz", "Busy Lock"])
    args = p.parse_args()
    out = args.output_dir; out.mkdir(parents=True, exist_ok=True)
    cache = out / "words"; cache.mkdir(exist_ok=True)
    in_spi, in_cdi, in_cdd = package_paths(args.input_base)
    spi = bytearray(in_spi.read_bytes()); cdi = bytearray(in_cdi.read_bytes()); cdd = bytearray(in_cdd.read_bytes())
    if int.from_bytes(spi[6:10], "little") != len(cdd) or int.from_bytes(cdi[260:264], "little") != len(cdd):
        raise ValueError("size fields")
    if BASE + len(cdd) != BLOB:
        raise ValueError(f"image end 0x{BASE + len(cdd):08x} != 0x{BLOB:08x}")
    hooks = [(mm.DISP_SITE, mm.DISP_RECV, "tramp_disp"), (mm.PLAY_SITE, mm.BUILD_PLAYLIST, "play_hook"),
             (mm.BT_APPLY_SITE, mm.BT_APPLY_ORIG, "bt_defer_set"), (vm.TONE_SITE, vm.TIM5_CONFIG, "tone_hook"),
             (vm.ISR_SITE, vm.SINE_STEP, "isr_hook")]
    for site, orig, _ in hooks:
        have = bytes(cdd[site - BASE:site - BASE + 4])
        if have != thumb_bl(site, orig):
            raise ValueError(f"hook mismatch @0x{site:08x}: {have.hex()}")

    data_c, data_h, phrases, strings, vocab, wid, meta = make_data(bytes(cdd), args.say, cache)
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "voice_data.c").write_text(data_c); (t / "voice_data.h").write_text(data_h)
        (t / "glue.S").write_text(asm_source())
        stub, sym = build_target(t)
        exe = build_host(t)
        # host check: the radio's playlist builder picks the right words for real lists
        for text in args.preview:
            a = next(a for a, s in strings.items() if s == text)
            want = phrases[a]
            got = run([exe, "--pick", f"{a:#x}"], capture_output=True, text=True).stdout.split()
            if [vocab[int(i)] for i in got] != want:
                raise ValueError(f"on-radio lookup for {text!r} gave {got}")
            preview(exe, [wid[w] for w in want], out / f"preview_{text.replace(' ', '_').replace('.', '_')}.wav")
            print(f"{text!r} -> {' '.join(want)}")
        exe_copy = out / "host_preview"; exe_copy.write_bytes(exe.read_bytes()); exe_copy.chmod(0o755)

    end = BLOB + len(stub)
    if end > vm.FLASH_LIMIT:
        raise ValueError(f"blob ends 0x{end:08x}, past 0x{vm.FLASH_LIMIT:08x}")
    if sym.get("vl", VLRAM) + 0x200 > VLRAM_LIMIT:
        raise ValueError("vlram overflow")

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    code = list(md.disasm(stub[:sym["isr_hook"] - BLOB + 16], BLOB))
    bls = [int(i.op_str.lstrip("#"), 16) for i in code if i.mnemonic == "bl"]
    want = [mm.DISP_RECV, sym["detect"], mm.BT_APPLY_ORIG, mm.BT_SAVE_CODEPLUG, mm.BT_LESCAN_START,
            sym["menu_cue"], mm.BUILD_PLAYLIST, sym["vl_pick"], mm.POST_FEEDBACK, sym["vl_tone"],
            vm.TIM5_CONFIG, vm.PIN_AF, vm.TIM5_CONFIG, sym["vl_isr"], vm.SINE_STEP]
    if bls[:len(want)] != want:
        raise ValueError(f"glue BLs {[hex(b) for b in bls]} != {[hex(w) for w in want]}")

    for site, _, target in hooks:
        cdd[site - BASE:site - BASE + 4] = thumb_bl(site, sym[target])
    cdd.extend(stub)
    cdd.extend(b"\xff" * (align_up(len(cdd), 32) - len(cdd)))
    patch_size_fields(spi, cdi, len(cdd))
    base = out / "D890UV_V1.05_20260521"
    Path(str(base) + ".spi").write_bytes(spi); Path(str(base) + ".CDI").write_bytes(cdi); Path(str(base) + ".CDD").write_bytes(cdd)
    secs = sum(n for _, n in meta) * vg.FRAME / vg.RATE
    print(f"{len(phrases)} item strings, {len(vocab)} words, {secs:.0f} s of speech, "
          f"frames {sum(n for _, n in meta) * vg.FRAME_BYTES} B, blob {len(stub)} B")
    print(f"vl state @0x{sym['vl']:08x}; hooks: " + ", ".join(f"0x{s:08x}->{n}" for s, _, n in hooks))
    print(f"new_size={len(cdd)}/0x{len(cdd):x} end=0x{BASE + len(cdd):08x}  sha256={hashlib.sha256(cdd).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
