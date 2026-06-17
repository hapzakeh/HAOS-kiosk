#!/usr/bin/env python3
"""
HAOS Kiosk Touch Filter (touch_filter.py)

Grabs the physical multi-touch touchscreen exclusively via EVIOCGRAB so that
no raw XI2 touch events reach WebKitGTK / GtkGestureZoom.  A virtual uinput
device is created that emits only single-touch (ABS_X / ABS_Y / BTN_TOUCH)
events derived from the first active finger.  X11 treats this as a plain
pointer device, so luakit interaction continues to work while pinch-to-zoom is
physically impossible.

Note: 2-finger / 3-finger TOUCH gestures in mouse_touch_inputs.py will no
longer fire because the virtual device is single-touch.  They can be replaced
with keyboard shortcuts or corner gestures in gesture_commands.json.
"""
import sys
import signal
import time
import evdev
from evdev import ecodes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_mt_device():
    """Return the first evdev device that advertises ABS_MT_SLOT."""
    for path in sorted(evdev.list_devices()):
        try:
            dev = evdev.InputDevice(path)
            abs_codes = [c for c, _ in dev.capabilities().get(ecodes.EV_ABS, [])]
            if ecodes.ABS_MT_SLOT in abs_codes:
                return dev
        except Exception:
            continue
    return None


def build_virtual_device(src):
    """Create a single-touch uinput device mirroring src's ABS resolution."""
    abs_info = {code: info for code, info in src.capabilities().get(ecodes.EV_ABS, [])}

    # Prefer MT position axes; fall back to ABS_X / ABS_Y
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
# Main
# ---------------------------------------------------------------------------

def main():
    # Retry loop: udev may not have settled when we are first called
    dev = None
    for attempt in range(1, 6):
        dev = find_mt_device()
        if dev:
            break
        print(f"touch_filter: no MT device yet (attempt {attempt}/5), retrying…",
              flush=True)
        time.sleep(1)

    if dev is None:
        print("touch_filter: no multi-touch device found — filter inactive", flush=True)
        sys.exit(0)

    print(f"touch_filter: found {dev.name!r} at {dev.path}", flush=True)

    try:
        ui = build_virtual_device(dev)
    except Exception as e:
        print(f"touch_filter: failed to create virtual device: {e}", flush=True)
        sys.exit(1)

    print(f"touch_filter: virtual device at {ui.device.path}", flush=True)

    dev.grab()
    print(f"touch_filter: grabbed {dev.path} exclusively", flush=True)

    # -----------------------------------------------------------------------
    # Per-slot state
    # -----------------------------------------------------------------------
    cur_slot   = 0          # active MT slot index
    slots      = {}         # slot → tracking_id (int, or -1 = lifted)
    first_slot = None       # which slot is forwarded as "the" finger

    def cleanup(sig=None, frame=None):
        try:
            dev.ungrab()
        except Exception:
            pass
        try:
            ui.close()
        except Exception:
            pass
        print("touch_filter: cleaned up and exiting", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    # -----------------------------------------------------------------------
    # Event loop
    # -----------------------------------------------------------------------
    try:
        for ev in dev.read_loop():
            if ev.type == ecodes.EV_ABS:

                if ev.code == ecodes.ABS_MT_SLOT:
                    cur_slot = ev.value

                elif ev.code == ecodes.ABS_MT_TRACKING_ID:
                    if ev.value == -1:
                        # Finger lifted on cur_slot
                        slots.pop(cur_slot, None)
                        if cur_slot == first_slot:
                            # Primary finger gone → promote next, or release
                            first_slot = min(slots) if slots else None
                            if not slots:
                                ui.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 0)
                                ui.syn()
                    else:
                        # New finger down
                        slots[cur_slot] = ev.value
                        if first_slot is None:
                            first_slot = cur_slot
                            ui.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 1)

                elif ev.code == ecodes.ABS_MT_POSITION_X:
                    if cur_slot == first_slot:
                        ui.write(ecodes.EV_ABS, ecodes.ABS_X, ev.value)

                elif ev.code == ecodes.ABS_MT_POSITION_Y:
                    if cur_slot == first_slot:
                        ui.write(ecodes.EV_ABS, ecodes.ABS_Y, ev.value)

                # Drop all other ABS events (pressure, touch major/minor, etc.)

            elif ev.type == ecodes.EV_SYN:
                # Forward a SYN only if something was written this cycle
                ui.syn()

            # EV_KEY (BTN_TOUCH from raw device) is intentionally dropped;
            # we synthesise BTN_TOUCH ourselves based on MT tracking IDs.

    except Exception as e:
        print(f"touch_filter: error in event loop: {e}", flush=True)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
