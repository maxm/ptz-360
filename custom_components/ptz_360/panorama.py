"""The panorama: the camera's whole range swept at its widest zoom and
stitched into one picture, equirectangular (x: yaw, y: pitch, `PX_PER_DEGREE`), in the camera model's world.

Each frame is first placed by the model from where the camera reported it was; matching the overlaps (one frame
warped into the other's view, so neighbours turned against each other still match) then gives each frame's real
direction, kept close to the reported one so the picture stays where the model puts things. Brightness is evened
out between frames and the frames are feathered into each other. Plain numpy and PIL, run in an executor.
"""
from __future__ import annotations

import io
import logging
import math
from typing import Any

try:
    from . import model as ptz_model
except ImportError:  # run on its own (tools, tests)
    import model as ptz_model  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

PX_PER_DEGREE = 12
# Rows and frames a little over half a frame apart.
STEP = 0.6
# The blended pictures' size (the matching uses ptz_model's W×H).
BW, BH = 960, 540
# How far a frame's real direction may stray from the reported one (degrees): the weight of its correction.
STRAY = 0.3


def _np():
    import numpy as np  # noqa: PLC0415

    return np


def plan(model: ptz_model.Model, pan_range: list[float], tilt_range: list[float], zoom: float,
         wraps: bool) -> list[tuple[float, float]]:
    """Where to stop (pan, tilt) to see the whole range at `zoom`: rows `STEP` of a frame apart; along each row
    `STEP` of a frame where the frame is narrowest in yaw (its edge nearest the horizon: looking down, a frame
    spans more degrees of yaw, so fewer frames); all the way round when the pan `wraps`. Back and forth."""
    f = model.focal(zoom)
    k1, k2 = (float(v) for v in model.distortion(zoom))

    def straight(r):  # the radius a point recorded at r comes from (the lens squeezes the edges in)
        u = r
        for _ in range(10):
            u = r / (1 + k1 * u * u + k2 * u ** 4)
        return u
    hfov = math.degrees(2 * math.atan(straight(0.5) / f))
    vfov = math.degrees(2 * math.atan(straight(0.5 * 9 / 16) / f))
    (p_lo, p_hi), (t_lo, t_hi) = pan_range, tilt_range
    pitches = sorted([model.at * t_lo + model.bt, model.at * t_hi + model.bt])
    rows, p = [], pitches[0]
    while p < pitches[1]:
        rows.append(p)
        p += STEP * vfov
    rows.append(pitches[1])
    stops: list[tuple[float, float]] = []
    for r, pitch in enumerate(rows):
        tilt = (pitch - model.bt) / model.at
        nearest = max(0.0, abs(pitch) - vfov / 2)
        step = STEP * hfov / max(0.05, math.cos(math.radians(nearest)))
        if wraps:
            n = max(1, math.ceil(360 / step))
            pans = [p_lo + k * (p_hi - p_lo) / n for k in range(n)]
        else:
            span = abs(model.ap) * (p_hi - p_lo)
            n = max(1, math.ceil(span / step))
            pans = [p_lo + k * (p_hi - p_lo) / n for k in range(n + 1)]
        stops += [(pan, tilt) for pan in (pans if r % 2 == 0 else reversed(pans))]
    return stops


def _rot(yaw, pitch, roll):
    """Camera to world: yaw, then pitch, then roll about the view axis (degrees)."""
    np = _np()
    y, p, r = (math.radians(v) for v in (yaw, pitch, roll))
    ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
    rx = np.array([[1, 0, 0], [0, math.cos(p), math.sin(p)], [0, -math.sin(p), math.cos(p)]])
    rz = np.array([[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]])
    return ry @ rx @ rz


