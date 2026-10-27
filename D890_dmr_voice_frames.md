# D890 DMR voice frames to and from the main board (hotspot groundwork)

Mapped 2026-10-26 from the V1.05 image in Ghidra, plus the CT3258 Packet Interface spec
(cdn.hackaday.io/files/1642237026116832/CT3258_Packet_Interface.pdf; the SCT3288 uses the same
protocol). Nothing here has been run on the radio yet.

## The mechanism the baseband offers
- Field `0x3E` VOCODER_IO_SET (1 byte) routes vocoder data. Bits: 6 MOD_IN (modulator input from
  the MCU via CHAN_D), 5:4 ENC_OUT (encoder output to the MCU), 3 DEC_IN (decoder input from the
  MCU), 2 DEMOD_OUT (demodulated frames to the MCU and to the decoder).
  Spec presets: local record 0x20, remote record 0x04, local playback 0x08, remote playback 0x40.
- Field `0x01` CHAN_D carries encoded voice: 36 bytes per 80 ms in dPMR.
- The 2015 spec says both are "dPMR only". The firmware disagrees (below).

## What the firmware already does
- Sender for 0x3E: `FUN_080fffe4(io)` → `FUN_080122ec(0x3e, &io)`. Sender for CHAN_D:
  `FUN_080f9b28(buf)` → `FUN_080122ec(1, buf)`; the state machine calls it via `FUN_080f9b48`.
- Voice-mode switch `FUN_080fad68(mode, ...)` sends `0x3E` = `table_0x08150fc8[mode]`:
  `00 08 40 04 20 10 60 0c 61 0e e0 80 44 01 41 80 40 04 42 42`, so mode 0x10 → 0x40 (MCU frames
  get transmitted) and mode 0x11 → 0x04 (received frames go to the MCU).
- Public entry point: `FUN_08115370(mode, arg)` posts baseband API command 0x14 via the queue
  `FUN_080d747c`; it's drained by `FUN_08065214` → `FUN_080f3a68` case 0x14 → `FUN_080fad68`.
  The current mode is `DAT_2000f9be`.
- RX path: UART parser `FUN_0802de84` (sync `84 A9 61`) queues packet pointers at `0x200046bc`.
  `FUN_08146930` dispatches fields. For case 0x01 CHAN_D with length 0x24 (dPMR, 36 B) or **0x1b
  (DMR, 27 B = three 9-byte AMBE+2 frames per 60 ms burst)**, it copies the frames to `DAT_2000d5e2`
  and sets `DAT_2000d56a |= 1`. A len byte of 0 sets bit 1 instead (baseband requests the next TX
  frame).
- Per-mode consumer `FUN_080fb67c(mode)`:
  - 0x11 (DMR record): `FUN_08108d40` appends the 27 bytes to a ring at `0x20021e34`, 216 bytes =
    8 bursts, write index `_DAT_20021f0c`.
  - 0x10 (DMR playback): on request, sets len 0x1b; `FUN_08108da0` pops 27 bytes from the same
    ring (read index `_DAT_20021f0e`), then `FUN_080f9b48` sends CHAN_D.
  - dPMR record/playback = modes 3–5 / 1–2 with 36-byte frames (`FUN_080fbc0c`, `FUN_080f2110`).
- Nothing in the UI ever requests modes 0x10/0x11 (all 26 `FUN_08115370` callers checked: they
  use 0, 1, 2, 0xd–0xf, 0x12–0x15, 0x17, 0x18). The DMR record/relay code looks like unused SDK
  code, so whether the SCT3288 honours it in DMR is untested.

## Hotspot shape this implies
RX: set mode 0x11 → each received burst lands in the ring → forward 27 B/60 ms (450 B/s) to the
phone over BLE (`FUN_080d71e8(0x10, …)` = AT+DATA notify) → phone wraps it as MMDVM/BrandMeister
DMRD. TX: phone sends frames over BLE → push them into the ring → mode 0x10 → baseband transmits.
Caller/talkgroup IDs and call start/stop come from other fields (call setup), not mapped yet.

