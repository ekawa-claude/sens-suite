"""
Sens Finder 2.0 — self-contained sensitivity finder & aim benchmark.

What changed vs v1 (main.py):
  * No console prompts, no presets, no offline analysis. Everything happens
    in-app: menu -> play -> verdict on screen.
  * FIND MY SENS is adaptive: a 5-level wide sweep picks the best region,
    then a matched-flick fine phase around it decides the winner and shows
    "your Marvel Rivals sens = X" directly.
  * BENCHMARK: 3 scored rounds at your current sens -> score 0-1000 with a
    progress graph across sessions (history2.json keeps summaries only).
  * Settings (DPI / cm-per-360 at sens 1.0 / current sens) live in config2.json.

New in 2.2:
  * TRACKING: strafing target you have to stay on; score = time-on-target.
  * A/B DUEL: two sens values, blind matched rounds, bootstrap confidence.
  * Benchmark verdict shows a per-flick-distance breakdown and a hit map
    (where your clicks landed relative to the target center).
  * Synthesized sound effects (M in menu toggles).

New in 2.3:
  * FIND TRACKING SENS: the adaptive wide->fine sweep, but on the strafing
    tracking target. Fine passes replay an identical target path across all
    levels (matched motion seeds); verdict = best sens for tracking.
  * On-target audio in tracking rounds: warm low pad while you hold the
    target (pitch steps up with hold time) + a soft blip on acquire.

The proven v1 engine (raw relative input, camera, renderer with the
Liang-Barsky clip fix, per-target metric tracker) is imported from main.py.

Run:  python sens2.py            (or sens2.bat)
      python sens2.py --selftest (headless analysis check, no window)
      python sens2.py --shot <menu|info|round|bench|verdict|settings|track|
                              duel|trackverdict> [out.png]
                                 (headless UI screenshot for design review)
"""

import os
import sys
import json
import math
import time
import random
import statistics
from array import array
from datetime import datetime, timezone

os.environ.setdefault("SDL_MOUSE_RELATIVE_MODE_WARP", "0")

import pygame

from main import (
    Camera, Renderer, TargetTracker,
    dir_from_yawpitch, angle_between_deg,
    PITCH_LIMIT_DEG, TARGET_ANGULAR_DIAMETER_DEG, FPS_CAP,
)

APP2_VERSION = "2.4.0"


class Renderer2(Renderer):
    """v1 renderer with a fixed target glow: v1 sized the glow surface smaller
    than the largest glow circle, so the circles got clipped into a visible
    square on dark backgrounds."""

    def _blit_target(self, sx, sy, rad, spawn_anim):
        sx, sy = int(sx), int(sy)
        if spawn_anim > 0:
            ping_r = int(rad + rad * 3 * spawn_anim)
            a = max(0, int(180 * (1 - spawn_anim)))
            ping = pygame.Surface((ping_r * 2 + 4, ping_r * 2 + 4),
                                  pygame.SRCALPHA)
            pygame.draw.circle(ping, (255, 120, 120, a),
                               (ping_r + 2, ping_r + 2), ping_r, 3)
            self.s.blit(ping, (sx - ping_r - 2, sy - ping_r - 2))
        gr = int(rad * 2.8) + 2      # >= largest glow circle radius
        glow = pygame.Surface((gr * 2, gr * 2), pygame.SRCALPHA)
        for i in range(4, 0, -1):
            a = 26 * i // 4
            pygame.draw.circle(glow, (255, 120, 120, a), (gr, gr),
                               int(rad + i * rad * 0.4))
        self.s.blit(glow, (sx - gr, sy - gr))
        pygame.draw.circle(self.s, (255, 86, 86), (sx, sy), int(rad))
        pygame.draw.circle(self.s, (255, 180, 180), (sx, sy), int(rad), 2)
        pygame.draw.circle(self.s, (255, 255, 255), (sx, sy),
                           max(1, int(rad * 0.18)))
DIR = os.path.dirname(os.path.abspath(__file__))
if not getattr(sys, "frozen", False):
    _root = os.path.dirname(DIR)
    if _root not in sys.path:
        sys.path.insert(0, _root)
import suitepaths

DATA_DIR = suitepaths.sensfinder_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config2.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history2.json")

DEFAULT_CONFIG = {
    "dpi": 1600,
    # cm/360 at Marvel Rivals sens 1.0 — measure your own (see README);
    # 38.3 is a typical value at DPI 1600 with no driver accel.
    # sens <-> cm/360:  cm360(s) = sens1_cm360 / s
    "sens1_cm360": 38.3,
    "current_sens": 1.0,
    "targets_per_round": 12,
    "sound": True,
}

FLICK_BUCKETS = {
    "small":  (5.0, 15.0),
    "medium": (20.0, 40.0),
    "large":  (50.0, 90.0),
}

# Find-mode design: wide sweep multipliers around current feel, then a fine
# bracket around the wide winner tested with matched flicks.
WIDE_MULTIPLIERS = [0.55, 0.75, 1.0, 1.35, 1.8]
FINE_BRACKET = 1.25          # fine levels = winner / 1.25, winner, winner * 1.25
FINE_PASSES = 2
PLATEAU_TOL = 0.05           # levels within 5% of best median TTK = plateau

# A/B duel: each pass runs both sens values on identical flicks, blind order.
DUEL_PASSES = 3

# Tracking mode
TRACK_ROUNDS = 3
TRACK_ROUND_S = 20.0

# Find tracking sens: same wide -> fine ladder as FIND MY SENS, but with
# tracking rounds. Winner = highest time-on-target; plateau ties go to the
# level with the lowest mean angular error. Fine passes replay an identical
# target path (shared motion seed) across all levels — matched comparison.
TRACKFIND_ROUND_S = 15.0
TRACKFIND_WARMUP_S = 10.0
TRACKFIND_FINE_PASSES = 2
TRACKFIND_PLATEAU_TOL = 0.05
TRACK_SPEED_RANGE = (9.0, 20.0)      # target strafe speed, deg/s
TRACK_SEG_RANGE = (0.45, 1.30)       # seconds between direction decisions
TRACK_PAUSE_CHANCE = 0.15            # chance a segment is a brief stop

# ---- palette (dark "aim lab") ---------------------------------------------- #
BG        = (8, 10, 18)
PANEL     = (15, 19, 32)
PANEL_HI  = (22, 27, 44)
BORDER    = (255, 255, 255, 20)
ACCENT    = (94, 234, 182)     # mint
AMBER     = (255, 196, 92)
RED       = (255, 107, 107)
TXT       = (228, 233, 245)
TXT_DIM   = (140, 150, 174)
TXT_FAINT = (86, 94, 118)
BAR_DIM   = (62, 72, 104)


# --------------------------------------------------------------------------- #
# Config / history
# --------------------------------------------------------------------------- #

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    return cfg


def sens_for_cm360(cfg, cm360):
    return cfg["sens1_cm360"] / cm360


def cm360_for_sens(cfg, sens):
    return cfg["sens1_cm360"] / sens


def deg_per_count(cm360, dpi):
    return 360.0 / (dpi * cm360 / 2.54)


# --------------------------------------------------------------------------- #
# Flicks & scoring
# --------------------------------------------------------------------------- #

def gen_flicks(rng, n):
    """n flick specs: (bucket, signed h deg, signed v deg), balanced L/R."""
    per, extra = divmod(n, 3)
    mix = (["small"] * per + ["medium"] * per + ["large"] * per +
           ["small", "medium", "large"][:extra])
    rng.shuffle(mix)
    flicks = []
    sign = 1 if rng.random() < 0.5 else -1
    for bucket in mix:
        lo, hi = FLICK_BUCKETS[bucket]
        mag = rng.uniform(lo, hi)
        h = sign * mag
        sign = -sign
        v = rng.uniform(-1.0, 1.0) * (0.15 * mag)
        flicks.append((bucket, h, v))
    return flicks


def target_score(t):
    """0..100 per target: fast clean hit ~90+, slow/missy drops toward 0."""
    s = 100.0 * math.exp(-t["time_to_hit_ms"] / 900.0)
    s -= 8.0 * t["miss_clicks"]
    return max(0.0, s)


def round_score(targets):
    """0..1000 composite for motivation/progress tracking."""
    if not targets:
        return 0
    return int(round(10.0 * statistics.fmean(target_score(t) for t in targets)))


def level_stats(targets):
    """Pool per-target records for one sensitivity level into summary stats."""
    ttks = [t["time_to_hit_ms"] for t in targets]
    misses = sum(t["miss_clicks"] for t in targets)
    hits = len(targets)
    return {
        "n": hits,
        "ttk_median": statistics.median(ttks),
        "ttk_mean": statistics.fmean(ttks),
        "accuracy": hits / (hits + misses) if hits + misses else 0.0,
        "overshoot": statistics.fmean(t["overshoot_events"] for t in targets),
        "microcorr": statistics.fmean(t["n_microcorrections"] for t in targets),
        "path_eff": statistics.fmean(t["path_efficiency"] for t in targets),
        "score": round_score(targets),
    }


def bucket_stats(targets):
    """{bucket: {n, ttk_median, accuracy}} for small/medium/large flicks."""
    out = {}
    for b in FLICK_BUCKETS:
        ts = [t for t in targets if t.get("bucket") == b]
        if not ts:
            continue
        misses = sum(t["miss_clicks"] for t in ts)
        out[b] = {
            "n": len(ts),
            "ttk_median": statistics.median(t["time_to_hit_ms"] for t in ts),
            "accuracy": len(ts) / (len(ts) + misses) if ts else 0.0,
        }
    return out


def bootstrap_faster_prob(ttks_a, ttks_b, iters=2000, seed=1):
    """P(median TTK of A < median TTK of B) under bootstrap resampling."""
    rng = random.Random(seed)
    wins = 0.0
    for _ in range(iters):
        ma = statistics.median(rng.choices(ttks_a, k=len(ttks_a)))
        mb = statistics.median(rng.choices(ttks_b, k=len(ttks_b)))
        if ma < mb:
            wins += 1.0
        elif ma == mb:
            wins += 0.5
    return wins / iters


def track_score(on_pct):
    """0..1000 from time-on-target fraction; slightly convex to reward
    the hard last percents."""
    return int(round(1000.0 * max(0.0, min(1.0, on_pct)) ** 1.15))


class StrafeMotion:
    """Enemy-like strafing: yaw segments with random speed/direction flips
    and occasional brief stops, plus a slow pitch bob."""

    def __init__(self, rng, yaw):
        self.rng = rng
        self.yaw = yaw
        self.t = 0.0
        self.dir = 1 if rng.random() < 0.5 else -1
        self.speed = 0.0
        self.seg_left = 0.0
        self.pitch_phase = rng.uniform(0.0, math.tau)
        self._new_segment()

    def _new_segment(self):
        r = self.rng
        if r.random() < TRACK_PAUSE_CHANCE:
            self.speed = 0.0
            self.seg_left = r.uniform(0.15, 0.40)
            return
        if r.random() < 0.75:
            self.dir = -self.dir
        self.speed = r.uniform(*TRACK_SPEED_RANGE)
        self.seg_left = r.uniform(*TRACK_SEG_RANGE)

    def step(self, dt):
        """Advance dt seconds -> (yaw_deg, pitch_deg)."""
        self.seg_left -= dt
        if self.seg_left <= 0.0:
            self._new_segment()
        self.yaw += self.dir * self.speed * dt
        self.t += dt
        pitch = -2.0 + 2.2 * math.sin(self.t * 0.9 + self.pitch_phase)
        return self.yaw, pitch


def analyze_levels(by_level):
    """by_level: {cm360: [target records]} -> (stats, plateau_levels, winner).

    Winner = fastest median TTK; if several levels sit within PLATEAU_TOL of
    the best (a plateau), prefer the one with the cleanest control
    (fewest overshoots + microcorrections).
    """
    stats = {cm: level_stats(ts) for cm, ts in by_level.items() if ts}
    best_ttk = min(s["ttk_median"] for s in stats.values())
    plateau = sorted(cm for cm, s in stats.items()
                     if s["ttk_median"] <= best_ttk * (1.0 + PLATEAU_TOL))
    winner = min(plateau,
                 key=lambda cm: stats[cm]["overshoot"] + stats[cm]["microcorr"])
    return stats, plateau, winner


