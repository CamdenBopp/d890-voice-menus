# D890UV V1.05 — verified audio & state map

Re-derived 2026-10-24 by four parallel Ghidra investigations, each required to cite primary
evidence and rate confidence. **This supersedes older notes, which were
wrong in several places — corrections are called out below.** Load base `0x0800c000`.

## Corrections to the old findings
- **Keypad/menu beep is NOT `boot_chirp`/I2S.** It is a square wave from **STM32 TIM5 + TIM4**
  via a tone/voice-prompt playlist engine (in digital mode: bit-banged to the baseband codec).
  `boot_chirp_080d6f5c` → I2S osc `FUN_0806207c` has exactly 1 caller and is ONLY the boot chirp.
- **`DAT_20015277` is the frequency BAND index (0..0x12), NOT the current channel.** That's why a
  live read returned 0 while the screen showed CH-3. `FUN_0801eacc` detects *band* change (which
  often coincides with channel change — why the old channel-beep patch seemed to work).
- **There IS a volume control:** `SPKGAIN=` (5 steps). The old "no volume command" was wrong.
- **`board_command_queue` is the Jieli path; `FUN_080d71e8` is a RAM queue, not the wire TX.**

## Audio output architecture (who makes the sound)
The STM32 generates no received-audio PCM. Confirmed: no DAC/SAI/I2S3 referenced; I2S2 (SPI2
`0x40003800`) exists but is used ONLY for the boot chirp.
- **RX audio (FM/DMR/NOAA):** the **baseband SCT3288QD** demodulates and produces the PCM; the
  **Jieli AC695x** board routes/DACs/amps it to the speaker. The STM32 only commands them + flips a
  local amp GPIO.
- **Beeps/sidetone:** STM32 **TIM5/TIM4** square wave (analog mode) or bit-banged to the codec
  (digital mode). Voice-prompt clips: the playlist engine below.

## Beep / tone / voice-feedback engine (the accessibility lever)
Chain (all Confirmed):
1. Keypad scan `FUN_080ca6c4` → key code `DAT_20007cc0` → queue (`FUN_080d7870`).
2. Key-event task `FUN_081223e0` → action switch `FUN_08016978`; after each key, `FUN_080c5ffc`.
3. Feedback code latched in `DAT_20003096` (∈{1,2,5}); `FUN_080c5ffc`→`FUN_080d77e8(code)` posts to
   the audio-feedback queue.
4. Feedback task `FUN_081224f8` → player `FUN_0801a420` → `FUN_0801a6d0` (select) + `FUN_080d1680`
   (unmute/route speaker).
5. `FUN_0801a6d0` → `FUN_0811cd44(code)` builds a `(freq,duration)` playlist in `DAT_200159e0` →
   `FUN_08133210` plays each item.
6. **Tone producer `FUN_08133210`** (branches on digital-context flag `DAT_2001afc2`):
   - analog: `FUN_0811a440(750000/freq-1, 9)` (TIM5 PSC=9, ARR), `FUN_0811a4c0(1)` (TIM5 enable),
     `FUN_080916b8(0x40000800)` (TIM4 gate). Stop: `FUN_0811a4c0(0)` + `FUN_08091578(0x40000800)`.
   - digital: `FUN_081162b8(freq)` bit-bangs the codec via GPIO.

**Clean menu-beep primitive (no gate, works in menus, unlike boot_chirp):**
`FUN_0811a440(750000/freq-1, 9)` + `FUN_0811a4c0(1)` + `FUN_080916b8(0x40000800)`.
The playlist engine (`FUN_0801a420` → `FUN_0811cd44`/`FUN_08133210`) is the natural hook for
spoken/tonal menu feedback.

## RX audio on/off (speaker gating)
- `FUN_080c317c` = RX audio ON (squelch open): `AUDIO=1` + `SPKEN=1`, asserts amp GPIO.
- `FUN_08025104` = RX audio OFF: `AUDIO=0`, `SPKEN=0`, mute.
- Both reached from master RX/mode state machine `FUN_0811b308` (on `DAT_20013060`).
- Jieli senders: SPKEN `FUN_0810f570` (param 0 = ON, inverted), AUDIO `FUN_0810ee8c`, SPKGAIN(volume)
  `FUN_0810f49c` (LUT `0x0814a4a8`), MICEN `FUN_0810f000` — all via `board_command_queue_080d75d0`.
- Speaker-amp enable GPIO: **GPIOA pin15** (`0x42020000` mask `0x8000`) [Likely]; GPIOE1 secondary.

## Real state variables (for narrating by ear)
- Current channel (zone-relative): **`DAT_20014903`** (VFO A) / **`DAT_20014904`** (VFO B), 0-based. [Confirmed]
- Absolute channel (codeplug index): `DAT_20004212` / `DAT_20004213`. [Confirmed]
- Active/focused VFO: **`DAT_20014910`** (0=A). [Confirmed]  Primary/TX VFO: `DAT_20015979` [Likely].
- Per-VFO state structs (0x80): `DAT_20013a28` (A) / `DAT_20013aa8` (B). [Confirmed]
- Current zone name (UTF-16): **`DAT_200143f0`**; zone presence bitmap `DAT_2001deb0`. [Confirmed]
- Band index: `DAT_20015277` (shadow `DAT_200152d4`). [Confirmed]

## Retune path (channel change)
`FUN_080d71e8` id=1 (via `FUN_0810cbc0`, 5-byte freq at `DAT_20004c1e`) is the tune command; pump
`FUN_0812cbe4` walks a pending-command bitmap (`0x20005030` quick / `0x20005034` full); requested by
`FUN_0810ce54(1)` from channel handlers `FUN_08036160`/`FUN_08036498`. [Confirmed except the exact
5-byte packer = Unknown]

## Open questions (need a light live-diff; heavy reads destabilize the radio)
- Zone **index** scalar (name buffer known, scalar not). 
- True VFO/MR work-mode flag (candidate: VFO-struct+8 &3 = `DAT_20013a30`/`ab0`). 
- Key-Tone on/off codeplug byte.
- The baseband on-wire framing (queue id → "QX CMD 00X" string) — TX reads rings by computed addr.

## Accessibility-relevant hook points
- Audible menu cue: TIM5/TIM4 primitive above, or hook `FUN_080d77e8` / the playlist player.
- "Audio is playing" state: hook `FUN_080c317c` / `FUN_08025104`.
- State to narrate: `DAT_20014903` (channel), `DAT_20014910` (VFO), `DAT_200143f0` (zone name).
- Readout/validation: the invisible light USB read (`make_d890_noflip_read_firmware.py`).

## Menu Morse — WORKING (2026-10-24, confirmed by ear)
Builder `make_d890_morsemenu_firmware.py`; hook addresses in how_it_works.md.
Corrections from this work:
- Playlist durations are 10 ms ticks (TIM7 = PSC 119 / ARR 999 = 1 ms; tone tick every 10th).
- Sound table at RAM `0x20001150` is initialised from armlink-compressed .data
  (load `0x081a4318`, decompressor `0x08010682`); decoded codes 0x01–0x25.
- `FUN_080d7824` = redraw-request bitmask, not a beep.
- Settings menu does NOT use scroll workers `FUN_0809878c`/`FUN_0809a3dc`.
- Menu depth `DAT_200097e9` (1 main / 2 sub), cursor `_DAT_20004244` confirmed live.