## Receive test build (built 2026-10-26, not flashed)
`make_d890_dmrframes_firmware.py` on `patched_blecps` (3654fffd, what the radio runs) →
`patched_dmrframes/`, CDD sha256 `e89360cf4985516f`. It doesn't use mode 0x11. It sets DEMOD_OUT
(0x04) directly: added after every PROCESS_MODE (0x18) field and ORed into every 0x3E the firmware
sends. Each received CHAN_D is forwarded as `AT+DATA=00,057\r\nV<seq><54 hex>\r\n` via
`FUN_080d71e8(0x10, ...)`. The stub (236 B @ 0x081d0540) was checked in Unicorn: the fields it adds,
the OR, the exact AT line, and no send on an empty frame. Scratch RAM 0x2002A300.., unreferenced.
Test: flash under `.d890_radio.lock` → D890 on a DMR simplex channel → run
`.venv/bin/python dmr_frames_listen.py` from Terminal.app → key the GD77 on the same frequency, color
code and slot → pass = ~16 bursts/s, low drops. If the 0x18 hook never fires on DMR, the next
place to try is the voice-mode switch (`FUN_08115370(0x11)` at call start).

## RF IC: not identified (2026-10-26)
- No part name appears in the firmware, the CPS or the update packages. CML lists the SCT3288 as a
  baseband processor that needs external RF; their reference design pairs it with the CMX994.
- `FUN_080e2f64(reg7, val16, cs)` bit-bangs a 3-wire bus to two identical chips (CS on GPIOD
  0x08 / GPIOB 0x20). Its init (`FUN_0805ffe8`) writes 0x50, 0x58, 0x40 (8-entry table with the
  index in the top nibble), 0x41–0x47, 0x49, 0x4d, 0x57, 0x5c–0x60, 0x2a=0x1081, 0x30=0x5f6b,
  0x19, 0x59, 0x29=0x1090, 0x56, 0x3c; reg 0x2e takes sub-addressed writes (top nibble). It never
  writes BK4819's frequency registers (0x38/0x39) or AT1846S's frequency math, so the tuner part
  number is still open. The heard symptom (crackle at 560, silence at 774) fits a tuner/VCO that
  can't lock above ~520.
- Reliable oracle: the chip markings on the PCB, or FCC internal photos (fccid.io returns a bot
  check, not scraped).

## Diagnostic result (2026-10-27, by ear + BLE)
Flashed `patched_dmrdiag` (make_d890_dmrdiag_firmware.py, CDD b7ac6a63): keeps DEMOD_OUT set and
adds BLE traces — H1/H2 (the PROCESS_MODE / VOCODER_IO_SET hooks fired), F<id> (every field the
dispatcher FUN_08146930 saw, deduped), V<seq><hex> (CHAN_D copies). Transmitted a buzz DMR call
(gt-dmrtx, below) three times at 145.575; the radio DECODED it (audible buzz, confirmed by ear).
- **BLE link proven on this build:** ble_cps_bridge.py read 0x02000000 returned live data over
  FFE2, so AT+DATA send/notify works here.
- **Zero traces during the call:** no H1/H2, no F<id>, no V. The dispatcher never ran.
- **Conclusion:** in passive RX the MCU does NOT pull voice frames. The baseband decodes and
  plays audio itself; FUN_08146930 (and the CHAN_D path) only run while the MCU is actively in the
  record/playback feature. DEMOD_OUT alone changes nothing because nobody reads CHAN_D. H1/H2
  fired once at channel entry (before the BLE client connected) and the dedup suppressed repeats.
- **Next step for a hotspot RX tap:** drive the radio into record mode during RX — call
  FUN_08115370(0x11) when a digital call starts (hook the call-start path, e.g. where DAT_2000f9be
  becomes nonzero / the voice-sync-received state) — so the MCU polls CHAN_D, then forward those
  frames. TX side: FUN_08115370(0x10) + feed frames via CHAN_D. This is real RE, not a quick patch.

