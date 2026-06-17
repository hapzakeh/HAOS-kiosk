#!/usr/bin/env python3
"""
HAOS Kiosk Touch Filter (touch_filter.py)

Grabs the physical multi-touch touchscreen exclusively via EVIOCGRAB so that
no raw XI2 touch events reach WebKitGTK / GtkGestureZoom.

Touch gestures are translated to X11 events as follows:
  - Tap  (small total movement)      → ButtonPress + ButtonRelease (click)
  - Vertical drag (scroll)           → scroll-wheel events (button 4 / 5)
  - Horizontal drag (sliders etc.)   → ButtonPress + MotionNotify + ButtonRelease

Requires: py3-evdev, py3-xlib  (no /dev/uinput / SYS_MODULE needed)
"""
import os, sys, signal, time
import evdev
from evdev import ecodes
from Xlib import display as xdisplay, X
from Xlib.ext import xtest as xt

# Tune these if scroll speed or tap sensitivity feels wrong
SCROLL_THRESHOLD  = 15   # screen-px of total movement before classifying as drag
PIXELS_PER_SCROLL = 30   # screen-px per one scroll-wheel click

# State machine constants
IDLE, PENDING, SCROLLING, DRAGGING = 0, 1, 2, 3


# ---------------------------------------------------------------------------
# Device / display helpers
# ---------------------------------------------------------------------------

def find_mt_device():
    for path in sorted(evdev.list_devices()):
        try:
            dev = evdev.InputDevice(path)
            codes = [c for c, _ in dev.capabilities().get(ecodes.EV_ABS, [])]
            if ecodes.ABS_MT_SLOT in codes or ecodes.ABS_MT_POSITION_X in codes:
                return dev
        except Exception:
            continue
    return None


def connect_display():
    name = os.environ.get('DISPLAY', ':0')
    for i in range(1, 11):
        try:
            return xdisplay.Display(name)
        except Exception as e:
            print(f"touch_filter: waiting for display ({i}/10): {e}", flush=True)
            time.sleep(1)
    return None


def make_scalers(dev, sw, sh):
    ai = {c: i for c, i in dev.capabilities().get(ecodes.EV_ABS, [])}
    xi = ai.get(ecodes.ABS_MT_POSITION_X) or ai.get(ecodes.ABS_X)
    yi = ai.get(ecodes.ABS_MT_POSITION_Y) or ai.get(ecodes.ABS_Y)
    if xi is None or yi is None:
        raise RuntimeError("Cannot determine touch axis resolution")
    xr, yr = max(xi.max - xi.min, 1), max(yi.max - yi.min, 1)
    return (
        lambda v: max(0, min(sw - 1, int((v - xi.min) * (sw - 1) / xr))),
        lambda v: max(0, min(sh - 1, int((v - yi.min) * (sh - 1) / yr))),
    )


# ---------------------------------------------------------------------------
# XTest emit helpers
# ---------------------------------------------------------------------------

def emit_move(dpy, x, y):
    xt.fake_input(dpy, X.MotionNotify, False, 0, X.NONE, x, y)


def emit_tap(dpy, x, y):
    emit_move(dpy, x, y)
    xt.fake_input(dpy, X.ButtonPress,   1)
    xt.fake_input(dpy, X.ButtonRelease, 1)
    dpy.flush()


def emit_scroll_v(dpy, delta_y, remainder):
    """Accumulate vertical delta and emit whole scroll clicks.
    delta_y > 0  finger moved down → scroll down (button 5)
    delta_y < 0  finger moved up   → scroll up   (button 4)
    Returns updated remainder."""
    remainder += delta_y
    clicks = int(remainder / PIXELS_PER_SCROLL)
    if clicks:
        remainder -= clicks * PIXELS_PER_SCROLL
        btn = 5 if clicks > 0 else 4
        for _ in range(abs(clicks)):
            xt.fake_input(dpy, X.ButtonPress,   btn)
            xt.fake_input(dpy, X.ButtonRelease, btn)
        dpy.flush()
    return remainder


# ---------------------------------------------------------------------------
# Shared state-machine logic (called from both Type A and B loops)
# ---------------------------------------------------------------------------

class TouchSM:
    """State machine that converts a single-touch stream into X11 events."""

    def __init__(self, dpy):
        self.dpy        = dpy
        self.state      = IDLE
        self.start_x    = 0
        self.start_y    = 0
        self.last_x     = 0
        self.last_y     = 0
        self.scroll_rem = 0.0
        self.start_set  = False

    def touch_down(self):
        """Called when the first finger makes contact."""
        self.state      = PENDING
        self.start_set  = False
        self.scroll_rem = 0.0

    def touch_up(self):
        """Called when the last finger lifts."""
        dpy = self.dpy
        if self.state == PENDING:
            emit_tap(dpy, self.start_x, self.start_y)
        elif self.state == DRAGGING:
            xt.fake_input(dpy, X.ButtonRelease, 1)
            dpy.flush()
        # SCROLLING: scroll events already emitted, nothing to do on lift
        self.state     = IDLE
        self.start_set = False
        self.scroll_rem = 0.0

    def update(self, cur_x, cur_y):
        """Called on each SYN_REPORT while finger is active."""
        dpy = self.dpy

        if self.state == PENDING:
            if not self.start_set:
                # First position report after touch down — record origin
                self.start_x = self.last_x = cur_x
                self.start_y = self.last_y = cur_y
                self.start_set = True
                return

            dx = cur_x - self.start_x
            dy = cur_y - self.start_y
            dist = (dx * dx + dy * dy) ** 0.5

            if dist > SCROLL_THRESHOLD:
                if abs(dy) >= abs(dx):
                    # Predominantly vertical → scroll mode
                    self.state = SCROLLING
                else:
                    # Predominantly horizontal → drag mode (sliders, etc.)
                    self.state = DRAGGING
                    emit_move(dpy, self.start_x, self.start_y)
                    xt.fake_input(dpy, X.ButtonPress, 1)
                    dpy.flush()

        if self.state == SCROLLING:
            dy = cur_y - self.last_y
            self.scroll_rem = emit_scroll_v(dpy, dy, self.scroll_rem)
            self.last_x = cur_x
            self.last_y = cur_y

        elif self.state == DRAGGING:
            emit_move(dpy, cur_x, cur_y)
            dpy.flush()
            self.last_x = cur_x
            self.last_y = cur_y