def track_level_stats(rounds):
    """Pool tracking-round results for one sensitivity level."""
    on = statistics.fmean(r["on_pct"] for r in rounds)
    return {
        "n": len(rounds),
        "on_pct": on,
        "mean_err": statistics.fmean(r["mean_err"] for r in rounds),
        "best_streak": max(r["best_streak"] for r in rounds),
        "score": track_score(on),
    }


def speed_summary(samples):
    """Input-speed profile of movement frames (counts/ms).

    Feeds RawAccel Studio's curve suggestion: the flick profile's median and
    the tracking profile's p90 bracket where the accel curve should ramp."""
    s = sorted(v for v in samples if v > 0.05)
    if len(s) < 50:
        return None
    return {"med": round(s[len(s) // 2], 2),
            "p90": round(s[int(len(s) * 0.9)], 2),
            "n": len(s)}


def analyze_track_levels(by_level):
    """by_level: {cm360: [round results]} -> (stats, plateau_levels, winner).

    Winner = highest time-on-target; levels within TRACKFIND_PLATEAU_TOL of
    the best form a plateau, resolved by the lowest mean angular error
    (the steadiest hold wins).
    """
    stats = {cm: track_level_stats(rs) for cm, rs in by_level.items() if rs}
    best_on = max(s["on_pct"] for s in stats.values())
    plateau = sorted(cm for cm, s in stats.items()
                     if s["on_pct"] >= best_on * (1.0 - TRACKFIND_PLATEAU_TOL))
    winner = min(plateau, key=lambda cm: stats[cm]["mean_err"])
    return stats, plateau, winner


# --------------------------------------------------------------------------- #
# UI toolkit
# --------------------------------------------------------------------------- #

class UI:
    """Fonts, panels, chips, key hints — everything scaled to screen height."""

    def __init__(self, screen):
        self.s = screen
        self.W, self.H = screen.get_size()
        self.cx, self.cy = self.W // 2, self.H // 2
        self.k = self.H / 1080.0
        pygame.font.init()
        z = lambda px: max(10, int(px * self.k))
        self.f_display = pygame.font.SysFont("Segoe UI", z(74), bold=True)
        self.f_title   = pygame.font.SysFont("Segoe UI", z(42), bold=True)
        self.f_h2      = pygame.font.SysFont("Segoe UI", z(27), bold=True)
        self.f_body    = pygame.font.SysFont("Segoe UI", z(21))
        self.f_small   = pygame.font.SysFont("Segoe UI", z(17))
        self.f_tiny    = pygame.font.SysFont("Segoe UI", z(14))
        self.f_mono    = pygame.font.SysFont("Consolas", z(19))
        self.f_num     = pygame.font.SysFont("Consolas", z(30), bold=True)
        self.f_hero    = pygame.font.SysFont("Consolas", z(132), bold=True)
        self._vignette = self._make_vignette()

    def px(self, v):
        return int(v * self.k)

    def _make_vignette(self):
        w, h = 192, 108
        small = pygame.Surface((w, h), pygame.SRCALPHA)
        for y in range(h):
            for x in range(w):
                nx = (x / w - 0.5) * 2.0
                ny = (y / h - 0.5) * 2.0
                d = math.hypot(nx, ny) / 1.35
                a = max(0.0, min(1.0, (d - 0.45) / 0.75))
                small.set_at((x, y), (0, 0, 0, int(215 * a ** 1.6)))
        return pygame.transform.smoothscale(small, (self.W, self.H))

    # ---- primitives ---- #

    def rrect(self, rect, color, radius=16, border=None):
        rect = pygame.Rect(rect)
        tmp = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(tmp, color, tmp.get_rect(),
                         border_radius=self.px(radius))
        if border:
            pygame.draw.rect(tmp, border, tmp.get_rect(), width=1,
                             border_radius=self.px(radius))
        self.s.blit(tmp, rect.topleft)

    def text(self, font, string, color, center=None, topleft=None,
             topright=None):
        surf = font.render(string, True, color)
        rect = surf.get_rect()
        if center:
            rect.center = center
        elif topleft:
            rect.topleft = topleft
        elif topright:
            rect.topright = topright
        self.s.blit(surf, rect)
        return rect

    def caps(self, font, string, color, center=None, topleft=None):
        return self.text(font, " ".join(string.upper()), color,
                         center=center, topleft=topleft)

    def vignette(self):
        self.s.blit(self._vignette, (0, 0))

    def dim(self, alpha=110):
        ov = pygame.Surface((self.W, self.H), pygame.SRCALPHA)
        ov.fill((*BG, alpha))
        self.s.blit(ov, (0, 0))

    # ---- components ---- #

    def card(self, w, h, y=None):
        """Centered panel card; returns its Rect."""
        rect = pygame.Rect(0, 0, self.px(w), self.px(h))
        rect.centerx = self.cx
        rect.centery = self.cy if y is None else self.px(y) + rect.h // 2
        self.rrect(rect, (*PANEL, 236), radius=22, border=BORDER)
        return rect

    def chip_row(self, cy, chips):
        """chips: [(label, value, color|None)]. Centered row of stat chips."""
        cw, ch, gap = self.px(190), self.px(84), self.px(14)
        total = len(chips) * cw + (len(chips) - 1) * gap
        x = self.cx - total // 2
        for label, value, col in chips:
            rect = pygame.Rect(x, cy - ch // 2, cw, ch)
            self.rrect(rect, (*PANEL_HI, 235), radius=14, border=BORDER)
            self.text(self.f_num, value, col or TXT,
                      center=(rect.centerx, rect.centery - self.px(12)))
            self.caps(self.f_tiny, label, TXT_FAINT,
                      center=(rect.centerx, rect.centery + self.px(24)))
            x += cw + gap

    def keyhints(self, cy, pairs):
        """pairs: [(key, action)]. Centered row of keycap + label hints."""
        gap_in, gap_out = self.px(10), self.px(36)
        pad = self.px(9)
        items = []
        total = 0
        for key, action in pairs:
            kw = self.f_mono.size(key)[0] + pad * 2
            aw = self.f_small.size(action)[0]
            items.append((key, action, kw, aw))
            total += kw + gap_in + aw + gap_out
        total -= gap_out
        x = self.cx - total // 2
        for key, action, kw, aw in items:
            kh = self.px(30)
            rect = pygame.Rect(x, cy - kh // 2, kw, kh)
            self.rrect(rect, (*PANEL_HI, 240), radius=7,
                       border=(255, 255, 255, 36))
            self.text(self.f_mono, key, TXT_DIM, center=rect.center)
            self.text(self.f_small, action, TXT_FAINT,
                      center=(rect.right + gap_in + aw // 2, cy))
            x += kw + gap_in + aw + gap_out

    def segment_bar(self, cx, cy, w, n, done, color=ACCENT):
        gap = self.px(3)
        h = self.px(5)
        seg = (w - gap * (n - 1)) / n
        x = cx - w // 2
        for i in range(n):
            c = color if i < done else (255, 255, 255, 34)
            self.rrect((int(x), cy, max(2, int(seg)), h), c
                       if len(c) == 4 else (*c, 255), radius=3)
            x += seg + gap

    def hbar(self, x, y, w, h, frac, color):
        self.rrect((x, y, w, h), (255, 255, 255, 18), radius=6)
        if frac > 0.01:
            self.rrect((x, y, max(self.px(8), int(w * frac)), h),
                       (*color, 255), radius=6)


# --------------------------------------------------------------------------- #
# Animated menu backdrop: slow camera drift over the world grid
# --------------------------------------------------------------------------- #

class Backdrop:
    def __init__(self, renderer, ui):
        self.r = renderer
        self.ui = ui
        self.cam = Camera()
        self.t = 0.0

    def draw(self, dt):
        self.t += dt
        self.cam.yaw = self.t * 2.4
        self.cam.pitch = -7.0 + 2.5 * math.sin(self.t * 0.23)
        self.r.draw_world(self.cam)
        self.ui.dim(126)
        self.ui.vignette()


# --------------------------------------------------------------------------- #
# Sound: tiny synthesized cues (no assets, no numpy)
# --------------------------------------------------------------------------- #

class Sound:
    RATE = 44100

    def __init__(self, enabled=True):
        self.ok = False
        self.enabled = enabled
        try:
            # pygame.init() has already opened the mixer at its defaults,
            # and a second mixer.init is a silent no-op — buffers made for
            # another rate then play at the wrong speed (the "whistle"
            # bug). Reopen with the exact spec; allowedchanges=0 makes SDL
            # convert rather than deviate. Buffer 1024: 512 was small
            # enough to crackle under load, audible in sustained loops.
            pygame.mixer.quit()
            pygame.mixer.init(self.RATE, -16, 1, 1024, allowedchanges=0)
            self.s_hit_lo = self._tone(560, 55, vol=0.30)
            self.s_hit_md = self._tone(780, 55, vol=0.32)
            self.s_hit_hi = self._tone(1040, 60, vol=0.34)
            self.s_miss = self._tone(150, 80, vol=0.35, shape="square")
            self.s_tick = self._tone(880, 22, vol=0.12)
            self.s_pb = self._chord([(660, 90), (880, 90), (1320, 160)],
                                    vol=0.30)
            # tracking feedback: warm low on-target pad, pitch steps up the
            # longer you hold (C3/E3/G3 = low major triad), + acquire blip
            self.s_lock = self._tone(520, 60, vol=0.10)
            self.track_loops = [self._loop_pad(f, vol=0.10)
                                for f in (130.8, 164.8, 196.0)]
            pygame.mixer.set_reserved(2)
            self.ch_track = [pygame.mixer.Channel(0), pygame.mixer.Channel(1)]
            self._track_i = 0
            self._track_cur = None
            self.ok = True
        except pygame.error:
            pass

    def _samples(self, freq, ms, vol, shape):
        n = int(self.RATE * ms / 1000.0)
        attack = max(1, int(self.RATE * 0.002))
        out = array("h")
        for i in range(n):
            v = math.sin(math.tau * freq * i / self.RATE)
            if shape == "square":
                v = 0.6 if v > 0 else -0.6
            env = min(1.0, i / attack) * (1.0 - i / n) ** 1.5
            out.append(int(32000 * vol * env * v))
        return out

    def _tone(self, freq, ms, vol=0.3, shape="sine"):
        return pygame.mixer.Sound(
            buffer=self._samples(freq, ms, vol, shape).tobytes())

    def _loop_pad(self, freq, vol, loop_s=3.0, attack_s=0.12):
        """Seamless warm pad loop: a detuned pair (gentle ~2 Hz beat) plus
        soft octave/twelfth harmonics — hums instead of whistling. All
        components are whole periods over the loop, so it loops clean.

        The attack swell is baked into the buffer: SDL's fade_ms ramps
        channel volume once per audio callback (~23 ms), which staircases
        audibly on a sustained tone ("static" at hum onset). A long loop
        makes the baked re-attack rare (every loop_s of continuous hold)."""
        n = int(self.RATE * loop_s)
        k = max(1, int(round(freq * loop_s)))
        beat = max(1, int(round(2.0 * loop_s)))     # ~2 Hz detune beat
        parts = [(k, 1.0), (k + beat, 0.9), (2 * k, 0.35), (3 * k, 0.15)]
        norm = sum(a for _, a in parts)
        n_a = max(1, int(self.RATE * attack_s))
        out = array("h")
        for i in range(n):
            v = sum(a * math.sin(math.tau * kk * i / n) for kk, a in parts)
            env = 0.5 - 0.5 * math.cos(math.pi * i / n_a) if i < n_a else 1.0
            out.append(int(32000 * vol * env * v / norm))
        return pygame.mixer.Sound(buffer=out.tobytes())

    def _chord(self, notes, vol=0.3):
        out = array("h")
        for freq, ms in notes:
            out.extend(self._samples(freq, ms, vol, "sine"))
        return pygame.mixer.Sound(buffer=out.tobytes())

    def _play(self, snd):
        if self.ok and self.enabled:
            snd.play()

    def hit(self, score):
        self._play(self.s_hit_hi if score >= 75
                   else self.s_hit_md if score >= 45 else self.s_hit_lo)

    def miss(self):
        self._play(self.s_miss)

    def tick(self):
        self._play(self.s_tick)

    def pb(self):
        self._play(self.s_pb)

    def lock(self):
        self._play(self.s_lock)

    def track_on(self, hold_s):
        """Keep the on-target hum running; pitch steps up with hold time.
        Level changes overlap two reserved channels: the new loop swells
        in via its baked attack while the old one fades out. No fade_ms
        anywhere — SDL fade-in staircases audibly (see _loop_pad)."""
        if not (self.ok and self.enabled):
            return
        snd = self.track_loops[0 if hold_s < 1.0
                               else 1 if hold_s < 2.5 else 2]
        if self._track_cur is snd and self.ch_track[self._track_i].get_busy():
            return
        old = self.ch_track[self._track_i] if self._track_cur else None
        self._track_i = 1 - self._track_i
        self.ch_track[self._track_i].play(snd, loops=-1)
        if old is not None:
            old.fadeout(120)
        self._track_cur = snd

    def track_off(self):
        if self.ok and self._track_cur is not None:
            for ch in self.ch_track:
                ch.fadeout(80)
            self._track_cur = None


# --------------------------------------------------------------------------- #
# In-round hit effects (rings + floating score popups)
# --------------------------------------------------------------------------- #

class FX:
    def __init__(self, ui):
        self.ui = ui
        self.items = []

    def hit(self, score):
        col = ACCENT if score >= 75 else (AMBER if score >= 45 else RED)
        self.items.append({"kind": "ring", "age": 0.0, "life": 0.45})
        self.items.append({"kind": "pop", "age": 0.0, "life": 0.8,
                           "text": f"+{score:.0f}", "col": col})

    def miss(self):
        self.items.append({"kind": "pop", "age": 0.0, "life": 0.5,
                           "text": "miss", "col": RED})

    def update_draw(self, dt):
        ui = self.ui
        alive = []
        for it in self.items:
            it["age"] += dt
            t = it["age"] / it["life"]
            if t >= 1.0:
                continue
            alive.append(it)
            if it["kind"] == "ring":
                r = ui.px(22 + 80 * t)
                a = int(170 * (1 - t))
                ring = pygame.Surface((r * 2 + 4, r * 2 + 4), pygame.SRCALPHA)
                pygame.draw.circle(ring, (*ACCENT, a), (r + 2, r + 2), r,
                                   max(1, ui.px(3)))
                ui.s.blit(ring, (ui.cx - r - 2, ui.cy - r - 2))
            else:
                a = int(235 * (1 - t * t))
                y = ui.cy - ui.px(56) - ui.px(46) * t
                surf = ui.f_num.render(it["text"], True, it["col"])
                surf.set_alpha(a)
                rect = surf.get_rect(center=(ui.cx + ui.px(52), int(y)))
                ui.s.blit(surf, rect)
        self.items = alive


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

class App:
    def __init__(self, headless_size=None):
        self.first_run = not os.path.exists(CONFIG_PATH)
        self.cfg = load_config()
        self.history = load_json(HISTORY_PATH, [])

        pygame.init()
        if headless_size:
            self.screen = pygame.display.set_mode(headless_size)
        else:
            info = pygame.display.Info()
            try:
                self.screen = pygame.display.set_mode(
                    (info.current_w, info.current_h),
                    pygame.FULLSCREEN | pygame.DOUBLEBUF, vsync=0)
            except pygame.error:
                self.screen = pygame.display.set_mode(
                    (info.current_w, info.current_h), pygame.FULLSCREEN)
        pygame.display.set_caption("Sens Finder 2.0")

        try:
            pygame.event.set_grab(True)
            pygame.mouse.set_visible(False)
            if hasattr(pygame.mouse, "set_relative_mode"):
                pygame.mouse.set_relative_mode(True)
        except Exception:
            pass

        self.r = Renderer2(self.screen)
        self.ui = UI(self.screen)
        self.backdrop = Backdrop(self.r, self.ui)
        self.clock = pygame.time.Clock()
        self.snd = Sound(self.cfg.get("sound", True))
        self.last_round_points = []   # (h_deg, v_deg, hit) clicks of last round
        self.speed_samples = []       # per-frame input speed (counts/ms), see speed_summary

    # ---------------- persistence ---------------- #

    def save_config(self):
        save_json(CONFIG_PATH, self.cfg)

    def add_history(self, entry):
        entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.history.append(entry)
        save_json(HISTORY_PATH, self.history)

    # ---------------- generic info screen ---------------- #

    def draw_info(self, dt, title, subtitle, lines, chips=None,
                  hints=(("SPACE", "continue"), ("Q", "back"))):
        ui = self.ui
        self.backdrop.draw(dt)
        n_lines = len(lines)
        h = 250 + (110 if chips else 0) + 34 * n_lines
        card = ui.card(880, h)
        y = card.top + ui.px(56)
        ui.text(ui.f_title, title, TXT, center=(ui.cx, y))
        y += ui.px(52)
        if subtitle:
            ui.caps(ui.f_small, subtitle, ACCENT, center=(ui.cx, y))
        y += ui.px(46)
        if chips:
            ui.chip_row(y + ui.px(28), chips)
            y += ui.px(96)
        for ln in lines:
            ui.text(ui.f_body, ln, TXT_DIM, center=(ui.cx, y))
            y += ui.px(34)
        ui.keyhints(card.bottom - ui.px(48), hints)

    def info_screen(self, title, subtitle, lines, chips=None,
                    hints=(("SPACE", "continue"), ("Q", "back"))):
        """Returns True on SPACE, False on Q/quit."""
        while True:
            dt = self.clock.tick(60) / 1000.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return False
                if ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_SPACE:
                        pygame.event.clear(pygame.MOUSEMOTION)
                        return True
                    if ev.key in (pygame.K_q, pygame.K_ESCAPE):
                        return False
            self.draw_info(dt, title, subtitle, lines, chips, hints)
            pygame.display.flip()

    # ---------------- round runner ---------------- #

    def draw_round_frame(self, cam, target_dir, anim, hud, fx, dt,
                         hit_flash=0.0, paused=False):
        ui = self.ui
        self.r.draw_world(cam)
        if target_dir is not None:
            self.r.draw_target(cam, target_dir, spawn_anim=anim)
        self.r.draw_crosshair(flash=hit_flash)
        fx.update_draw(dt)

        # top-center pill: title / target progress / segment bar
        pw, ph = ui.px(360), ui.px(86)
        pill = pygame.Rect(ui.cx - pw // 2, ui.px(22), pw, ph)
        ui.rrect(pill, (*PANEL, 200), radius=16, border=BORDER)
        ui.caps(ui.f_tiny, hud["title"], TXT_FAINT,
                center=(ui.cx, pill.top + ui.px(20)))
        base = f"target {hud['idx'] + 1} / {hud['n']}"
        if hud["misses"]:
            miss = f"   ·   {hud['misses']} miss"
            bw = ui.f_small.size(base)[0]
            mw = ui.f_small.size(miss)[0]
            x = ui.cx - (bw + mw) // 2
            cy = pill.top + ui.px(44)
            ui.text(ui.f_small, base, TXT_DIM, center=(x + bw // 2, cy))
            ui.text(ui.f_small, miss, RED, center=(x + bw + mw // 2, cy))
        else:
            ui.text(ui.f_small, base, TXT_DIM,
                    center=(ui.cx, pill.top + ui.px(44)))
        ui.segment_bar(ui.cx, pill.top + ui.px(64), ui.px(300),
                       hud["n"], hud["idx"])

        ui.text(ui.f_tiny, f"{hud['fps']:.0f} fps", TXT_FAINT,
                topright=(ui.W - ui.px(24), ui.H - ui.px(34)))
        ui.text(ui.f_tiny, "ESC pause", TXT_FAINT,
                topleft=(ui.px(24), ui.H - ui.px(34)))

        if paused:
            ui.dim(150)
            ui.vignette()
            card = ui.card(560, 240)
            ui.text(ui.f_title, "Paused", TXT,
                    center=(ui.cx, card.top + ui.px(74)))
            ui.keyhints(card.bottom - ui.px(58),
                        [("ESC", "resume"), ("Q", "abort round")])

    def run_round(self, cm360, flicks, header):
        """One round at a given cm/360. Returns list of target records | None."""
        cfg = self.cfg
        cam = Camera()
        dpc = deg_per_count(cm360, cfg["dpi"])
        n_targets = len(flicks)
        records = []
        points = []
        self.last_round_points = points
        fx = FX(self.ui)

        def spawn(idx):
            bucket, h, v = flicks[idx]
            t_yaw = cam.yaw + h
            t_pitch = max(-PITCH_LIMIT_DEG + 1,
                          min(PITCH_LIMIT_DEG - 1, cam.pitch + v))
            tdir = dir_from_yawpitch(t_yaw, t_pitch)
            now = time.perf_counter_ns()
            return tdir, TargetTracker(tdir, cam.forward(), now, (h, v), bucket)

        idx = 0
        target_dir, tracker = spawn(0)
        miss_clicks = 0
        spawn_anim_t = time.perf_counter()
        hit_flash = 0.0
        paused = False
        prev = time.perf_counter_ns()

        pygame.event.clear(pygame.MOUSEMOTION)

        while True:
            now = time.perf_counter_ns()
            dt_s = (now - prev) / 1e9
            prev = now
            clicks = []
            frame_dist = 0.0

            for ev in pygame.event.get():
                ev_t = time.perf_counter_ns()
                if ev.type == pygame.QUIT:
                    return None
                elif ev.type == pygame.MOUSEMOTION:
                    dx, dy = ev.rel
                    if (dx or dy) and not paused:
                        cam.apply_counts(dx, dy, dpc)
                        frame_dist += math.hypot(dx, dy)
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    if not paused:
                        clicks.append(ev_t)
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_ESCAPE:
                        paused = not paused
                        prev = time.perf_counter_ns()
                    elif paused and ev.key == pygame.K_q:
                        return None

            if frame_dist and dt_s > 0 and not paused:
                self.speed_samples.append(frame_dist / (dt_s * 1000.0))

            if not paused:
                tracker.update(cam.forward(), now, dt_s)
                for ct in clicks:
                    ang = angle_between_deg(cam.forward(), target_dir)
                    off = tracker._offset2d(cam.forward())
                    if ang <= TARGET_ANGULAR_DIAMETER_DEG / 2.0:
                        points.append((off[0], off[1], True))
                        rec = tracker.finalize(ct, miss_clicks)
                        records.append(rec)
                        sc = target_score(rec)
                        fx.hit(sc)
                        self.snd.hit(sc)
                        hit_flash = 1.0
                        idx += 1
                        if idx >= n_targets:
                            return records
                        miss_clicks = 0
                        target_dir, tracker = spawn(idx)
                        spawn_anim_t = time.perf_counter()
                    else:
                        points.append((off[0], off[1], False))
                        miss_clicks += 1
                        fx.miss()
                        self.snd.miss()

            anim = max(0.0, 1.0 - (time.perf_counter() - spawn_anim_t) / 0.35)
            hud = {"title": header, "idx": idx, "n": n_targets,
                   "misses": miss_clicks, "fps": self.clock.get_fps()}
            self.draw_round_frame(cam, target_dir, anim, hud, fx, dt_s,
                                  hit_flash, paused)
            hit_flash = max(0.0, hit_flash - dt_s * 4)
            pygame.display.flip()
            self.clock.tick(FPS_CAP)

    def run_plan(self, plan, title):
        """plan: list of (cm360, flicks). Blind. Returns {cm360: [targets]} | None."""
        by_level = {}
        total = len(plan)
        for i, (cm, flicks) in enumerate(plan, start=1):
            recs = self.run_round(cm, flicks, f"{title} · round {i}/{total}")
            if recs is None:
                return None
            by_level.setdefault(cm, []).extend(recs)
            if i < total:
                sc = round_score(recs)
                ttk = statistics.median(t["time_to_hit_ms"] for t in recs)
                go = self.info_screen(
                    f"Round {i} of {total} done",
                    "sensitivity is hidden — keep going",
                    ["Take a breath."],
                    chips=[("round score", f"{sc}", ACCENT),
                           ("median hit", f"{ttk:.0f} ms", None)],
                )
                if not go:
                    return None
        return by_level

    # ---------------- FIND MY SENS ---------------- #

    def mode_find(self):
        cfg = self.cfg
        rng = random.Random()
        n = cfg["targets_per_round"]
        center = cm360_for_sens(cfg, cfg["current_sens"])

        wide_levels = [round(center * m, 1) for m in WIDE_MULTIPLIERS]
        total_rounds = 1 + len(wide_levels) + 3 * FINE_PASSES

        go = self.info_screen(
            "Find my sens",
            f"about {total_rounds} rounds · fully automatic",
            ["Phase 1 — warmup (unscored).",
             "Phase 2 — wide sweep: 5 hidden sensitivities.",
             "Phase 3 — fine duel around the best one.",
             "Then you get a verdict. Aim naturally, fast & accurate."],
        )
        if not go:
            return

        self.speed_samples = []
        recs = self.run_round(center, gen_flicks(rng, n), "warmup · unscored")
        if recs is None:
            return

        order = wide_levels[:]
        rng.shuffle(order)
        plan = [(cm, gen_flicks(rng, n)) for cm in order]
        go = self.info_screen("Phase 2 — wide sweep",
                              f"{len(plan)} rounds · sensitivity hidden", [])
        if not go:
            return
        wide = self.run_plan(plan, "find · wide")
        if wide is None:
            return

        _, _, wide_best = analyze_levels(wide)

        fine_levels = [round(wide_best / FINE_BRACKET, 1),
                       round(wide_best, 1),
                       round(wide_best * FINE_BRACKET, 1)]
        plan = []
        for _ in range(FINE_PASSES):
            shared = gen_flicks(rng, n)
            order = fine_levels[:]
            rng.shuffle(order)
            plan.extend((cm, list(shared)) for cm in order)

        go = self.info_screen("Phase 3 — fine duel",
                              f"{len(plan)} rounds · identical flicks per pass",
                              ["This decides the winner."])
        if not go:
            return
        fine = self.run_plan(plan, "find · fine")
        if fine is None:
            return

        stats, plateau, winner = analyze_levels(fine)
        rec_sens = sens_for_cm360(cfg, winner)
        entry = {
            "mode": "find",
            "wide_best_cm360": wide_best,
            "fine_levels": fine_levels,
            "winner_cm360": winner,
            "recommended_sens": round(rec_sens, 2),
            "plateau_cm360": plateau,
            "speed": speed_summary(self.speed_samples),
            "levels": {str(cm): stats[cm] for cm in stats},
        }
        self.add_history(entry)
        self.verdict_screen(stats, plateau, winner, rec_sens)

    # ---------------- verdict screen ---------------- #

    def draw_verdict(self, dt, stats, plateau, winner, rec_sens):
        ui = self.ui
        cfg = self.cfg
        self.backdrop.draw(dt)

        band_lo = sens_for_cm360(cfg, max(plateau))
        band_hi = sens_for_cm360(cfg, min(plateau))
        levels = sorted(stats)
        max_ttk = max(stats[cm]["ttk_median"] for cm in levels)

        top = ui.px(64)
        ui.caps(ui.f_small, "find my sens · verdict", TXT_FAINT,
                center=(ui.cx, top))
        ui.text(ui.f_hero, f"{rec_sens:.2f}", ACCENT,
                center=(ui.cx, top + ui.px(110)))
        ui.text(ui.f_body, "recommended in-game sensitivity", TXT_DIM,
                center=(ui.cx, top + ui.px(196)))
        if len(plateau) > 1:
            sub = (f"{winner:.0f} cm/360 · plateau {min(plateau):.0f}–{max(plateau):.0f} cm "
                   f"(sens {band_lo:.2f}–{band_hi:.2f})")
        else:
            sub = f"{winner:.0f} cm/360 · clear winner, no plateau"
        ui.text(ui.f_small, sub, TXT_FAINT, center=(ui.cx, top + ui.px(228)))

        card = ui.card(1000, 58 + 74 * len(levels),
                       y=(top + ui.px(270)) / ui.k)
        y = card.top + ui.px(44)
        bar_x = card.left + ui.px(300)
        bar_w = card.w - ui.px(560)
        for cm in levels:
            s = stats[cm]
            win = cm == winner
            plat = cm in plateau
            col = ACCENT if win else (AMBER if plat else BAR_DIM)
            if win:
                ui.rrect((card.left + ui.px(14), y - ui.px(14),
                          card.w - ui.px(28), ui.px(64)),
                         (*ACCENT, 16), radius=12)
            ui.text(ui.f_num, f"{cm:.0f}", TXT if plat else TXT_DIM,
                    topright=(card.left + ui.px(96), y - ui.px(8)))
            ui.text(ui.f_tiny, "cm/360", TXT_FAINT,
                    topleft=(card.left + ui.px(104), y + ui.px(4)))
            ui.text(ui.f_small, f"sens {sens_for_cm360(cfg, cm):.2f}",
                    TXT_DIM, topleft=(card.left + ui.px(176), y - ui.px(1)))
            ui.hbar(bar_x, y + ui.px(2), bar_w, ui.px(16),
                    s["ttk_median"] / max_ttk, col)
            ui.text(ui.f_mono, f"{s['ttk_median']:.0f} ms", TXT,
                    topleft=(bar_x + bar_w + ui.px(16), y - ui.px(2)))
            ui.text(ui.f_tiny,
                    f"acc {s['accuracy'] * 100:.0f}%  ·  overshoot {s['overshoot']:.2f}",
                    TXT_FAINT, topleft=(bar_x, y + ui.px(22)))
            y += ui.px(74)

        legend_y = card.bottom + ui.px(30)
        legend = "green — winner"
        if len(plateau) > 1:
            legend += ("      yellow — same-speed plateau "
                       "(winner has the cleanest control)")
        ui.text(ui.f_small, legend, TXT_FAINT, center=(ui.cx, legend_y))
        ui.keyhints(legend_y + ui.px(46), [("SPACE", "menu")])

    def verdict_screen(self, stats, plateau, winner, rec_sens):
        while True:
            dt = self.clock.tick(60) / 1000.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type == pygame.KEYDOWN and ev.key in (
                        pygame.K_SPACE, pygame.K_ESCAPE, pygame.K_q):
                    return
            self.draw_verdict(dt, stats, plateau, winner, rec_sens)
            pygame.display.flip()

    # ---------------- BENCHMARK ---------------- #

    def mode_bench(self):
        cfg = self.cfg
        rng = random.Random()
        n = cfg["targets_per_round"]
        cm = round(cm360_for_sens(cfg, cfg["current_sens"]), 1)

        go = self.info_screen(
            "Benchmark",
            f"sens {cfg['current_sens']:.2f} · {cm:.0f} cm/360",
            ["1 warmup + 3 scored rounds.",
             "Score 0–1000; history builds your progress graph."],
        )
        if not go:
            return

        recs = self.run_round(cm, gen_flicks(rng, n), "warmup · unscored")
        if recs is None:
            return

        all_targets = []
        all_points = []
        for i in range(1, 4):
            recs = self.run_round(cm, gen_flicks(rng, n), f"benchmark · round {i}/3")
            if recs is None:
                return
            all_targets.extend(recs)
            all_points.extend(self.last_round_points)
            if i < 3:
                if not self.info_screen(
                        f"Round {i} of 3 done", "",
                        [],
                        chips=[("score so far", f"{round_score(all_targets)}",
                                ACCENT)]):
                    return

        s = level_stats(all_targets)
        buckets = bucket_stats(all_targets)
        entry = {
            "mode": "bench",
            "sens": cfg["current_sens"],
            "cm360": cm,
            "score": s["score"],
            "ttk_median": round(s["ttk_median"], 1),
            "accuracy": round(s["accuracy"], 4),
            "buckets": {b: {"ttk_median": round(v["ttk_median"], 1),
                            "accuracy": round(v["accuracy"], 4)}
                        for b, v in buckets.items()},
        }
        self.add_history(entry)
        self.bench_results_screen(s, buckets, all_points)

    def draw_gauge(self, cx, cy, radius, frac, color):
        """Dotted arc gauge from 135 deg to 405 deg."""
        ui = self.ui
        n_dots = 64
        start, sweep = 135.0, 270.0
        for i in range(n_dots):
            t = i / (n_dots - 1)
            a = math.radians(start + sweep * t)
            x = cx + radius * math.cos(a)
            y = cy + radius * math.sin(a)
            if t <= frac:
                col, r = color, ui.px(5)
            else:
                col, r = (70, 78, 106), ui.px(3)
            pygame.draw.circle(ui.s, col, (int(x), int(y)), r)

    def draw_bucket_panel(self, rect, s, buckets):
        """Flick-distance breakdown: median TTK bar + accuracy per bucket."""
        ui = self.ui
        ui.rrect(rect, (*PANEL, 235), radius=14, border=BORDER)
        ui.caps(ui.f_tiny, "by flick distance", TXT_FAINT,
                center=(rect.centerx, rect.top + ui.px(24)))
        if not buckets:
            ui.text(ui.f_small, "no data", TXT_FAINT, center=rect.center)
            return
        max_ttk = max(v["ttk_median"] for v in buckets.values())
        worst = max(buckets, key=lambda b: buckets[b]["ttk_median"])
        y = rect.top + ui.px(52)
        for b in ("small", "medium", "large"):
            v = buckets.get(b)
            if not v:
                continue
            lo, hi = FLICK_BUCKETS[b]
            col = AMBER if b == worst and len(buckets) > 1 else ACCENT
            ui.text(ui.f_small, b, TXT,
                    topleft=(rect.left + ui.px(22), y))
            ui.text(ui.f_tiny, f"{lo:.0f}–{hi:.0f}°", TXT_FAINT,
                    topleft=(rect.left + ui.px(22), y + ui.px(24)))
            bar_x = rect.left + ui.px(110)
            bar_w = rect.w - ui.px(230)
            ui.hbar(bar_x, y + ui.px(6), bar_w, ui.px(14),
                    v["ttk_median"] / max_ttk, col)
            ui.text(ui.f_mono, f"{v['ttk_median']:.0f} ms", TXT,
                    topleft=(bar_x + bar_w + ui.px(12), y + ui.px(2)))
            ui.text(ui.f_tiny, f"acc {v['accuracy'] * 100:.0f}%", TXT_FAINT,
                    topleft=(bar_x, y + ui.px(26)))
            y += ui.px(62)
        if len(buckets) > 1:
            hint = {"small": "close flicks lag — try a touch higher sens",
                    "medium": "mid flicks lag — sens is off or warm up more",
                    "large": "long flicks lag — try a touch lower sens"}[worst]
            ui.text(ui.f_tiny, hint, TXT_FAINT,
                    center=(rect.centerx, rect.bottom - ui.px(20)))

    def draw_hitmap_panel(self, rect, points):
        """Scatter of click positions relative to the target center."""
        ui = self.ui
        ui.rrect(rect, (*PANEL, 235), radius=14, border=BORDER)
        ui.caps(ui.f_tiny, "hit map", TXT_FAINT,
                center=(rect.centerx, rect.top + ui.px(24)))
        cx, cy = rect.centerx, rect.centery + ui.px(10)
        t_rad_deg = TARGET_ANGULAR_DIAMETER_DEG / 2.0
        scale = ui.px(52) / t_rad_deg          # px per degree
        max_r = min(rect.w, rect.h) // 2 - ui.px(30)
        # target outline + crosshair lines
        pygame.draw.circle(ui.s, (70, 78, 106), (cx, cy),
                           int(t_rad_deg * scale), max(1, ui.px(2)))
        pygame.draw.line(ui.s, (34, 40, 60), (cx - max_r, cy),
                         (cx + max_r, cy))
        pygame.draw.line(ui.s, (34, 40, 60), (cx, cy - max_r),
                         (cx, cy + max_r))
        if not points:
            ui.text(ui.f_small, "no data", TXT_FAINT, center=(cx, cy))
            return
        sx = sy = 0.0
        for h, v, hit in points:
            px_ = cx + h * scale
            py_ = cy - v * scale
            d = math.hypot(px_ - cx, py_ - cy)
            if d > max_r:                       # clamp far misses to the rim
                px_ = cx + (px_ - cx) * max_r / d
                py_ = cy + (py_ - cy) * max_r / d
            col = ACCENT if hit else RED
            pygame.draw.circle(ui.s, col, (int(px_), int(py_)), ui.px(3))
            sx += h
            sy += v
        mx, my = sx / len(points), sy / len(points)
        bx, by = cx + mx * scale, cy - my * scale
        pygame.draw.line(ui.s, AMBER, (bx - ui.px(7), by), (bx + ui.px(7), by),
                         max(1, ui.px(2)))
        pygame.draw.line(ui.s, AMBER, (bx, by - ui.px(7)), (bx, by + ui.px(7)),
                         max(1, ui.px(2)))
        side = "right" if mx > 0 else "left"
        vert = "high" if my > 0 else "low"
        ui.text(ui.f_tiny,
                f"bias {abs(mx):.2f}° {side} · {abs(my):.2f}° {vert}",
                TXT_FAINT, center=(rect.centerx, rect.bottom - ui.px(20)))

    def draw_bench_results(self, dt, s, scores, buckets=None, points=None):
        ui = self.ui
        self.backdrop.draw(dt)
        best = max(scores) if scores else s["score"]
        is_pb = s["score"] >= best and len(scores) > 1

        top = ui.px(110)
        ui.caps(ui.f_small, "benchmark · result", TXT_FAINT,
                center=(ui.cx, top))

        gy = top + ui.px(200)
        self.draw_gauge(ui.cx, gy, ui.px(150), s["score"] / 1000.0,
                        ACCENT if is_pb or s["score"] >= best else AMBER)
        ui.text(ui.f_hero, f"{s['score']}",
                ACCENT if is_pb else TXT, center=(ui.cx, gy - ui.px(6)))
        ui.caps(ui.f_tiny, "score", TXT_FAINT,
                center=(ui.cx, gy + ui.px(64)))
        if is_pb:
            ui.text(ui.f_h2, "new personal best!", ACCENT,
                    center=(ui.cx, gy + ui.px(120)))

        # side panels: flick breakdown (left) + hit map (right)
        pw, ph = ui.px(430), ui.px(310)
        if buckets is not None:
            self.draw_bucket_panel(
                pygame.Rect(ui.cx - ui.px(330) - pw, gy - ph // 2 + ui.px(20),
                            pw, ph), s, buckets)
        if points is not None:
            self.draw_hitmap_panel(
                pygame.Rect(ui.cx + ui.px(330), gy - ph // 2 + ui.px(20),
                            pw, ph), points)

        chips_y = gy + ui.px(210)
        ui.chip_row(chips_y, [
            ("median hit", f"{s['ttk_median']:.0f} ms", None),
            ("accuracy", f"{s['accuracy'] * 100:.0f}%", None),
            ("overshoot", f"{s['overshoot']:.2f}", None),
            ("path eff", f"{s['path_eff'] * 100:.0f}%", None),
        ])

        self.draw_progress(scores, chips_y + ui.px(80))
        ui.keyhints(ui.H - ui.px(60), [("SPACE", "menu")])

    def bench_results_screen(self, s, buckets=None, points=None):
        scores = [h["score"] for h in self.history if h.get("mode") == "bench"]
        if len(scores) > 1 and s["score"] >= max(scores):
            self.snd.pb()
        while True:
            dt = self.clock.tick(60) / 1000.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type == pygame.KEYDOWN and ev.key in (
                        pygame.K_SPACE, pygame.K_ESCAPE, pygame.K_q):
                    return
            self.draw_bench_results(dt, s, scores, buckets, points)
            pygame.display.flip()

    # ---------------- A/B DUEL ---------------- #

    def duel_setup(self):
        """Two-value editor. Returns (sens_a, sens_b) or None."""
        cfg = self.cfg
        finds = [h for h in self.history if h.get("mode") == "find"
                 and h.get("recommended_sens")]
        default_b = (finds[-1]["recommended_sens"] if finds
                     else round(cfg["current_sens"] * 0.8, 2))
        vals = [round(cfg["current_sens"], 2), default_b]
        labels = ["Sens A", "Sens B"]
        sel, edit, warn = 0, None, ""
        while True:
            dt = self.clock.tick(60) / 1000.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return None
                if ev.type != pygame.KEYDOWN:
                    continue
                if edit is not None:
                    if ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        try:
                            vals[sel] = max(0.05, min(5.0, float(
                                edit.replace(",", "."))))
                        except ValueError:
                            pass
                        edit = None
                    elif ev.key == pygame.K_ESCAPE:
                        edit = None
                    elif ev.key == pygame.K_BACKSPACE:
                        edit = edit[:-1]
                    elif ev.unicode and (ev.unicode.isdigit()
                                         or ev.unicode in ".,"):
                        edit += ev.unicode
                    continue
                if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                    return None
                elif ev.key in (pygame.K_UP, pygame.K_DOWN, pygame.K_TAB,
                                pygame.K_w, pygame.K_s):
                    sel = 1 - sel
                elif ev.key in (pygame.K_LEFT, pygame.K_a):
                    vals[sel] = max(0.05, round(vals[sel] - 0.02, 2))
                elif ev.key in (pygame.K_RIGHT, pygame.K_d):
                    vals[sel] = min(5.0, round(vals[sel] + 0.02, 2))
                elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    edit = ""
                elif ev.key == pygame.K_SPACE:
                    cm_a = round(cm360_for_sens(self.cfg, vals[0]), 1)
                    cm_b = round(cm360_for_sens(self.cfg, vals[1]), 1)
                    if cm_a == cm_b:
                        warn = "values are too close — nothing to compare"
                    else:
                        return vals[0], vals[1]

            self.backdrop.draw(dt)
            ui = self.ui
            card = ui.card(760, 430)
            ui.text(ui.f_title, "A/B duel", TXT,
                    center=(ui.cx, card.top + ui.px(52)))
            ui.caps(ui.f_small, "blind matched rounds decide which is faster",
                    ACCENT, center=(ui.cx, card.top + ui.px(96)))
            y = card.top + ui.px(140)
            for i in range(2):
                row = pygame.Rect(card.left + ui.px(24), y,
                                  card.w - ui.px(48), ui.px(58))
                if i == sel:
                    ui.rrect(row, (*ACCENT, 18), radius=12)
                    ui.rrect((row.left, row.top, ui.px(4), row.h),
                             (*ACCENT, 200), radius=2)
                ui.text(ui.f_h2, labels[i], TXT if i == sel else TXT_DIM,
                        topleft=(row.left + ui.px(26), row.top + ui.px(10)))
                cm = cm360_for_sens(self.cfg, vals[i])
                ui.text(ui.f_small, f"{cm:.1f} cm/360", TXT_FAINT,
                        topleft=(row.left + ui.px(210), row.top + ui.px(16)))
                if i == sel and edit is not None:
                    txt, col = edit + "_", AMBER
                else:
                    txt, col = f"{vals[i]:.2f}", (ACCENT if i == sel else TXT)
                ui.text(ui.f_num, txt, col,
                        topright=(row.right - ui.px(30), row.top + ui.px(10)))
                y += ui.px(72)
            ui.text(ui.f_body,
                    f"{DUEL_PASSES} passes x 2 rounds · identical flicks · "
                    "order hidden", TXT_DIM, center=(ui.cx, y + ui.px(20)))
            if warn:
                ui.text(ui.f_small, warn, RED, center=(ui.cx, y + ui.px(52)))
            ui.keyhints(card.bottom - ui.px(44),
                        [("↑↓", "row"), ("←→", "adjust"), ("ENTER", "type"),
                         ("SPACE", "start"), ("Q", "back")])
            pygame.display.flip()

    def mode_duel(self):
        cfg = self.cfg
        setup = self.duel_setup()
        if setup is None:
            return
        sens_a, sens_b = setup
        rng = random.Random()
        n = cfg["targets_per_round"]
        cm_a = round(cm360_for_sens(cfg, sens_a), 1)
        cm_b = round(cm360_for_sens(cfg, sens_b), 1)

        recs = self.run_round(cm_a, gen_flicks(rng, n), "warmup · unscored")
        if recs is None:
            return

        plan = []
        for _ in range(DUEL_PASSES):
            shared = gen_flicks(rng, n)
            order = [cm_a, cm_b]
            rng.shuffle(order)
            plan.extend((cm, list(shared)) for cm in order)
        by_level = self.run_plan(plan, "duel")
        if by_level is None:
            return

        stats = {cm: level_stats(ts) for cm, ts in by_level.items()}
        conf_a = bootstrap_faster_prob(
            [t["time_to_hit_ms"] for t in by_level[cm_a]],
            [t["time_to_hit_ms"] for t in by_level[cm_b]],
            seed=int(time.time()))
        entry = {
            "mode": "duel",
            "sens_a": sens_a, "sens_b": sens_b,
            "cm_a": cm_a, "cm_b": cm_b,
            "conf_a_faster": round(conf_a, 3),
            "stats_a": stats[cm_a], "stats_b": stats[cm_b],
        }
        self.add_history(entry)
        self.duel_verdict_screen(sens_a, sens_b, cm_a, cm_b, stats, conf_a)

    def draw_duel_verdict(self, dt, sens_a, sens_b, cm_a, cm_b, stats, conf_a):
        ui = self.ui
        self.backdrop.draw(dt)
        if conf_a >= 0.5:
            w_sens, w_cm, p = sens_a, cm_a, conf_a
        else:
            w_sens, w_cm, p = sens_b, cm_b, 1.0 - conf_a
        tie = p < 0.65

        top = ui.px(70)
        ui.caps(ui.f_small, "a/b duel · verdict", TXT_FAINT,
                center=(ui.cx, top))
        if tie:
            ui.text(ui.f_hero, "TIE", TXT, center=(ui.cx, top + ui.px(105)))
            sub = (f"difference is within noise ({p * 100:.0f}%) — "
                   "keep whichever feels better")
        else:
            ui.text(ui.f_hero, f"{w_sens:.2f}", ACCENT,
                    center=(ui.cx, top + ui.px(105)))
            grade = "clear winner" if p >= 0.85 else "slight edge"
            sub = f"{grade} — faster with {p * 100:.0f}% confidence"
        ui.text(ui.f_body, sub, TXT_DIM, center=(ui.cx, top + ui.px(190)))

        # confidence tug bar: A side vs B side
        bw, bh = ui.px(520), ui.px(14)
        bx = ui.cx - bw // 2
        by = top + ui.px(228)
        ui.rrect((bx, by, bw, bh), (255, 255, 255, 18), radius=6)
        ui.rrect((bx, by, max(ui.px(8), int(bw * conf_a)), bh),
                 (*(ACCENT if conf_a >= 0.5 else BAR_DIM), 255), radius=6)
        ui.text(ui.f_tiny, f"A · {conf_a * 100:.0f}%", TXT_FAINT,
                topright=(bx - ui.px(12), by - ui.px(2)))
        ui.text(ui.f_tiny, f"{(1 - conf_a) * 100:.0f}% · B", TXT_FAINT,
                topleft=(bx + bw + ui.px(12), by - ui.px(2)))

        # side-by-side stat cards
        cw, ch = ui.px(430), ui.px(360)
        y0 = top + ui.px(280)
        for side, sens, cm, x in (("A", sens_a, cm_a, ui.cx - cw - ui.px(30)),
                                  ("B", sens_b, cm_b, ui.cx + ui.px(30))):
            s = stats[cm]
            won = not tie and cm == w_cm
            rect = pygame.Rect(x, y0, cw, ch)
            ui.rrect(rect, (*PANEL, 236), radius=18,
                     border=(*ACCENT, 150) if won else BORDER)
            ui.text(ui.f_h2, f"{side} · sens {sens:.2f}",
                    ACCENT if won else TXT,
                    topleft=(rect.left + ui.px(28), rect.top + ui.px(22)))
            ui.text(ui.f_small, f"{cm:.1f} cm/360", TXT_FAINT,
                    topleft=(rect.left + ui.px(28), rect.top + ui.px(58)))
            rows = [("median hit", f"{s['ttk_median']:.0f} ms"),
                    ("accuracy", f"{s['accuracy'] * 100:.0f}%"),
                    ("overshoot", f"{s['overshoot']:.2f}"),
                    ("path efficiency", f"{s['path_eff'] * 100:.0f}%"),
                    ("score", f"{s['score']}")]
            ry = rect.top + ui.px(100)
            for label, val in rows:
                ui.text(ui.f_small, label, TXT_DIM,
                        topleft=(rect.left + ui.px(28), ry))
                ui.text(ui.f_num, val, TXT,
                        topright=(rect.right - ui.px(28), ry - ui.px(6)))
                ry += ui.px(48)
        ui.keyhints(y0 + ch + ui.px(46), [("SPACE", "menu")])

    def duel_verdict_screen(self, sens_a, sens_b, cm_a, cm_b, stats, conf_a):
        while True:
            dt = self.clock.tick(60) / 1000.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type == pygame.KEYDOWN and ev.key in (
                        pygame.K_SPACE, pygame.K_ESCAPE, pygame.K_q):
                    return
            self.draw_duel_verdict(dt, sens_a, sens_b, cm_a, cm_b, stats,
                                   conf_a)
            pygame.display.flip()

    # ---------------- TRACKING ---------------- #

    def run_track_round(self, cm360, seconds, header, seed=None):
        """One tracking round. Returns result dict or None on abort.
        With a seed, the target path is reproducible (matched comparisons)."""
        cfg = self.cfg
        cam = Camera()
        dpc = deg_per_count(cm360, cfg["dpi"])
        rng = random.Random(seed)
        motion = StrafeMotion(
            rng, cam.yaw + rng.choice((-1, 1)) * rng.uniform(8.0, 14.0))
        radius = TARGET_ANGULAR_DIAMETER_DEG / 2.0
        grace = 0.8            # settle-in time before scoring starts
        elapsed = 0.0
        t_on = t_total = err_sum = 0.0
        streak = best_streak = 0.0
        hold = 0.0             # continuous on-target time (audio feedback)
        off_t = 1.0            # time since target lost (acquire-blip debounce)
        paused = False
        prev = time.perf_counter_ns()
        pygame.event.clear(pygame.MOUSEMOTION)
        yaw, pitch = motion.yaw, -2.0

        while True:
            now = time.perf_counter_ns()
            dt_s = (now - prev) / 1e9
            prev = now
            frame_dist = 0.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    self.snd.track_off()
                    return None
                elif ev.type == pygame.MOUSEMOTION:
                    dx, dy = ev.rel
                    if (dx or dy) and not paused:
                        cam.apply_counts(dx, dy, dpc)
                        frame_dist += math.hypot(dx, dy)
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_ESCAPE:
                        paused = not paused
                        if paused:
                            self.snd.track_off()
                            hold, off_t = 0.0, 1.0
                        prev = time.perf_counter_ns()
                    elif paused and ev.key == pygame.K_q:
                        return None

            if frame_dist and dt_s > 0 and not paused:
                self.speed_samples.append(frame_dist / (dt_s * 1000.0))

            on = False
            if not paused:
                elapsed += dt_s
                yaw, pitch = motion.step(dt_s)
                tdir = dir_from_yawpitch(yaw, pitch)
                ang = angle_between_deg(cam.forward(), tdir)
                on = ang <= radius
                # audio: 0.12 s grace so edge flicker (on/off every frame
                # while the crosshair rides the target rim) doesn't chop
                # the hum into static; scoring below stays exact
                if on:
                    if off_t > 0.25:
                        self.snd.lock()
                    hold += dt_s
                    off_t = 0.0
                    self.snd.track_on(hold)
                else:
                    off_t += dt_s
                    if off_t > 0.12:
                        hold = 0.0
                        self.snd.track_off()
                if elapsed > grace:
                    t_total += dt_s
                    err_sum += ang * dt_s
                    if on:
                        t_on += dt_s
                        streak += dt_s
                        best_streak = max(best_streak, streak)
                    else:
                        streak = 0.0
                if elapsed >= seconds + grace:
                    self.snd.track_off()
                    return {
                        "on_pct": t_on / t_total if t_total else 0.0,
                        "mean_err": err_sum / t_total if t_total else 0.0,
                        "best_streak": best_streak,
                    }
            else:
                tdir = dir_from_yawpitch(yaw, pitch)

            ui = self.ui
            self.r.draw_world(cam)
            spawn_anim = max(0.0, 1.0 - elapsed / 0.35)
            self.r.draw_target(cam, tdir, spawn_anim=spawn_anim)
            self.r.draw_crosshair(flash=0.55 if on else 0.0)

            pw, ph = ui.px(360), ui.px(86)
            pill = pygame.Rect(ui.cx - pw // 2, ui.px(22), pw, ph)
            ui.rrect(pill, (*PANEL, 200), radius=16, border=BORDER)
            ui.caps(ui.f_tiny, header, TXT_FAINT,
                    center=(ui.cx, pill.top + ui.px(20)))
            left = max(0.0, seconds + grace - elapsed)
            live = t_on / t_total if t_total else 0.0
            ui.text(ui.f_small,
                    f"{left:4.1f} s   ·   on target {live * 100:3.0f}%",
                    ACCENT if on else TXT_DIM,
                    center=(ui.cx, pill.top + ui.px(44)))
            ui.hbar(pill.left + ui.px(30), pill.top + ui.px(62),
                    pill.w - ui.px(60), ui.px(6), live, ACCENT)
            ui.text(ui.f_tiny, f"{self.clock.get_fps():.0f} fps", TXT_FAINT,
                    topright=(ui.W - ui.px(24), ui.H - ui.px(34)))
            ui.text(ui.f_tiny, "ESC pause", TXT_FAINT,
                    topleft=(ui.px(24), ui.H - ui.px(34)))
            if paused:
                ui.dim(150)
                ui.vignette()
                card = ui.card(560, 240)
                ui.text(ui.f_title, "Paused", TXT,
                        center=(ui.cx, card.top + ui.px(74)))
                ui.keyhints(card.bottom - ui.px(58),
                            [("ESC", "resume"), ("Q", "abort round")])
            pygame.display.flip()
            self.clock.tick(FPS_CAP)

    def mode_track(self):
        cfg = self.cfg
        cm = round(cm360_for_sens(cfg, cfg["current_sens"]), 1)
        go = self.info_screen(
            "Tracking",
            f"sens {cfg['current_sens']:.2f} · {cm:.0f} cm/360",
            [f"{TRACK_ROUNDS} rounds x {TRACK_ROUND_S:.0f} s.",
             "The target strafes like an enemy — stay on it.",
             "Score = time on target."],
        )
        if not go:
            return

        results = []
        for i in range(1, TRACK_ROUNDS + 1):
            res = self.run_track_round(cm, TRACK_ROUND_S,
                                       f"tracking · round {i}/{TRACK_ROUNDS}")
            if res is None:
                return
            results.append(res)
            if i < TRACK_ROUNDS:
                if not self.info_screen(
                        f"Round {i} of {TRACK_ROUNDS} done", "",
                        [],
                        chips=[("on target",
                                f"{res['on_pct'] * 100:.0f}%", ACCENT),
                               ("best streak",
                                f"{res['best_streak']:.1f} s", None)]):
                    return

        on_pct = statistics.fmean(r["on_pct"] for r in results)
        agg = {
            "on_pct": on_pct,
            "mean_err": statistics.fmean(r["mean_err"] for r in results),
            "best_streak": max(r["best_streak"] for r in results),
            "score": track_score(on_pct),
        }
        entry = {
            "mode": "track",
            "sens": cfg["current_sens"],
            "cm360": cm,
            "score": agg["score"],
            "on_pct": round(on_pct, 4),
            "mean_err": round(agg["mean_err"], 3),
            "best_streak": round(agg["best_streak"], 2),
        }
        self.add_history(entry)
        self.track_results_screen(agg)

    def draw_track_results(self, dt, agg, scores):
        ui = self.ui
        self.backdrop.draw(dt)
        best = max(scores) if scores else agg["score"]
        is_pb = agg["score"] >= best and len(scores) > 1

        top = ui.px(110)
        ui.caps(ui.f_small, "tracking · result", TXT_FAINT,
                center=(ui.cx, top))
        gy = top + ui.px(200)
        self.draw_gauge(ui.cx, gy, ui.px(150), agg["score"] / 1000.0,
                        ACCENT if is_pb or agg["score"] >= best else AMBER)
        ui.text(ui.f_hero, f"{agg['score']}",
                ACCENT if is_pb else TXT, center=(ui.cx, gy - ui.px(6)))
        ui.caps(ui.f_tiny, "score", TXT_FAINT, center=(ui.cx, gy + ui.px(64)))
        if is_pb:
            ui.text(ui.f_h2, "new personal best!", ACCENT,
                    center=(ui.cx, gy + ui.px(120)))

        chips_y = gy + ui.px(210)
        ui.chip_row(chips_y, [
            ("on target", f"{agg['on_pct'] * 100:.0f}%", ACCENT),
            ("mean error", f"{agg['mean_err']:.2f}°", None),
            ("best streak", f"{agg['best_streak']:.1f} s", None),
        ])
        self.draw_progress(scores, chips_y + ui.px(80))
        ui.keyhints(ui.H - ui.px(60), [("SPACE", "menu")])

    def track_results_screen(self, agg):
        scores = [h["score"] for h in self.history if h.get("mode") == "track"]
        if len(scores) > 1 and agg["score"] >= max(scores):
            self.snd.pb()
        while True:
            dt = self.clock.tick(60) / 1000.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type == pygame.KEYDOWN and ev.key in (
                        pygame.K_SPACE, pygame.K_ESCAPE, pygame.K_q):
                    return
            self.draw_track_results(dt, agg, scores)
            pygame.display.flip()

    # ---------------- FIND TRACKING SENS ---------------- #

    def run_track_plan(self, plan, title):
        """plan: list of (cm360, motion_seed). Blind. Returns
        {cm360: [round results]} | None on abort."""
        by_level = {}
        total = len(plan)
        for i, (cm, seed) in enumerate(plan, start=1):
            res = self.run_track_round(cm, TRACKFIND_ROUND_S,
                                       f"{title} · round {i}/{total}",
                                       seed=seed)
            if res is None:
                return None
            by_level.setdefault(cm, []).append(res)
            if i < total:
                go = self.info_screen(
                    f"Round {i} of {total} done",
                    "sensitivity is hidden — keep going",
                    ["Take a breath."],
                    chips=[("on target", f"{res['on_pct'] * 100:.0f}%",
                            ACCENT),
                           ("mean error", f"{res['mean_err']:.2f}°", None)],
                )
                if not go:
                    return None
        return by_level

    def mode_find_track(self):
        cfg = self.cfg
        rng = random.Random()
        center = cm360_for_sens(cfg, cfg["current_sens"])

        wide_levels = [round(center * m, 1) for m in WIDE_MULTIPLIERS]
        total_rounds = 1 + len(wide_levels) + 3 * TRACKFIND_FINE_PASSES

        go = self.info_screen(
            "Find my tracking sens",
            f"about {total_rounds} rounds x {TRACKFIND_ROUND_S:.0f} s · "
            "fully automatic",
            ["Phase 1 — warmup (unscored).",
             "Phase 2 — wide sweep: 5 hidden sensitivities.",
             "Phase 3 — fine duel: identical target path per pass.",
             "Stay on the strafing target — the verdict is at the end."],
        )
        if not go:
            return

        self.speed_samples = []
        warm = self.run_track_round(center, TRACKFIND_WARMUP_S,
                                    "warmup · unscored")
        if warm is None:
            return

        order = wide_levels[:]
        rng.shuffle(order)
        plan = [(cm, rng.randrange(1 << 30)) for cm in order]
        go = self.info_screen("Phase 2 — wide sweep",
                              f"{len(plan)} rounds · sensitivity hidden", [])
        if not go:
            return
        wide = self.run_track_plan(plan, "track find · wide")
        if wide is None:
            return

        _, _, wide_best = analyze_track_levels(wide)

        fine_levels = [round(wide_best / FINE_BRACKET, 1),
                       round(wide_best, 1),
                       round(wide_best * FINE_BRACKET, 1)]
        plan = []
        for _ in range(TRACKFIND_FINE_PASSES):
            seed = rng.randrange(1 << 30)
            order = fine_levels[:]
            rng.shuffle(order)
            plan.extend((cm, seed) for cm in order)

        go = self.info_screen("Phase 3 — fine duel",
                              f"{len(plan)} rounds · identical path per pass",
                              ["This decides the winner."])
        if not go:
            return
        fine = self.run_track_plan(plan, "track find · fine")
        if fine is None:
            return

        stats, plateau, winner = analyze_track_levels(fine)
        rec_sens = sens_for_cm360(cfg, winner)
        entry = {
            "mode": "find_track",
            "wide_best_cm360": wide_best,
            "fine_levels": fine_levels,
            "winner_cm360": winner,
            "recommended_sens": round(rec_sens, 2),
            "plateau_cm360": plateau,
            "speed": speed_summary(self.speed_samples),
            "levels": {str(cm): {k: round(v, 4) if isinstance(v, float) else v
                                 for k, v in stats[cm].items()}
                       for cm in stats},
        }
        self.add_history(entry)
        self.track_verdict_screen(stats, plateau, winner, rec_sens)

    def draw_track_verdict(self, dt, stats, plateau, winner, rec_sens):
        ui = self.ui
        cfg = self.cfg
        self.backdrop.draw(dt)

        band_lo = sens_for_cm360(cfg, max(plateau))
        band_hi = sens_for_cm360(cfg, min(plateau))
        levels = sorted(stats)

        top = ui.px(64)
        ui.caps(ui.f_small, "find tracking sens · verdict", TXT_FAINT,
                center=(ui.cx, top))
        ui.text(ui.f_hero, f"{rec_sens:.2f}", ACCENT,
                center=(ui.cx, top + ui.px(110)))
        ui.text(ui.f_body, "recommended in-game sensitivity for tracking",
                TXT_DIM, center=(ui.cx, top + ui.px(196)))
        if len(plateau) > 1:
            sub = (f"{winner:.0f} cm/360 · plateau "
                   f"{min(plateau):.0f}–{max(plateau):.0f} cm "
                   f"(sens {band_lo:.2f}–{band_hi:.2f})")
        else:
            sub = f"{winner:.0f} cm/360 · clear winner, no plateau"
        ui.text(ui.f_small, sub, TXT_FAINT, center=(ui.cx, top + ui.px(228)))

        card = ui.card(1000, 58 + 74 * len(levels),
                       y=(top + ui.px(270)) / ui.k)
        y = card.top + ui.px(44)
        bar_x = card.left + ui.px(300)
        bar_w = card.w - ui.px(560)
        for cm in levels:
            s = stats[cm]
            win = cm == winner
            plat = cm in plateau
            col = ACCENT if win else (AMBER if plat else BAR_DIM)
            if win:
                ui.rrect((card.left + ui.px(14), y - ui.px(14),
                          card.w - ui.px(28), ui.px(64)),
                         (*ACCENT, 16), radius=12)
            ui.text(ui.f_num, f"{cm:.0f}", TXT if plat else TXT_DIM,
                    topright=(card.left + ui.px(96), y - ui.px(8)))
            ui.text(ui.f_tiny, "cm/360", TXT_FAINT,
                    topleft=(card.left + ui.px(104), y + ui.px(4)))
            ui.text(ui.f_small, f"sens {sens_for_cm360(cfg, cm):.2f}",
                    TXT_DIM, topleft=(card.left + ui.px(176), y - ui.px(1)))
            ui.hbar(bar_x, y + ui.px(2), bar_w, ui.px(16), s["on_pct"], col)
            ui.text(ui.f_mono, f"{s['on_pct'] * 100:.0f}%", TXT,
                    topleft=(bar_x + bar_w + ui.px(16), y - ui.px(2)))
            ui.text(ui.f_tiny,
                    f"mean err {s['mean_err']:.2f}°  ·  "
                    f"best streak {s['best_streak']:.1f} s",
                    TXT_FAINT, topleft=(bar_x, y + ui.px(22)))
            y += ui.px(74)

        legend_y = card.bottom + ui.px(30)
        legend = "bars — time on target      green — winner"
        if len(plateau) > 1:
            legend += ("      yellow — same-hold plateau "
                       "(winner has the steadiest aim)")
        ui.text(ui.f_small, legend, TXT_FAINT, center=(ui.cx, legend_y))
        ui.keyhints(legend_y + ui.px(46), [("SPACE", "menu")])

    def track_verdict_screen(self, stats, plateau, winner, rec_sens):
        while True:
            dt = self.clock.tick(60) / 1000.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type == pygame.KEYDOWN and ev.key in (
                        pygame.K_SPACE, pygame.K_ESCAPE, pygame.K_q):
                    return
            self.draw_track_verdict(dt, stats, plateau, winner, rec_sens)
            pygame.display.flip()

    def draw_progress(self, scores, y0):
        """Area chart of the last 20 benchmark scores."""
        ui = self.ui
        pts = scores[-20:]
        w, h = ui.px(640), ui.px(150)
        x0 = ui.cx - w // 2
        rect = pygame.Rect(x0, y0, w, h)
        ui.rrect(rect, (*PANEL, 235), radius=14, border=BORDER)
        ui.caps(ui.f_tiny, "progress · last sessions", TXT_FAINT,
                center=(ui.cx, y0 + ui.px(20)))
        if len(pts) < 2:
            ui.text(ui.f_small, "play more sessions to see your graph",
                    TXT_FAINT, center=(ui.cx, y0 + h // 2 + ui.px(10)))
            return
        lo, hi = min(pts), max(pts)
        span = max(1, hi - lo)
        pad_x, pad_top, pad_bot = ui.px(26), ui.px(40), ui.px(22)
        coords = []
        for i, sc in enumerate(pts):
            x = x0 + pad_x + (w - pad_x * 2) * i / (len(pts) - 1)
            y = y0 + h - pad_bot - (h - pad_top - pad_bot) * (sc - lo) / span
            coords.append((x, y))
        # gradient-ish area fill
        area = pygame.Surface((w, h), pygame.SRCALPHA)
        poly = ([(c[0] - x0, c[1] - y0) for c in coords] +
                [(coords[-1][0] - x0, h - ui.px(6)),
                 (coords[0][0] - x0, h - ui.px(6))])
        pygame.draw.polygon(area, (*ACCENT, 26), poly)
        ui.s.blit(area, (x0, y0))
        pygame.draw.lines(ui.s, ACCENT, False, coords, max(2, ui.px(2)))
        for c in coords[:-1]:
            pygame.draw.circle(ui.s, TXT_DIM, (int(c[0]), int(c[1])),
                               ui.px(3))
        pygame.draw.circle(ui.s, ACCENT,
                           (int(coords[-1][0]), int(coords[-1][1])),
                           ui.px(5))
        ui.text(ui.f_tiny, f"{hi}", TXT_FAINT,
                topleft=(x0 + ui.px(8), y0 + pad_top - ui.px(8)))
        ui.text(ui.f_tiny, f"{lo}", TXT_FAINT,
                topleft=(x0 + ui.px(8), y0 + h - pad_bot - ui.px(8)))

    # ---------------- SETTINGS ---------------- #

    SETTINGS = [
        ("Mouse DPI",             "dpi",               50,   100, 25600, "{:.0f}"),
        ("cm/360 at MR sens 1.0", "sens1_cm360",       0.1,  5.0, 200.0, "{:.1f}"),
        ("Current MR sens",       "current_sens",      0.01, 0.05, 5.0,  "{:.2f}"),
        ("Targets per round",     "targets_per_round", 1,    6,   24,    "{:.0f}"),
    ]

    def draw_settings(self, dt, sel, edit):
        ui = self.ui
        self.backdrop.draw(dt)
        card = ui.card(880, 520)
        ui.text(ui.f_title, "Settings", TXT,
                center=(ui.cx, card.top + ui.px(52)))

        y = card.top + ui.px(110)
        row_h = ui.px(62)
        for i, (label, key, step, lo, hi, fmt) in enumerate(self.SETTINGS):
            row = pygame.Rect(card.left + ui.px(24), y,
                              card.w - ui.px(48), row_h - ui.px(10))
            if i == sel:
                ui.rrect(row, (*ACCENT, 18), radius=12)
                ui.rrect((row.left, row.top, ui.px(4), row.h),
                         (*ACCENT, 200), radius=2)
            ui.text(ui.f_h2, label, TXT if i == sel else TXT_DIM,
                    topleft=(row.left + ui.px(26), row.top + ui.px(8)))
            if i == sel and edit is not None:
                val_txt = edit + "_"
                col = AMBER
            else:
                val_txt = fmt.format(self.cfg[key])
                col = ACCENT if i == sel else TXT
            ui.text(ui.f_num, val_txt, col,
                    topright=(row.right - ui.px(30), row.top + ui.px(6)))
            y += row_h

        cur_cm = cm360_for_sens(self.cfg, self.cfg["current_sens"])
        ui.text(ui.f_small, f"current feel:  {cur_cm:.1f} cm/360",
                ACCENT, center=(ui.cx, y + ui.px(16)))
        ui.text(ui.f_tiny,
                "cm/360 at sens 1.0 — measure with swipe_count.py "
                "(focused window, pad-edge trick)",
                TXT_FAINT, center=(ui.cx, y + ui.px(44)))
        ui.keyhints(card.bottom - ui.px(44),
                    [("↑↓", "select"), ("←→", "adjust"),
                     ("ENTER", "type"), ("ESC", "save & back")])

    def mode_settings(self):
        sel = 0
        edit = None  # None or string buffer
        while True:
            dt = self.clock.tick(60) / 1000.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    self.save_config()
                    return
                if ev.type != pygame.KEYDOWN:
                    continue
                label, key, step, lo, hi, fmt = self.SETTINGS[sel]
                if edit is not None:
                    if ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        try:
                            val = float(edit.replace(",", "."))
                            self.cfg[key] = self._clamp(key, val, lo, hi)
                        except ValueError:
                            pass
                        edit = None
                    elif ev.key == pygame.K_ESCAPE:
                        edit = None
                    elif ev.key == pygame.K_BACKSPACE:
                        edit = edit[:-1]
                    elif ev.unicode and (ev.unicode.isdigit() or ev.unicode in ".,"):
                        edit += ev.unicode
                    continue
                if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                    self.save_config()
                    return
                elif ev.key in (pygame.K_UP, pygame.K_w):
                    sel = (sel - 1) % len(self.SETTINGS)
                elif ev.key in (pygame.K_DOWN, pygame.K_s):
                    sel = (sel + 1) % len(self.SETTINGS)
                elif ev.key in (pygame.K_LEFT, pygame.K_a):
                    self.cfg[key] = self._clamp(key, self.cfg[key] - step, lo, hi)
                elif ev.key in (pygame.K_RIGHT, pygame.K_d):
                    self.cfg[key] = self._clamp(key, self.cfg[key] + step, lo, hi)
                elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    edit = ""
            self.draw_settings(dt, sel, edit)
            pygame.display.flip()

    def _clamp(self, key, val, lo, hi):
        val = max(lo, min(hi, val))
        if key in ("dpi", "targets_per_round"):
            return int(round(val))
        return round(val, 3)

    # ---------------- MENU ---------------- #

    MENU_ITEMS = [
        ("Benchmark", "flick 3 rounds at your sens — score & progress"),
        ("Tracking", "hold a strafing target — time-on-target score"),
        ("Find my sens", "adaptive sweep — ends with a verdict"),
        ("Find tracking sens", "same sweep on a strafing target"),
        ("A/B duel", "two sens values head to head, blind"),
        ("Settings", "DPI, sens, calibration"),
        ("Quit", ""),
    ]

    def draw_menu(self, dt, sel, sel_anim):
        ui = self.ui
        self.backdrop.draw(dt)

        ty = ui.cy - ui.px(300)
        r1 = ui.text(ui.f_display, "SENS FINDER", TXT,
                     center=(ui.cx - ui.px(44), ty))
        ui.text(ui.f_display, "2", ACCENT,
                topleft=(r1.right + ui.px(18), r1.top))
        ui.caps(ui.f_small, "marvel rivals · aim lab", TXT_FAINT,
                center=(ui.cx, ty + ui.px(64)))

        item_w, item_h = ui.px(620), ui.px(74)
        y0 = ui.cy - ui.px(160)
        # animated selection highlight
        hy = y0 + sel_anim * item_h
        hi_rect = pygame.Rect(ui.cx - item_w // 2, int(hy), item_w, item_h - ui.px(10))
        ui.rrect(hi_rect, (*ACCENT, 20), radius=14)
        ui.rrect((hi_rect.left, hi_rect.top, ui.px(4), hi_rect.h),
                 (*ACCENT, 220), radius=2)

        for i, (name, desc) in enumerate(self.MENU_ITEMS):
            iy = y0 + i * item_h
            active = i == sel
            ui.text(ui.f_mono, f"{i + 1}", ACCENT if active else TXT_FAINT,
                    topleft=(ui.cx - item_w // 2 + ui.px(30), iy + ui.px(12)))
            ui.text(ui.f_h2, name, TXT if active else TXT_DIM,
                    topleft=(ui.cx - item_w // 2 + ui.px(76), iy + ui.px(2)))
            if desc:
                ui.text(ui.f_small, desc, TXT_DIM if active else TXT_FAINT,
                        topleft=(ui.cx - item_w // 2 + ui.px(76), iy + ui.px(38)))

        # status bar
        cfg = self.cfg
        cur_cm = cm360_for_sens(cfg, cfg["current_sens"])
        bench = [h for h in self.history if h.get("mode") == "bench"]
        parts = [f"sens {cfg['current_sens']:.2f}", f"{cur_cm:.0f} cm/360",
                 f"dpi {cfg['dpi']}"]
        if bench:
            parts += [f"best {max(h['score'] for h in bench)}",
                      f"{len(bench)} sessions"]
        parts.append("sound on  (M)" if cfg.get("sound", True)
                     else "sound off  (M)")
        bar_w = ui.px(700)
        bar = pygame.Rect(ui.cx - bar_w // 2, ui.H - ui.px(96), bar_w, ui.px(44))
        ui.rrect(bar, (*PANEL, 210), radius=12, border=BORDER)
        ui.text(ui.f_small, "      ·      ".join(parts), TXT_DIM,
                center=bar.center)

    def menu(self):
        actions = [self.mode_bench, self.mode_track, self.mode_find,
                   self.mode_find_track, self.mode_duel, self.mode_settings,
                   None]
        if self.first_run:
            self.first_run = False
            if self.info_screen(
                "Welcome to Sens Finder", "first run — quick setup",
                ["Set your mouse DPI first — every score depends on it.",
                 "Optional: measure how many cm a 360 takes at in-game",
                 "sens 1.0 and enter it as 'cm/360 at sens 1.0' — that is",
                 "what converts verdicts into an exact in-game sens.",
                 "Until then, verdicts are still valid as cm/360 numbers."],
                hints=(("SPACE", "open settings"), ("Q", "skip"))):
                self.mode_settings()
            self.save_config()
        sel = 0
        sel_anim = 0.0
        while True:
            dt = self.clock.tick(60) / 1000.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type != pygame.KEYDOWN:
                    continue
                if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                    return
                elif ev.key == pygame.K_m:
                    self.cfg["sound"] = not self.cfg.get("sound", True)
                    self.snd.enabled = self.cfg["sound"]
                    self.snd.tick()
                elif ev.key in (pygame.K_UP, pygame.K_w):
                    sel = (sel - 1) % len(actions)
                    self.snd.tick()
                elif ev.key in (pygame.K_DOWN, pygame.K_s):
                    sel = (sel + 1) % len(actions)
                    self.snd.tick()
                elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER,
                                pygame.K_SPACE):
                    if actions[sel] is None:
                        return
                    actions[sel]()
                    pygame.event.clear()
                elif pygame.K_1 <= ev.key < pygame.K_1 + len(actions):
                    i = ev.key - pygame.K_1
                    if actions[i] is None:
                        return
                    sel = sel_anim = i
                    actions[i]()
                    pygame.event.clear()

            sel_anim += (sel - sel_anim) * min(1.0, dt * 14)
            self.draw_menu(dt, sel, sel_anim)
            pygame.display.flip()

    def shutdown(self):
        self.save_config()
        try:
            pygame.event.set_grab(False)
            pygame.mouse.set_visible(True)
        except Exception:
            pass
        pygame.quit()


# --------------------------------------------------------------------------- #
# Selftest (headless): verify the analysis picks the right level
# --------------------------------------------------------------------------- #

def _fake_targets(rng, n, ttk_mean, overshoot=0.3):
    out = []
    for i in range(n):
        out.append({
            "bucket": ("small", "medium", "large")[i % 3],
            "time_to_hit_ms": max(120.0, rng.gauss(ttk_mean, ttk_mean * 0.18)),
            "miss_clicks": 1 if rng.random() < 0.12 else 0,
            "overshoot_events": 1 if rng.random() < overshoot else 0,
            "n_microcorrections": rng.choice([0, 0, 1, 1, 2]),
            "path_efficiency": min(1.0, rng.gauss(0.88, 0.05)),
        })
    return out


def _fake_track_rounds(rng, n, on, err, jitter=0.02):
    return [{"on_pct": max(0.0, min(1.0, rng.gauss(on, jitter))),
             "mean_err": max(0.1, rng.gauss(err, jitter * 2)),
             "best_streak": rng.uniform(1.0, 5.0)} for _ in range(n)]


def _fake_points(rng, n):
    pts = []
    for _ in range(n):
        hit = rng.random() < 0.8
        r = rng.uniform(0.1, 0.9) if hit else rng.uniform(1.05, 2.2)
        a = rng.uniform(0, math.tau)
        pts.append((r * math.cos(a) + 0.12, r * math.sin(a) - 0.05, hit))
    return pts


def selftest():
    rng = random.Random(42)
    # clear winner at 45: fastest
    by_level = {
        27.0: _fake_targets(rng, 24, 720),
        36.0: _fake_targets(rng, 24, 620),
        45.0: _fake_targets(rng, 24, 540),
        56.0: _fake_targets(rng, 24, 650),
        67.0: _fake_targets(rng, 24, 780),
    }
    stats, plateau, winner = analyze_levels(by_level)
    assert winner == 45.0, f"expected 45.0, got {winner}"

    # plateau case: 40/45/50 identical speed, 45 cleanest control -> wins
    rng = random.Random(7)
    by_level = {
        40.0: _fake_targets(rng, 40, 560, overshoot=0.5),
        45.0: _fake_targets(rng, 40, 560, overshoot=0.1),
        50.0: _fake_targets(rng, 40, 560, overshoot=0.4),
    }
    stats, plateau, winner = analyze_levels(by_level)
    assert len(plateau) >= 2, f"expected a plateau, got {plateau}"
    assert winner == 45.0, f"expected 45.0 (cleanest), got {winner}"

    cfg = dict(DEFAULT_CONFIG)
    s = sens_for_cm360(cfg, 47.0)
    assert abs(s - 0.815) < 0.01, s

    sc = round_score(_fake_targets(random.Random(1), 24, 550))
    assert 0 < sc <= 1000, sc

    # bucket stats cover all three buckets and carry sane medians
    bs = bucket_stats(_fake_targets(random.Random(2), 24, 600))
    assert set(bs) == {"small", "medium", "large"}, bs
    for v in bs.values():
        assert 300 < v["ttk_median"] < 900 and 0 < v["accuracy"] <= 1

    # bootstrap: clearly-faster A wins with high confidence; equal ~= 0.5
    r = random.Random(3)
    fast = [r.gauss(520, 60) for _ in range(24)]
    slow = [r.gauss(680, 60) for _ in range(24)]
    p = bootstrap_faster_prob(fast, slow, seed=3)
    assert p > 0.9, p
    p_eq = bootstrap_faster_prob(fast, list(fast), seed=4)
    assert 0.25 < p_eq < 0.75, p_eq

    # track scoring is monotonic and bounded
    assert track_score(0.0) == 0 and track_score(1.0) == 1000
    assert track_score(0.4) < track_score(0.6)

    # track-level analysis: clear on-target winner
    r = random.Random(11)
    by_level = {
        36.0: _fake_track_rounds(r, 2, 0.48, 1.30),
        45.0: _fake_track_rounds(r, 2, 0.66, 0.85),
        56.0: _fake_track_rounds(r, 2, 0.55, 1.05),
    }
    tstats, tplateau, twinner = analyze_track_levels(by_level)
    assert twinner == 45.0, f"expected 45.0, got {twinner}"

    # plateau case: equal hold time, steadier aim (lower mean err) wins
    r = random.Random(12)
    by_level = {
        40.0: _fake_track_rounds(r, 4, 0.60, 1.10, jitter=0.0),
        45.0: _fake_track_rounds(r, 4, 0.60, 0.70, jitter=0.0),
        50.0: _fake_track_rounds(r, 4, 0.60, 0.95, jitter=0.0),
    }
    tstats, tplateau, twinner = analyze_track_levels(by_level)
    assert len(tplateau) >= 2, f"expected a plateau, got {tplateau}"
    assert twinner == 45.0, f"expected 45.0 (steadiest), got {twinner}"

    # seeded strafe motion is reproducible (matched fine passes rely on it)
    m1 = StrafeMotion(random.Random(9), 5.0)
    m2 = StrafeMotion(random.Random(9), 5.0)
    for _ in range(500):
        assert m1.step(0.016) == m2.step(0.016)

    # strafe motion stays sane over 60 simulated seconds
    m = StrafeMotion(random.Random(5), 0.0)
    for _ in range(6000):
        yaw, pitch = m.step(0.01)
        assert -6.0 < pitch < 2.0, pitch
    assert math.isfinite(yaw)

    print("selftest OK")
    print(f"  plateau case: plateau={plateau} winner={winner}")
    print(f"  sens for 47 cm/360 = {s:.3f}")
    print(f"  sample bench score @ ~550ms = {sc}")
    print(f"  bootstrap fast-vs-slow p = {p:.3f}, self-vs-self p = {p_eq:.3f}")


# --------------------------------------------------------------------------- #
# Screenshot mode (headless): render one screen to PNG for design review
# --------------------------------------------------------------------------- #

def screenshot(which, out_path):
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    app = App(headless_size=(1920, 1080))
    rng = random.Random(3)
    dt = 1 / 60.0
    app.backdrop.t = 4.0

    if which == "menu":
        app.history = [{"mode": "bench", "score": s}
                       for s in (512, 555, 540, 601)]
        app.draw_menu(dt, 1, 1.0)
    elif which == "info":
        app.draw_info(dt, "Round 3 of 5 done",
                      "sensitivity is hidden — keep going",
                      ["Take a breath."],
                      chips=[("round score", "574", ACCENT),
                             ("median hit", "541 ms", None)])
    elif which == "round":
        cam = Camera()
        tdir = dir_from_yawpitch(14.0, 3.0)
        fx = FX(app.ui)
        fx.hit(82)
        fx.update_draw(0.12)
        hud = {"title": "find · wide — round 3/5", "idx": 6, "n": 12,
               "misses": 1, "fps": 990}
        app.draw_round_frame(cam, tdir, 0.0, hud, fx, dt, hit_flash=0.4)
    elif which == "bench":
        targets = _fake_targets(rng, 36, 540)
        s = level_stats(targets)
        scores = [488, 510, 495, 531, 540, 570, 562, s["score"]]
        app.draw_bench_results(dt, s, scores, bucket_stats(targets),
                               _fake_points(rng, 42))
    elif which == "track":
        agg = {"score": 618, "on_pct": 0.66, "mean_err": 0.84,
               "best_streak": 3.7}
        app.draw_track_results(dt, agg, [402, 447, 430, 512, 555, 618])
    elif which == "duel":
        cfg = app.cfg
        sens_a, sens_b = 0.81, 0.65
        cm_a = round(cm360_for_sens(cfg, sens_a), 1)
        cm_b = round(cm360_for_sens(cfg, sens_b), 1)
        stats = {cm_a: level_stats(_fake_targets(rng, 36, 610)),
                 cm_b: level_stats(_fake_targets(rng, 36, 545))}
        app.draw_duel_verdict(dt, sens_a, sens_b, cm_a, cm_b, stats, 0.22)
    elif which == "verdict":
        by_level = {
            36.9: _fake_targets(rng, 24, 640, overshoot=0.45),
            46.1: _fake_targets(rng, 24, 545, overshoot=0.15),
            57.6: _fake_targets(rng, 24, 560, overshoot=0.35),
        }
        stats, plateau, winner = analyze_levels(by_level)
        app.draw_verdict(dt, stats, plateau, winner,
                         sens_for_cm360(app.cfg, winner))
    elif which == "trackverdict":
        r = random.Random(6)
        by_level = {
            36.9: _fake_track_rounds(r, 2, 0.51, 1.25),
            46.1: _fake_track_rounds(r, 2, 0.68, 0.78),
            57.6: _fake_track_rounds(r, 2, 0.66, 0.98),
        }
        stats, plateau, winner = analyze_track_levels(by_level)
        app.draw_track_verdict(dt, stats, plateau, winner,
                               sens_for_cm360(app.cfg, winner))
    elif which == "settings":
        app.draw_settings(dt, 2, None)
    else:
        print(f"unknown screen: {which}")
        return

    pygame.image.save(app.screen, out_path)
    pygame.quit()
    print(f"saved {which} -> {out_path}")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        which = sys.argv[i + 1] if i + 1 < len(sys.argv) else "menu"
        out = (sys.argv[i + 2] if i + 2 < len(sys.argv)
               else os.path.join(DATA_DIR, f"shot_{which}.png"))
        screenshot(which, out)
        return
    app = App()
    try:
        app.menu()
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