## HackRF DMR test transmitter (gt-dmrtx/)
Standalone Go module (GopherTrunk DMR framing/FEC/C4FM copied under internal/). `cmd/dmrtx` builds
a HackRF int8 TX file: analog-FM KF8CKX voice ID (Samantha), a DMR MS group call (CC/TG/src flags,
silence or -buzz voiced frames), FM ID again; signal offset +200 kHz so the LO leak is off-channel.
Deviation calibrated to ±648/±1944 Hz (devCorrection); GopherTrunk's own decoder locks it (TG 9,
CC 1) and plays the buzz. Tests: TestCallDecodes (every field round-trips), TestDeviation. Run:
gt-dmrtx/run_kf8ckx_test.sh (flash + TX). HackRF must be in SDR mode: `gt-scanner/hrf_eth off`
(it defaults to CDC-ECM ethernet mode for the iPhone; `hrf_eth on` to restore).

## RX tap attempts — the wall (2026-10-27)
Tapped all three baseband->MCU receive parsers over three flashed builds; BLE proven working
throughout (ble_cps_bridge read returned live data on each build). Transmitted a decoded buzz DMR
call (TG 9, CC 1; radio played the buzz by ear). Every tap stayed silent:
1. `FUN_08146930` (synchronous reply matcher, drained by the wait loops FUN_080dee40 /
   FUN_080e0bf8 / FUN_0810b8fc) — patched_dmrdiag F<id> trace: zero.
2. `FUN_080fad68` (the central voice-mode apply; writes DAT_2000f9be) — patched_dmrtap C<mode>
   trace: zero, i.e. the MCU applies no voice mode when passively receiving.
3. `FUN_080f335c` (the async RX sink: FUN_0802de84 UART demux, sync 84 A9 61, calls it for every
   unsolicited packet when DAT_2001bafc==0; field id at +6) — patched_rxtap R/P trace: zero, even
   with the channel contact set to TG 9 (matched call).
**Conclusion:** in normal digital monitor RX the main board does not receive the voice frames at
all. FUN_0802de84 queues packets at 0x200046bc but nobody drains them unless the MCU is actively in
a voice service (FUN_080f40f8 case 0 -> FUN_080fbc44 -> FUN_080f21c0 -> FUN_080e0bf8 ->
FUN_08146930) or waiting for a command reply. DEMOD_OUT changes nothing because no MCU code pulls
CHAN_D. The baseband decodes and plays the call autonomously.
**Real next step (multi-session RE, not a hook):** add a background task that drives the receive
voice-service loop while DEMOD_OUT is set — i.e. keep calling FUN_080f40f8(0,...) (or the
FUN_080f21c0 drain) during RX so the queue is pulled and CHAN_D reaches the MCU — without
disturbing the stock audio/UI. Risky; verify by ear that RX audio survives. Builds to date:
patched_dmrdiag (b7ac6a63), patched_dmrtap (136905b1), patched_rxtap (c91625e9); builders
make_d890_dmr{diag,tap}_firmware.py and make_d890_rxtap_firmware.py. BLE listener dmr_diag_listen.py.

## Deep dive — the three stacked walls and the real design (2026-10-27, 4-agent RE)
Four parallel RE passes over the headless Ghidra DB corrected the earlier "DEMOD_OUT is inert /
MCU never receives voice" conclusion. Every tap was silent because of THREE independent walls,
any one of which yields zero. The earlier negative traces also prove less than thought: Wall 3
means a hook could have fired and still produced no BLE output.

### Wall 1 — plain RX never routes voice to the MCU
Mode var `DAT_2000f9be` stays 0 in an ordinary incoming voice call. Field 0x3e (VOCODER_IO /
DEMOD_OUT) is written per-mode from the table at **0x08150fc8**; bit2 (0x04 = route RX voice to
MCU) is set ONLY for modes 3, 7, 9, 0xc, 0x11. Plain-RX modes 3/4/5 leave bit2 clear (table byte
0x00 for mode 0). The baseband decodes AMBE and drives the speaker over a SEPARATE field, **0x69**
(FUN_080e897c), independent of 0x3e. So RX audio and MCU routing are decoupled — adding 0x3e bit2
need not mute the speaker (verify by ear). The only automatic bit2 trigger is the record-on-receive
setting `DAT_200125c8==1`, which makes FUN_080ff350 call FUN_080fad68(6/8) on a voice call.

