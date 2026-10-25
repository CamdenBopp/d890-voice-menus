"""Menu-item speech data for the D890: item strings -> words -> DECtalk -> LPC frames -> C.

Item strings are the English UTF-16 strings the firmware's menu lists point at: every
{u32 list, u8 count} record whose list is `count` pointers into the menu-text region
(the cursor indexes that list at runtime, _DAT_20004b40, confirmed live 2026-10-25).
"""
from __future__ import annotations
import math, re, struct, subprocess, wave
from pathlib import Path
import numpy as np

BASE = 0x0800C000
TEXT_LO, TEXT_HI = 0x0817B000, 0x0818D000   # UTF-16 menu text region
RATE, FRAME, ORDER = 8000, 160, 12
FRAME_BYTES = ORDER + 2
PREEMPH = 0.9
DICT = {w.strip().lower() for w in open("/usr/share/dict/words")}

# spoken form of abbreviations and run-together tokens (lowercase key)
ABBREV = {
    "ana": "analog", "ack": "acknowledge", "aleart": "alert", "bat": "battery", "ch": "channel",
    "chan": "channel", "ctr": "center", "dec": "decode", "del": "delete", "dest": "destination",
    "digi": "digital", "dis": "display", "disl": "display", "dist": "distance", "enc": "encode",
    "fobid": "forbid", "txfobid": "T X forbid", "txforbid": "T X forbid", "fir": "first",
    "func": "function", "freq": "frequency", "gourp": "group", "hdr": "header", "infor": "info",
    "lange": "language", "mic": "mike", "moni": "monitor", "msg": "message", "num": "number",
    "nr": "N R", "pri": "priority", "prio": "priority", "prot": "protect", "pwd": "password",
    "rptr": "repeater", "repter": "repeater", "rpt": "repeater", "reprort": "report",
    "sc": "scan", "sec": "second", "sel": "select", "spd": "speed", "spk": "speaker",
    "sq": "squelch", "ste": "S T E", "startup": "start up", "trans": "transmit", "vox": "vox",
    "wx": "weather", "zopne": "zone", "dd": "day", "mm": "month", "yyyy": "year", "yyyyy": "year",
    "int": "interval", "ver": "version", "noaa": "noah", "glonass": "glonass", "kiss": "kiss",
    "null": "null", "rev": "reverse", "misc": "misc", "offl": "off", "cur": "current",
    "callsign": "call sign", "passall": "pass all", "talkaround": "talk around", "vol": "volume",
    "compander": "compander", "unicode": "unicode", "firmware": "firmware", "seeked": "sought",
    "rx": "R X", "tx": "T X", "maxu": "max U", "maxv": "max V", "minu": "min U", "minv": "min V",
}
UNITS = {"s": "seconds", "min": "minutes", "ms": "milliseconds", "hz": "hertz",
         "mhz": "megahertz", "k": "K", "w": "watts", "v": "volts"}
SAY_CAPS = {"all", "off", "on", "no", "to", "own", "new", "zone", "scan", "tone", "auto", "save",
            "high", "low", "mid", "open", "beep", "noaa", "kiss", "null", "rev", "misc", "freq",
            "func", "digi", "ana", "mic", "glonass", "vox", "info", "set", "end", "main", "key"}
LETTERS = dict(zip("abcdefghijklmnopqrstuvwxyz",
                   "ay bee see dee ee eff gee aitch eye jay kay el em en oh pee cue are ess tee you "
                   "vee double-you ex why zee".split()))
ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def utf16_at(img: bytes, a: int) -> str | None:
    o, out = a - BASE, []
    while len(out) < 64:
        c = struct.unpack_from("<H", img, o)[0]
        if c == 0:
            return "".join(out)
        if not 0x20 <= c < 0x7F:
            return None
        out.append(chr(c)); o += 2
    return None


def item_strings(img: bytes, end: int) -> dict[int, str]:
    """{string address: text} for every English string reachable from a menu item list."""
    n = end - BASE
    ptr = {}
    for o in range(0, n - 4, 4):
        v = struct.unpack_from("<I", img, o)[0]
        if TEXT_LO <= v < TEXT_HI and v % 2 == 0:
            s = utf16_at(img, v)
            if s and s.strip():
                ptr[o] = (v, s)
    out = {}
    for o in range(0, n - 8, 4):
        lst, cnt = struct.unpack_from("<IB", img, o)
        if BASE <= lst < end and 1 <= cnt <= 64 and (lst - BASE) % 4 == 0:
            slots = [(lst - BASE) + 4 * i for i in range(cnt)]
            if all(s in ptr for s in slots):
                for s in slots:
                    out[ptr[s][0]] = ptr[s][1]
    return out


