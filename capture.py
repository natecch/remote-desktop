"""Screen capture and change detection for the host. Every call must come from one thread.

Windows uses DirectX desktop duplication (dxcam), which delivers up to the
display's refresh rate; ordinary GDI capture is locked to about 30 fps. Other
systems, and Windows when DirectX capture is unavailable, use mss.

Only the parts of the screen that changed are encoded and sent.
"""

import io
import struct
import sys
import time

import mss
import numpy as np
from PIL import Image

dxcam = None
if sys.platform == "win32":
    try:
        import dxcam
    except Exception:  # missing, or no usable graphics device
        dxcam = None

GRID = 16          # align regions to JPEG block size so their edges don't show
ROW_GAP = 32       # unchanged rows that may sit between two changes in one region
MAX_REGIONS = 6
RETRY_SECONDS = 3  # how long to stay on the fallback after DirectX capture fails
DX_BLOCKED_UNTIL = 0.0  # set by the host's watchdog to keep DirectX capture off for a while


class CaptureLost(Exception):
    """DirectX capture lost the display (UAC secure desktop, lock screen, sign-out, mode change)."""


def _capture_lost():
    # dxcam's own recovery (_on_output_change) retries in an endless loop for as long as
    # Windows refuses access, which froze the capture thread and with it every new login.
    # Raise instead: the Grabber drops the camera, uses mss, and tries DirectX again later.
    raise CaptureLost()


class Grabber:
    def __init__(self):
        self.sct = None
        self.cameras = {}   # monitor index -> dxcam camera, or the time to retry after
        self.latest = {}    # monitor index -> (version, BGRA array)

    def reset(self):
        """Drop capture handles after the thread moved to another desktop (secure desktop,
        lock screen). Versions keep counting, so viewers still see the next change."""
        for cam in self.cameras.values():
            if cam is not None and not isinstance(cam, float):
                try:
                    cam.release()
                except Exception:
                    pass
        self.cameras.clear()
        self.sct = None

    def _mss(self):
        if self.sct is None:
            self.sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        return self.sct

    def monitors(self):
        return [dict(m) for m in self._mss().monitors[1:]]

    def _camera(self, index, monitor):
        cam = self.cameras.get(index)
        now = time.monotonic()
        if dxcam is None or now < DX_BLOCKED_UNTIL or (isinstance(cam, float) and now < cam):
            return None
        if cam is not None and not isinstance(cam, float):
            return cam
        self.cameras[index] = time.monotonic() + RETRY_SECONDS  # assume failure until one matches
        try:
            for output in range(8):
                try:
                    candidate = dxcam.create(output_idx=output, output_color="BGRA")
                except Exception:
                    break
                if candidate is None:
                    break
                try:
                    area = candidate._output.desc.DesktopCoordinates
                    same = (area.left, area.top) == (monitor["left"], monitor["top"])
                except Exception:  # internals changed: accept only an unambiguous size match
                    same = (candidate.width, candidate.height) == (monitor["width"], monitor["height"])
                if same and (candidate.width, candidate.height) == (monitor["width"], monitor["height"]):
                    candidate._on_output_change = _capture_lost  # never run dxcam's endless retry
                    self.cameras[index] = candidate
                    return candidate
                candidate.release()
        except Exception:
            pass
        return None

    def fast(self, index):
        """True when polling this monitor is nearly free (DirectX capture is active)."""
        cam = self.cameras.get(index)
        return cam is not None and not isinstance(cam, float)

    def frame(self, index, monitor):
        """(version, picture) for a monitor. The version changes only when the picture may have."""
        version, current = self.latest.get(index, (0, None))
        cam = self._camera(index, monitor)
        if cam is not None:
            try:
                new = cam.grab()  # None when nothing was redrawn since the last call
            except Exception:
                new = None
                try:
                    cam.release()
                except Exception:
                    pass
                self.cameras[index] = time.monotonic() + RETRY_SECONDS
                cam = None
            if new is not None and new.ndim == 3 and new.shape[2] == 4:
                self.latest[index] = (version + 1, new)
                return self.latest[index]
            if cam is not None and current is not None:
                return version, current
        try:
            shot = self._mss().grab(monitor)
            new = np.frombuffer(shot.raw, np.uint8).reshape(shot.height, shot.width, 4)
        except Exception:
            # The display cannot be read at all right now (secure desktop, locked, signed out).
            # Keep the last picture, or a black one, and try again on the next call.
            self.sct = None
            if current is not None:
                return version, current
            new = np.zeros((monitor["height"], monitor["width"], 4), np.uint8)
            new[:, :, 3] = 255
        if current is not None and current.shape == new.shape and np.array_equal(current, new):
            return version, current
        self.latest[index] = (version + 1, new)
        return self.latest[index]


def scaled(frame, scale):
    height, width = frame.shape[:2]
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    image = Image.frombuffer("RGBX", (width, height), frame, "raw", "RGBX", 0, 1)
    return np.array(image.resize(size, Image.BILINEAR))  # channel order is untouched


def changed_regions(previous, current):
    """Rectangles (x, y, w, h) that differ between two pictures of the same size."""
    height, width = current.shape[:2]
    diff = previous.view(np.uint32).reshape(height, width) != current.view(np.uint32).reshape(height, width)
    rows = np.flatnonzero(diff.any(axis=1))
    if not len(rows):
        return []
    breaks = np.flatnonzero(np.diff(rows) > ROW_GAP)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [len(rows) - 1]))
    bands = [(int(rows[s]), int(rows[e]) + 1) for s, e in zip(starts, ends)]
    if len(bands) > MAX_REGIONS:
        bands = [(bands[0][0], bands[-1][1])]
    regions = []
    for top, bottom in bands:
        cols = np.flatnonzero(diff[top:bottom].any(axis=0))
        left, right = int(cols[0]), int(cols[-1]) + 1
        left -= left % GRID
        top -= top % GRID
        right = min(width, -(-right // GRID) * GRID)
        bottom = min(height, -(-bottom // GRID) * GRID)
        regions.append((left, top, right - left, bottom - top))
    return regions


def _jpeg(pixels, quality):
    height, width = pixels.shape[:2]
    image = Image.frombuffer("RGB", (width, height), np.ascontiguousarray(pixels), "raw", "BGRX", 0, 1)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return out.getvalue()


def build_update(frame, previous, quality):
    """Binary message with the changed regions, or None when nothing changed.

    Layout: 0x02, width u16, height u16, count u8, then per region: x u16, y u16,
    length u32, JPEG bytes. All little-endian.
    """
    height, width = frame.shape[:2]
    if previous is None or previous.shape != frame.shape:
        regions = [(0, 0, width, height)]
    else:
        regions = changed_regions(previous, frame)
        if not regions:
            return None
    parts = [struct.pack("<BHHB", 2, width, height, len(regions))]
    for x, y, w, h in regions:
        data = _jpeg(frame[y:y + h, x:x + w], quality)
        parts += [struct.pack("<HHI", x, y, len(data)), data]
    return b"".join(parts)