### Wall 2 — the plain-RX CHAN_D consumer is a no-op stub
Even with bit2 forced in a plain mode (API FUN_080f3a68 case 0x14 → mode 3), the per-tick consumer
`FUN_080fb67c(DAT_2000f9be)` dispatches to **FUN_080fbc0c for modes 3/4/5, which is a verified stub**
(`01 20 70 47` = movs r0,#1; bx lr). Frames are dropped. Only record modes 6/7/8/9 (FUN_080f135c/
FUN_080f1578) and data mode 0x11 (FUN_08108d40 → ring 0x20021e34) have a real consumer — and record
just store-and-replays the frame back to the baseband. KEY: the CHAN_D *copy* into DAT_2000d5e2 +
ready bit DAT_2000d56a&1 happens in the field dispatcher **FUN_08146930 case 0x01**, BEFORE any
consumer runs. So capturing there sidesteps the stub entirely.

### Wall 3 — BLE send from a passive hook never flushes (explains ALL prior zeros)
`FUN_080d71e8(0x10,…)` is a pure producer into a 16-slot TX ring; it never transmits and has no
overrun guard. The drain `FUN_08023354` (→ framer FUN_0810bbd8 → UART FUN_0812c8d8) is called from
exactly six functions, ALL blocking send-and-wait-for-ack pump loops (FUN_0801ee18, FUN_08133a98/
b24/c44/cf4/eb8). The scheduler FUN_0812cbe4 has the same six-caller restriction. There is NO
systick/idle drain (FUN_080bd704 only decrements cooldowns). The CPS read works solely because it
REPLIES to an inbound BT command, and inbound handling (FUN_0801fb10) runs inside pump loop
FUN_08133a98 — which drains synchronously. A passive baseband/voice hook that enqueues and returns
leaves the frame in the ring forever (and after 16, the ring wraps and corrupts). id 0x10 is not
gated or dropped — it is privileged (zero cooldown); the only problem is nothing spins the drain.

### Safe injection point (RTOS map)
Stripped preemptive FreeRTOS (PendSV port @0x0800c2c8, SysTick @0x081209c0). Main service task
**FUN_08105d3c** runs a ~10 ms super-loop; its inner idle poll `bl FUN_081249e0 @ 0x08105d4a` runs
every ~10 ms in TASK context in EVERY mode (before the DAT_2000f0ad work-gate). That BL is the tap
site — fits the proven trampoline recipe (append stub at 0x081a5008, redirect this one BL,
re-encode original byte-for-byte, bump .spi[6:10] + .CDI[260:264]). DMR voice service runs
automatically from this task (FUN_08105d3c → FUN_080dfc0c → FUN_080e5040, gated DAT_20021f10==0,
voice frame marker 0x4c, call-state DAT_20004bd0==1); there is no user "accept" step.

### The design that threads all three walls (surgical, speaker-preserving)
1. Route a COPY of RX voice to the MCU without muting the speaker: during a DMR voice call
   (DAT_20004bd0==1), add bit2 to field 0x3e only — FUN_080122ec(0x3e, cur|0x04) + flush
   FUN_0810c3b0(3,200) — leaving audio field 0x69 untouched. (Fallback if the baseband won't emit
   CHAN_D without a full mode apply: FUN_080fad68(3,0) or force record mode via DAT_200125c8=1,
   accept possible speaker mute, read the proper ring.)
2. Capture in the parser, not the consumer: trampoline FUN_08146930 case 0x01's 27-byte copy
   (→ DAT_2000d5e2, sets DAT_2000d56a&1) to ALSO snapshot the frame into our scratch ring + our flag.
   Dodges the Wall-2 stub.
3. Forward in task context: in the 0x08105d4a 10 ms stub, if our flag is set, build
   "AT+DATA=00,<len>\r\n<payload>\r\n", FUN_080d71e8(0x10,…), then self-drain:
   loop FUN_0812cbe4(); FUN_08023354(); until DAT_20001fc3==0. Task context, not ISR → safe.

