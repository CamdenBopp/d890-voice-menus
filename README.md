# Spoken menus for the AnyTone AT-D890UV

A firmware patch that makes the D890UV speak its menus. Scroll a menu and the radio says
the highlighted item ("Talk Group", "Settings", "Busy Lock") in a DECtalk voice. It was
built so the radio can be used by ear, without reading the screen.

The speech comes from the radio's main CPU. Nothing is added to the Bluetooth or audio
board, and the stock beep and tone behaviour is unchanged.

This repo has no AnyTone firmware in it. You supply the stock V1.05 image, and the scripts
patch it on your machine.

## Status

Tested by ear on one radio, main firmware V1.05 (20260521):

- Main menu and submenus speak the item name.
- Rows the radio hides (Zone and Scan on some setups, Satellite, Digi Moni) are skipped
  the same way the screen skips them.
- Lists the radio fills at runtime, such as contacts, speak the row number instead.
- On a digital (DMR) channel the radio's beeps go through a different path, so the menus
  fall back to the item number in Morse.

Speech is 8 kHz, LPC-coded to fit in spare flash, so it sounds rougher than DECtalk on a
computer. Short words and acronyms come through clearly.

## Risk

Flashing modified firmware can leave the radio unable to boot. In testing, the
bootloader was never overwritten and reflashing the stock image always brought the radio
back, but you do this at your own risk. Keep the stock `.spi/.CDI/.CDD` files.

## What you need

- The stock D890UV V1.05 main firmware: `D890UV_V1.05_20260521.spi`, `.CDI` and `.CDD`,
  from AnyTone's firmware download. `build.sh` checks the `.CDD` sha256 (`6d84926e…`).
- [DECtalkMini](https://github.com/dectalk/DECtalkMini), built, for its `say` program.
- `arm-none-eabi-gcc` and binutils, a host C compiler, `sox`, and Python 3 with
  `pip install -r requirements.txt`.

## Build

```bash
DECTALK_SAY=path/to/DECtalkMini/build/say ./build.sh "path/to/D890UV_V1.05_20260521"
```

The output is `build/voiceitems/D890UV_V1.05_20260521.spi` (plus `.CDI` and `.CDD`).
The build is reproducible: from the stock image above you should get a `.CDD` with
sha256 `a40f6574743f21235581256e285d6c37598a68b8aa490ff0bfbf4091262ad224`.

The build also writes `build/voiceitems/preview_*.wav`. These are rendered by the same
decoder the radio runs, so you can hear the result before flashing.

## Flash

1. Power the radio off. Hold PF3 (the blue top button) and PTT, power on, and connect
   USB.
2. Run:

   ```bash
   python3 tools/flash_firmware_d890.py build/voiceitems/D890UV_V1.05_20260521.spi /dev/cu.usbmodemXXXX
   ```

   It takes about two minutes. It ends with "Finishing transfer" and then a "Device not
   configured" error, which is the radio resetting off USB.
3. Unplug, power cycle, and scroll a menu on an analog channel.

To go back to stock, flash the stock `.spi` the same way.

## How it works

Short version: the STM32 already drives the speaker through a PWM pin, using an 8-bit
duty cycle clocked by a timer interrupt. Stock firmware uses it for beeps. The patch adds
a word decoder to that interrupt, and speaks the text of the menu row the cursor is on.
[docs/how_it_works.md](docs/how_it_works.md) has the details and addresses.

## Files

| File | Purpose |
|---|---|
| `build.sh` | Runs the patch chain from stock |
| `make_d890_rampeek_firmware.py`, `make_d890_nopcm_read_firmware.py`, `make_d890_noflip_read_firmware.py` | Base layers: a read-only USB peek of live RAM, used for development probes |
| `make_d890_voiceitems_firmware.py` | The spoken-menu patch |
| `voice_items/voicegen.py` | Item strings to words, DECtalk rendering, LPC encoder |
| `voice_items/voice_lpc.c` | On-radio decoder and menu cue (also builds on the host for previews) |
| `make_d890_morsemenu_firmware.py`, `make_d890_voicemenu_firmware.py`, `make_d890_morse_firmware.py` | Earlier stages, reused for shared hooks and constants |
| `tools/flash_firmware_d890.py` | Main-board flasher |
| `tools/item_probe.py`, `tools/menu_diag.py`, `tools/anytone_readonly_dump.py` | Live menu-state probe and read-only memory reads over USB |

## Credits and license

- `tools/flash_firmware_d890.py` is from
  [reald/anytone-flash-tools](https://github.com/reald/anytone-flash-tools) (GPL-2.0),
  changed to accept the D890's `_MAIN.bin` image name.
- Speech is rendered with [DECtalk](https://github.com/dectalk/DECtalkMini), which is not
  included here.
- [MM9PDY's D890 patches](https://github.com/pskillen/mm9pdy-firmware) target the same
  stock image and were a useful cross-check.

GPL-2.0, see [LICENSE](LICENSE). AnyTone firmware is AnyTone's and is not distributed here.