class _Frame:
    """A swept frame: its pictures, lens and direction (reported, plus the stitch's correction)."""

    def __init__(self, model: ptz_model.Model, pose: list[float], jpeg: bytes):
        np = _np()
        from PIL import Image  # noqa: PLC0415

        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        self.gray = np.asarray(img.convert("L").resize((ptz_model.W, ptz_model.H)), dtype=np.float32)
        self.color = np.asarray(img.resize((BW, BH)), dtype=np.float32)
        self.pose = pose
        self.f = model.focal(pose[2])
        self.k1, self.k2 = (float(v) for v in model.distortion(pose[2]))
        self.yaw, self.pitch = model.ap * pose[0], model.at * pose[1] + model.bt
        self.fix = np.zeros(3)  # yaw, pitch, roll corrections (degrees)

    def rotation(self, fix=None):
        d = self.fix if fix is None else fix
        return _rot(self.yaw + d[0], self.pitch + d[1], d[2])

    def rays(self, px, py, width, rotation=None):
        """World rays of pixels (px, py) of this frame drawn `width` wide."""
        np = _np()
        u, v = (px - width / 2) / width, (py - width * 9 / 32) / width
        uu, vv = u.copy(), v.copy()
        for _ in range(6):
            r2 = uu * uu + vv * vv
            d = 1 + self.k1 * r2 + self.k2 * r2 * r2
            uu, vv = u / d, v / d
        c = np.stack([uu, -vv, np.full_like(uu, self.f)], -1)
        return c @ (self.rotation() if rotation is None else rotation).T

    def project(self, rays, width, rotation=None):
        """Pixels of this frame drawn `width` wide where world `rays` land (and whether in front)."""
        np = _np()
        c = rays @ (self.rotation() if rotation is None else rotation)
        z = np.where(c[..., 2] > 1e-6, c[..., 2], 1e-6)
        u, v = self.f * c[..., 0] / z, -self.f * c[..., 1] / z
        r2 = u * u + v * v
        d = 1 + self.k1 * r2 + self.k2 * r2 * r2
        return u * d * width + width / 2, v * d * width + width * 9 / 32, c[..., 2] > 1e-6

    def inside(self, px, py, width, height):
        """In the usable part of the picture: away from the edges, above the clock, not the name."""
        np = _np()
        ok = (px > 0.02 * width) & (px < 0.98 * width - 1) & (py > 0.02 * height) & (py < 0.98 * height - 1)
        for x0, y0, x1, y1 in ptz_model.OVERLAYS:
            ok &= ~((px > x0 * width - 2) & (px < x1 * width + 2) & (py > y0 * height - 2) & (py < y1 * height + 2))
        return ok


def _sample(img, px, py):
    """Bilinear samples of `img` (H×W or H×W×3) at (px, py)."""
    np = _np()
    h, w = img.shape[:2]
    x0 = np.clip(np.floor(px).astype(int), 0, w - 2)
    y0 = np.clip(np.floor(py).astype(int), 0, h - 2)
    tx, ty = np.clip(px - x0, 0, 1), np.clip(py - y0, 0, 1)
    if img.ndim == 3:
        tx, ty = tx[..., None], ty[..., None]
    top = img[y0, x0] * (1 - tx) + img[y0, x0 + 1] * tx
    bottom = img[y0 + 1, x0] * (1 - tx) + img[y0 + 1, x0 + 1] * tx
    return top * (1 - ty) + bottom * ty


def _matches(a: _Frame, b: _Frame):
    """Points of `a` and where they are in `b` (pixels of the W×H grey pictures), and the two pictures'
    brightness there: `b` warped into `a`'s view by the model, patches of `a` found in it, then back."""
    np = _np()
    W, H = ptz_model.W, ptz_model.H
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float64)
    bx, by, front = b.project(a.rays(xs, ys, W), W)
    usable = front & b.inside(bx, by, W, H)
    if usable.mean() < 0.1:
        return []
    warped = np.where(usable, _sample(b.gray, bx, by), 0).astype(np.float32)
    out = []
    for ay in np.linspace(30, H - 30, 9):
        for ax in np.linspace(30, W - 30, 14):
            iy, ix = int(ay), int(ax)
            if not (usable[iy - 24:iy + 24, ix - 24:ix + 24].all() and a.inside(np.array([ax]), np.array([ay]), W, H)[0]):
                continue
            found = ptz_model._match(a.gray, warped, ax, ay, ax, ay, 12)  # noqa: SLF001
            if not found:
                continue
            fx, fy = found
            if not (1 <= fx < W - 2 and 1 <= fy < H - 2):
                continue
            jx = float(_sample(bx, np.array([fx]), np.array([fy]))[0])
            jy = float(_sample(by, np.array([fx]), np.array([fy]))[0])
            pa = a.gray[iy - 8:iy + 8, ix - 8:ix + 8].mean()
            pb = warped[int(fy) - 8:int(fy) + 8, int(fx) - 8:int(fx) + 8].mean()
            out.append((ax, ay, jx, jy, pa, pb))
    return out


