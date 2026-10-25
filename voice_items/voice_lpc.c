/* D890 spoken menu items: LPC word decoder + menu cue, linked after the stock image.
 *
 * Each word is a run of 20 ms LPC frames (vd_frames, FRAME_BYTES each): ORDER reflection
 * coefficients as int8 indices into vd_ktab (Q15, arcsine-spaced), a log gain index into
 * vd_gtab, and the pitch period in samples (0 = unvoiced). Decoding is integer-only so it
 * is safe in the TIM5 ISR at 8 kHz: impulse/noise excitation -> all-pole lattice -> de-emphasis.
 * Parameters are interpolated over four 40-sample subframes. The same file builds on the
 * host (-DHOST) to render previews bit-exactly.
 */
#include <stdint.h>
#include "voice_data.h"

#define SUB 40
#define SUBS (FRAME / SUB)
#define RAMP 80
#define CENTER 128
#define SWING 110
#define DEEMPH 29491            /* 0.9 in Q15 */
#define LEAD 15                 /* stock leading silence, 10 ms ticks */
#define WORD_GAP 5
#define MAX_WORDS 16
#define PL_LEN (2 + 4 * MAX_WORDS + 2)
#define CLIP_FREQ 0xF000u

enum { IDLE = 0, PLAYING = 1, DONE = 2 };
enum { RAMP_UP, FRAMES, RAMP_DOWN };

struct vl_state {
    uint32_t state;              /* read by the ISR first: written last when starting */
    uint32_t phase, pos;         /* RAMP_UP/FRAMES/RAMP_DOWN, sample index within phase */
    uint32_t first, frame, end;  /* word's first frame, current frame, one past its last */
    int32_t b[ORDER];            /* lattice backward state */
    int32_t y;                   /* de-emphasis state */
    int32_t k[ORDER], gain;      /* interpolated parameters for this subframe */
    uint32_t pitch, pcount, noise;
    uint16_t pl[2][PL_LEN];      /* double-buffered playlists */
    uint32_t pl_sel;
};

#ifdef HOST
struct vl_state vl;
#else
struct vl_state vl __attribute__((section(".vlram")));
#define SET_PWM_FN ((void (*)(unsigned))(0x08125854 | 1))
static void set_pwm(unsigned d) { SET_PWM_FN(d); }
#define CURSOR_LIST (*(volatile uint32_t *)0x20004B40)
#define CURSOR_CNT (*(volatile uint32_t *)0x20008790)
#define FREQ_VAR (*(volatile uint16_t *)0x2000309A)
#endif

static const uint8_t *frame_ptr(uint32_t f) { return &vd_frames[f * FRAME_BYTES]; }

static void load_subframe(uint32_t sub)
{
    /* blend the previous frame into this one: weight (sub+1)/SUBS on the current frame */
    const uint8_t *cur = frame_ptr(vl.frame);
    const uint8_t *prev = vl.frame > vl.first ? cur - FRAME_BYTES : cur;
    int32_t w = (int32_t)(sub + 1), iw = SUBS - w;
    for (int i = 0; i < ORDER; i++)
        vl.k[i] = (vd_ktab[cur[i]] * w + vd_ktab[prev[i]] * iw) / SUBS;
    int32_t g = ((int32_t)cur[ORDER] * w + (int32_t)prev[ORDER] * iw) / SUBS;
    vl.gain = vd_gtab[g];
    uint32_t p = cur[ORDER + 1];
    if (p != vl.pitch) {
        if (vl.pitch == 0 || p == 0) vl.pcount = 0;
        vl.pitch = p;
    }
}

static int32_t excite(void)
{
    if (vl.pitch) {
        if (vl.pcount == 0) {
            vl.pcount = vl.pitch;
            return (int32_t)(((int64_t)vl.gain * vd_sqrt_q8[vl.pitch]) >> 8);
        }
        vl.pcount--;
        return 0;
    }
    vl.noise ^= vl.noise << 13; vl.noise ^= vl.noise >> 17; vl.noise ^= vl.noise << 5;
    int32_t u = (int32_t)(vl.noise >> 16) - 32768;          /* uniform, |u| < 2^15 */
    return (int32_t)(((int64_t)vl.gain * u * 56756) >> 30); /* scale by sqrt(3) */
}

