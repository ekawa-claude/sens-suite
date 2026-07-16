"""
Mouse Sensitivity Finder — a minimalist FPS aim test that sweeps sensitivity
across rounds and logs detailed performance data to results.json.

Design goals (per spec):
  * Raw WM_INPUT counts via SDL relative mouse mode (preserves the Raw Accel
    driver chain, skips Windows pointer ballistics).
  * No smoothing / no interpolation of input anywhere.
  * FOV is constant; sensitivity only changes deg-per-count.
  * Accumulate *every* mouse-motion event per frame with perf_counter_ns
    timestamps (no coalescing).
  * Sensitivity expressed internally as cm/360, converted via DPI.

The JSON schema is the real deliverable — visuals are kept clean but secondary.
"""

import os
import sys
import json
import math
import time
import random
import statistics
from datetime import datetime, timezone

# Prefer SDL raw input (not warp) for relative mode so we get true raw counts.
os.environ.setdefault("SDL_MOUSE_RELATIVE_MODE_WARP", "0")

import pygame

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

APP_VERSION = "1.0.0"

# --- User setup (metadata; do not modify input) ---------------------------- #
MOUSE_DPI = 1600
RAW_ACCEL = {
    "installed": True,
    "note": "driver-level filter applied to raw input before the app sees it; "
            "not compensated or disabled by this tool",
    "curve_type": "Motivity",
    "sens_multiplier": 1,
    "growth": 1.1111,
    "motivity": 1.25,
    "midpoint": 5,
}
TARGET_GAME = "Marvel Rivals"

# --- Test methodology ------------------------------------------------------ #
DEFAULT_CENTER_CM360 = 35.0
LEVEL_MULTIPLIERS = [0.5, 0.67, 0.82, 1.0, 1.22, 1.5, 2.0]   # log-spaced-ish
# Narrow sweep: tight +/-25% band, 5 levels, for resolving a known plateau.
NARROW_MULTIPLIERS = [0.80, 0.90, 1.0, 1.11, 1.25]
N_PASSES = 3
N_PASSES_NARROW = 4   # more passes -> more samples per level for the fine test
TARGETS_PER_ROUND = 15
TARGET_ANGULAR_DIAMETER_DEG = 2.0      # within the 1.5-2.5 deg band
WARMUP_ENABLED = True

# Flick magnitude buckets (degrees) and how many of each per round.
FLICK_BUCKETS = {
    "small":  (5.0, 15.0),
    "medium": (20.0, 40.0),
    "large":  (50.0, 90.0),
}
FLICK_MIX = ["small"] * 5 + ["medium"] * 5 + ["large"] * 5   # 15 targets

# --- Rendering ------------------------------------------------------------- #
HFOV_DEG = 103.0          # constant horizontal FOV (Marvel Rivals max-ish)
PITCH_LIMIT_DEG = 88.0
FPS_CAP = 1000            # high cap; vsync off
EYE_HEIGHT = 1.6          # for the reference floor grid

# --- Metric thresholds ----------------------------------------------------- #
MICROCORR_MAG_DEG = 2.0   # reversals below this magnitude count as micro-corr
NEAR_TARGET_DEG = 8.0     # "near the target" gate for micro-corrections
SPLIT_SPEED_FRAC = 0.20   # ballistic->correction split when speed < 20% of peak

# --- Output ---------------------------------------------------------------- #
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_JSON_BYTES = 20 * 1024 * 1024

# --------------------------------------------------------------------------- #
# Sensitivity math
# --------------------------------------------------------------------------- #

CM_PER_INCH = 2.54


def cm360_to_deg_per_count(cm360: float, dpi: int = MOUSE_DPI) -> float:
    """deg/count = 360 / counts_per_360 ; counts_per_360 = dpi * cm360 / 2.54."""
    counts_per_360 = dpi * (cm360 / CM_PER_INCH)
    return 360.0 / counts_per_360


# --------------------------------------------------------------------------- #
# Tiny 3D vector helpers (no numpy dependency)
# --------------------------------------------------------------------------- #

WORLD_UP = (0.0, 1.0, 0.0)


def v_add(a, b):       return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def v_sub(a, b):       return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def v_scale(a, s):     return (a[0] * s, a[1] * s, a[2] * s)
def v_dot(a, b):       return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def v_norm(a):
    m = math.sqrt(v_dot(a, a))
    if m < 1e-12:
        return (0.0, 0.0, 0.0)
    return (a[0] / m, a[1] / m, a[2] / m)


def dir_from_yawpitch(yaw_deg, pitch_deg):
    """yaw around +Y, pitch around local X. yaw=pitch=0 -> +Z."""
    y = math.radians(yaw_deg)
    p = math.radians(pitch_deg)
    cp = math.cos(p)
    return (math.sin(y) * cp, math.sin(p), math.cos(y) * cp)


def angle_between_deg(a, b):
    d = max(-1.0, min(1.0, v_dot(a, b)))
    return math.degrees(math.acos(d))


# --------------------------------------------------------------------------- #
# Camera
# --------------------------------------------------------------------------- #

