#!/usr/bin/env python3
"""
HAOS Kiosk Touch Filter (touch_filter.py)

Grabs the physical multi-touch touchscreen exclusively via EVIOCGRAB so that
no raw XI2 touch events reach WebKitGTK / GtkGestureZoom.  Single-touch
events are re-injected as synthetic X11 pointer events via the XTest extension
(MotionNotify + ButtonPress/Release).  Because these are core pointer events
— not XI2 TouchBegin/Update/End — GtkGestureZoom never fires.

Requires: py3-evdev, py3-xlib  (no uinput / SYS_MODULE needed)
"""
import os
import sys
import signal
import time
import evdev
from evdev import ecodes
from Xlib import display as xdisplay, X
from Xlib.ext import xtest as xtest_ext


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

def find_mt_device():
    """Return first evdev device with multi-touch capability (Type A or B)."""
    for path in sorted(evdev.list_devices()):
        try:
            dev = evdev.InputDevice(path)
            abs_codes = [c for c, _ in dev.capabilities().get(ecodes.EV_ABS, [])]
            if ecodes.ABS_MT_SLOT in abs_codes or ecodes.ABS_MT_POSITION_X in abs_codes:
                return dev
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# X display connection
# ---------------------------------------------------------------------------

def connect_display(display_name=None):
    name = display_name or os.environ.get('DISPLAY', ':0')
    for attempt in range(1, 11):
        try:
            dpy = xdisplay.Display(name)
            return dpy
        except Exception as e:
            print(f"touch_filter: waiting for display (attempt {attempt}/10): {e}",
                  flush=True)
            time.sleep(1)
    return None


# ---------------------------------------------------------------------------
# Coordinate scaling (device units → screen pixels)
# ---------------------------------------------------------------------------

def make_scalers(dev, screen_w, screen_h):
    abs_info = {code: info for code, info in dev.capabilities().get(ecodes.EV_ABS, [])}
    xi = abs_info.get(ecodes.ABS_MT_POSITION_X) or abs_info.get(ecodes.ABS_X)
    yi = abs_info.get(ecodes.ABS_MT_POSITION_Y) or abs_info.get(ecodes.ABS_Y)
    if xi is None or yi is None:
        raise RuntimeError("Cannot determine touch axis resolution")

    x_range = max(xi.max - xi.min, 1)
    y_range = max(yi.max - yi.min, 1)

    def sx(raw):
        return max(0, min(screen_w - 1, int((raw - xi.min) * (screen_w - 1) / x_range)))

    def sy(raw):
        return max(0, min(screen_h - 1, int((raw - yi.min) * (screen_h - 1) / y_range)))

    return sx, sy


# ---------------------------------------------------------------------------
# XTest helpers
# ---------------------------------------------------------------------------

def xtest_move(dpy, x, y):
    xtest_ext.fake_input(dpy, X.MotionNotify, False, 0, X.NONE, x, y)


def xtest_press(dpy):
    xtest_ext.fake_input(dpy, X.ButtonPress, 1)


def xtest_release(dpy):
    xtest_ext.fake_input(dpy, X.ButtonRelease, 1)


# ---------------------------------------------------------------------------
# Event loop — Type B (slot-based, modern protocol)
# ---------------------------------------------------------------------------

def run_type_b(dev, dpy, sx, sy):
    cur_slot   = 0
    slots      = {}     # slot -> tracking_id
    first_slot = None
    cur_x      = 0
    cur_y      = 0
    pressed    = False

    for ev in dev.read_loop():
        if ev.type == ecodes.EV_ABS:
            if ev.code == ecodes.ABS_MT_SLOT:
                cur_slot = ev.value
            elif ev.code == ecodes.ABS_MT_TRACKING_ID:
                if ev.value == -1:
                    slots.pop(cur_slot, None)
                    if cur_slot == first_slot:
                        first_slot = min(slots) if slots else None
                        if not slots and pressed:
                            xtest_release(dpy)
                            dpy.sync()
                            pressed = False
                else:
                    slots[cur_slot] = ev.value
                    if first_slot is None:
                        first_slot = cur_slot
            elif ev.code == ecodes.ABS_MT_POSITION_X and cur_slot == first_slot:
                cur_x = sx(ev.value)
            elif ev.code == ecodes.ABS_MT_POSITION_Y and cur_slot == first_slot:
                cur_y = sy(ev.value)
        elif ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_REPORT:
            if first_slot is not None and slots:
                xtest_move(dpy, cur_x, cur_y)
                if not pressed:
                    xtest_press(dpy)
                    pressed = True
                dpy.sync()


# ---------------------------------------------------------------------------
# Event loop — Type A (legacy, no slots)
# ---------------------------------------------------------------------------

def run_type_a(dev, dpy, sx, sy):
    first_x      = None
    first_y      = None
    in_first     = True    # True until first SYN_MT_REPORT in frame
    finger_count = 0
    pressed      = False

    for ev in dev.read_loop():
        if ev.type == ecodes.EV_ABS:
            if ev.code == ecodes.ABS_MT_POSITION_X and in_first:
                first_x = ev.value
            elif ev.code == ecodes.ABS_MT_POSITION_Y and in_first:
                first_y = ev.value

        elif ev.type == ecodes.EV_SYN:
            if ev.code == ecodes.SYN_MT_REPORT:
                finger_count += 1
                in_first = False    # extra fingers — ignore their coords

            elif ev.code == ecodes.SYN_REPORT:
                if finger_count > 0 and first_x is not None and first_y is not None:
                    xtest_move(dpy, sx(first_x), sy(first_y))
                    if not pressed:
                        xtest_press(dpy)
                        pressed = True
                    dpy.sync()
                elif finger_count == 0 and pressed:
                    xtest_release(dpy)
                    dpy.sync()
                    pressed = False
                # Reset for next frame
                first_x = first_y = None
                in_first = True
                finger_count = 0

        elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH and ev.value == 0:
            if pressed:
                xtest_release(dpy)
                dpy.sync()
                pressed = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Find touchscreen (retry — udev may still be settling)
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

    # Connect to X display
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
        sx, sy = make_scalers(dev, sw, sh)
    except Exception as e:
        print(f"touch_filter: scaler setup failed: {e}", flush=True)
        sys.exit(1)

    # Grab physical device — X loses raw touch events, GtkGestureZoom starved
    try:
        dev.grab()
    except Exception as e:
        print(f"touch_filter: EVIOCGRAB failed: {e}", flush=True)
        sys.exit(1)

    print(f"touch_filter: grabbed {dev.path} exclusively — pinch-to-zoom blocked",
          flush=True)

    def cleanup(sig=None, frame=None):
        try:
            dev.ungrab()
        except Exception:
            pass
        try:
            dpy.close()
        except Exception:
            pass
        print("touch_filter: cleaned up", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    try:
        if type_b:
            run_type_b(dev, dpy, sx, sy)
        else:
            run_type_a(dev, dpy, sx, sy)
    except Exception as e:
        print(f"touch_filter: error in event loop: {e}", flush=True)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