def stitch(model: ptz_model.Model, shots: list[tuple[list[float], bytes]], wraps: bool) -> tuple[bytes, dict[str, Any]]:
    """The panorama from swept frames (pose, jpeg): the picture (JPEG) and where it lies."""
    np = _np()
    from PIL import Image  # noqa: PLC0415

    frames = [_Frame(model, pose, jpeg) for pose, jpeg in shots]
    n = len(frames)
    W = ptz_model.W
    # The overlapping pairs: centres closer than a frame's width.
    hfov = math.degrees(2 * math.atan(0.5 / min(f.f for f in frames)))
    dirs = [f.rotation() @ np.array([0, 0, 1.0]) for f in frames]
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)
             if math.degrees(math.acos(min(1.0, float(dirs[i] @ dirs[j])))) < 0.85 * hfov]
    obs, light = [], []
    for i, j in pairs:
        for ax, ay, jx, jy, pa, pb in _matches(frames[i], frames[j]):
            obs.append((i, j, ax, ay, jx, jy))
            if pa > 8 and pb > 8:
                light.append((i, j, math.log(pa / pb)))
    obs = np.array(obs)
    _LOGGER.warning("PTZ panorama: %d frames, %d pairs, %d matches", n, len(pairs), len(obs))

    # The frames' corrections: matches agree, each near its reported direction.
    def residuals(x):
        fixes = x.reshape(n, 3)
        rot = [frames[k].rotation(fixes[k]) for k in range(n)]
        out = [x / STRAY]
        if len(obs):
            i, j = obs[:, 0].astype(int), obs[:, 1].astype(int)
            res = np.empty((len(obs), 2))
            for k in np.unique(i):
                for m in np.unique(j[i == k]):
                    s = (i == k) & (j == m)
                    rays = frames[k].rays(obs[s, 2], obs[s, 3], W, rot[k])
                    px, py, _ = frames[m].project(rays, W, rot[m])
                    res[s, 0], res[s, 1] = px - obs[s, 4], py - obs[s, 5]
            out.append(res.ravel())
        return np.concatenate(out)

    x = np.zeros(3 * n)
    keep = np.ones(len(obs), bool)
    for _round in range(2):
        r = residuals(x)
        for _ in range(12):
            jac = np.zeros((len(r), len(x)))
            for k in range(len(x)):
                dx = np.zeros_like(x)
                dx[k] = 1e-3
                jac[:, k] = (residuals(x + dx) - r) / 1e-3
            a, g = jac.T @ jac, jac.T @ r
            step = np.linalg.solve(a + 1e-3 * np.diag(np.diag(a) + 1e-9), -g)
            xn = x + step
            rn = residuals(xn)
            if (rn * rn).sum() >= (r * r).sum():
                break
            x, r = xn, rn
            if np.abs(step).max() < 1e-4:
                break
        # Matches far off the agreement (a moving branch, a repeated pattern) out, then again.
        rr = np.hypot(r[3 * n::2], r[3 * n + 1::2])
        keep = rr < max(2.0, 3 * float(np.median(rr))) if len(rr) else keep
        if _round == 0 and len(obs):
            obs = obs[keep]
    for k in range(n):
        frames[k].fix = x.reshape(n, 3)[k]
    rr = np.hypot(r[3 * n::2], r[3 * n + 1::2]) if len(obs) else np.zeros(1)
    fixes = np.abs(x.reshape(n, 3))
    _LOGGER.warning("PTZ panorama: matches off by %.2f px (rms, of %d), frames moved %.2f° on average, %.2f° at most",
                    float(np.sqrt((rr ** 2).mean())), W, float(fixes[:, :2].mean()), float(fixes[:, :2].max()))

    # Brightness: a gain per frame (log), the overlaps agreeing, each near 1.
    gains = np.zeros(n)
    if light:
        rows = []
        for i, j, d in light:
            row = np.zeros(n)
            row[i], row[j] = 1, -1
            rows.append((row, -d))
        a = np.array([r for r, _ in rows] + list(np.eye(n) * 0.3))
        b = np.array([d for _, d in rows] + [0.0] * n)
        gains = np.linalg.lstsq(a, b, rcond=None)[0]

    # Where it lies: all the way round, or the yaws the frames reach; the pitches they reach.
    edges = []
    for f in frames:
        bx = np.concatenate([np.linspace(0, BW, 9), np.full(5, BW), np.linspace(BW, 0, 9), np.zeros(5)])
        by = np.concatenate([np.zeros(9), np.linspace(0, BH, 5), np.full(9, BH), np.linspace(BH, 0, 5)])
        edges.append(f.rays(bx, by, BW))
    allrays = np.concatenate(edges)
    pitch = np.degrees(np.arcsin(np.clip(allrays[:, 1] / np.linalg.norm(allrays, axis=1), -1, 1)))
    pitch_top, pitch_bottom = min(90.0, float(pitch.max())), max(-90.0, float(pitch.min()))
    if wraps:
        yaw_left, yaw_span = -180.0, 360.0
    else:
        yaws = np.degrees(np.arctan2(allrays[:, 0], allrays[:, 2]))
        yaw_left, yaw_span = float(yaws.min()), float(yaws.max() - yaws.min())
    width, height = round(yaw_span * PX_PER_DEGREE), round((pitch_top - pitch_bottom) * PX_PER_DEGREE)
    total = np.zeros((height, width, 3), np.float32)
    weight = np.zeros((height, width), np.float32)
    for k, f in enumerate(frames):
        # The frame's footprint (yaws relative to its centre, so it doesn't break at ±180).
        centre = math.degrees(math.atan2(dirs[k][0], dirs[k][2]))
        fy = np.degrees(np.arctan2(edges[k][:, 0], edges[k][:, 2]))
        rel = (fy - centre + 180) % 360 - 180
        fp = np.degrees(np.arcsin(np.clip(edges[k][:, 1] / np.linalg.norm(edges[k], axis=1), -1, 1)))
        top, bottom = float(fp.max()), float(fp.min())
        # A frame over a pole (the pole lands in its picture) covers every yaw there.
        pole = None
        for sign in (1.0, -1.0):
            px, py, front = f.project(np.array([[0.0, sign, 0.0]]), BW)
            if front[0] and 0 <= px[0] < BW and 0 <= py[0] < BH:
                pole = sign
        if pole is not None or rel.max() - rel.min() > 300:
            cols = np.arange(width)
            if pole is not None:
                top, bottom = (90.0, bottom) if pole > 0 else (top, -90.0)
        else:
            x0 = math.floor((centre + rel.min() - yaw_left) * PX_PER_DEGREE)
            x1 = math.ceil((centre + rel.max() - yaw_left) * PX_PER_DEGREE)
            cols = np.arange(x0, x1 + 1)
            cols = cols % width if wraps else cols[(cols >= 0) & (cols < width)]
        y0 = max(0, math.floor((pitch_top - min(top, pitch_top)) * PX_PER_DEGREE))
        y1 = min(height, math.ceil((pitch_top - max(bottom, pitch_bottom)) * PX_PER_DEGREE) + 1)
        if len(cols) == 0 or y1 <= y0:
            continue
        rows = np.arange(y0, y1)
        yaw = np.radians(yaw_left + (cols + 0.5) / PX_PER_DEGREE)
        pit = np.radians(pitch_top - (rows + 0.5) / PX_PER_DEGREE)
        yy, pp = np.meshgrid(yaw, pit)
        rays = np.stack([np.cos(pp) * np.sin(yy), np.sin(pp), np.cos(pp) * np.cos(yy)], -1)
        px, py, front = f.project(rays, BW)
        ok = front & f.inside(px, py, BW, BH)
        if not ok.any():
            continue
        # Feathered: weight falling to nothing towards the usable part's edges (the bottom's above the clock).
        clock = min(y0 for _, y0, x1, _ in ptz_model.OVERLAYS if x1 >= 0.99) * BH
        fx = np.minimum(px - 0.02 * BW, 0.98 * BW - px) / (0.5 * BW)
        fyw = np.minimum(py - 0.02 * BH, clock - py) / (0.5 * BH)
        w = np.clip(np.minimum(fx, fyw), 0, 1) ** 2 * ok
        colour = _sample(f.color, np.where(ok, px, 0), np.where(ok, py, 0)) * math.exp(gains[k])
        ri, ci = np.meshgrid(rows, cols, indexing="ij")
        np.add.at(total, (ri, ci), colour * w[..., None])
        np.add.at(weight, (ri, ci), w)
    seen = weight > 1e-6
    picture = np.where(seen[..., None], total / np.maximum(weight, 1e-6)[..., None], 0)
    # Gaps: the colour around them (blurred outward, coarse to fine).
    if not seen.all():
        filled, have = picture.copy(), seen.astype(np.float32)
        for scale in (2, 4, 8, 16, 32, 64):
            hs, ws = max(1, height // scale), max(1, width // scale)
            # Box-average of the seen colour and of seen-ness at this scale, back up.
            sc = np.stack([np.asarray(Image.fromarray((filled[..., c] * have).astype(np.float32)).resize((ws, hs), Image.BOX)) for c in range(3)], -1)
            sh = np.asarray(Image.fromarray(have.astype(np.float32)).resize((ws, hs), Image.BOX))
            up_c = np.stack([np.asarray(Image.fromarray(sc[..., c]).resize((width, height), Image.BILINEAR)) for c in range(3)], -1)
            up_h = np.asarray(Image.fromarray(sh).resize((width, height), Image.BILINEAR))
            fill = (up_h > 1e-3) & (have < 0.5)
            filled[fill] = up_c[fill] / up_h[fill][..., None]
            have = np.where(fill, 1.0, have)
            if have.min() >= 0.5:
                break
        picture = filled
    out = io.BytesIO()
    Image.fromarray(np.clip(picture, 0, 255).astype(np.uint8)).save(out, "JPEG", quality=85)
    meta = {"yaw": yaw_left, "pitch": pitch_top, "px_per_degree": PX_PER_DEGREE, "width": width, "height": height,
            "frames": n, "seen": float(seen.mean())}
    return out.getvalue(), meta
