"""A PTZ camera's geometry, fitted from overlapping frames.

    yaw   = ap · pan                 (degrees; + turns right)
    pitch = at · tilt + bt           (degrees; + looks up)
    focal length f(zoom), as a fraction of the image width over the focal length's tangent plane:
        a ray (x, y, f) in camera space lands at (x, y) / width from the centre (y down)
    radial distortion: r_d = r · (1 + k1 r² + k2 r⁴), r in units of the image width

World space: y up, yaw 0 / pitch 0 looking along +z. Everything here is plain numpy, run in an executor.
"""
from __future__ import annotations

import io
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Analysis resolution.
W, H = 640, 360
# Text the camera draws over the picture (it doesn't move with the view): left out of the matching. Fractions of
# the frame: x0, y0, x1, y1. The clock (bottom) and the name (top left).
OVERLAYS = ((0.0, 0.86, 1.0, 1.0), (0.0, 0.0, 0.45, 0.08))
# How far a frame's real direction may stray from the reported pose (the motors), degrees: the weight of a
# per-frame correction in the fit (a straying of this much costs as much as a pixel off).
STRAY = 0.05


def _clear(x, y, margin: float = 24) -> bool:
    """Whether a patch at pixel (x, y) is clear of the overlays."""
    return not any(x0 * W - margin < x < x1 * W + margin and y0 * H - margin < y < y1 * H + margin
                   for x0, y0, x1, y1 in OVERLAYS)


def _np():
    import numpy as np  # noqa: PLC0415

    return np


def rotation(yaw: float, pitch: float):
    np = _np()
    y, p = np.radians(yaw), np.radians(pitch)
    ry = np.array([[np.cos(y), 0, np.sin(y)], [0, 1, 0], [-np.sin(y), 0, np.cos(y)]])
    rx = np.array([[1, 0, 0], [0, np.cos(p), np.sin(p)], [0, -np.sin(p), np.cos(p)]])
    return ry @ rx


class Model:
    """`k1`, `k2`: one per zoom in `zoom` (the lens bends the picture at wide angle, hardly at all zoomed in), or
    one number for all."""

    def __init__(self, ap: float, at: float, bt: float, k1, k2, zoom: list[float], f: list[float]):
        self.ap, self.at, self.bt = ap, at, bt
        self.zoom, self.f = list(zoom), list(f)
        self.k1 = list(k1) if isinstance(k1, (list, tuple)) else [k1] * len(self.zoom)
        self.k2 = list(k2) if isinstance(k2, (list, tuple)) else [k2] * len(self.zoom)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Model:
        return cls(d["ap"], d["at"], d["bt"], d.get("k1_zoom", d["k1"]), d.get("k2_zoom", d["k2"]), d["zoom"], d["f"])

    def vector(self):
        return _np().array([self.ap, self.at, self.bt, *self.f, *self.k1, *self.k2])

    @classmethod
    def from_vector(cls, x, zoom):
        n = len(zoom)
        return cls(x[0], x[1], x[2], list(x[3 + n:3 + 2 * n]), list(x[3 + 2 * n:3 + 3 * n]), zoom, list(x[3:3 + n]))

    def as_dict(self) -> dict[str, Any]:
        """`k1` / `k2`: the widest zoom's, for apps that know one lens for all zooms."""
        return {"ap": float(self.ap), "at": float(self.at), "bt": float(self.bt),
                "k1": float(self.k1[0]), "k2": float(self.k2[0]),
                "k1_zoom": [float(v) for v in self.k1], "k2_zoom": [float(v) for v in self.k2],
                "zoom": [float(z) for z in self.zoom], "f": [float(v) for v in self.f]}

    def _at(self, zoom):
        """The measured zooms around `zoom` and how far along between them (numpy arrays welcome)."""
        np = _np()
        z = np.asarray(self.zoom)
        i = np.clip(np.searchsorted(z, zoom, side="right") - 1, 0, len(z) - 2)
        return i, (zoom - z[i]) / (z[i + 1] - z[i])

    def focal(self, zoom):
        """f at `zoom`, log-linear between the measured zooms."""
        np = _np()
        i, t = self._at(zoom)
        lf = np.log(np.asarray(self.f))
        f = np.exp(lf[i] + (lf[i + 1] - lf[i]) * t)
        return float(f) if np.ndim(f) == 0 else f

    def distortion(self, zoom):
        """(k1, k2) at `zoom`, linear between the measured zooms, held past the ends."""
        np = _np()
        i, t = self._at(zoom)
        t = np.clip(t, 0, 1)
        k1, k2 = np.asarray(self.k1), np.asarray(self.k2)
        return k1[i] + (k1[i + 1] - k1[i]) * t, k2[i] + (k2[i + 1] - k2[i]) * t

    def rays(self, px, py, pose):
        """World rays of pixels (px, py) of a W×H frame taken at `pose` (pan, tilt, zoom); a pose per pixel
        (arrays) or one for all."""
        np = _np()
        pan, tilt, zoom = (np.asarray(v, dtype=float) for v in pose)
        k1, k2 = self.distortion(zoom)
        u, v = (px - W / 2) / W, (py - H / 2) / W
        uu, vv = u.copy(), v.copy()
        for _ in range(5):
            r2 = uu * uu + vv * vv
            d = 1 + k1 * r2 + k2 * r2 * r2
            uu, vv = u / d, v / d
        c = np.stack([uu, -vv, np.broadcast_to(self.focal(zoom), uu.shape)], axis=-1)
        return np.einsum("...ij,...j->...i", rotations(self.ap * pan, self.at * tilt + self.bt), c)

    def project(self, rays, pose):
        """Pixels of a W×H frame taken at `pose` where world `rays` land (a pose per ray, or one for all)."""
        np = _np()
        pan, tilt, zoom = (np.asarray(v, dtype=float) for v in pose)
        c = np.einsum("...ji,...j->...i", rotations(self.ap * pan, self.at * tilt + self.bt), rays)
        f = self.focal(zoom)
        k1, k2 = self.distortion(zoom)
        u, v = f * c[..., 0] / c[..., 2], -f * c[..., 1] / c[..., 2]
        r2 = u * u + v * v
        d = 1 + k1 * r2 + k2 * r2 * r2
        return u * d * W + W / 2, v * d * W + H / 2