class Camera:
    def __init__(self):
        self.yaw = 0.0
        self.pitch = 0.0

    def apply_counts(self, dx, dy, deg_per_count):
        # No smoothing, no acceleration here — Raw Accel already shaped dx/dy.
        self.yaw += dx * deg_per_count
        self.pitch -= dy * deg_per_count   # invert so moving up looks up
        if self.pitch > PITCH_LIMIT_DEG:
            self.pitch = PITCH_LIMIT_DEG
        if self.pitch < -PITCH_LIMIT_DEG:
            self.pitch = -PITCH_LIMIT_DEG

    def forward(self):
        return dir_from_yawpitch(self.yaw, self.pitch)

    def basis(self):
        """Return (forward, right, up) orthonormal basis.

        right = up x forward so that screen-right is +X at yaw 0 and increasing
        yaw (mouse moving right) pans the view to the right.
        """
        f = self.forward()
        r = v_norm(v_cross(WORLD_UP, f))     # horizontal, points screen-right
        if v_dot(r, r) < 1e-9:               # looking straight up/down
            r = (1.0, 0.0, 0.0)
        u = v_cross(f, r)
        return f, r, u


# --------------------------------------------------------------------------- #
# Per-target metric tracker
# --------------------------------------------------------------------------- #

class TargetTracker:
    """Accumulates trajectory metrics for one target between spawn and hit."""

    def __init__(self, target_dir, aim_dir_at_spawn, spawn_t_ns,
                 offset_components, bucket):
        self.target_dir = target_dir
        self.spawn_t = spawn_t_ns
        self.bucket = bucket
        self.offset_h = offset_components[0]   # signed deg, +right
        self.offset_v = offset_components[1]   # signed deg, +up
        self.ideal_dist = angle_between_deg(aim_dir_at_spawn, target_dir)

        # Target tangent-plane frame (for signed approach-axis analysis).
        # Same handedness as the camera basis: +right, +up.
        ft = v_norm(target_dir)
        rt = v_norm(v_cross(WORLD_UP, ft))
        if v_dot(rt, rt) < 1e-9:
            rt = (1.0, 0.0, 0.0)
        ut = v_cross(ft, rt)
        self._ft, self._rt, self._ut = ft, rt, ut

        init_off = self._offset2d(aim_dir_at_spawn)
        self._init_off = init_off
        m = math.hypot(*init_off)
        # approach axis = unit vector pointing from start toward target (= -init)
        self._approach = (-init_off[0] / m, -init_off[1] / m) if m > 1e-9 else (1.0, 0.0)

        # running state
        self.actual_path = 0.0
        self.max_overshoot = 0.0
        self.overshoot_events = 0
        self.microcorrections = 0
        self._prev_dir = aim_dir_at_spawn
        self._prev_off = init_off
        self._prev_delta = None
        self._prev_signed = self._signed(init_off)
        self._samples = []   # (t_ns, speed_dps, reversal_bool)

    def _offset2d(self, d):
        a = v_dot(d, self._rt)
        b = v_dot(d, self._ut)
        c = v_dot(d, self._ft)
        return (math.degrees(math.atan2(a, c)),
                math.degrees(math.atan2(b, c)))

    def _signed(self, off):
        return off[0] * self._approach[0] + off[1] * self._approach[1]

    def update(self, aim_dir, t_ns, dt_s):
        off = self._offset2d(aim_dir)
        ang = angle_between_deg(aim_dir, self.target_dir)

        # path length via great-circle step
        step = angle_between_deg(self._prev_dir, aim_dir)
        self.actual_path += step

        # tangent-plane movement this frame
        delta = (off[0] - self._prev_off[0], off[1] - self._prev_off[1])
        dmag = math.hypot(*delta)
        speed = (step / dt_s) if dt_s > 1e-9 else 0.0

        reversal = False
        if self._prev_delta is not None:
            if (delta[0] * self._prev_delta[0] +
                    delta[1] * self._prev_delta[1]) < 0:
                reversal = True
                if dmag < MICROCORR_MAG_DEG and ang < NEAR_TARGET_DEG:
                    self.microcorrections += 1

        # overshoot along approach axis: signed crosses 0 upward = crossed center
        signed = self._signed(off)
        if self._prev_signed <= 0.0 < signed:
            self.overshoot_events += 1
        if signed > self.max_overshoot:
            self.max_overshoot = signed

        self._samples.append((t_ns, speed, reversal))
        self._prev_dir = aim_dir
        self._prev_off = off
        if dmag > 1e-9:
            self._prev_delta = delta
        self._prev_signed = signed

    def finalize(self, hit_t_ns, miss_clicks):
        ttk_ms = (hit_t_ns - self.spawn_t) / 1e6
        path_eff = 1.0
        if self.actual_path > 1e-6:
            path_eff = min(1.0, self.ideal_dist / self.actual_path)

        # ballistic vs correction split
        ballistic_ms = ttk_ms
        correction_ms = 0.0
        if self._samples:
            peak = max(s[1] for s in self._samples)
            peak_idx = max(range(len(self._samples)),
                           key=lambda i: self._samples[i][1])
            split_t = None
            for i in range(peak_idx, len(self._samples)):
                t_i, sp_i, rev_i = self._samples[i]
                if rev_i or sp_i < SPLIT_SPEED_FRAC * peak:
                    split_t = t_i
                    break
            if split_t is not None:
                ballistic_ms = (split_t - self.spawn_t) / 1e6
                correction_ms = (hit_t_ns - split_t) / 1e6

        return {
            "bucket": self.bucket,
            "spawn_offset_deg": round(self.ideal_dist, 4),
            "spawn_offset_h_deg": round(self.offset_h, 4),
            "spawn_offset_v_deg": round(self.offset_v, 4),
            "time_to_hit_ms": round(ttk_ms, 3),
            "miss_clicks": miss_clicks,
            "path_efficiency": round(path_eff, 4),
            "actual_path_deg": round(self.actual_path, 4),
            "overshoot_events": self.overshoot_events,
            "max_overshoot_deg": round(self.max_overshoot, 4),
            "n_microcorrections": self.microcorrections,
            "ballistic_phase_ms": round(ballistic_ms, 3),
            "correction_phase_ms": round(correction_ms, 3),
        }