static int32_t lattice(int32_t e)
{
    int32_t f = e;
    for (int m = ORDER; m >= 1; m--) {
        f -= (int32_t)(((int64_t)vl.k[m - 1] * vl.b[m - 1]) >> 15);
        if (m < ORDER) vl.b[m] = vl.b[m - 1] + (int32_t)(((int64_t)vl.k[m - 1] * f) >> 15);
    }
    if (f > (1 << 22)) f = 1 << 22;
    if (f < -(1 << 22)) f = -(1 << 22);
    vl.b[0] = f;
    vl.y = f + (int32_t)(((int64_t)vl.y * DEEMPH) >> 15);
    return vl.y;
}

void vl_start(uint32_t id)
{
    vl.state = IDLE;
    vl.phase = RAMP_UP; vl.pos = 0;
    vl.first = vl.frame = vd_word[id][0];
    vl.end = vd_word[id][0] + vd_word[id][1];
    for (int i = 0; i < ORDER; i++) vl.b[i] = 0;
    vl.y = 0; vl.pitch = 0; vl.pcount = 0;
    if (vl.noise == 0) vl.noise = 0x9E3779B9u;
    vl.state = PLAYING;
}

/* next PWM duty 0..255, or -1 when the word is finished */
int vl_sample(void)
{
    switch (vl.phase) {
    case RAMP_UP:
        if (vl.pos < RAMP) return (int)(CENTER * vl.pos++ / RAMP);
        vl.phase = FRAMES; vl.pos = 0;
        /* fall through */
    case FRAMES:
        if (vl.frame < vl.end) {
            if (vl.pos % SUB == 0) load_subframe(vl.pos / SUB);
            int32_t y = lattice(excite());
            int32_t d = CENTER + (int32_t)(((int64_t)y * SWING) >> 15);
            if (++vl.pos == FRAME) { vl.pos = 0; vl.frame++; }
            return d < 0 ? 0 : d > 255 ? 255 : d;
        }
        vl.phase = RAMP_DOWN; vl.pos = 0;
        /* fall through */
    default:
        if (vl.pos < RAMP) return (int)(CENTER * (RAMP - vl.pos++) / RAMP);
        return -1;
    }
}

uint32_t vl_ticks(uint32_t id) { return 2 * vd_word[id][1] + 3; }

#ifndef HOST
/* TIM5 ISR: 1 = handled (clip or its trailing silence), 0 = let the stock sine step run */
int vl_isr(void)
{
    if (vl.state == PLAYING) {
        int d = vl_sample();
        if (d < 0) { vl.state = DONE; d = 0; }
        set_pwm((unsigned)d);
        return 1;
    }
    return vl.state == DONE;
}

/* tone producer: 1 = this playlist item is a word (started), 0 = ordinary tone */
int vl_tone(void)
{
    uint32_t f = FREQ_VAR;
    if (f >= CLIP_FREQ && f - CLIP_FREQ < NWORDS) {
        vl_start(f - CLIP_FREQ);
        return 1;
    }
    vl.state = IDLE;
    return 0;
}
#endif

static int phrase_for(uint32_t str)
{
    int lo = 0, hi = NSTR - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (vd_str_addr[mid] == str) return mid;
        if (vd_str_addr[mid] < str) lo = mid + 1; else hi = mid - 1;
    }
    return -1;
}

static int add_number(uint16_t *ids, int n, uint32_t v)
{
    if (v >= 1000) { n = add_number(ids, n, v / 1000); ids[n++] = W_THOUSAND; v %= 1000; if (!v) return n; }
    if (v >= 100) { ids[n++] = vd_num_word[v / 100]; ids[n++] = W_HUNDRED; v %= 100; if (!v) return n; }
    if (v >= 20) { ids[n++] = vd_tens_word[v / 10]; v %= 10; if (!v) return n; }
    ids[n++] = vd_num_word[v];
    return n;
}