Only-the-radio-settles unknowns: (a) does 0x3e bit2 keep the speaker on (field 0x69 independent)?
(b) does the SCT3288 forward CHAN_D with just 0x3e bit2 toggled mid-call, outside a full mode apply?
Both verify on-air. New addresses: table 0x08150fc8; stub FUN_080fbc0c; audio field 0x69 FUN_080e897c;
task FUN_08105d3c (tap BL 0x08105d4a); TX drain FUN_08023354 / scheduler FUN_0812cbe4 / framer
FUN_0810bbd8; send+pump wrapper FUN_0810b728; queue count DAT_20001fc3.

## Build: patched_rxtap2 — the three-wall build (2026-10-27, NOT yet flashed)
Builder make_d890_rxtap2_firmware.py on patched_blecps_verified (21e62ebb). Output CDD sha256
7efd0956c078d921, blob @0x081d04a0 (444 B, ends 0x081d0660). Seven BL sites redirected:
pm(0x081003e8)/io(0x080ffff8) OR DEMOD_OUT bit2 + H1/H2; CHAN_D copy(0x08146e90) -> V<seq><hex>;
dispatcher(0x080def56/0x080e0c50/0x0810bb5e) -> F<id> dedup; and the NEW Wall-3 drain at
0x08105d4a (bl FUN_081249e0 in the 10 ms service task) -> after the original, if PEND armed, spin
FUN_08023354 until COUNT(0x20001fc3)==0, bailing on COOLDOWN(0x2001aefa)!=0, bounded 20. send_payload
arms PEND after each FUN_080d71e8(0x10,...) enqueue. Drain uses ONLY FUN_08023354, not the scheduler
FUN_0812cbe4 (the scheduler generates new outbound requests). Verified in Unicorn (emu_rxtap2.py,
19/19): orig-first, drain-exactly-to-empty, PEND clear, cooldown bail, bound=20, V body = V+seq+54
hex, H1/H2 once, F dedup. On-air reading of one buzz DMR call is now diagnostic: V-lines => working
tap; H1/H2 but no F01 => baseband ignores bit2 (need active mode apply in RX); no H => stock never
writes 0x3e in plain RX (need active 0x3e=table[DAT_2000f9be]|0x04 assertion). Flash via the radio
lock + base-hash gate; verify by ear that RX audio survives.

## WIN: RX voice capture CONFIRMED over USB live-read (2026-10-27)
patched_rxtap3 (make_d890_rxtap3_firmware.py, CDD 9233b36a, on 21e62ebb) flashed and tested on the
real radio with the HackRF buzz DMR call (145.575, CC1, TS1, digital monitor). Read live over USB
with the stock 'R' command in NORMAL mode (no PROGRAM, UI not suspended) - the morse_probe
mechanism. KEY: 'R' reads live SRAM directly (0x2000xxxx/0x2002xxxx), only internal flash
0x08xxxxxx returns 0xFF (corrects the old d890-usb-cps "SRAM reads 0xFF" note).

Evidence (scratch at 0x2002a3xx; TICK u32 @300, SEQ @305, FRAMECNT u16 @306, SEEN @310,
LASTFRAME @340):
- TICK climbs ~107/s (every ~9.3 ms) in normal mode = the 10 ms task hook (0x08105d4a) runs.
  PROGRAM mode SUSPENDS the service task (TICK freezes) - that's why BLE/program reads looked dead.
- FRAMECNT/SEQ: idle delta = 0, one-burst delta = +40, three-burst = +115 (SEQ wraps match exactly).
  So the CHAN_D copy hook (0x08146e90, FUN_08146930 case 0x01) fires only on a received call:
  ~40 CHAN_D voice frames/burst reach the MCU.
- LASTFRAME == DAT_2000d5e2 == "aedfe5140823d398d9" x3 = three IDENTICAL 9-byte AMBE+2 sub-frames =
  the constant buzz tone encoded. Real captured voice, not garbage (first misread as garbage).