# --------------------------------------------------------------------------- #
# Test plan
# --------------------------------------------------------------------------- #

def build_test_plan(center_cm360, rng, multipliers=None, n_passes=None):
    """Return list of round-specs: dicts with cm360, level_index, pass_index."""
    multipliers = multipliers if multipliers is not None else LEVEL_MULTIPLIERS
    n_passes = n_passes if n_passes is not None else N_PASSES
    levels = [round(center_cm360 * m, 4) for m in multipliers]
    rounds = []
    for p in range(n_passes):
        order = list(range(len(levels)))
        rng.shuffle(order)
        for li in order:
            rounds.append({
                "pass_index": p,
                "level_index": li,
                "level_multiplier": multipliers[li],
                "cm360": levels[li],
                "deg_per_count": cm360_to_deg_per_count(levels[li]),
            })
    return levels, rounds


def make_round_flicks(rng):
    """15 flick specs: (bucket, h_offset_deg signed, v_offset_deg signed)."""
    mix = FLICK_MIX[:]
    rng.shuffle(mix)
    flicks = []
    sign = 1 if rng.random() < 0.5 else -1
    for bucket in mix:
        lo, hi = FLICK_BUCKETS[bucket]
        mag = rng.uniform(lo, hi)
        h = sign * mag
        sign = -sign  # alternate left/right for balance
        v = rng.uniform(-1.0, 1.0) * (0.15 * mag)   # slight vertical variation
        flicks.append((bucket, h, v))
    return flicks


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

# Palette
C_SKY_TOP = (14, 16, 26)
C_SKY_BOT = (30, 36, 54)
C_GRID = (54, 62, 92)
C_GRID_AXIS = (80, 92, 130)
C_TARGET = (255, 86, 86)
C_TARGET_RING = (255, 180, 180)
C_TARGET_GLOW = (255, 120, 120)
C_CROSS = (90, 255, 170)
C_TEXT = (220, 226, 240)
C_TEXT_DIM = (140, 150, 175)
C_HUD_BG = (10, 12, 20)
C_ARROW = (255, 200, 90)
C_HIT_FLASH = (255, 255, 255)