def number_words(v: int) -> list[str]:
    if v >= 1000:
        return number_words(v // 1000) + ["thousand"] + (number_words(v % 1000) if v % 1000 else [])
    if v >= 100:
        return [ONES[v // 100], "hundred"] + (number_words(v % 100) if v % 100 else [])
    if v >= 20:
        return [TENS[v // 10]] + ([ONES[v % 10]] if v % 10 else [])
    return [ONES[v]]


def spell(tok: str) -> list[str]:
    return [f"letter {c}" for c in tok.lower()]


def words(text: str) -> list[str]:
    """Spoken word keys for one item string. 'letter x' keys are spelled letters."""
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    s = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", s)
    toks = re.findall(r"\d+(?:\.\d+)?|[A-Za-z]+|[+&%#]", s)
    out: list[str] = []
    prev_num = False
    for t in toks:
        low = t.lower()
        if t[0].isdigit():
            whole, _, frac = t.partition(".")
            out += number_words(int(whole))
            frac = frac.rstrip("0")
            if frac:
                out += ["point"] + [ONES[int(c)] for c in frac]
            prev_num = True
            continue
        if prev_num and low in UNITS:
            spoken = UNITS[low]
        elif t in "+&%#":
            spoken = {"+": "plus", "&": "and", "%": "percent", "#": "number"}[t]
        elif low in ABBREV:
            spoken = ABBREV[low]
        elif len(t) == 1 or (t.isupper() and low not in SAY_CAPS and (len(t) <= 3 or low not in DICT)):
            out += spell(t); prev_num = False
            continue
        else:
            spoken = low
        for w in spoken.split():
            out += spell(w) if (w.isupper() and len(w) == 1) else [w.lower()]
        prev_num = False
    return out


def dectalk_text(key: str) -> str:
    if key.startswith("letter "):
        return key[-1].upper()          # DECtalk's own letter name; "ay" comes out as "eye"
    return key.replace("-", " ")


def render(key: str, say: Path, cache: Path) -> np.ndarray:
    name = re.sub(r"[^a-z0-9]+", "_", key)
    raw, wav8 = cache / f"{name}.wav", cache / f"{name}.8k.wav"
    text = dectalk_text(key)
    # DECtalk drops some short tokens ("mi."); fall back to no period, then letter names
    for attempt in (f"{text}.", text, " ".join(LETTERS.get(c, c) for c in text if c.isalpha())):
        if wav8.exists():
            break
        with raw.open("wb") as f:
            subprocess.run([str(say), "-p"], input=f"[:rate 230] {attempt}".encode(), stdout=f,
                           stderr=subprocess.DEVNULL, check=True)
        subprocess.run(["sox", "-D", raw, "-r", str(RATE), "-c", "1", "-b", "16", "-e", "signed", wav8,
                        "silence", "1", "0.005", "0.5%", "reverse", "silence", "1", "0.005", "0.5%",
                        "reverse", "compand", "0.005,0.1", "-60,-60,-30,-12,0,-3", "norm", "-1"],
                       check=True, capture_output=True)
        with wave.open(str(wav8)) as w:
            if w.getnframes() < 300:
                wav8.unlink()
    if not wav8.exists():
        raise ValueError(f"{key!r}: DECtalk rendered nothing")
    with wave.open(str(wav8)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
    if len(pcm) < 300:
        raise ValueError(f"{key!r}: only {len(pcm)} samples from DECtalk")
    return pcm


def levinson(r: np.ndarray) -> tuple[np.ndarray, float]:
    a = np.zeros(ORDER + 1); a[0] = 1.0
    k = np.zeros(ORDER)
    e = r[0]
    for i in range(1, ORDER + 1):
        acc = r[i] + np.dot(a[1:i], r[i - 1:0:-1])
        ki = -acc / e
        ki = max(-0.999, min(0.999, ki))
        k[i - 1] = ki
        a[1:i] = a[1:i] + ki * a[i - 1:0:-1]
        a[i] = ki
        e *= 1 - ki * ki
    return k, e


def pitch_lag(x: np.ndarray, c: int) -> int:
    seg = x[max(0, c - 160):c + 160]
    if len(seg) < 200 or np.sqrt(np.mean(seg ** 2)) < 300:
        return 0
    best, lag = 0.0, 0
    rs = []
    for L in range(20, 161):
        a, b = seg[:-L], seg[L:]
        den = math.sqrt(np.dot(a, a) * np.dot(b, b)) or 1.0
        rs.append(np.dot(a, b) / den)
    rs = np.array(rs)
    m = rs.max()
    if m < 0.55:
        return 0
    # first peak within 90 % of the max avoids octave-low errors
    for i in range(1, len(rs) - 1):
        if rs[i] >= 0.9 * m and rs[i] >= rs[i - 1] and rs[i] >= rs[i + 1]:
            return 20 + i
    return 20 + int(rs.argmax())


def k_index(k: float) -> int:
    return int(round(math.asin(k) / (math.pi / 2) * 127)) + 128


def k_table() -> list[int]:
    return [max(-32767, min(32767, round(math.sin((i - 128) / 127 * math.pi / 2) * 32768))) for i in range(256)]


def g_table() -> list[int]:
    return [round(2 ** (min(i, 247) / 8)) for i in range(256)]


def encode(x: np.ndarray) -> bytes:
    pre = np.append(x[0], x[1:] - PREEMPH * x[:-1])
    win = np.hamming(240)
    nf = math.ceil(len(x) / FRAME)
    lagw = np.exp(-0.5 * (2 * math.pi * 60 * np.arange(ORDER + 1) / RATE) ** 2)
    out = bytearray()
    for j in range(nf):
        c = j * FRAME + FRAME // 2
        seg = np.zeros(240)
        lo = c - 120
        src = pre[max(0, lo):lo + 240]
        seg[max(0, -lo):max(0, -lo) + len(src)] = src
        seg *= win
        r = np.array([np.dot(seg[:240 - i], seg[i:]) for i in range(ORDER + 1)]) * lagw
        if r[0] < 1e-3:
            k, gain = np.zeros(ORDER), 0.0
        else:
            r[0] *= 1.0001
            k, e = levinson(r)
            gain = math.sqrt(max(e, 0.0) / np.dot(win, win))
        out += bytes(k_index(v) for v in k)
        out.append(max(0, min(247, int(round(8 * math.log2(gain + 1))))))
        out.append(pitch_lag(x, c))
    return bytes(out)


def c_array(ctype: str, name: str, vals, per_line: int = 24) -> str:
    vals = list(vals)
    lines = [", ".join(str(v) for v in vals[i:i + per_line]) for i in range(0, len(vals), per_line)]
    return f"const {ctype} {name}[{len(vals)}] = {{\n" + ",\n".join(lines) + "\n};\n"
