"""
Focused swipe counter — a reliable cm/360 meter for games that block
background raw input (like Marvel Rivals).

Instead of capturing raw input while the game runs (which the game blocks),
this captures raw counts while THIS window is focused (proven reliable — the
aim trainer logged 250k events with zero loss). You reproduce, here, the exact
physical swipe that does one 360 in-game.

CALIBRATION (do once, in-game):
  You said one full 360 just fits your desk. So your 360 swipe = one desk edge
  to the other. Note the two hand positions (e.g. right pad edge -> left pad
  edge). Those tactile edges ARE your 360 reference.

MEASURE (here):
  1) Run this. It goes fullscreen and captures the mouse.
  2) Put your hand at the SAME start edge as in-game.
  3) Press SPACE  -> ONE beep, counting starts.
  4) Do the identical edge-to-edge swipe (one 360), same speed you turn in
     fights.
  5) Press SPACE  -> TWO beeps, the reading is recorded.
  Repeat 5x. The running AVERAGE is what we use. ESC to finish.

Why focused capture is fine: it's the same mouse, same DPI, same Raw Accel
chain — the counts for a given physical swipe are identical whether the game,
the trainer, or this tool receives them.
"""

import sys
import math
import statistics

import pygame

try:
    import winsound
    def beep(n, f=900):
        import time
        for _ in range(n):
            winsound.Beep(f, 110)
            time.sleep(0.03)
except Exception:
    def beep(n, f=900):
        pass

DPI = 1600
CURRENT_SENS = 1.0
TARGET_CM360 = 47.0


def cm360_from_counts(counts):
    return abs(counts) / DPI * 2.54


def main():
    pygame.init()
    info = pygame.display.Info()
    try:
        screen = pygame.display.set_mode((info.current_w, info.current_h),
                                         pygame.FULLSCREEN | pygame.DOUBLEBUF,
                                         vsync=0)
    except pygame.error:
        screen = pygame.display.set_mode((info.current_w, info.current_h),
                                         pygame.FULLSCREEN)
    pygame.display.set_caption("Swipe counter")
    pygame.event.set_grab(True)
    pygame.mouse.set_visible(False)
    if hasattr(pygame.mouse, "set_relative_mode"):
        try:
            pygame.mouse.set_relative_mode(True)
        except Exception:
            pass

    W, H = screen.get_size()
    cx, cy = W // 2, H // 2
    f_big = pygame.font.SysFont("consolas", 120, bold=True)
    f_med = pygame.font.SysFont("consolas", 40, bold=True)
    f_sm = pygame.font.SysFont("consolas", 26)
    clock = pygame.time.Clock()

    armed = False
    net = 0          # signed horizontal counts since arm
    gross = 0        # absolute, to flag wobble
    recorded = []    # list of cm/360 readings

    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.MOUSEMOTION:
                if armed:
                    dx, _ = ev.rel
                    net += dx
                    gross += abs(dx)
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    running = False
                elif ev.key == pygame.K_SPACE:
                    if not armed:
                        armed = True
                        net = gross = 0
                        beep(1, 1000)
                    else:
                        armed = False
                        beep(2, 700)
                        if abs(net) > 200:           # ignore accidental taps
                            recorded.append(cm360_from_counts(net))
                elif ev.key == pygame.K_BACKSPACE and recorded:
                    recorded.pop()                   # undo a bad reading

        # ---- render ----
        screen.fill((12, 14, 22))
        live_counts = abs(net)
        live_cm = cm360_from_counts(net)
        wob = (gross - abs(net)) / gross * 100 if gross else 0

        title = "COUNTING — swipe one 360" if armed else "READY — SPACE to start"
        col = (120, 255, 180) if armed else (200, 210, 230)
        s = f_med.render(title, True, col)
        screen.blit(s, s.get_rect(center=(cx, cy - 220)))

        s = f_big.render(f"{live_cm:.1f}", True, (235, 240, 250))
        screen.blit(s, s.get_rect(center=(cx, cy - 70)))
        s = f_sm.render("cm/360 (live)   |   counts: %d   wobble: %.0f%%"
                        % (live_counts, wob), True, (150, 160, 185))
        screen.blit(s, s.get_rect(center=(cx, cy + 20)))

        if recorded:
            avg = statistics.fmean(recorded)
            rec_sens = CURRENT_SENS * avg / TARGET_CM360
            vals = "  ".join(f"{v:.1f}" for v in recorded[-8:])
            s = f_sm.render(f"readings: {vals}", True, (180, 190, 210))
            screen.blit(s, s.get_rect(center=(cx, cy + 110)))
            s = f_med.render(f"AVG {avg:.1f} cm/360  ->  in-game sens {rec_sens:.2f}",
                             True, (255, 210, 120))
            screen.blit(s, s.get_rect(center=(cx, cy + 165)))

        s = f_sm.render("SPACE start/stop   BACKSPACE undo last   ESC finish",
                        True, (120, 130, 155))
        screen.blit(s, s.get_rect(center=(cx, H - 60)))

        pygame.display.flip()
        clock.tick(250)

    pygame.event.set_grab(False)
    pygame.mouse.set_visible(True)
    pygame.quit()

    print("\n" + "=" * 56)
    if recorded:
        avg = statistics.fmean(recorded)
        sd = statistics.pstdev(recorded) if len(recorded) > 1 else 0
        rec_sens = CURRENT_SENS * avg / TARGET_CM360
        print(f"  readings (cm/360): {[round(v,1) for v in recorded]}")
        print(f"  AVERAGE current cm/360 : {avg:.1f}  (+/- {sd:.1f})")
        print(f"  target cm/360          : {TARGET_CM360:.0f}")
        print(f"  >>> RECOMMENDED in-game sens : {rec_sens:.2f}")
    else:
        print("  no readings taken.")
    print("=" * 56)


if __name__ == "__main__":
    main()
