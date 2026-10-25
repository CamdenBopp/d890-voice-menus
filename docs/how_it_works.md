# How the spoken menus work

Addresses are for the D890UV main firmware V1.05 (20260521), load base `0x0800c000`.
Function names are Ghidra's.

## The audio path the radio already has

The STM32 drives the speaker with a PWM pin. There is no DAC.

- TIM4 channel 4 (GPIOB pin 9, AF2) runs PWM with ARR `0xff`, so the duty cycle is an
  8-bit sample. `FUN_08125854(duty)` sets it.
- The TIM5 update interrupt (IRQ 48, handler at `0x08120e70`) writes one sample per tick.
  For beeps, the BL at `0x08120eb2` steps a 16-point sine table at `0x081505c0`. A second
  stock mode streams a RAM buffer, used for data transmissions.
- Beeps are playlists of `{frequency Hz, duration in 10 ms ticks}` pairs. The producer
  `FUN_08133210` sets TIM5's rate for each item with `FUN_0811a440(750000/f - 1, 9)` (BL at
  `0x08133444`). Its sine runs 16 updates per cycle, so the timer clock is 120 MHz, and
  `FUN_0811a440(14999, 0)` gives exactly 8000 interrupts a second.

## Hooks

All hooks redirect an existing BL to code appended after the image. No stock call is
removed.

| Site | Stock target | Hook |
|---|---|---|
| `0x0812263a` | display-task mailbox receive | detect a change of menu cursor, depth or page, and start a cue |
| `0x0801a75c` | `FUN_0811cd44` playlist select | swap in the cue's playlist |
| `0x08133444` | `FUN_0811a440` TIM5 setup | a playlist frequency of `0xF000 + n` starts word `n` at 8 kHz |
| `0x08120eb2` | sine step in the TIM5 ISR | while a word plays, output the decoder's next sample |
| `0x08124678` | BT mode apply | earlier work: defers a Bluetooth mode change |

A cue posts feedback code `0x1c`, which the stock gate `FUN_080d0ad0` always lets through.
That way speech plays with key beeps on or off.

## Which item is highlighted

- `_DAT_20004b40` points to the current menu's list of UTF-16 string pointers.
  `_DAT_20008790` is the visible row count, and `_DAT_20004244` is the cursor.
- The cursor counts visible rows. The row drawer `FUN_0807dc4c` draws
  `list[remap(row)]`: the main menu uses `FUN_080bb9dc` to skip hidden items, settings
  lists use `FUN_080bbbe4`, and a few other menus have their own. `voice_lpc.c` mirrors
  that logic exactly. Without it, the main menu spoke items several rows off.
- The string address is looked up in a sorted table. That table is built from every
  `{list, count}` record in the image whose entries all point at English menu text
  (1,242 strings). Anything not found, including runtime-built lists, speaks the row
  number.

## Speech data

- Each item string becomes words. Acronyms are spelled ("T X", "A P R S"), abbreviations
  are expanded ("Rptr" becomes "repeater"), and numbers are read as words
  ("162.475MHz" becomes "one hundred sixty two point four seven five megahertz").
  That leaves a 431-word vocabulary.
- DECtalk renders each word. sox trims, resamples to 8 kHz and compresses dynamics.
- At 8-bit, 8 kHz the vocabulary would take about 1.8 MB, and only about 240 KB of flash
  is free. So each word is stored as 20 ms LPC frames: 12 reflection coefficients
  (arcsine-quantised), a log gain and a pitch period, 14 bytes per frame, about
  0.7 KB/s. That comes to 130 KB for 185 s of speech.
- The decoder is integer-only, so it never touches the FPU inside the interrupt.
  Excitation (impulses or noise) feeds a 12-pole lattice, then de-emphasis, with
  parameters interpolated over four 40-sample subframes. Each word gets a 10 ms fade in
  and out so it doesn't click.

## Memory used

- Flash: the stock image ends at `0x081a5008`. The patch ends at `0x081d01a0`. Nothing in
  the stock image references flash past its own end. The builder refuses to go past
  `0x081E0000`.
- RAM: decoder state and two playlist buffers at `0x2002a900`, inside the unreferenced
  SRAM gap `0x20029b06`–`0x2002b2c1`.

## Dead ends

- The Jieli audio board already stores spoken digits, but nothing on the main CPU can
  trigger them. Reflashing it is blocked by its bootloader's product check.
- External flash past the icon data (`0x480000` and up) holds the codeplug, so it can't be
  used for clips.
- DECtalk reads the spelling "ay" as "eye". Letter names use bare capitals instead.
