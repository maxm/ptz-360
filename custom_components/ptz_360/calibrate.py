"""The calibration pass: everything the card needs, in one go (a few minutes; the camera moves and comes back).

1. Nudges: at a few zooms, move a fifth of a frame and phase-correlate the pictures before and after: how many
   pan / tilt units a frame spans (a first guess for the model).
2. Model frames: at the widest zoom a 3×3 grid half a frame apart and two rows at other pitches; at each other model
   zoom the centre and four frames 0.4 of a frame away. Patches matched between overlapping frames fit the camera
   model (model.py: yaw / pitch per pan / tilt unit, focal length per zoom, the lens's bending).
3. Sweep: the whole range at the widest zoom, 0.6 of a frame apart, each frame placed by the model.
4. Stitch: the sweep into one equirectangular panorama in the model's world (panorama.py).

The frames are kept next to the results, so a fit or a stitch can be redone without moving the camera.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import shutil
import time
from typing import Any, Callable

from homeassistant.core import HomeAssistant

from . import model as ptz_model, panorama as ptz_panorama
from .const import MODEL_ZOOMS, NUDGE_FRAMES, NUDGE_ZOOMS
from .device import PtzDevice, PtzError

_LOGGER = logging.getLogger(__name__)


def _gray(jpeg: bytes):
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    img = Image.open(io.BytesIO(jpeg)).convert("L")
    img = img.resize((320, round(320 * img.height / img.width)))
    a = np.asarray(img, dtype=np.float32)
    # Away from the edges: overlays (a clock, the camera's name) don't move with the picture.
    h, w = a.shape
    a = a[int(h * 0.12):int(h * 0.88), int(w * 0.05):int(w * 0.95)]
    a = a - a.mean()
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    return a * win


def _shift(before: bytes, after: bytes) -> tuple[float, float, float]:
    """How far the picture moved from `before` to `after`, in frames (x right, y down), and how clear the match is
    (the correlation peak against the mean: a real match stands far out)."""
    import numpy as np  # noqa: PLC0415

    a, b = _gray(before), _gray(after)
    r = np.fft.fft2(b) * np.conj(np.fft.fft2(a))
    r /= np.abs(r) + 1e-9
    c = np.fft.ifft2(r).real
    y, x = np.unravel_index(np.argmax(c), c.shape)
    clarity = float((c.max() - c.mean()) / (c.std() + 1e-9))
    h, w = c.shape
    if y > h // 2:
        y -= h
    if x > w // 2:
        x -= w
    return x / w, y / h, clarity


def _small(jpeg: bytes, width: int = 1280) -> bytes:
    """A frame kept for the fit and the stitch: `width` wide, JPEG."""
    from PIL import Image  # noqa: PLC0415

    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if img.width > width:
        img = img.resize((width, round(width * img.height / img.width)))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


class Calibration:
    """One run of the pass, reporting where it is (`progress`) as it goes."""

    def __init__(self, hass: HomeAssistant, device: PtzDevice, folder: str, on_progress: Callable[[dict[str, Any]], None]):
        self.hass, self.device, self.folder = hass, device, folder
        self._on_progress = on_progress
        self.progress: dict[str, Any] = {"stage": "starting", "done": 0, "total": 0}

    def _report(self, stage: str, done: int = 0, total: int = 0) -> None:
        self.progress = {"stage": stage, "done": done, "total": total, "at": time.time()}
        self._on_progress(self.progress)

    async def _look(self, pan: float, tilt: float, zoom: float) -> tuple[list[float], bytes]:
        """The camera at a pose, settled: where it reports it is, and its picture."""
        await self.device.goto(pan, tilt, zoom)
        s = await self.device.settle()
        jpeg = await self.device.snapshot()
        return [s["pan"], s["tilt"], s["zoom"]], await self.hass.async_add_executor_job(_small, jpeg)

    async def run(self) -> dict[str, Any]:
        """The whole pass; returns {calibration, panorama}. The camera goes back where it was, whatever happens."""
        dev = self.device
        start = await dev.status()
        try:
            p0 = min(max(start["pan"], dev.ranges["pan"][0] + 0.5), dev.ranges["pan"][1] - 0.5)
            t0 = min(max(start["tilt"], dev.ranges["tilt"][0] + 0.5), dev.ranges["tilt"][1] - 0.5)
            nudged = await self._nudges(p0, t0)
            model, ap = await self._model(p0, t0, nudged)
            panorama = await self._sweep(ptz_model.Model.from_dict(model), ap)
            self._report("done")
            return {"calibration": {**nudged, "model": model, "at": time.time()}, "panorama": panorama}
        except Exception as err:
            self.progress = {"stage": "error", "error": str(err) or type(err).__name__, "at": time.time()}
            self._on_progress(self.progress)
            raise
        finally:
            try:
                await dev.goto(start["pan"], start["tilt"], start["zoom"])
            except PtzError:
                pass

    async def _nudges(self, p0: float, t0: float) -> dict[str, Any]:
        """How many pan / tilt units one frame spans at each of `NUDGE_ZOOMS` (signed: + turns the view right / down)."""
        dev = self.device
        frames = {"pan": 0.35, "tilt": 0.45}
        zooms, pans, tilts = [], [], []
        for k, zoom_level in enumerate(NUDGE_ZOOMS):
            z = dev.ranges["zoom"][0] + zoom_level * (dev.ranges["zoom"][1] - dev.ranges["zoom"][0])
            self._report("measuring", k, len(NUDGE_ZOOMS))
            measured = {}
            for axis in ("pan", "tilt"):
                # Zooming in narrows the frame: start small, grow while the picture barely moves.
                step = NUDGE_FRAMES * abs(frames[axis]) / (1 if k == 0 else 2.5)
                for _attempt in range(6):
                    await dev.goto(p0, t0, z)
                    await dev.settle()
                    before = await dev.snapshot()
                    await dev.goto(p0 + (step if axis == "pan" else 0), t0 + (step if axis == "tilt" else 0), z)
                    await dev.settle()
                    after = await dev.snapshot()
                    dx, dy, clarity = await self.hass.async_add_executor_job(_shift, before, after)
                    moved = dx if axis == "pan" else dy
                    _LOGGER.debug("nudge zoom %.2f %s step %.4f: moved %.3f frames (clarity %.1f)", z, axis, step, moved, clarity)
                    # Measurable: a clear match, between a twentieth and under half a frame.
                    if clarity >= 8 and 0.05 <= abs(moved) <= 0.4:
                        break
                    # No clear match: most likely too far (or the picture changed): smaller.
                    step = step / 2.5 if clarity < 8 else step * 2.5 if abs(moved) < 0.05 else step / 2
                else:
                    raise PtzError(f"couldn't measure the {axis} at zoom {z:.2f}: is the picture changing, or plain?")
                # The view moves against the picture.
                measured[axis] = step / -moved
            zooms.append((await dev.status())["zoom"])
            pans.append(measured["pan"])
            tilts.append(measured["tilt"])
            frames = measured
        return {"zoom": zooms, "pan_per_frame": pans, "tilt_per_frame": tilts}

    async def _model(self, p0: float, t0: float, nudged: dict[str, Any]) -> tuple[dict[str, Any], float | None]:
        """The camera model from frames around (p0, t0) at every model zoom; and the pan scale when the pan wraps."""
        dev = self.device
        zooms, pans, tilts = nudged["zoom"], nudged["pan_per_frame"], nudged["tilt_per_frame"]

        def per_frame(values: list[float], z: float) -> float:
            # The measured frame size at `z`, log-linear between the nudged zooms.
            i = max(0, min(len(zooms) - 2, sum(1 for v in zooms if v <= z) - 1))
            t = (z - zooms[i]) / (zooms[i + 1] - zooms[i])
            a, b = abs(values[i]), abs(values[i + 1])
            return math.exp(math.log(a) + (math.log(b) - math.log(a)) * t)

        lo, hi = dev.ranges["zoom"]
        model_zooms = [lo + z * (hi - lo) for z in MODEL_ZOOMS]
        plan: list[tuple[float, float, float]] = []
        z0 = model_zooms[0]
        pf, tf = per_frame(pans, z0), per_frame(tilts, z0)
        plan += [(p0 + dp * pf / 2, t0 + dt * tf / 2, z0) for dt in (-1, 0, 1) for dp in (-1, 0, 1)]
        plan += [(p0 + dp * pf / 2, t0 + dt * tf, z0) for dt in (-2, 2) for dp in (0, 1)]
        for z in model_zooms[1:]:
            pz, tz = per_frame(pans, z), per_frame(tilts, z)
            plan += [(p0, t0, z), (p0 - 0.4 * pz, t0, z), (p0 + 0.4 * pz, t0, z), (p0, t0 - 0.4 * tz, z), (p0, t0 + 0.4 * tz, z)]
        # A pan range that wraps (one end lands on the other) spans a full turn: the pan scale is known.
        p_lo, p_hi = dev.ranges["pan"]
        await dev.goto(p_lo, t0, z0)
        arrived = await dev.settle()
        for _ in range(5):  # settled: two looks agree
            again = await dev.settle()
            if abs(again["pan"] - arrived["pan"]) < 1e-4:
                break
            arrived = again
        ap = 360 / (p_hi - p_lo) if abs(arrived["pan"] - p_hi) < 0.01 else None
        frames, levels = [], {}
        for k, (p, t, z) in enumerate(plan):
            self._report("model frames", k, len(plan))
            pose, jpeg = await self._look(p, t, z)
            frames.append((pose, jpeg))
            levels.setdefault(round(pose[2], 3), per_frame(pans, z))
        await self.hass.async_add_executor_job(self._keep, "calibration", frames, {"levels": levels, "ap": ap})
        self._report("fitting")
        model = await self.hass.async_add_executor_job(ptz_model.fit, frames, list(levels), levels, ap)
        return model, ap

    async def _sweep(self, model: ptz_model.Model, ap: float | None) -> dict[str, Any]:
        """The whole range at the widest zoom, stitched (panorama.py)."""
        dev = self.device
        wraps = ap is not None or abs(360 / abs(model.ap) - (dev.ranges["pan"][1] - dev.ranges["pan"][0])) < 0.02
        z0 = model.zoom[0]
        stops = ptz_panorama.plan(model, dev.ranges["pan"], dev.ranges["tilt"], z0, wraps)
        shots = []
        for k, (p, t) in enumerate(stops):
            self._report("sweeping", k, len(stops))
            shots.append(await self._look(p, t, z0))
        await self.hass.async_add_executor_job(self._keep, "sweep", shots, {})
        self._report("stitching")
        jpeg, meta = await self.hass.async_add_executor_job(ptz_panorama.stitch, model, shots, wraps)
        meta["id"] = f"{int(time.time() * 1000):x}"
        meta["at"] = time.time()
        meta["wraps"] = wraps

        def write():
            for name in os.listdir(self.folder):
                if name.startswith("panorama-"):
                    os.remove(os.path.join(self.folder, name))
            with open(os.path.join(self.folder, f"panorama-{meta['id']}.jpg"), "wb") as f:
                f.write(jpeg)
        await self.hass.async_add_executor_job(write)
        return meta

    def _keep(self, name: str, frames: list[tuple[list[float], bytes]], extra: dict[str, Any]) -> None:
        folder = os.path.join(self.folder, name)
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder)
        for k, (_, jpeg) in enumerate(frames):
            with open(os.path.join(folder, f"{k:02d}.jpg"), "wb") as f:
                f.write(jpeg)
        with open(os.path.join(folder, "frames.json"), "w") as f:
            json.dump({"poses": [pose for pose, _ in frames], **extra}, f)