def rotations(yaw, pitch):
    """`rotation` for arrays of yaws and pitches: (..., 3, 3)."""
    np = _np()
    y, p = np.radians(yaw), np.radians(pitch)
    cy, sy, cp, sp = np.cos(y), np.sin(y), np.cos(p), np.sin(p)
    z, o = np.zeros_like(y), np.ones_like(y)
    ry = np.stack([np.stack([cy, z, sy], -1), np.stack([z, o, z], -1), np.stack([-sy, z, cy], -1)], -2)
    rx = np.stack([np.stack([o, z, z], -1), np.stack([z, cp, sp], -1), np.stack([z, -sp, cp], -1)], -2)
    return ry @ rx


def gray(jpeg: bytes):
    np = _np()
    from PIL import Image  # noqa: PLC0415

    return np.asarray(Image.open(io.BytesIO(jpeg)).convert("L").resize((W, H)), dtype=np.float32)


def _match(a, b, ax, ay, bx, by, search, size=48):
    """Where the patch of `a` at (ax, ay) is in `b`, searched around (bx, by): NCC, sub-pixel; None if unclear."""
    np = _np()
    h = size // 2
    if not (h <= ax < W - h and h <= ay < H - h):
        return None
    t = a[int(ay) - h:int(ay) + h, int(ax) - h:int(ax) + h]
    t = t - t.mean()
    tn = np.sqrt((t * t).sum())
    if tn < 200:
        return None
    scores: dict[tuple[int, int], float] = {}

    def score(x, y):
        if (x, y) in scores:
            return scores[(x, y)]
        if not (h <= x < W - h and h <= y < H - h):
            return None
        p = b[y - h:y + h, x - h:x + h]
        p = p - p.mean()
        pn = np.sqrt((p * p).sum())
        s = float((t * p).sum() / (tn * pn)) if pn >= 1 else None
        scores[(x, y)] = s
        return s

    best = (-2.0, 0, 0)
    for dy in range(-search, search + 1, 2):
        for dx in range(-search, search + 1, 2):
            s = score(int(bx) + dx, int(by) + dy)
            if s is not None and s > best[0]:
                best = (s, int(bx) + dx, int(by) + dy)
    if best[0] < 0.7:
        return None
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            score(best[1] + dx, best[2] + dy)
    s, x, y = max((v, k[0], k[1]) for k, v in scores.items() if v is not None)

    def sub(m, c, p):
        if m is None or p is None:
            return 0.0
        den = m - 2 * c + p
        return 0.5 * (m - p) / den if den != 0 else 0.0

    return x + sub(score(x - 1, y), s, score(x + 1, y)), y + sub(score(x, y - 1), s, score(x, y + 1))