All three walls cleared for CAPTURE: Wall 1 DEMOD_OUT bit2 (pm/io hooks OR it into field 0x3e;
frames arrive => baseband forwards voice to MCU in plain digital-monitor RX); Wall 2 (captured in
the dispatcher copy site, before the no-op stub FUN_080fbc0c); Wall 3 (USB live-read sidesteps the
BLE drain). The received DMR voice frames can be pulled off the radio.

Flaw in this build (works around, not fatal): scratch is NOT zero-initialized (it's the unreferenced
SRAM gap, not .bss), so HFLAGS/SEEN absolute values are power-on garbage - only DELTAS (FRAMECNT/SEQ)
and TICK-rate are trustworthy. Next build should zero scratch on first task tick.

Remaining for a hotspot: stream the 27-byte frames continuously (not snapshots) and get them off in
real time - either fix the Wall-3 BLE drain for FUN_080d71e8(0x10) sends, or stream over USB. Reader
tools: read_rxtap3_diag.py, /tmp/fc.py (FRAMECNT), 'R' live-read snippet.

## WIN 2: real-time voice STREAM over USB (2026-10-27)
patched_rxstream (make_d890_rxstream_firmware.py, CDD 83a2d7559a0fca0c, on 21e62ebb). Firmware
writes each captured 27-byte CHAN_D frame into a 64-slot SRAM ring at 0x2002a320 (slot = WRIDX&63,
28 B = 27 frame + 1 seq tag), WRIDX @0x2002a304 published after each slot; 10 ms task one-shot
zero-inits the header (INITFLAG magic @0x2002a300) so WRIDX starts at 0 (fixes rxtap3's uninit flaw).
4 hooks only: task(0x08105d4a), pm(0x081003e8)/io(0x080ffff8) DEMOD_OUT bit2, CHAN_D copy(0x08146e90).
Host stream_rxframes.py polls WRIDX + reads new slots with the stock 'R' read, NORMAL mode (no
PROGRAM). Unicorn 20/20 (init one-shot, ring append, wrap@64, cap@27, len filter, pm/io).

On-air result (3 buzz bursts, 145.575 CC1 TS1 digital monitor): **115 frames streamed live, 0
dropped**, WRIDX started at 0. 6 distinct frames: the steady buzz frame aedfe5140823d398d9 x3
(3 identical 9-byte AMBE+2 sub-frames = constant tone) dominates, with onset frames (e.g. frame[0]
aaaaa514...) at burst starts = genuine decoded voice. rxframes.bin = 115 x 27 B. The RX
capture+stream half of the hotspot is proven on hardware.

Remaining for a full hotspot: (1) decode AMBE+2 -> audio or forward the 27 B frames to BrandMeister
(the bridge); (2) the TX path (radio transmits frames from the network). Tools: stream_rxframes.py,
make_d890_rxstream_firmware.py.

## WIN 3: full voice round-trip — real DMR TX + decode-to-audio over USB (2026-10-27)
A) Proper synthetic TX from GopherTrunk code (not a tone): gt-dmrtx gained -voice <raw>, streaming
   real AMBE+2 voice frames (49-bit info, 7 B/frame) from GopherTrunk's dmr-voice.raw (378 frames,
   7.56 s) through ambefec.EncodeAMBEFrame, one per AMBE slot (3/burst) instead of a constant
   buzz/silence. Copied dmr-voice.raw into gt-dmrtx/.
B) Decode to audio: GopherTrunk cmd/gt-dmrdecode (uncommitted, private fork): 9 B on-air -> 72
   bit-values -> dmr/voice.DecodeAMBEFrame (FEC -> 49 info) -> pack 7 B MSB-first ->
   ambe2.NewDMR().Decode -> int16 8 kHz -> WAV.