class Renderer:
    def __init__(self, surface):
        self.s = surface
        self.W, self.H = surface.get_size()
        self.cx, self.cy = self.W // 2, self.H // 2
        # focal length from horizontal FOV (square pixels -> same focal in y)
        self.focal = (self.W / 2.0) / math.tan(math.radians(HFOV_DEG) / 2.0)
        self.vfov_deg = 2.0 * math.degrees(math.atan((self.H / 2.0) / self.focal))
        self._sky = self._make_sky()
        pygame.font.init()
        self.font_s = pygame.font.SysFont("consolas", 20)
        self.font_m = pygame.font.SysFont("consolas", 30, bold=True)
        self.font_l = pygame.font.SysFont("consolas", 56, bold=True)

    def _make_sky(self):
        sky = pygame.Surface((self.W, self.H))
        for y in range(self.H):
            t = y / max(1, self.H - 1)
            col = tuple(int(C_SKY_TOP[i] + (C_SKY_BOT[i] - C_SKY_TOP[i]) * t)
                        for i in range(3))
            pygame.draw.line(sky, col, (0, y), (self.W, y))
        try:
            return sky.convert()   # match display format -> fast blit
        except pygame.error:
            return sky             # no classic display (e.g. _sdl2 GPU path)

    # -- projection -------------------------------------------------------- #
    def _view(self, world_pos, basis):
        f, r, u = basis
        return (v_dot(world_pos, r), v_dot(world_pos, u), v_dot(world_pos, f))

    def _project_view(self, v):
        return (self.cx + self.focal * v[0] / v[2],
                self.cy - self.focal * v[1] / v[2])

    def _clip_segment(self, v0, v1, eps=0.01):
        z0, z1 = v0[2], v1[2]
        if z0 <= eps and z1 <= eps:
            return None
        if z0 < eps:
            t = (eps - z0) / (z1 - z0)
            v0 = (v0[0] + (v1[0] - v0[0]) * t,
                  v0[1] + (v1[1] - v0[1]) * t, eps)
        elif z1 < eps:
            t = (eps - z1) / (z0 - z1)
            v1 = (v1[0] + (v0[0] - v1[0]) * t,
                  v1[1] + (v0[1] - v1[1]) * t, eps)
        p0 = self._project_view(v0)
        p1 = self._project_view(v1)
        # Clip the projected segment to the screen rect. Without this, points
        # just past the near plane project to coordinates in the millions and
        # draw.line rasterizes an astronomically long span (tens of ms/line).
        return self._clip_2d(p0[0], p0[1], p1[0], p1[1])

    def _clip_2d(self, x0, y0, x1, y1, margin=2):
        """Liang-Barsky clip of a 2D segment to the viewport rectangle."""
        xmin, ymin = -margin, -margin
        xmax, ymax = self.W + margin, self.H + margin
        dx, dy = x1 - x0, y1 - y0
        p = (-dx, dx, -dy, dy)
        q = (x0 - xmin, xmax - x0, y0 - ymin, ymax - y0)
        u1, u2 = 0.0, 1.0
        for pi, qi in zip(p, q):
            if pi == 0:
                if qi < 0:
                    return None          # parallel and outside
            else:
                t = qi / pi
                if pi < 0:
                    if t > u2:
                        return None
                    if t > u1:
                        u1 = t
                else:
                    if t < u1:
                        return None
                    if t < u2:
                        u2 = t
        return ((x0 + u1 * dx, y0 + u1 * dy),
                (x0 + u2 * dx, y0 + u2 * dy))

    # -- scene ------------------------------------------------------------- #
    def draw_world(self, cam):
        self.s.blit(self._sky, (0, 0))
        basis = cam.basis()
        y = -EYE_HEIGHT
        rng = 40
        step = 4
        for gx in range(-rng, rng + 1, step):
            p0 = (float(gx), y, float(-rng))
            p1 = (float(gx), y, float(rng))
            seg = self._clip_segment(self._view(p0, basis), self._view(p1, basis))
            if seg:
                col = C_GRID_AXIS if gx == 0 else C_GRID
                pygame.draw.line(self.s, col, seg[0], seg[1], 1)
        for gz in range(-rng, rng + 1, step):
            p0 = (float(-rng), y, float(gz))
            p1 = (float(rng), y, float(gz))
            seg = self._clip_segment(self._view(p0, basis), self._view(p1, basis))
            if seg:
                col = C_GRID_AXIS if gz == 0 else C_GRID
                pygame.draw.line(self.s, col, seg[0], seg[1], 1)

    def draw_target(self, cam, target_dir, spawn_anim=0.0):
        """Draw the target if on-screen, else an edge arrow pointing to it."""
        f, r, u = cam.basis()
        tx = v_dot(target_dir, r)
        ty = v_dot(target_dir, u)
        tz = v_dot(target_dir, f)

        on_screen = False
        if tz > 1e-4:
            sx = self.cx + self.focal * tx / tz
            sy = self.cy - self.focal * ty / tz
            ang_r = math.radians(TARGET_ANGULAR_DIAMETER_DEG / 2.0)
            rad = max(3.0, self.focal * math.tan(ang_r) / tz)
            margin = rad + 4
            if -margin <= sx <= self.W + margin and -margin <= sy <= self.H + margin:
                on_screen = True
                self._blit_target(sx, sy, rad, spawn_anim)

        if not on_screen:
            self._draw_edge_arrow(tx, ty)

    def _blit_target(self, sx, sy, rad, spawn_anim):
        sx, sy = int(sx), int(sy)
        # spawn ping
        if spawn_anim > 0:
            ping_r = int(rad + rad * 3 * spawn_anim)
            a = max(0, int(180 * (1 - spawn_anim)))
            ping = pygame.Surface((ping_r * 2 + 4, ping_r * 2 + 4), pygame.SRCALPHA)
            pygame.draw.circle(ping, (*C_TARGET_GLOW, a),
                               (ping_r + 2, ping_r + 2), ping_r, 3)
            self.s.blit(ping, (sx - ping_r - 2, sy - ping_r - 2))
        # soft glow
        gr = int(rad * 2.2)
        glow = pygame.Surface((gr * 2, gr * 2), pygame.SRCALPHA)
        for i in range(4, 0, -1):
            a = 26 * i // 4
            pygame.draw.circle(glow, (*C_TARGET_GLOW, a), (gr, gr),
                               int(rad + i * rad * 0.4))
        self.s.blit(glow, (sx - gr, sy - gr))
        # body + ring + center
        pygame.draw.circle(self.s, C_TARGET, (sx, sy), int(rad))
        pygame.draw.circle(self.s, C_TARGET_RING, (sx, sy), int(rad), 2)
        pygame.draw.circle(self.s, (255, 255, 255), (sx, sy), max(1, int(rad * 0.18)))

    def _draw_edge_arrow(self, tx, ty):
        dirx, diry = tx, -ty
        m = math.hypot(dirx, diry)
        if m < 1e-6:
            return
        dirx, diry = dirx / m, diry / m
        margin = 70
        px = self.cx + dirx * (self.cx - margin)
        py = self.cy + diry * (self.cy - margin)
        ang = math.atan2(diry, dirx)
        size = 18
        tip = (px + math.cos(ang) * size, py + math.sin(ang) * size)
        left = (px + math.cos(ang + 2.4) * size, py + math.sin(ang + 2.4) * size)
        right = (px + math.cos(ang - 2.4) * size, py + math.sin(ang - 2.4) * size)
        pygame.draw.polygon(self.s, C_ARROW, [tip, left, right])

    def draw_crosshair(self, flash=0.0):
        c = C_CROSS
        if flash > 0:
            c = tuple(int(C_CROSS[i] + (C_HIT_FLASH[i] - C_CROSS[i]) * flash)
                      for i in range(3))
        gap, length, thick = 6, 14, 2
        x, y = self.cx, self.cy
        pygame.draw.line(self.s, c, (x - gap - length, y), (x - gap, y), thick)
        pygame.draw.line(self.s, c, (x + gap, y), (x + gap + length, y), thick)
        pygame.draw.line(self.s, c, (x, y - gap - length), (x, y - gap), thick)
        pygame.draw.line(self.s, c, (x, y + gap), (x, y + gap + length), thick)
        pygame.draw.circle(self.s, c, (x, y), 2)

    # -- text helpers ------------------------------------------------------ #
    def text(self, s, font, color, center=None, topleft=None):
        surf = font.render(s, True, color)
        rect = surf.get_rect()
        if center:
            rect.center = center
        elif topleft:
            rect.topleft = topleft
        self.s.blit(surf, rect)
        return rect

    def hud(self, lines):
        pad = 12
        surfs = [self.font_s.render(t, True, C_TEXT) for t in lines]
        w = max(s.get_width() for s in surfs) + pad * 2
        h = sum(s.get_height() for s in surfs) + pad * 2
        box = pygame.Surface((w, h), pygame.SRCALPHA)
        box.fill((*C_HUD_BG, 170))
        self.s.blit(box, (16, 16))
        y = 16 + pad
        for surf in surfs:
            self.s.blit(surf, (16 + pad, y))
            y += surf.get_height()

    def overlay(self, alpha=180):
        ov = pygame.Surface((self.W, self.H), pygame.SRCALPHA)
        ov.fill((6, 8, 14, alpha))
        self.s.blit(ov, (0, 0))


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #

# Game states
ST_BREAK = "break"
ST_PLAYING = "playing"
ST_PAUSED = "paused"
ST_DONE = "done"


def ask_center_cm360(default=DEFAULT_CENTER_CM360):
    print("=" * 60)
    print("  Mouse Sensitivity Finder")
    print("=" * 60)
    print(f"Mouse DPI: {MOUSE_DPI}  |  Target game: {TARGET_GAME}")
    prompt = (f"\nEnter your current cm/360 (or in-game feel) "
              f"[default {default}]: ")
    try:
        raw = input(prompt).strip()
    except EOFError:
        raw = ""
    if not raw:
        return default
    try:
        val = float(raw.replace(",", "."))
        if val <= 0 or val > 200:
            raise ValueError
        return val
    except ValueError:
        print(f"  invalid -> using default {default}")
        return default


def parse_args():
    import argparse
    p = argparse.ArgumentParser(description="Mouse sensitivity finder")
    p.add_argument("--preset", choices=["wide", "narrow", "matched"],
                   default="wide",
                   help="wide = 7 levels x0.5..x2.0 (default); "
                        "narrow = 5 levels +/-25%% around center; "
                        "matched = narrow band but every level in a pass gets "
                        "the identical flick set (paired comparison)")
    p.add_argument("--center", type=float, default=None,
                   help="center cm/360 (skips the prompt)")
    p.add_argument("--passes", type=int, default=None,
                   help="override number of passes")
    p.add_argument("--out", type=str, default=None,
                   help="output json filename (default depends on preset)")
    return p.parse_args()