def fit(frames: list[tuple[list[float], bytes]], zoom_levels: list[float], first_frames: dict[float, float],
        ap: float | None = None) -> dict[str, Any]:
    """The model from frames (pose, jpeg) taken at a few poses per zoom level. `first_frames`: a rough frame width
    in pan units per zoom level (the old calibration) for the first guess. `ap`: the pan scale when it's known (a
    pan range that wraps a full turn), kept as is: from frames alone it trades off against the wide lens's
    bending. Returns the model and its error."""
    np = _np()
    imgs = [gray(j) for _, j in frames]
    poses = np.array([p for p, _ in frames])
    levels = sorted(zoom_levels)

    def level(z):
        return int(np.argmin([abs(z - lv) for lv in levels]))

    lvl = [level(p[2]) for p in poses]
    # First guess: 360° of pan over the 2 units, the frame widths turned into focal lengths.
    ap0 = ap or 180.0
    f0 = [0.5 / np.tan(np.radians(ap0 * first_frames[lv] / 2)) for lv in levels]
    model = Model(ap0, 72.0, -30.0, 0.0, 0.0, levels, f0)
    grid = [(x, y) for y in np.linspace(40, H - 40, 6) for x in np.linspace(40, W - 40, 9)]

    def collect(m: Model, search: int, ps):
        obs = []
        for i in range(len(frames)):
            for j in range(len(frames)):
                if i == j or lvl[i] != lvl[j]:
                    continue
                pts = np.array(grid)
                bx, by = m.project(m.rays(pts[:, 0], pts[:, 1], ps[i]), ps[j])
                for (ax, ay), px, py in zip(grid, bx, by):
                    if 40 < px < W - 40 and 40 < py < H - 40 and _clear(ax, ay) and _clear(px, py):
                        found = _match(imgs[i], imgs[j], ax, ay, px, py, search)
                        if found:
                            obs.append((i, j, ax, ay, found[0], found[1]))
        return np.array(obs)

    nm = 3 + 3 * len(levels)  # the model's parameters; then a yaw and pitch correction per frame (degrees)

    def corrected(x, m: Model):
        """The frames' poses with their corrections."""
        c = x[nm:].reshape(len(frames), 2)
        return np.stack([poses[:, 0] + c[:, 0] / m.ap, poses[:, 1] + c[:, 1] / m.at, poses[:, 2]], 1)

    def residuals(x, obs):
        m = Model.from_vector(x[:nm], levels)
        ps = corrected(x, m)
        i, j = obs[:, 0].astype(int), obs[:, 1].astype(int)
        px, py = m.project(m.rays(obs[:, 2], obs[:, 3], ps[i].T), ps[j].T)
        out = np.empty(2 * len(obs) + 2 * len(levels) + 2 * len(frames))
        n = 2 * len(obs)
        out[0:n:2], out[1:n:2] = px - obs[:, 4], py - obs[:, 5]
        # A weak pull of the lens terms towards none, where few frames pin them down.
        out[n:n + 2 * len(levels)] = 5 * np.array([*m.k1, *m.k2])
        # The motors are close to what they report.
        out[n + 2 * len(levels):] = x[nm:] / STRAY
        return out

    def lm(x, obs, iters=25):
        lam = 1e-3
        r = residuals(x, obs)
        cost = float((r * r).sum())
        for _ in range(iters):
            jac = np.zeros((len(r), len(x)))
            for k in range(1 if ap else 0, len(x)):
                dx = np.zeros_like(x)
                dx[k] = 1e-4 * max(1.0, abs(x[k]))
                jac[:, k] = (residuals(x + dx, obs) - r) / dx[k]
            a, g = jac.T @ jac, jac.T @ r
            improved = False
            while lam < 1e8:
                xn = x + np.linalg.solve(a + lam * np.diag(np.diag(a) + 1e-9) + np.diag([1.0 if ap and k == 0 else 0.0 for k in range(len(x))]), -g)
                rn = residuals(xn, obs)
                cn = float((rn * rn).sum())
                if cn < cost:
                    x, r, cost, lam, improved = xn, rn, cn, lam / 3, True
                    break
                lam *= 5
            if not improved:
                break
        return x, r

    x = np.concatenate([model.vector(), np.zeros(2 * len(frames))])
    obs = None
    for search in (60, 16, 10):
        m = Model.from_vector(x[:nm], levels)
        obs = collect(m, search, corrected(x, m))
        if len(obs) < 20:
            raise ValueError(f"only {len(obs)} matches")
        x, r = lm(x, obs)
        r = r[:2 * len(obs)]
        rr = np.hypot(r[0::2], r[1::2])
        obs = obs[rr < max(2.0, 3 * float(np.median(rr)))]
        x, r = lm(x, obs)
        r = r[:2 * len(obs)]
    model = Model.from_vector(x[:nm], levels)
    stray = np.abs(x[nm:]).reshape(len(frames), 2)
    _LOGGER.warning("PTZ 360 model: frames off their reported pose by %.3f° on average, %.3f° at most",
                    float(stray.mean()), float(stray.max()))
    rms = float(np.sqrt((r * r).mean()))
    _LOGGER.warning("PTZ 360 model: %s, rms %.2f px of %d (%d matches)", model.as_dict(), rms, W, len(obs))
    return {**model.as_dict(), "rms": rms, "matches": int(len(obs)), "width": W}
