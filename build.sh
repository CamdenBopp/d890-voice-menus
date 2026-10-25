#!/bin/sh
# Build the spoken-menu firmware from the stock AnyTone D890UV V1.05 main firmware.
#   ./build.sh "path/to/D890UV_V1.05_20260521"    (the .spi/.CDI/.CDD base name, no extension)
# Output: build/voiceitems/D890UV_V1.05_20260521.spi (+ .CDI, .CDD), flash with tools/flash_firmware_d890.py
set -eu
STOCK=${1:?usage: ./build.sh path/to/D890UV_V1.05_20260521}
SAY=${DECTALK_SAY:-DECtalkMini/build/say}
PY=${PYTHON:-python3}
WANT=6d84926eecf309b25f68287d3521adaae2765f171db9d9f6a1358a454c71c96e
HAVE=$(shasum -a 256 "$STOCK.CDD" | cut -d' ' -f1)
if [ "$HAVE" != "$WANT" ]; then
  echo "stock .CDD sha256 is $HAVE, expected $WANT (D890UV V1.05 20260521)" >&2
  exit 1
fi
[ -x "$SAY" ] || { echo "DECtalk 'say' not found at $SAY (set DECTALK_SAY)" >&2; exit 1; }
N=D890UV_V1.05_20260521
$PY make_d890_rampeek_firmware.py --input-base "$STOCK" --output-dir build/rampeek
$PY make_d890_nopcm_read_firmware.py --input-base build/rampeek/$N --output-dir build/nopcm
$PY make_d890_noflip_read_firmware.py --input-base build/nopcm/$N --output-dir build/noflip
$PY make_d890_voiceitems_firmware.py --input-base build/noflip/$N --output-dir build/voiceitems --say "$SAY"