# ---------------------------------------------------------------------------
# Type B (slot-based, modern protocol)
# ---------------------------------------------------------------------------

def run_type_b(dev, dpy, sx, sy):
    cur_slot   = 0
    slots      = {}
    first_slot = None
    cur_x = cur_y = 0
    sm = TouchSM(dpy)

    for ev in dev.read_loop():
        if ev.type == ecodes.EV_ABS:
            if ev.code == ecodes.ABS_MT_SLOT:
                cur_slot = ev.value

            elif ev.code == ecodes.ABS_MT_TRACKING_ID:
                if ev.value == -1:
                    slots.pop(cur_slot, None)
                    if cur_slot == first_slot:
                        first_slot = min(slots) if slots else None
                    if not slots:
                        sm.touch_up()
                else:
                    was_empty = not slots
                    slots[cur_slot] = ev.value
                    if first_slot is None:
                        first_slot = cur_slot
                    if was_empty:
                        sm.touch_down()

            elif ev.code == ecodes.ABS_MT_POSITION_X and cur_slot == first_slot:
                cur_x = sx(ev.value)
            elif ev.code == ecodes.ABS_MT_POSITION_Y and cur_slot == first_slot:
                cur_y = sy(ev.value)

        elif ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_REPORT:
            if slots and first_slot is not None:
                sm.update(cur_x, cur_y)


# ---------------------------------------------------------------------------
# Type A (legacy, no slots)
# ---------------------------------------------------------------------------

def run_type_a(dev, dpy, sx, sy):
    first_x = first_y = None
    in_first     = True
    finger_count = 0
    had_fingers  = False
    sm = TouchSM(dpy)

    for ev in dev.read_loop():
        if ev.type == ecodes.EV_ABS:
            if ev.code == ecodes.ABS_MT_POSITION_X and in_first:
                first_x = ev.value
            elif ev.code == ecodes.ABS_MT_POSITION_Y and in_first:
                first_y = ev.value

        elif ev.type == ecodes.EV_SYN:
            if ev.code == ecodes.SYN_MT_REPORT:
                finger_count += 1
                in_first = False

            elif ev.code == ecodes.SYN_REPORT:
                if finger_count > 0:
                    if not had_fingers:
                        sm.touch_down()
                        had_fingers = True
                    if first_x is not None and first_y is not None:
                        sm.update(sx(first_x), sy(first_y))
                else:
                    if had_fingers:
                        sm.touch_up()
                        had_fingers = False
                first_x = first_y = None
                in_first = True
                finger_count = 0

        elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH and ev.value == 0:
            if had_fingers:
                sm.touch_up()
                had_fingers = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dev = None
    for attempt in range(1, 6):
        dev = find_mt_device()
        if dev:
            break
        print(f"touch_filter: no MT device yet (attempt {attempt}/5), retrying...",
              flush=True)
        time.sleep(1)

    if dev is None:
        print("touch_filter: no multi-touch device found — filter inactive", flush=True)
        sys.exit(0)

    print(f"touch_filter: found {dev.name!r} at {dev.path}", flush=True)

    abs_codes = [c for c, _ in dev.capabilities().get(ecodes.EV_ABS, [])]
    type_b = ecodes.ABS_MT_SLOT in abs_codes
    print(f"touch_filter: protocol Type {'B (slot-based)' if type_b else 'A (legacy)'}",
          flush=True)

    dpy = connect_display()
    if dpy is None:
        print("touch_filter: cannot connect to display — filter inactive", flush=True)
        sys.exit(1)

    if not dpy.has_extension('XTEST'):
        print("touch_filter: XTest extension unavailable — filter inactive", flush=True)
        sys.exit(1)

    screen = dpy.screen()
    sw, sh = screen.width_in_pixels, screen.height_in_pixels
    print(f"touch_filter: screen {sw}x{sh}", flush=True)

    try:
        scalers = make_scalers(dev, sw, sh)
    except Exception as e:
        print(f"touch_filter: scaler setup failed: {e}", flush=True)
        sys.exit(1)

    try:
        dev.grab()
    except Exception as e:
        print(f"touch_filter: EVIOCGRAB failed: {e}", flush=True)
        sys.exit(1)

    print(f"touch_filter: grabbed {dev.path} — pinch-to-zoom blocked", flush=True)

    def cleanup(sig=None, frame=None):
        try: dev.ungrab()
        except Exception: pass
        try: dpy.close()
        except Exception: pass
        print("touch_filter: cleaned up", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    sx, sy = scalers
    try:
        if type_b:
            run_type_b(dev, dpy, sx, sy)
        else:
            run_type_a(dev, dpy, sx, sy)
    except Exception as e:
        print(f"touch_filter: error: {e}", flush=True)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