static int valid_list(uintptr_t p)
{
#ifdef HOST
    return p != 0;
#endif
    return (p & 3) == 0 && ((p >= IMAGE_START && p < IMAGE_END) || (p >= 0x20000000 && p < 0x20030000));
}

/* playlist for the highlighted row `cur` of list `list` (count `cnt`), whose text is
 * list[idx]: its words, or the row number */
const uint16_t *vl_pick_for(uintptr_t list, uint32_t cnt, uint32_t cur, uint32_t idx)
{
    uint16_t ids[MAX_WORDS];
    int n = 0, ph = -1;
    if (cur < cnt && cnt <= 64 && idx < 64 && valid_list(list)) ph = phrase_for(((const uint32_t *)list)[idx]);
    if (ph >= 0) {
        const uint16_t *w = &vd_phrase[vd_str_off[ph]];
        n = w[0] > MAX_WORDS ? MAX_WORDS : w[0];
        for (int i = 0; i < n; i++) ids[i] = w[1 + i];
    } else {
        n = add_number(ids, 0, cur < 999 ? cur + 1 : 999);   /* <= 4 words */
    }
    vl.pl_sel ^= 1;
    uint16_t *pl = vl.pl[vl.pl_sel & 1];
    int o = 0;
    pl[o++] = 0; pl[o++] = LEAD;
    for (int i = 0; i < n; i++) {
        pl[o++] = (uint16_t)(CLIP_FREQ + ids[i]); pl[o++] = (uint16_t)vl_ticks(ids[i]);
        pl[o++] = 0; pl[o++] = WORD_GAP;
    }
    pl[o++] = 0xFFFF; pl[o++] = 0;
    return pl;
}

#ifndef HOST
/* Row index -> list index, mirroring the stock row drawer FUN_0807dc4c (which draws
 * list[that index] for row == cursor): some menus hide rows, e.g. the main menu skips
 * Zone/Scan/Satellite/Digi Moni depending on settings. The helpers are pure getters. */
typedef uint32_t (*remap1)(uint32_t);
typedef uint32_t (*remap2)(uint32_t, uint32_t);
#define STOCK(t, a) ((t)((a) | 1))
#define U8(a) (*(volatile uint8_t *)(a))
#define U32(a) (*(volatile uint32_t *)(a))

static uint32_t item_index(uint32_t cur)
{
    uint32_t page = U32(0x20004248), sub = U32(0x2000424c) & 0xff, depth = U8(0x200097e9);
    uint32_t sel = U8(0x20004254), idx, v;
    uint32_t mode = U8(U32(0x20008780) + 8) & 3;
    remap1 is_list = STOCK(remap1, 0x08047334), kind = STOCK(remap1, 0x08049bf8);
    remap1 group = STOCK(remap1, 0x080cb448);
    cur &= 0xff;
    if (page == 6 && (is_list(sub) & 0xff) == 0 && (kind(U8(0x20004250)) & 0xff) == 2 &&
        (v = group(sel) & 0xff, v > 0 && v < 0xb) && depth == 5)
        idx = STOCK(remap2, 0x080bbbe4)(cur, group(sel) & 0xff) & 0xff;
    else if (page == 2 && U32(0x2000424c) == 0 && depth == 6)
        idx = STOCK(remap1, 0x08046a40)(cur) & 0xff;
    else if (page == 0xd && depth == 2)
        idx = STOCK(remap1, 0x080c5a94)(cur) & 0xff;
    else if (depth == 1)
        idx = STOCK(remap1, 0x080bb9dc)(cur) & 0xff;
    else
        idx = cur;
    if (page == 6 && (is_list(sub) & 0xff) == 1 && mode != 0 && U8(0x200149ee) == 0 && depth == 3 &&
        U8(0x20014a36) == 0) {
        if (mode == 1) { if (idx > 0x11) idx++; }
        else if (mode == 3) { if (idx > 0x15) idx++; }
        else if (idx > 0x16) idx++;
    }
    return idx;
}

const uint16_t *vl_pick(uint32_t cur) { return vl_pick_for(CURSOR_LIST, CURSOR_CNT, cur, item_index(cur)); }
#endif
