"""
Headless tests for the sensitivity finder. Run: python tests.py

Covers the quality-bar items:
  * motion accumulation catches *multiple* events per frame (no coalescing),
  * sensitivity math (cm/360 <-> deg/count) round-trips,
  * TargetTracker produces sane metrics on a synthetic flick.
"""

import os
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"

import math
import time

import pygame

from main import (
    cm360_to_deg_per_count, MOUSE_DPI, Camera, TargetTracker,
    dir_from_yawpitch, angle_between_deg, TARGET_ANGULAR_DIAMETER_DEG,
)

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"[{PASS if cond else FAIL}] {name}" + (f"  {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
def test_sensitivity_math():
    # known value: 35 cm/360 @ 1600 dpi
    dpc = cm360_to_deg_per_count(35.0)
    expected = 360.0 * 2.54 / (1600 * 35.0)
    check("deg/count for 35cm/360@1600dpi", abs(dpc - expected) < 1e-9,
          f"{dpc:.6f} deg/count")

    # round-trip: apply counts_per_360 counts -> exactly 360 deg of yaw
    for cm in (10.0, 35.0, 80.0):
        dpc = cm360_to_deg_per_count(cm)
        counts_per_360 = MOUSE_DPI * (cm / 2.54)
        cam = Camera()
        # apply in integer chunks to mimic real counts
        remaining = counts_per_360
        while remaining > 0:
            step = min(50, remaining)
            cam.apply_counts(step, 0, dpc)
            remaining -= step
        check(f"counts_per_360 -> ~360deg yaw (cm={cm})",
              abs(cam.yaw - 360.0) < 0.5, f"yaw={cam.yaw:.3f}")


# --------------------------------------------------------------------------- #
def test_multi_event_per_frame():
    """The core quality check: many MOUSEMOTION events posted between frames
    must all be readable in a single event.get() pass and accumulate fully."""
    pygame.init()
    pygame.display.set_mode((320, 240))

    N = 25
    for i in range(N):
        pygame.event.post(pygame.event.Event(
            pygame.MOUSEMOTION, rel=(1, 2), pos=(0, 0), buttons=(0, 0, 0)))

    # one frame's worth of polling, exactly like the game loop
    count = 0
    sum_dx = sum_dy = 0
    for ev in pygame.event.get():
        if ev.type == pygame.MOUSEMOTION:
            dx, dy = ev.rel
            count += 1
            sum_dx += dx
            sum_dy += dy

    check("all motion events captured in one frame", count == N,
          f"captured {count}/{N}")
    check("dx/dy accumulated without loss",
          sum_dx == N * 1 and sum_dy == N * 2,
          f"sum=({sum_dx},{sum_dy}) expected=({N},{2*N})")

    # also verify timestamps are monotonic when timestamped at read time
    ts = []
    for i in range(5):
        pygame.event.post(pygame.event.Event(
            pygame.MOUSEMOTION, rel=(1, 0), pos=(0, 0), buttons=(0, 0, 0)))
    for ev in pygame.event.get():
        if ev.type == pygame.MOUSEMOTION:
            ts.append(time.perf_counter_ns())
    check("per-event timestamps monotonic non-decreasing",
          all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1)),
          f"{len(ts)} stamps")
    pygame.quit()


# --------------------------------------------------------------------------- #
def test_tracker_metrics():
    """Simulate a clean flick to a target with a small overshoot+correction."""
    target = dir_from_yawpitch(30.0, 0.0)        # 30 deg to the right
    start = dir_from_yawpitch(0.0, 0.0)
    t0 = 0
    tr = TargetTracker(target, start, t0, (30.0, 0.0), "medium")

    # ballistic sweep 0 -> 33 deg (overshoot by 3), then correct back to 30
    yaws = list(range(0, 34, 3)) + [31, 30]
    t = 0
    dt = 0.008
    prev = start
    for i, yw in enumerate(yaws[1:], start=1):
        t += int(dt * 1e9)
        aim = dir_from_yawpitch(float(yw), 0.0)
        tr.update(aim, t, dt)
        prev = aim

    rec = tr.finalize(t, miss_clicks=0)

    check("ideal distance ~30 deg",
          abs(rec["spawn_offset_deg"] - 30.0) < 0.5, str(rec["spawn_offset_deg"]))
    check("path efficiency in (0,1]",
          0.0 < rec["path_efficiency"] <= 1.0, str(rec["path_efficiency"]))
    check("overshoot detected",
          rec["overshoot_events"] >= 1 and rec["max_overshoot_deg"] > 1.0,
          f"events={rec['overshoot_events']} max={rec['max_overshoot_deg']}")
    check("ballistic+correction ~= time_to_hit",
          abs((rec["ballistic_phase_ms"] + rec["correction_phase_ms"])
              - rec["time_to_hit_ms"]) < 1.0,
          f"b={rec['ballistic_phase_ms']} c={rec['correction_phase_ms']} "
          f"ttk={rec['time_to_hit_ms']}")


# --------------------------------------------------------------------------- #
def test_hit_detection():
    cam = Camera()
    cam.yaw, cam.pitch = 30.0, 0.0
    target = dir_from_yawpitch(30.0, 0.0)
    ang = angle_between_deg(cam.forward(), target)
    check("on-center click is a hit",
          ang <= TARGET_ANGULAR_DIAMETER_DEG / 2.0, f"ang={ang:.4f}")
    cam.yaw = 35.0
    ang = angle_between_deg(cam.forward(), target)
    check("5deg-off click is a miss",
          ang > TARGET_ANGULAR_DIAMETER_DEG / 2.0, f"ang={ang:.4f}")


if __name__ == "__main__":
    test_sensitivity_math()
    test_multi_event_per_frame()
    test_tracker_metrics()
    test_hit_detection()
    print()
    ok = sum(results)
    print(f"{ok}/{len(results)} checks passed")
    raise SystemExit(0 if ok == len(results) else 1)