Validated: (1) buzz capture rxframes.bin -> clean ~tone, 0 FEC bad. (2) pure-software round-trip
dmr-voice.raw -> EncodeAMBEFrame (cmd/ambe-reencode in gt-dmrtx) -> gt-dmrdecode -> speech envelope
(per-0.5s RMS 10..7502, var 0.93). (3) FULL HARDWARE round-trip: gt-dmrtx -voice dmr-voice.raw ->
HackRF C4FM TX 145.575 -> AnyTone rxstream RX -> USB live stream (stream_rxframes.py) ->
gt-dmrdecode -> rxvoice.wav: 85 frames captured (0 dropped, only 4 steady = real varying voice),
5.1 s, envelope var 0.68 = intelligible speech over the air (played by ear). The hotspot RX path is
end-to-end: received DMR voice -> off the radio -> decoded to audio. Tools: gt-dmrtx -voice,
gt-dmrdecode, stream_rxframes.py, cmd/ambe-reencode. Outputs: voice.s8, rxvoice.wav,
voice_roundtrip.wav, buzz_decoded.wav.

## Diagnosis: "something's wrong" with rxvoice = lossy RF link, NOT the codec (2026-10-27)
Software decode is provably perfect: ground-truth direct ambe2 decode of dmr-voice.raw (gt-dmrdecode
... raw7) and the encode->decode round-trip (voice_roundtrip.wav) are BYTE-IDENTICAL (0/60480 diff,
cross-corr 1.0000); ref_direct.wav is GopherTrunk's own canonical decode of its own fixture. The
sample is foreign-language speech, so a correct decode sounds foreign.
Over-air capture (rxvoice.bin) analysis: 255 AMBE frames, 0 FEC-bad, 18 RF-mismatch (7%); consecutive
tx-index steps +1 x152, +2 x65, +3 x12 (so ~1/3 of frames never forwarded); span 0..377. The drops +
cross-frame AMBE prediction = the "almost speech" garble. Ring reported 0 overruns (host kept up), so
the loss is the baseband not forwarding every frame (weak signal: TX ~tens of uW a few feet away).
Improve: higher HackRF gain / amp on, tighter freq centering, or check if baseband forwards a subset
by design. Debug modes added to gt-dmrdecode: `raw7` (direct 7B decode), `dump` (per-frame info hex).

## Synthetic test instrument + systematic-drop finding (2026-10-27)
Added gt-dmrtx -synth: a rising-pitch "siren" where each frame's AMBE+2 pitch (b0) = 20 + framenum%80,
so every captured frame self-identifies. Validated in software (encode->decode->extract b0 = perfect
ramp). Helper cmds: gt-dmrtx/cmd/synthdump (emit N synth 7B frames), gt-dmrdecode `dump` (per-frame
info hex). Extract b0 from the dump at info bit positions {0,1,2,3,37,38,39} MSB-first.