def main():
    args = parse_args()
    matched = args.preset == "matched"
    if args.preset in ("narrow", "matched"):
        multipliers = NARROW_MULTIPLIERS
        n_passes = args.passes if args.passes else N_PASSES_NARROW
        default_center = 45.0
        out_name = args.out or (f"results_{args.preset}.json")
    else:
        multipliers = LEVEL_MULTIPLIERS
        n_passes = args.passes if args.passes else N_PASSES
        default_center = DEFAULT_CENTER_CM360
        out_name = args.out or "results.json"

    if args.center is not None:
        center_cm360 = args.center
    else:
        center_cm360 = ask_center_cm360(default_center)

    seed = int(time.time())
    rng = random.Random(seed)
    levels, plan = build_test_plan(center_cm360, rng, multipliers, n_passes)

    print(f"\nPreset: {args.preset}  |  center {center_cm360} cm/360")
    print(f"Sensitivity levels (cm/360): {levels}")
    print(f"Plan: warmup + {len(plan)} scored rounds "
          f"({n_passes} passes x {len(levels)} levels), "
          f"{TARGETS_PER_ROUND} targets each.")
    print("\nControls: aim with mouse, left-click to shoot, "
          "SPACE to start a round, ESC to pause.\n")

    pygame.init()
    info = pygame.display.Info()
    # Native fullscreen, no SCALED (SCALED forces a full-frame software rescale
    # every frame -> huge cost at 4K). vsync off to minimize input latency.
    try:
        screen = pygame.display.set_mode(
            (info.current_w, info.current_h),
            pygame.FULLSCREEN | pygame.DOUBLEBUF, vsync=0)
    except pygame.error:
        screen = pygame.display.set_mode(
            (info.current_w, info.current_h), pygame.FULLSCREEN)
    pygame.display.set_caption("Mouse Sensitivity Finder")

    # Relative mouse mode -> raw counts, no Windows ballistics.
    pygame.event.set_grab(True)
    pygame.mouse.set_visible(False)
    rel_mode = False
    if hasattr(pygame.mouse, "set_relative_mode"):
        try:
            pygame.mouse.set_relative_mode(True)
            rel_mode = True
        except Exception:
            rel_mode = False
    if not rel_mode:
        print("WARNING: SDL relative mode unavailable; falling back to grab. "
              "Upgrade pygame-ce for cleaner raw input.")

    renderer = Renderer(screen)
    clock = pygame.time.Clock()

    # session-wide motion diagnostics (verifies multi-event-per-frame capture)
    diag = {"frames": 0, "frames_multi_motion": 0,
            "max_events_per_frame": 0, "total_motion_events": 0}

    results_rounds = []
    aborted = False

    # matched preset: precompute one shared flick set per pass, so every level
    # within a pass faces identical flicks (paired comparison), while the set
    # differs across passes (avoids memorization).
    pass_flicks = None
    if matched:
        pass_flicks = {p: make_round_flicks(rng) for p in range(n_passes)}

    # ---- helper: run one round, returns (round_record or None, abort_flag) -- #
    def run_round(round_index, total_scored, spec, scored, warmup=False,
                  flicks=None):
        cam = Camera()
        deg_per_count = (spec["deg_per_count"] if spec
                         else cm360_to_deg_per_count(center_cm360))

        # matched preset supplies a shared flick set; otherwise draw a fresh one
        flicks = flicks if flicks is not None else make_round_flicks(rng)
        raw_t, raw_dx, raw_dy = [], [], []
        target_records = []

        # spawn first target relative to initial aim
        def spawn(idx):
            bucket, h, v = flicks[idx]
            t_yaw = cam.yaw + h
            t_pitch = max(-PITCH_LIMIT_DEG + 1,
                          min(PITCH_LIMIT_DEG - 1, cam.pitch + v))
            tdir = dir_from_yawpitch(t_yaw, t_pitch)
            now = time.perf_counter_ns()
            tracker = TargetTracker(tdir, cam.forward(), now, (h, v), bucket)
            return tdir, tracker, now

        target_idx = 0
        target_dir, tracker, spawn_t = spawn(0)
        miss_clicks = 0
        spawn_anim_t = time.perf_counter()
        hit_flash = 0.0

        state = ST_PLAYING
        prev_perf = time.perf_counter_ns()

        while True:
            frame_t = time.perf_counter_ns()
            dt_s = (frame_t - prev_perf) / 1e9
            prev_perf = frame_t

            motion_this_frame = 0
            click_events = []

            for ev in pygame.event.get():
                ev_t = time.perf_counter_ns()
                if ev.type == pygame.QUIT:
                    return None, True
                elif ev.type == pygame.MOUSEMOTION:
                    dx, dy = ev.rel
                    if dx == 0 and dy == 0:
                        continue
                    motion_this_frame += 1
                    if state == ST_PLAYING:
                        cam.apply_counts(dx, dy, deg_per_count)
                        raw_t.append(ev_t)
                        raw_dx.append(dx)
                        raw_dy.append(dy)
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    click_events.append(ev_t)
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_ESCAPE:
                        state = ST_PAUSED if state == ST_PLAYING else ST_PLAYING
                        prev_perf = time.perf_counter_ns()
                    elif state == ST_PAUSED and ev.key == pygame.K_q:
                        return None, True   # abort, save partial

            # diagnostics
            diag["frames"] += 1
            diag["total_motion_events"] += motion_this_frame
            if motion_this_frame > diag["max_events_per_frame"]:
                diag["max_events_per_frame"] = motion_this_frame
            if motion_this_frame > 1:
                diag["frames_multi_motion"] += 1

            if state == ST_PLAYING:
                # update tracker with current aim
                tracker.update(cam.forward(), frame_t, dt_s)

                # process clicks (hit / miss)
                for ct in click_events:
                    ang = angle_between_deg(cam.forward(), target_dir)
                    if ang <= TARGET_ANGULAR_DIAMETER_DEG / 2.0:
                        rec = tracker.finalize(ct, miss_clicks)
                        rec["target_index"] = target_idx
                        target_records.append(rec)
                        hit_flash = 1.0
                        target_idx += 1
                        if target_idx >= TARGETS_PER_ROUND:
                            target_dir = None
                            break
                        miss_clicks = 0
                        target_dir, tracker, spawn_t = spawn(target_idx)
                        spawn_anim_t = time.perf_counter()
                    else:
                        miss_clicks += 1

                if target_dir is None:
                    break  # round complete

            # ---- render ---- #
            renderer.draw_world(cam)
            if target_dir is not None:
                anim = max(0.0, 1.0 - (time.perf_counter() - spawn_anim_t) / 0.35)
                renderer.draw_target(cam, target_dir, spawn_anim=anim)
            renderer.draw_crosshair(flash=hit_flash)
            hit_flash = max(0.0, hit_flash - dt_s * 4)

            label = "WARMUP" if warmup else f"Round {round_index}/{total_scored}"
            renderer.hud([
                label,
                f"Target {min(target_idx + 1, TARGETS_PER_ROUND)}/{TARGETS_PER_ROUND}",
                f"Misses this target: {miss_clicks}",
                f"FPS: {clock.get_fps():.0f}",
                "ESC: pause",
            ])

            if state == ST_PAUSED:
                renderer.overlay()
                renderer.text("PAUSED", renderer.font_l, C_TEXT,
                              center=(renderer.cx, renderer.cy - 60))
                renderer.text("ESC  resume    Q  abort & save",
                              renderer.font_m, C_TEXT_DIM,
                              center=(renderer.cx, renderer.cy + 20))

            pygame.display.flip()
            clock.tick(FPS_CAP)

        # ---- assemble round record ---- #
        if warmup or not scored:
            # still record warmup for completeness, flagged unscored
            pass

        hits = len(target_records)
        total_miss = sum(t["miss_clicks"] for t in target_records)
        accuracy = hits / (hits + total_miss) if (hits + total_miss) else 0.0

        def agg(key):
            vals = [t[key] for t in target_records]
            if not vals:
                return None
            return {
                "mean": round(statistics.fmean(vals), 4),
                "median": round(statistics.median(vals), 4),
                "std": round(statistics.pstdev(vals), 4) if len(vals) > 1 else 0.0,
            }

        record = {
            "round_index": round_index,
            "scored": scored and not warmup,
            "warmup": warmup,
            "pass_index": spec["pass_index"] if spec else None,
            "level_index": spec["level_index"] if spec else None,
            "level_multiplier": spec["level_multiplier"] if spec else None,
            "sensitivity_cm360": spec["cm360"] if spec else round(center_cm360, 4),
            "deg_per_count": round(deg_per_count, 8),
            "accuracy": round(accuracy, 4),
            "total_miss_clicks": total_miss,
            "n_targets": hits,
            "aggregates": {
                "time_to_hit_ms": agg("time_to_hit_ms"),
                "path_efficiency": agg("path_efficiency"),
                "overshoot_events": agg("overshoot_events"),
                "max_overshoot_deg": agg("max_overshoot_deg"),
                "n_microcorrections": agg("n_microcorrections"),
                "ballistic_phase_ms": agg("ballistic_phase_ms"),
                "correction_phase_ms": agg("correction_phase_ms"),
            },
            "targets": target_records,
            "raw_motion": {"t_ns": raw_t, "dx": raw_dx, "dy": raw_dy},
        }
        return record, False

    # ---- break / info screen ---- #
    def break_screen(title, subtitle, lines):
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return False
                if ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_SPACE:
                        # flush any motion accumulated while idle
                        pygame.event.clear(pygame.MOUSEMOTION)
                        return True
                    if ev.key == pygame.K_q:
                        return False
            screen.fill(C_SKY_TOP)
            renderer.overlay(alpha=255)
            renderer.text(title, renderer.font_l, C_TEXT,
                          center=(renderer.cx, renderer.cy - 140))
            if subtitle:
                renderer.text(subtitle, renderer.font_m, C_TEXT_DIM,
                              center=(renderer.cx, renderer.cy - 70))
            y = renderer.cy - 10
            for ln in lines:
                renderer.text(ln, renderer.font_s, C_TEXT,
                              center=(renderer.cx, y))
                y += 34
            renderer.text("SPACE  continue        Q  quit & save",
                          renderer.font_s, C_TEXT_DIM,
                          center=(renderer.cx, renderer.cy + 180))
            pygame.display.flip()
            clock.tick(120)

    # ====================== run the session ============================== #
    try:
        # warmup
        if WARMUP_ENABLED:
            go = break_screen("Warmup", "1 unscored round at your center feel",
                              ["Aim and left-click each target.",
                               "Large flicks may start off-screen — follow the arrow."])
            if go:
                _, ab = run_round(0, len(plan), None, scored=False, warmup=True)
                aborted = ab
            else:
                aborted = True

        # scored rounds (sensitivity kept blind)
        if not aborted:
            go = break_screen("Test begins",
                              f"{len(plan)} rounds — sensitivity is hidden",
                              ["Just aim naturally and shoot fast & accurate."])
            aborted = not go

        for i, spec in enumerate(plan, start=1):
            if aborted:
                break
            fl = pass_flicks[spec["pass_index"]] if matched else None
            rec, ab = run_round(i, len(plan), spec, scored=True, warmup=False,
                                flicks=fl)
            if rec:
                results_rounds.append(rec)
            if ab:
                aborted = True
                break
            if i < len(plan):
                acc = rec["accuracy"] * 100 if rec else 0
                ttk = rec["aggregates"]["time_to_hit_ms"]
                ttk_txt = f"{ttk['median']:.0f} ms" if ttk else "-"
                go = break_screen(
                    f"Break  ({i}/{len(plan)} done)",
                    "Sensitivity hidden — keep going",
                    [f"Last round: {acc:.0f}% accuracy, median time-to-hit {ttk_txt}",
                     "Take a breath, then continue."])
                aborted = not go

    finally:
        pygame.event.set_grab(False)
        pygame.mouse.set_visible(True)
        pygame.quit()

    # ====================== save results ================================= #
    path = save_results(center_cm360, levels, plan, results_rounds,
                        renderer, diag, seed, aborted,
                        multipliers, n_passes, args.preset, out_name)
    print("\n" + "=" * 60)
    print(f"Session {'ABORTED (partial data saved)' if aborted else 'complete'}.")
    print(f"Rounds recorded: {len(results_rounds)}")
    print(f"Motion diagnostics: max events/frame = {diag['max_events_per_frame']}, "
          f"frames with >1 motion event = {diag['frames_multi_motion']}/"
          f"{diag['frames']}, total motion events = {diag['total_motion_events']}")
    print(f"\nresults.json -> {path}")
    print("=" * 60)


