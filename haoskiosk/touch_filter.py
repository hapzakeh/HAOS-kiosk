#!/usr/bin/env python3
"""
HAOS Kiosk Touch Filter (touch_filter.py)

Grabs the physical multi-touch touchscreen exclusively via EVIOCGRAB so that
no raw XI2 touch events reach WebKitGTK / GtkGestureZoom.  A virtual uinput
device is created that emits only single-touch (ABS_X / ABS_Y / BTN_TOUCH)
events derived from the first active finger.

Handles both:
  Type B (modern, slot-based):   has ABS_MT_SLOT in capabilities
  Type A (legacy, no slots):     has ABS_MT_POSITION_X but no ABS_MT_SLOT
"""
import sys
import signal
import time
import evdev
from evdev import ecodes


# ---------------------------------------------------------------------------
# Device discovery & virtual device setup
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


def build_virtual_device(src):
    abs_info = {code: info for code, info in src.capabilities().get(ecodes.EV_ABS, [])}
    x_info = abs_info.get(ecodes.ABS_MT_POSITION_X) or abs_info.get(ecodes.ABS_X)
    y_info = abs_info.get(ecodes.ABS_MT_POSITION_Y) or abs_info.get(ecodes.ABS_Y)
    if x_info is None or y_info is None:
        raise RuntimeError("Cannot determine touch axis resolution from source device")
    return evdev.UInput(
        {
            ecodes.EV_KEY: [ecodes.BTN_TOUCH, ecodes.BTN_LEFT],
            ecodes.EV_ABS: [
                (ecodes.ABS_X, x_info),
                (ecodes.ABS_Y, y_info),
            ],
        },
        name="HAOSKiosk-Touch",
        vendor=0x1234,
        product=0x5678,
        version=1,
    )


# ---------------------------------------------------------------------------
# Event loop — Type B (slot-based, modern)
# ---------------------------------------------------------------------------

def run_type_b(dev, ui):
    cur_slot   = 0
    slots      = {}     # slot -> tracking_id
    first_slot = None

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
                            ui.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 0)
                            ui.syn()
                else:
                    slots[cur_slot] = ev.value
                    if first_slot is None:
                        first_slot = cur_slot
                        ui.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 1)
            elif ev.code == ecodes.ABS_MT_POSITION_X and cur_slot == first_slot:
                ui.write(ecodes.EV_ABS, ecodes.ABS_X, ev.value)
            elif ev.code == ecodes.ABS_MT_POSITION_Y and cur_slot == first_slot:
                ui.write(ecodes.EV_ABS, ecodes.ABS_Y, ev.value)
        elif ev.type == ecodes.EV_SYN:
            ui.syn()


# ---------------------------------------------------------------------------
# Event loop — Type A (legacy, no slots)
# ---------------------------------------------------------------------------

def run_type_a(dev, ui):
    first_x      = None
    first_y      = None
    in_first     = True   # True until the first SYN_MT_REPORT in a frame
    finger_count = 0
    touching     = False

    for ev in dev.read_loop():
        if ev.type == ecodes.EV_ABS:
            if ev.code == ecodes.ABS_MT_POSITION_X and in_first:
                first_x = ev.value
            elif ev.code == ecodes.ABS_MT_POSITION_Y and in_first:
                first_y = ev.value

        elif ev.type == ecodes.EV_SYN:
            if ev.code == ecodes.SYN_MT_REPORT:
                # End of one finger's data in this frame
                finger_count += 1
                in_first = False   # subsequent MT groups = extra fingers, skip
            elif ev.code == ecodes.SYN_REPORT:
                if finger_count > 0:
                    if not touching:
                        touching = True
                        ui.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 1)
                    if first_x is not None:
                        ui.write(ecodes.EV_ABS, ecodes.ABS_X, first_x)
                    if first_y is not None:
                        ui.write(ecodes.EV_ABS, ecodes.ABS_Y, first_y)
                    ui.syn()
                else:
                    # No MT data this frame → all fingers up
                    if touching:
                        touching = False
                        ui.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 0)
                        ui.syn()
                # Reset for next frame
                first_x = first_y = None
                in_first = True
                finger_count = 0

        elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH:
            # Some Type A drivers send explicit BTN_TOUCH=0 on release
            if ev.value == 0 and touching:
                touching = False
                ui.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 0)
                ui.syn()


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
    proto = "B (slot-based)" if type_b else "A (legacy, no slots)"
    print(f"touch_filter: using multitouch protocol Type {proto}", flush=True)

    try:
        ui = build_virtual_device(dev)
    except Exception as e:
        print(f"touch_filter: failed to create virtual device: {e}", flush=True)
        sys.exit(1)

    print(f"touch_filter: virtual device created at {ui.device.path}", flush=True)

    try:
        dev.grab()
    except Exception as e:
        print(f"touch_filter: EVIOCGRAB failed: {e}", flush=True)
        ui.close()
        sys.exit(1)

    print(f"touch_filter: grabbed {dev.path} exclusively — pinch-to-zoom blocked",
          flush=True)

    def cleanup(sig=None, frame=None):
        try:
            dev.ungrab()
        except Exception:
            pass
        try:
            ui.close()
        except Exception:
            pass
        print("touch_filter: cleaned up", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    try:
        if type_b:
            run_type_b(dev, ui)
        else:
            run_type_a(dev, ui)
    except Exception as e:
        print(f"touch_filter: error in event loop: {e}", flush=True)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