Over-air (x40 + amp, 145.575): 309 captured frames, **only 1% gross RF errors** (so power/signal is
NOT the bottleneck - confirms raising power earlier didn't help). Step histogram of consecutive b0:
+1 (good) x201, **+2 (exactly 1 frame dropped) x100**, few larger. Effective delivery ~66%. The +2
drops land ~once per 3-frame CHAN_D packet: captured packets are [n,n+1,n+2] then [n+4,n+5,n+6] -
i.e. the radio forwards 3 of every 4 frames, dropping ~1 at each packet boundary. SYSTEMATIC, not
random RF (RF would scatter gap sizes + errors). Ring reported 0 overruns and the host kept up, so the
gap is upstream of our ring - in the baseband->MCU CHAN_D delivery or the stock copy (DAT_2000d5e2
holds one burst's 3 frames; a 4th per unit isn't captured). This ~1-in-4 loss + AMBE cross-frame
prediction = the "gurgle". Next lead for clean capture: instrument a baseband frame counter vs
CHAN_D-copy count to find where the 4th frame goes. Files: voice_synth.s8, rxsynth.bin/.wav.

## FIXED: every-4th-frame drop (2026-10-27) - rxstream2
Root cause (found via the synth siren): CHAN_D handler FUN_08146930 case 0x01 ALWAYS copies 0x24=36
bytes = 4 on-air AMBE frames (sub byte 0x24/0x1b is a gate, not the length); rxstream capped the ring
slot at 27 (3 frames), dropping the 4th of every packet -> the measured n==3 mod 4 loss.
Fix: rxstream2 (make_d890_rxstream2_firmware.py, CDD d19ab3d8) captures the full 36 bytes into 40-byte
slots (36 payload + [36]=sub + [37]=tag), 64 slots @0x2002a360, sub-type counters C24/C1b. Unicorn
20/20. Streamer stream_rxframes2.py.
On-air (x40+amp): SIREN now 412 frames, 0 dropped, 0 RF-err, b0 ramp 20..59.. PERFECTLY CONTIGUOUS,
100% +1 steps (was 66%). C24=103 C1b=0 => every CHAN_D is 4 frames, confirming the hypothesis. REAL
VOICE (dmr-voice.raw): 412 frames, 0 dropped, 0 bad, 8.24s, envelope var 0.90 = clean full-rate
speech (rxvoice4.wav). The RX voice tap now streams the COMPLETE frame sequence off the radio - the
capture half of the hotspot is clean. Remaining for hotspot: BrandMeister bridge + the TX path.

## Software AMBE+2 encoder (analysis-by-synthesis) (2026-10-27)
Built GopherTrunk cmd/gt-ambe-encode (uncommitted, private fork): a first-cut AMBE+2 2450 encoder
with NO DVSI vocoder/dongle. Uses GopherTrunk's ambe2 decoder as an oracle - per 20 ms frame (160
samp @8k), greedy coordinate-descent over each field (b0 pitch 0-119, b1 voicing 0-31, b2 gain 0-31,
b3 PRBA 0-511, b4 0-127, b5-b8 HOC) minimizing windowed log-magnitude spectral error vs the target;
packs b0..b8 into the 49-bit info (field bit-positions = inverse of unpackParams2450). WAV reader
handles leading JUNK/FLLR chunks. ~23 s to encode 1.6 s (82 frames). Output 7 B/frame raw -> decode
with gt-dmrdecode raw7.
Round-trip: Samantha "K F 8 C K X" (/tmp/kf8ckx.wav 24k) -> gt-ambe-encode -> gt-dmrdecode ->
kf8ckx_vocoded.wav (1.64 s, envelope var 0.59 = speech dynamics). Intelligibility pending ear check.
Known limits (first cut): stateless per-frame search ignores the decoder's cross-frame gain/log2Ml
prediction (gain may drift); greedy VQ not globally optimal; spectral-only metric. Iterations if rough:
state-snapshot search, autocorr pitch init, proper voicing decision, 2nd-pass gain tracking. This
unblocks sending ANY audio (Samantha) over DMR in pure software. Next: TX through the AnyTone + capture
back = the end-to-end "can it handle voice" test.

## Encoder iterations 2-3: fixed the gain-oracle + pitch warble (2026-10-27)
Sample-by-sample, v1 output had the AMBE gain-predictor bug: scoring candidates on a FRESH decoder
gave ~3800x the gain of the same frame decoded in sequence (gamma = dGamma + 0.5*prevGamma; fresh =>
prevGamma 0). Fix: added ambe2.Decoder.Clone() (shallow copy + deep-copy AGC + reseed RNGs; phase is
irrelevant to a magnitude match) and scored each candidate from the COMMITTED decoder's state,
advancing it with the chosen frame. v2 boundary-jump max 31876->13541. Pitch still warbled (coarse
spectral metric can't lock b0). v3: estimate f0 by normalized autocorrelation (72-296 Hz) per frame,
map to b0 via ambe2.F0ToB0 (uses dmrW0table), constrain the b0 search to +/-2 for voiced frames (free
for unvoiced). Result: voiced b0 now stable (...42,42,41,43,47...; ...52,53,53,53...), consonants
noise-like - real speech structure. Output kf8ckx_vocoded3.wav. Intelligibility pending ear check;
remaining knobs if needed: proper per-band voicing (b1) decision, gain(b2) energy-match, finer spectral
metric. Tools: cmd/gt-ambe-encode, internal/voice/ambe2/encoder_clone.go (uncommitted, private fork).