def save_results(center_cm360, levels, plan, rounds, renderer, diag, seed,
                 aborted, multipliers=None, n_passes=None, preset="wide",
                 out_name="results.json"):
    multipliers = multipliers if multipliers is not None else LEVEL_MULTIPLIERS
    n_passes = n_passes if n_passes is not None else N_PASSES
    metadata = {
        "app_version": APP_VERSION,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "rng_seed": seed,
        "aborted": aborted,
        "mouse_dpi": MOUSE_DPI,
        "raw_accel": RAW_ACCEL,
        "target_game": TARGET_GAME,
        "os": f"{sys.platform}",
        "pygame_version": pygame.version.ver,
        "sdl_version": ".".join(str(x) for x in pygame.version.SDL),
        "relative_mouse_mode": True,
        "input_notes": "raw WM_INPUT counts via SDL relative mode; "
                       "no smoothing/interpolation; Raw Accel chain preserved",
        "screen_resolution": [renderer.W, renderer.H],
        "fov": {
            "horizontal_deg": HFOV_DEG,
            "vertical_deg": round(renderer.vfov_deg, 4),
            "focal_px": round(renderer.focal, 4),
            "constant": True,
        },
        "methodology": {
            "preset": preset,
            "center_cm360": center_cm360,
            "level_multipliers": multipliers,
            "levels_cm360": levels,
            "n_passes": n_passes,
            "targets_per_round": TARGETS_PER_ROUND,
            "target_angular_diameter_deg": TARGET_ANGULAR_DIAMETER_DEG,
            "flick_buckets_deg": FLICK_BUCKETS,
            "flick_mix": FLICK_MIX,
            "warmup": WARMUP_ENABLED,
            "matched_flicks_within_pass": preset == "matched",
            "blind_to_sensitivity": True,
            "ballistic_split_rule": "first reversal or speed < 20% of peak after peak",
            "microcorrection_rule":
                f"direction reversal with magnitude < {MICROCORR_MAG_DEG} deg "
                f"within {NEAR_TARGET_DEG} deg of target",
        },
        "motion_diagnostics": diag,
    }

    payload = {"metadata": metadata, "rounds": rounds}
    path = os.path.join(OUTPUT_DIR, out_name)

    # serialize; downsample raw_motion if file would exceed the size cap.
    text = json.dumps(payload, separators=(",", ":"))
    downsample = 1
    while len(text.encode("utf-8")) > MAX_JSON_BYTES and downsample < 64:
        downsample *= 2
        for r in payload["rounds"]:
            rm = r.get("raw_motion")
            if rm and "t_ns" in rm:
                # if we already downsampled, recompute from a kept copy
                src = r.setdefault("_raw_full", {
                    "t_ns": rm["t_ns"], "dx": rm["dx"], "dy": rm["dy"]})
                r["raw_motion"] = {
                    "t_ns": src["t_ns"][::downsample],
                    "dx": src["dx"][::downsample],
                    "dy": src["dy"][::downsample],
                }
        text = json.dumps(
            {"metadata": metadata,
             "rounds": [{k: v for k, v in r.items() if k != "_raw_full"}
                        for r in payload["rounds"]]},
            separators=(",", ":"))

    if downsample > 1:
        metadata["raw_motion_downsample_factor"] = downsample
        for r in payload["rounds"]:
            r.pop("_raw_full", None)
        text = json.dumps(payload, separators=(",", ":"))

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


if __name__ == "__main__":
    main()
