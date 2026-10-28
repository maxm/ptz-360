// PTZ 360 card: an ONVIF PTZ camera (the ptz_360 integration) as a 360° view.
//
// The view is a virtual camera over the camera's stitched panorama; the live picture sits on it where the picture
// itself says it was taken. A drag or pinch moves the view at once; the real camera follows (one goto at a time) and
// its position comes back from ptz_360/status. While a move is followed, each new video frame is found in the last
// still one (normalised correlation, coarse to fine), checked against the panorama every few frames, and found again
// in the panorama after the video stalls. When the camera settles, the live video takes over where the last frame
// was; a check against the panorama then teaches the offset between the camera's reports and its picture (kept in
// this browser) and repaints the panorama there. A port of the Casa apps' PTZ drive with the fixes PTZ Lab found.

const GW = 160, GH = 90; // the grey frames found in each other

// ─────────────────────────── matching (a worker) ───────────────────────────

function worker() {
  const W = 160, H = 90;
  let model = null, pano = null;

  // The camera model: yaw = ap·pan, pitch = at·tilt + bt (degrees), focal length per zoom (image widths, log-linear),
  // radial distortion k1 / k2 per zoom (linear). World: y up, yaw 0 / pitch 0 along +z.
  function seg(z) {
    const zs = model.zoom;
    let i = 0;
    for (let k = 0; k < zs.length; k++) if (zs[k] <= z) i = k;
    i = Math.max(0, Math.min(zs.length - 2, i));
    return [i, (z - zs[i]) / (zs[i + 1] - zs[i])];
  }
  const focal = (z) => { const [i, t] = seg(z), f = model.f; return Math.exp(Math.log(f[i]) + (Math.log(f[i + 1]) - Math.log(f[i])) * t); };
  const lens = (z) => {
    const [i, t0] = seg(z), t = Math.max(0, Math.min(1, t0)), a = model.k1_zoom || [], b = model.k2_zoom || [];
    if (!a.length) return [model.k1 || 0, model.k2 || 0];
    return [a[i] + (a[i + 1] - a[i]) * t, b[i] + (b[i + 1] - b[i]) * t];
  };
  function zoomForFocal(fc) {
    const target = Math.log(fc), zs = model.zoom, f = model.f;
    for (let i = 0; i < zs.length - 1; i++) {
      const a = Math.log(f[i]), b = Math.log(f[i + 1]);
      if (target >= Math.min(a, b) && target <= Math.max(a, b)) return zs[i] + (zs[i + 1] - zs[i]) * (b === a ? 0 : (target - a) / (b - a));
    }
    return target < Math.log(f[0]) ? zs[0] : zs[zs.length - 1];
  }
  const rad = Math.PI / 180;
  function rotation(yaw, pitch) {
    const y = yaw * rad, p = pitch * rad, cy = Math.cos(y), sy = Math.sin(y), cp = Math.cos(p), sp = Math.sin(p);
    return [cy, -sy * sp, sy * cp, 0, cp, sp, -sy, -cy * sp, cy * cp]; // Ry · Rx, row-major
  }
  const mul = (r, v) => [r[0] * v[0] + r[1] * v[1] + r[2] * v[2], r[3] * v[0] + r[4] * v[1] + r[5] * v[2], r[6] * v[0] + r[7] * v[1] + r[8] * v[2]];
  const angles = (d) => [Math.atan2(d[0], d[2]) / rad, Math.asin(Math.max(-1, Math.min(1, d[1] / Math.hypot(d[0], d[1], d[2])))) / rad];
  const poseRotation = (pose) => rotation(model.ap * pose.pan, model.at * pose.tilt + model.bt);
  // The world ray through (u, v) (image widths from the centre, v down) of a straightened picture taken at `pose`.
  const straightRay = (u, v, pose) => mul(poseRotation(pose), [u, -v, focal(pose.zoom)]);
  const poseOfRay = (ray, zoom) => { const [yaw, pitch] = angles(ray); return { pan: yaw / model.ap, tilt: (pitch - model.bt) / model.at, zoom }; };

  // A grey frame as through a lens without bending (the lens bends a wide picture and not a zoomed one).
  function straightened(p, k1, k2) {
    if (Math.abs(k1) < 1e-4 && Math.abs(k2) < 1e-4) return p;
    const out = new Float32Array(W * H), cx = W / 2, cy = H / 2;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const u = (x - cx) / W, v = (y - cy) / W, r2 = u * u + v * v, f = 1 + k1 * r2 + k2 * r2 * r2;
      const fx = cx + u * f * W, fy = cy + v * f * W;
      const x0 = Math.min(W - 2, Math.max(0, Math.floor(fx))), y0 = Math.min(H - 2, Math.max(0, Math.floor(fy)));
      const tx = Math.min(1, Math.max(0, fx - x0)), ty = Math.min(1, Math.max(0, fy - y0)), o = y0 * W + x0;
      const top = p[o] + (p[o + 1] - p[o]) * tx, bottom = p[o + W] + (p[o + W + 1] - p[o + W]) * tx;
      out[y * W + x] = top + (bottom - top) * ty;
    }
    return out;
  }
  function half(p) {
    const hw = W / 2, hh = H / 2, out = new Float32Array(hw * hh);
    for (let y = 0; y < hh; y++) for (let x = 0; x < hw; x++) {
      const o = 2 * y * W + 2 * x;
      out[y * hw + x] = (p[o] + p[o + 1] + p[o + W] + p[o + W + 1]) / 4;
    }
    return out;
  }
  // `p` (w×h) brought to `scale` about the centre, and the part of it from the picture (not past its edges, not the
  // clock at the bottom): [x0, y0, x1, y1].
  function scaled(p, w, h, scale) {
    const cx = w / 2, cy = h / 2, sx0 = 2, sx1 = w - 3, sy0 = 2, sy1 = Math.floor(h * 7 / 8) - 2;
    const rect = [Math.max(0, Math.ceil(cx + (sx0 - cx) * scale)), Math.max(0, Math.ceil(cy + (sy0 - cy) * scale)),
      Math.min(w, Math.floor(cx + (sx1 - cx) * scale) + 1), Math.min(h, Math.floor(cy + (sy1 - cy) * scale) + 1)];
    if (Math.abs(scale - 1) < 0.0005) return [p, rect];
    const out = new Float32Array(w * h);
    for (let y = rect[1]; y < Math.max(rect[1], rect[3]); y++) {
      const fy = cy + (y - cy) / scale, y0 = Math.min(h - 2, Math.max(0, Math.floor(fy))), ty = Math.min(1, Math.max(0, fy - y0));
      for (let x = rect[0]; x < Math.max(rect[0], rect[2]); x++) {
        const fx = cx + (x - cx) / scale, x0 = Math.min(w - 2, Math.max(0, Math.floor(fx))), tx = Math.min(1, Math.max(0, fx - x0));
        const o = y0 * w + x0, top = p[o] + (p[o + 1] - p[o]) * tx, bottom = p[o + w] + (p[o + w + 1] - p[o + w]) * tx;
        out[y * w + x] = top + (bottom - top) * ty;
      }
    }
    return [out, rect];
  }
  // Normalised correlation of b against a moved by (dx, dy), every other row of the overlap (not the clock).
  function ncc(a, b, dx, dy, w, h, rect) {
    const r = rect || [0, 0, w, h];
    const x0 = Math.max(0, r[0] + dx) + 3, x1 = Math.min(w, r[2] + dx) - 3;
    const y0 = Math.max(0, r[1] + dy) + 3, y1 = Math.min(Math.floor(h * 7 / 8), r[3] + dy) - 3;
    const least = rect ? 1 : 2;
    if (!(x1 - x0 > Math.floor(w * least / 5) && y1 - y0 > Math.floor(h * least / 5))) return null;
    let sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0, n = 0;
    for (let y = y0; y < y1; y += 2) {
      const ob = y * w, oa = (y - dy) * w - dx;
      for (let x = x0; x < x1; x++) {
        const pb = b[ob + x], pa = a[oa + x];
        sa += pb; sb += pa; saa += pb * pb; sbb += pa * pa; sab += pb * pa;
      }
      n += x1 - x0;
    }
    const va = saa - sa * sa / n, vb = sbb - sb * sb / n;
    if (!(va > 1 && vb > 1)) return null;
    return (sab - sa * sb / n) / Math.sqrt(va * vb);
  }
  // The best shift within (±rx, ±ry) of the guess, sub-pixel, with its score.
  function search(a, b, gx0, gy0, rx, ry, w, h, rect, minScore) {
    const gx = Math.round(gx0), gy = Math.round(gy0), cols = 2 * rx + 1, scores = new Float64Array(cols * (2 * ry + 1)).fill(-2);
    let best = [-2, 0, 0];
    for (let dy = gy - ry; dy <= gy + ry; dy++) for (let dx = gx - rx; dx <= gx + rx; dx++) {
      const s = ncc(a, b, dx, dy, w, h, rect);
      if (s === null) continue;
      scores[(dy - gy + ry) * cols + (dx - gx + rx)] = s;
      if (s > best[0]) best = [s, dx, dy];
    }
    if (best[0] < minScore) return null;
    const score = (dx, dy) => { const i = dx - gx + rx, j = dy - gy + ry; if (i < 0 || i >= cols || j < 0 || j > 2 * ry) return null; const s = scores[j * cols + i]; return s > -2 ? s : null; };
    const sub = (m, c, p) => { if (m === null || p === null) return 0; const den = m - 2 * c + p; return den !== 0 ? Math.max(-0.5, Math.min(0.5, 0.5 * (m - p) / den)) : 0; };
    return { dx: best[1] + sub(score(best[1] - 1, best[2]), best[0], score(best[1] + 1, best[2])),
      dy: best[2] + sub(score(best[1], best[2] - 1), best[0], score(best[1], best[2] + 1)), score: best[0] };
  }
  // Where frame b lies in anchor a: b is a brought to `scale` about the centre, then moved by (dx, dy); searched around
  // `prior`, at half size then full size; the scale too while zooming. Null when nothing matches well.
  function locate(b, a, prior, zooming) {
    const ha = half(a), hb = half(b);
    const coarseScales = zooming ? [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5].map((k) => prior.scale * Math.exp(k * 0.03)) : [prior.scale];
    let best = null;
    for (const s of coarseScales) {
      const [r, rect] = scaled(ha, W / 2, H / 2, s);
      const f = search(r, hb, prior.dx / 2, prior.dy / 2, 12, 8, W / 2, H / 2, rect, 0.4);
      if (f && f.score > (best ? best.score : -2)) best = { scale: s, dx: f.dx * 2, dy: f.dy * 2, score: f.score };
    }
    if (!best) return null;
    const fineScales = zooming ? [-3, -2, -1, 0, 1, 2, 3].map((k) => best.scale * Math.exp(k * 0.008)) : [best.scale];
    const fine = fineScales.map((s) => {
      const [r, rect] = scaled(a, W, H, s);
      const f = search(r, b, best.dx, best.dy, 2, 2, W, H, rect, -1);
      return f ? { scale: s, dx: f.dx, dy: f.dy, score: f.score } : { scale: s, dx: best.dx, dy: best.dy, score: -2 };
    });
    let i = 0;
    fine.forEach((f, k) => { if (f.score > fine[i].score) i = k; });
    if (fine[i].score <= -2) return best;
    const result = { ...fine[i] };
    if (i > 0 && i < fine.length - 1 && fine[i - 1].score > -2 && fine[i + 1].score > -2) {
      const m = fine[i - 1].score, c = fine[i].score, p = fine[i + 1].score, den = m - 2 * c + p;
      if (den !== 0) result.scale *= Math.exp(Math.max(-0.5, Math.min(0.5, 0.5 * (m - p) / den)) * 0.008);
    }
    return result;
  }

  function sample(ray) {
    const L = pano.layout, [yaw, pitch] = angles(ray);
    let dx = yaw - L.yaw;
    if (L.wraps) { dx %= 360; if (dx < 0) dx += 360; }
    const x = dx * L.px_per_degree * pano.scale - 0.5, y = (L.pitch - pitch) * L.px_per_degree * pano.scale - 0.5;
    if (x < 0 || y < 0 || x >= pano.w - 1 || y >= pano.h - 1) return null;
    const x0 = Math.floor(x), y0 = Math.floor(y), fx = x - x0, fy = y - y0, o = y0 * pano.w + x0, g = pano.gray;
    const top = g[o] + (g[o + 1] - g[o]) * fx, bottom = g[o + pano.w] + (g[o + pano.w + 1] - g[o + pano.w]) * fx;
    return top + (bottom - top) * fy;
  }
  // What a camera at `view` sees according to the panorama, straightened, W×H; null when it covers too little.
  function render(view) {
    const out = new Float32Array(W * H), r = poseRotation(view), f = focal(view.zoom);
    let covered = 0, sum = 0;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const v = sample(mul(r, [(x - W / 2) / W, -(y - H / 2) / W, f]));
      if (v === null) { out[y * W + x] = NaN; continue; }
      out[y * W + x] = v; covered++; sum += v;
    }
    if (covered < W * H * 0.85) return null;
    const mean = sum / covered;
    for (let i = 0; i < out.length; i++) if (Number.isNaN(out[i])) out[i] = mean;
    return out;
  }
  // Where a straightened frame believed to be at `pose` is in the panorama; null when unclear or far off.
  function panoLocate(frame, pose) {
    const reference = render(pose);
    if (!reference) return null;
    const found = locate(frame, reference, { scale: 1, dx: 0, dy: 0 }, true);
    if (!found || found.score < 0.55 || Math.abs(Math.log(found.scale)) > 0.1 || Math.abs(found.dx) > W * 0.15 || Math.abs(found.dy) > H * 0.15) return null;
    const ray = straightRay(-found.dx / found.scale / W, -found.dy / found.scale / W, pose);
    return { pose: poseOfRay(ray, zoomForFocal(focal(pose.zoom) * found.scale)), score: found.score };
  }

  onmessage = (e) => {
    const m = e.data;
    let result = null;
    if (m.kind === "model") model = m.model;
    else if (m.kind === "pano") pano = m.pano;
    else if (m.kind === "straighten") { const [k1, k2] = lens(m.zoom); result = straightened(m.gray, k1, k2); }
    else if (m.kind === "locate") {
      // Straightened through the lens at the zoom it's about at, found in the anchor.
      const [k1, k2] = lens(zoomForFocal(focal(m.anchorPose.zoom) * m.prior.scale));
      const straight = straightened(m.gray, k1, k2);
      const found = locate(straight, m.anchor, m.prior, m.zooming);
      let pose = null;
      if (found) {
        const ray = straightRay(-found.dx / found.scale / W, -found.dy / found.scale / W, m.anchorPose);
        pose = poseOfRay(ray, zoomForFocal(focal(m.anchorPose.zoom) * found.scale));
      }
      result = { straight, found, pose };
    } else if (m.kind === "panorama") result = pano ? panoLocate(m.straight, m.pose) : null;
    else if (m.kind === "relocalize") {
      // Found again in the panorama around each candidate pose (straightened at its zoom).
      for (const c of m.candidates) {
        if (!pano) break;
        const [k1, k2] = lens(c.zoom), straight = straightened(m.gray, k1, k2), found = panoLocate(straight, c);
        if (found) { result = { pose: found.pose, straight, score: found.score }; break; }
      }
    }
    postMessage({ id: m.id, result });
  };
}

// ─────────────────────────── the camera model ───────────────────────────

class Model {
  constructor(m) { Object.assign(this, m); }
  seg(z) {
    const zs = this.zoom;
    let i = 0;
    for (let k = 0; k < zs.length; k++) if (zs[k] <= z) i = k;
    i = Math.max(0, Math.min(zs.length - 2, i));
    return [i, (z - zs[i]) / (zs[i + 1] - zs[i])];
  }
  focal(z) { const [i, t] = this.seg(z), f = this.f; return Math.exp(Math.log(f[i]) + (Math.log(f[i + 1]) - Math.log(f[i])) * t); }
  lens(z) {
    const [i, t0] = this.seg(z), t = Math.max(0, Math.min(1, t0)), a = this.k1_zoom || [], b = this.k2_zoom || [];
    if (!a.length) return [this.k1 || 0, this.k2 || 0];
    return [a[i] + (a[i + 1] - a[i]) * t, b[i] + (b[i + 1] - b[i]) * t];
  }
  zoomForFocal(fc) {
    const target = Math.log(fc), zs = this.zoom, f = this.f;
    for (let i = 0; i < zs.length - 1; i++) {
      const a = Math.log(f[i]), b = Math.log(f[i + 1]);
      if (target >= Math.min(a, b) && target <= Math.max(a, b)) return zs[i] + (zs[i + 1] - zs[i]) * (b === a ? 0 : (target - a) / (b - a));
    }
    return target < Math.log(f[0]) ? zs[0] : zs[zs.length - 1];
  }
  yaw(pan) { return this.ap * pan; }
  pitch(tilt) { return this.at * tilt + this.bt; }
  pan(yaw) { return yaw / this.ap; }
  tilt(pitch) { return (pitch - this.bt) / this.at; }
  viewHfov(z) { return 2 * Math.atan(0.5 / this.focal(z)) * 180 / Math.PI; }
  viewVfov(z) { return 2 * Math.atan(0.5 * 9 / 16 / this.focal(z)) * 180 / Math.PI; }
  rotation(pose) {
    const y = this.yaw(pose.pan) * Math.PI / 180, p = this.pitch(pose.tilt) * Math.PI / 180;
    const cy = Math.cos(y), sy = Math.sin(y), cp = Math.cos(p), sp = Math.sin(p);
    return [cy, -sy * sp, sy * cp, 0, cp, sp, -sy, -cy * sp, cy * cp]; // Ry · Rx, row-major
  }
  direction(pose) { const r = this.rotation(pose); return [r[2], r[5], r[8]]; }
  degreesBetween(a, b) {
    const u = this.direction(a), v = this.direction(b);
    return Math.acos(Math.min(1, u[0] * v[0] + u[1] * v[1] + u[2] * v[2])) * 180 / Math.PI;
  }
}

const colMajor = (r) => [r[0], r[3], r[6], r[1], r[4], r[7], r[2], r[5], r[8]];

// ─────────────────────────── drawing (WebGL2) ───────────────────────────

const VERTEX = `#version 300 es
in vec2 p; out vec2 uv;
void main() { uv = p * 0.5 + 0.5; gl_Position = vec4(p, 0.0, 1.0); }`;

// The view: each screen pixel's ray (through the view's lens, like the camera's: a frame seen from where it was taken
// lands as recorded), then the live frame where it covers the ray, else the panorama.
const FRAGMENT = `#version 300 es
precision highp float;
in vec2 uv; out vec4 color;
uniform float aspect;
uniform mat3 viewR; uniform float viewF; uniform vec2 viewK;
uniform sampler2D pano; uniform bool hasPano; uniform vec4 panoLayout; uniform vec2 panoSize;
uniform sampler2D live; uniform bool hasLive; uniform mat3 liveR; uniform float liveF; uniform vec2 liveK; uniform float liveAspect;
uniform float grey;
const float DEG = 57.29577951308232;
void main() {
  vec2 sd = vec2(uv.x - 0.5, (0.5 - uv.y) * aspect);
  vec2 s = sd;
  for (int i = 0; i < 5; i++) { float r2 = dot(s, s); s = sd / (1.0 + viewK.x * r2 + viewK.y * r2 * r2); }
  vec3 ray = normalize(viewR * vec3(s.x, -s.y, viewF));
  vec4 c = vec4(0.09, 0.09, 0.1, 1.0);
  if (hasPano) {
    float yaw = atan(ray.x, ray.z) * DEG, pitch = asin(clamp(ray.y, -1.0, 1.0)) * DEG;
    float dx = yaw - panoLayout.x;
    if (panoLayout.w > 0.5) dx = mod(dx, 360.0);
    vec2 p = vec2(dx * panoLayout.z, (panoLayout.y - pitch) * panoLayout.z) / panoSize;
    if (p.x >= 0.0 && p.x <= 1.0 && p.y >= 0.0 && p.y <= 1.0) {
      vec3 t = texture(pano, p).rgb;
      c = vec4(mix(t, vec3(dot(t, vec3(0.299, 0.587, 0.114))), grey) * 0.85, 1.0);
    }
  }
  if (hasLive) {
    vec3 q = transpose(liveR) * ray;
    if (q.z > 0.05) {
      vec2 l = vec2(liveF * q.x / q.z, -liveF * q.y / q.z);
      float r2 = dot(l, l), d = 1.0 + liveK.x * r2 + liveK.y * r2 * r2;
      vec2 t = vec2(0.5 + l.x * d, 0.5 + l.y * d / liveAspect);
      float edge = min(min(t.x, 1.0 - t.x), min(t.y, 1.0 - t.y));
      // Past the picture (0.65 widths out) the lens's polynomial folds back onto it: not there.
      if (edge > 0.0 && r2 < 0.42) c = mix(c, texture(live, t), smoothstep(0.0, 0.006, edge));
    }
  }
  color = c;
}`;

// Into the panorama (its texture as the target): a frame at a pose, without its clock and edges, the border feathered.
// The panorama's rows go down from row 0 (its top), like the texture's.
const PAINT = `#version 300 es
precision highp float;
in vec2 uv; out vec4 color;
uniform vec4 panoLayout; uniform vec4 rect;
uniform sampler2D live; uniform mat3 liveR; uniform float liveF; uniform vec2 liveK; uniform float liveAspect;
const float RAD = 0.017453292519943295;
void main() {
  vec2 px = mix(rect.xy, rect.zw, uv);
  float yaw = (panoLayout.x + px.x / panoLayout.z) * RAD, pitch = (panoLayout.y - px.y / panoLayout.z) * RAD;
  vec3 ray = vec3(cos(pitch) * sin(yaw), sin(pitch), cos(pitch) * cos(yaw));
  vec3 q = transpose(liveR) * ray;
  if (q.z < 0.05) discard;
  vec2 l = vec2(liveF * q.x / q.z, -liveF * q.y / q.z);
  float r2 = dot(l, l), d = 1.0 + liveK.x * r2 + liveK.y * r2 * r2;
  vec2 t = vec2(0.5 + l.x * d, 0.5 + l.y * d / liveAspect);
  float edge = min(min(t.x - 0.03, 0.97 - t.x), min(t.y - 0.03, 0.88 - t.y));
  if (edge <= 0.0 || r2 > 0.42) discard;
  color = vec4(texture(live, t).rgb, min(1.0, edge / 0.04));
}`;

class View {
  constructor(canvas) {
    const gl = canvas.getContext("webgl2", { antialias: false });
    if (!gl) throw new Error("WebGL2 isn't available");
    this.gl = gl;
    this.canvas = canvas;
    this.prog = this.program(VERTEX, FRAGMENT);
    this.paintProg = this.program(VERTEX, PAINT);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    this.panoTex = this.texture();
    this.liveTex = this.texture();
    this.fbo = gl.createFramebuffer();
    this.panoSize = [1, 1];
    this.maxTexture = gl.getParameter(gl.MAX_TEXTURE_SIZE);
  }
  program(vs, fs) {
    const gl = this.gl, p = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      gl.attachShader(p, s);
    }
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    p.u = {};
    const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < n; i++) { const info = gl.getActiveUniform(p, i); p.u[info.name] = gl.getUniformLocation(p, info.name); }
    return p;
  }
  texture() {
    const gl = this.gl, t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
    return t;
  }
  setPanorama(source) {
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.panoTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    this.panoSize = [source.width, source.height];
    this.hasPano = true;
  }
  setLive(source) {
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.liveTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
  }
  liveUniforms(p, model, pose, aspect) {
    const gl = this.gl, [k1, k2] = model.lens(pose.zoom);
    gl.uniformMatrix3fv(p.u.liveR, false, colMajor(model.rotation(pose)));
    gl.uniform1f(p.u.liveF, model.focal(pose.zoom));
    gl.uniform2f(p.u.liveK, k1, k2);
    gl.uniform1f(p.u.liveAspect, aspect);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.liveTex);
    gl.uniform1i(p.u.live, 1);
  }
  quad(p) {
    const gl = this.gl, loc = gl.getAttribLocation(p, "p");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }
  draw(model, view, layout, live, livePose, liveAspect, grey) {
    const gl = this.gl, p = this.prog, c = this.canvas;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, c.width, c.height);
    gl.disable(gl.BLEND);
    gl.useProgram(p);
    const [k1, k2] = model.lens(view.zoom);
    gl.uniform1f(p.u.aspect, c.height / c.width);
    gl.uniformMatrix3fv(p.u.viewR, false, colMajor(model.rotation(view)));
    gl.uniform1f(p.u.viewF, model.focal(view.zoom));
    gl.uniform2f(p.u.viewK, k1, k2);
    gl.uniform1f(p.u.grey, grey);
    gl.uniform1i(p.u.hasPano, this.hasPano && layout ? 1 : 0);
    if (layout) {
      gl.uniform4f(p.u.panoLayout, layout.yaw, layout.pitch, layout.px_per_degree, layout.wraps ? 1 : 0);
      gl.uniform2f(p.u.panoSize, layout.width, layout.height);
    }
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.panoTex);
    gl.uniform1i(p.u.pano, 0);
    gl.uniform1i(p.u.hasLive, live ? 1 : 0);
    if (live) this.liveUniforms(p, model, livePose, liveAspect);
    this.quad(p);
  }
  // The live texture (taken at `pose`) into the panorama texture, around where it lands.
  paint(model, layout, pose, liveAspect) {
    if (!this.hasPano) return;
    const gl = this.gl, p = this.paintProg;
    const yaw = model.yaw(pose.pan), pitch = model.pitch(pose.tilt);
    const reach = Math.atan(0.6 / model.focal(pose.zoom)) * 180 / Math.PI + 2;
    const per = layout.px_per_degree, sx = this.panoSize[0] / layout.width;
    let cx = (yaw - layout.yaw) * per;
    if (layout.wraps) cx = ((cx % layout.width) + layout.width) % layout.width;
    const xr = reach / Math.max(0.15, Math.cos(pitch * Math.PI / 180)) * per;
    const y0 = Math.max(0, (layout.pitch - pitch - reach) * per), y1 = Math.min(layout.height, (layout.pitch - pitch + reach) * per);
    if (y0 >= y1) return;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.panoTex, 0);
    gl.useProgram(p);
    gl.uniform4f(p.u.panoLayout, layout.yaw, layout.pitch, per, layout.wraps ? 1 : 0);
    this.liveUniforms(p, model, pose, liveAspect);
    gl.enable(gl.BLEND);
    // The colour feathered in; the panorama stays opaque (a see-through border saved as JPEG comes back dark).
    gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ZERO, gl.ONE);
    // Around a wrapping panorama the frame may run past either end: drawn at both.
    const spans = [[cx - xr, cx + xr]];
    if (layout.wraps) spans.push([cx - xr - layout.width, cx + xr - layout.width], [cx - xr + layout.width, cx + xr + layout.width]);
    for (const [a, b] of spans) {
      const x0 = Math.max(0, Math.floor(a * sx) / sx), x1 = Math.min(layout.width, Math.ceil(b * sx) / sx);
      if (x0 >= x1) continue;
      const vx = Math.round(x0 * sx), vy = Math.floor(y0 * sx), vw = Math.round((x1 - x0) * sx), vh = Math.ceil((y1 - y0) * sx);
      // The pixels the viewport covers, exactly (in the layout's units).
      gl.uniform4f(p.u.rect, vx / sx + (layout.wraps && a < 0 ? 0 : 0), vy / sx, (vx + vw) / sx, (vy + vh) / sx);
      gl.viewport(vx, vy, vw, vh);
      this.quad(p);
    }
    gl.disable(gl.BLEND);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }
  // The panorama texture's pixels, as a canvas (to keep it).
  readPanorama() {
    const gl = this.gl, [w, h] = this.panoSize;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.panoTex, 0);
    const px = new Uint8ClampedArray(w * h * 4);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
    for (let i = 3; i < px.length; i += 4) px[i] = 255;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    canvas.getContext("2d").putImageData(new ImageData(px, w, h), 0, 0);
    return canvas;
  }
}

// ─────────────────────────── keeping things in this browser ───────────────────────────

const kept = {
  db: null,
  async open() {
    if (this.db) return this.db;
    this.db = await new Promise((ok, fail) => {
      const r = indexedDB.open("ptz-360", 1);
      r.onupgradeneeded = () => r.result.createObjectStore("kv");
      r.onsuccess = () => ok(r.result);
      r.onerror = () => fail(r.error);
    });
    return this.db;
  },
  async get(key) {
    try {
      const db = await this.open();
      return await new Promise((ok) => { const r = db.transaction("kv").objectStore("kv").get(key); r.onsuccess = () => ok(r.result); r.onerror = () => ok(undefined); });
    } catch { return undefined; }
  },
  async set(key, value) {
    try {
      const db = await this.open();
      db.transaction("kv", "readwrite").objectStore("kv").put(value, key);
    } catch { /* not kept: fine */ }
  },
  async delete(key) {
    try {
      const db = await this.open();
      db.transaction("kv", "readwrite").objectStore("kv").delete(key);
    } catch { /* fine */ }
  },
};

// ─────────────────────────── the card ───────────────────────────

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// [yaw, pitch] medians of [yaw, pitch] samples.
const median = (samples) => {
  if (!samples.length) return [0, 0];
  const mid = (v) => { const s = [...v].sort((a, b) => a - b), n = s.length; return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2; };
  return [mid(samples.map((x) => x[0])), mid(samples.map((x) => x[1]))];
};
const STYLE = `
  ha-card { overflow: hidden; }
  .stage { position: relative; width: 100%; aspect-ratio: 16 / 9; background: #111; touch-action: none; user-select: none; cursor: grab; }
  .stage.dragging { cursor: grabbing; }
  canvas, video.plain, img.plain { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }
  [hidden] { display: none !important; }
  img.hidden { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
  video.hidden { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
  .buttons { position: absolute; right: 10px; top: 10px; display: flex; flex-direction: column; gap: 8px; }
  button { width: 34px; height: 34px; border-radius: 50%; border: none; background: rgba(0,0,0,.55); color: #fff; font-size: 17px; cursor: pointer; display: grid; place-items: center; }
  button:active { background: rgba(0,0,0,.8); }
  .badge { position: absolute; left: 10px; top: 10px; padding: 3px 9px; border-radius: 12px; background: rgba(0,0,0,.55); color: #fff; font: 600 11px system-ui, sans-serif; letter-spacing: .04em; }
  .notice { position: absolute; inset: auto 10px 10px 10px; padding: 8px 12px; border-radius: 10px; background: rgba(0,0,0,.65); color: #fff; font: 13px system-ui, sans-serif; display: flex; gap: 10px; align-items: center; }
  .menu { position: absolute; right: 52px; top: 136px; background: rgba(0,0,0,.75); border-radius: 10px; padding: 4px; }
  .menu { display: flex; flex-direction: column; }
  .menu button { width: auto; height: auto; padding: 8px 12px; border-radius: 8px; font: 13px system-ui, sans-serif; background: none; }
  .menu button:hover { background: rgba(255,255,255,.12); }
  .menu .about { padding: 6px 12px 8px; color: rgba(255,255,255,.6); font: 12px system-ui, sans-serif; }
  .notice button { width: auto; height: auto; padding: 6px 12px; border-radius: 8px; font-size: 13px; background: var(--primary-color, #03a9f4); }
`;

class Ptz360Card extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) throw new Error("Set `entity` to a PTZ 360 camera");
    this.config = config;
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.states).find((id) => id.startsWith("camera.") && hass.states[id].attributes.ptz_360);
    return { entity: entity || "camera.ptz_360" };
  }

  getCardSize() { return 6; }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && this.isConnected) this.start();
  }

  connectedCallback() { if (this._hass && !this.started) this.start(); }

  disconnectedCallback() { this.stop(); }

  // ── setting up ──

  async start() {
    this.started = true;
    this.alive = true;
    const root = this.shadowRoot || this.attachShadow({ mode: "open" });
    root.innerHTML = `<style>${STYLE}</style><ha-card><div class="stage">
      <canvas></canvas><video class="hidden" muted playsinline autoplay></video>
      <div class="badge">LIVE</div>
      <div class="buttons"><button class="home" title="Home">⌂</button><button class="in" title="Zoom in">+</button><button class="out" title="Zoom out">−</button><button class="more" title="More" hidden>⋯</button></div>
      <div class="menu" hidden><button class="sethome">Set this view as home</button><button class="recalibrate">Recalibrate…</button><div class="about"></div></div>
      <div class="notice" hidden></div></div></ha-card>`;
    this.stage = root.querySelector(".stage");
    this.canvas = root.querySelector("canvas");
    this.video = root.querySelector("video");
    // The picture drawn and matched: the WebRTC video, or HA's MJPEG (an <img>) when WebRTC can't reach the camera.
    this.source = this.video;
    this.badge = root.querySelector(".badge");
    this.notice = root.querySelector(".notice");
    root.querySelector(".home").onclick = () => this.goHome();
    root.querySelector(".in").onclick = () => this.zoomBy(1.6);
    root.querySelector(".out").onclick = () => this.zoomBy(1 / 1.6);
    // Admins: recalibrate (the camera moved, or the scene changed a lot).
    const more = root.querySelector(".more"), menu = root.querySelector(".menu");
    more.hidden = !this._hass.user?.is_admin;
    more.onclick = () => {
      menu.hidden = !menu.hidden;
      // What a bug report needs: where the picture comes from.
      const video = !this.sourceReady() ? "connecting" : this.img ? "MJPEG" : "WebRTC";
      menu.querySelector(".about").textContent = `Video: ${video}${this.videoEntity !== this.entity ? ` from ${this.videoEntity}` : ""}`;
    };
    root.querySelector(".sethome").onclick = async () => {
      menu.hidden = true;
      // Where the camera is now (its reported pose): home from now on, for everyone.
      try { this.info.home = await this._hass.callWS({ type: "ptz_360/set_home", entity_id: this.entity }); this.say("Home set to this view."); setTimeout(() => this.say(""), 2500); }
      catch (err) { this.say(`Set home: ${err.message || err}`); }
    };
    root.querySelector(".recalibrate").onclick = () => {
      menu.hidden = true;
      if (confirm("Recalibrate? The camera moves around its whole range for several minutes, then comes back.")) this.calibrate();
    };
    this.entity = this.config.entity;
    for (let attempt = 0; ; attempt++) {
      try {
        this.info = await this._hass.callWS({ type: "ptz_360/info", entity_id: this.entity });
        this.say("");
        break;
      } catch (err) {
        // Home Assistant starting, the camera not set up yet: again in a while.
        this.say(`${this.entity}: ${err.message || err}`);
        if (!this.alive || attempt > 60) return;
        await sleep(5000);
        if (!this.alive) return;
      }
    }
    // The picture from another camera entity of the same camera (Frigate's, say: its WebRTC through its go2rtc): the
    // card's own `video_entity`, else the integration's option, else the integration's camera.
    this.videoEntity = this.config.video_entity || this.info.video_entity || this.entity;
    this.startVideo();
    if (!this.info.calibration || !this.info.panorama) {
      this.plainVideo();
      this.offerCalibration();
      return;
    }
    this.setUp();
    // A calibration started elsewhere (the device's Calibrate button, an automation): shown here as it goes.
    this.followProgress();
  }

  stop() {
    this.alive = false;
    this.started = false;
    this.pc?.close();
    this.unsubscribe?.then((u) => u()).catch(() => {});
    this.progressSub?.then((u) => u()).catch(() => {});
    this.workerHandle?.terminate();
    cancelAnimationFrame(this.raf);
  }

  say(text, action) {
    this.notice.hidden = !text;
    this.notice.textContent = text || "";
    if (action) {
      const b = document.createElement("button");
      b.textContent = action.label;
      b.onclick = action.run;
      this.notice.append(b);
    }
  }

  // Without a calibration: the plain video, and (for admins) the calibration pass.
  plainVideo() {
    this.plain = true;
    this.source.className = "plain";
    this.canvas.hidden = true;
    this.stage.style.cursor = "default";
  }

  offerCalibration() {
    const admin = this._hass.user?.is_admin;
    this.say(admin ? "Not calibrated yet. The camera will move around its whole range for a few minutes, then come back."
      : "This camera hasn't been calibrated yet (an admin can do it here).",
    admin ? { label: "Calibrate", run: () => this.calibrate() } : null);
    this.followProgress();
  }

  async calibrate() {
    try {
      await this._hass.callWS({ type: "ptz_360/calibrate", entity_id: this.entity });
    } catch (err) {
      this.say(`Calibration: ${err.message || err}`);
    }
  }

  followProgress() {
    if (this.progressSub) return;
    const names = { starting: "Starting", measuring: "Measuring the zooms", "model frames": "Fitting the lens", fitting: "Fitting the lens",
      sweeping: "Sweeping the range", stitching: "Stitching the panorama" };
    this.progressSub = this._hass.connection.subscribeMessage((p) => {
      if (p.stage === "done") { if (this.calibrating) { this.stop(); this.start(); } return; }
      this.calibrating = true;
      if (p.stage === "error") { this.say(`Calibration failed: ${p.error}`, { label: "Again", run: () => this.calibrate() }); return; }
      this.say(`${names[p.stage] || p.stage}${p.total ? ` · ${p.done + 1} / ${p.total}` : ""}…`);
    }, { type: "ptz_360/subscribe_progress", entity_id: this.entity });
  }

  async setUp() {
    const info = this.info;
    this.model = new Model(info.calibration.model);
    this.layout = info.panorama;
    this.ranges = info.ranges;
    this.wraps = Math.abs(360 / Math.abs(this.model.ap) - (this.ranges.pan[1] - this.ranges.pan[0])) < 0.02;
    try {
      this.view = new View(this.canvas);
    } catch (err) {
      this.plainVideo();
      this.say(`No 360° view here: ${err.message}`);
      return;
    }
    // The matching, beside the page.
    const url = URL.createObjectURL(new Blob([`(${worker.toString()})()`], { type: "text/javascript" }));
    this.workerHandle = new Worker(url);
    this.pending = new Map();
    this.seq = 0;
    this.workerHandle.onmessage = (e) => { const r = this.pending.get(e.data.id); this.pending.delete(e.data.id); r?.(e.data.result); };
    this.ask("model", { model: info.calibration.model });
    this.offsetKey = `ptz-360-offset-${this.entity}`;
    this.offset = { pitch: {}, yaw: {} };
    try { Object.assign(this.offset, JSON.parse(localStorage.getItem(this.offsetKey)) || {}); } catch { /* none yet */ }
    const s = info.status;
    const start = this.picture({ pan: s.pan, tilt: s.tilt, zoom: s.zoom }, 1);
    Object.assign(this, { target: start, camera: start, livePose: start, sent: start, history: [[Date.now(), start]], move: 0,
      following: false, tracking: false, polling: false, sending: false, lastChange: 0, confirmed: false, frameNumber: 0 });
    this.grabCanvas = Object.assign(document.createElement("canvas"), { width: 640, height: 360 });
    this.grayCanvas = Object.assign(document.createElement("canvas"), { width: GW, height: GH });
    this.trackFrames();
    this.gestures();
    this.loadPanorama();
    const draw = () => {
      if (!this.alive) return;
      this.raf = requestAnimationFrame(draw);
      try { this.drawOnce(); } catch (err) { console.warn("ptz-360-card", err); }
    };
    this.drawOnce = () => {
      this.resize();
      // While still, the live video itself, where its picture was last found.
      const live = !this.following && this.sourceReady();
      if (live) this.view.setLive(this.source);
      const showLive = live || (this.following && this.liveFrame);
      this.view.draw(this.model, this.target, this.layout, showLive, this.following ? this.paintedPose : this.livePose, this.videoAspect(), 0.7);
      this.badge.textContent = this.following ? "FOLLOWING" : live ? "LIVE" : "CONNECTING";
    };
    draw();
    this.poll();
    // While the camera rests where the panorama confirmed, its live picture into the panorama now and then.
    this.repaintTimer = setInterval(() => {
      if (this.alive && this.confirmed && !this.following && !this.tracking && this.sourceReady()) this.repaint(this.livePose, true);
    }, 1500);
  }

  resize() {
    const r = this.canvas.getBoundingClientRect(), dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) { this.canvas.width = w; this.canvas.height = h; }
  }

  sourceSize() {
    const s = this.source;
    return s instanceof HTMLImageElement ? [s.naturalWidth, s.naturalHeight] : [s.videoWidth, s.videoHeight];
  }
  sourceReady() {
    const s = this.source;
    return s instanceof HTMLImageElement ? s.complete && s.naturalWidth > 0 : s.readyState >= 2;
  }
  videoAspect() { const [w, h] = this.sourceSize(); return w ? h / w : 9 / 16; }

  ask(kind, data) {
    const id = ++this.seq;
    return new Promise((ok) => { this.pending.set(id, ok); this.workerHandle.postMessage({ kind, id, ...data }); });
  }

  async loadPanorama() {
    const L = this.layout;
    // v2: repaints saved before the border was kept opaque have dark outlines baked in; they're dropped.
    this.panoKey = `ptz-360-repaint-v2-${this.info.entry_id}-${L.id}`;
    kept.delete(`ptz-360-repaint-${this.info.entry_id}-${L.id}`);
    try {
      const r = await fetch(`/api/ptz_360/${this.info.entry_id}/panorama/${L.id}.jpg`, { headers: { Authorization: `Bearer ${this._hass.auth.data.access_token}` } });
      const original = await createImageBitmap(await r.blob());
      // Within what the GPU takes.
      const scale = Math.min(1, this.view.maxTexture / original.width);
      const fit = (img) => {
        if (scale >= 1) return img;
        const c = Object.assign(document.createElement("canvas"), { width: Math.floor(img.width * scale), height: Math.floor(img.height * scale) });
        c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
        return c;
      };
      // Repainted here before: that, for drawing.
      const kept_ = await kept.get(this.panoKey);
      const shown = kept_ ? await createImageBitmap(kept_) : original;
      this.view.setPanorama(fit(shown));
      // Grey, half size, for finding frames in (always the integration's panorama: a misplaced frame mustn't become the reference).
      const gs = Math.min(1, 2200 / original.width), gw = Math.floor(original.width * gs), gh = Math.floor(original.height * gs);
      const c = Object.assign(document.createElement("canvas"), { width: gw, height: gh });
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(original, 0, 0, gw, gh);
      const px = ctx.getImageData(0, 0, gw, gh).data, gray = new Float32Array(gw * gh);
      for (let i = 0; i < gray.length; i++) gray[i] = 0.299 * px[4 * i] + 0.587 * px[4 * i + 1] + 0.114 * px[4 * i + 2];
      this.ask("pano", { pano: { gray, w: gw, h: gh, scale: gw / L.width, layout: L } });
    } catch (err) {
      this.say(`Panorama: ${err.message || err}`);
    }
  }

  // ── video (WebRTC through Home Assistant) ──

  async startVideo() {
    const hass = this._hass;
    try {
      const config = await hass.callWS({ type: "camera/webrtc/get_client_config", entity_id: this.videoEntity });
      const pc = new RTCPeerConnection(config.configuration);
      this.pc = pc;
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.onconnectionstatechange = () => { if (pc.connectionState === "failed") this.mjpeg(); };
      pc.ontrack = (e) => { this.video.srcObject = e.streams[0] || new MediaStream([e.track]); this.video.play().catch(() => {}); };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      let session = null;
      const waiting = [];
      const send = (c) => hass.callWS({ type: "camera/webrtc/candidate", entity_id: this.videoEntity, session_id: session, candidate: c }).catch(() => {});
      pc.onicecandidate = (e) => {
        if (!e.candidate) return;
        const c = { candidate: e.candidate.candidate, sdpMid: e.candidate.sdpMid, sdpMLineIndex: e.candidate.sdpMLineIndex };
        if (session) send(c); else waiting.push(c);
      };
      this.unsubscribe = hass.connection.subscribeMessage((m) => {
        if (m.type === "session") { session = m.session_id; waiting.splice(0).forEach(send); }
        else if (m.type === "answer") pc.setRemoteDescription({ type: "answer", sdp: m.answer });
        else if (m.type === "candidate") pc.addIceCandidate(m.candidate).catch(() => {});
        else if (m.type === "error") this.mjpeg();
      }, { type: "camera/webrtc/offer", entity_id: this.videoEntity, offer: offer.sdp });
      // No picture in a while: the camera's MJPEG through HA instead.
      setTimeout(() => { if (this.alive && !this.sourceReady()) this.mjpeg(); }, 12000);
    } catch {
      this.mjpeg();
    }
  }

  // HA's MJPEG proxy as a fallback (an <img>, drawn and matched like the video).
  mjpeg() {
    if (this.img || !this.alive) return;
    this.pc?.close();
    const token = this._hass.states[this.videoEntity]?.attributes.access_token;
    const img = document.createElement("img");
    img.className = this.plain ? "plain" : "hidden";
    img.src = `/api/camera_proxy_stream/${this.videoEntity}?token=${token}`;
    this.video.replaceWith(img);
    this.img = img;
    this.source = img;
    this.trackFrames();
  }

  // A number per new decoded frame.
  trackFrames() {
    const v = this.source;
    if (v instanceof HTMLImageElement) {
      // An MJPEG <img> says nothing when a frame arrives: a few of its pixels sampled, a change is a new frame.
      const probe = Object.assign(document.createElement("canvas"), { width: 16, height: 9 }).getContext("2d", { willReadFrequently: true });
      let last = "";
      const tick = () => {
        if (!this.alive || this.source !== v) return;
        if (this.sourceReady()) {
          probe.drawImage(v, 0, 0, 16, 9);
          const sig = probe.getImageData(0, 0, 16, 9).data.join(",");
          if (sig !== last) { last = sig; this.frameNumber++; }
        }
        setTimeout(tick, 20);
      };
      tick();
      return;
    }
    if (v.requestVideoFrameCallback) {
      const next = (_, meta) => { this.frameNumber = meta.presentedFrames; if (this.alive) v.requestVideoFrameCallback(next); };
      v.requestVideoFrameCallback(next);
    } else {
      let last = -1;
      const tick = () => { if (!this.alive) return; if (v.currentTime !== last) { last = v.currentTime; this.frameNumber++; } setTimeout(tick, 20); };
      tick();
    }
  }

  // The frame on show now: in colour (for drawing) and grey 160×90 (for matching).
  grab() {
    const g = this.grabCanvas.getContext("2d");
    g.drawImage(this.source, 0, 0, this.grabCanvas.width, this.grabCanvas.height);
    const ctx = this.grayCanvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(this.grabCanvas, 0, 0, GW, GH);
    const px = ctx.getImageData(0, 0, GW, GH).data, gray = new Float32Array(GW * GH);
    for (let i = 0; i < gray.length; i++) gray[i] = 0.299 * px[4 * i] + 0.587 * px[4 * i + 1] + 0.114 * px[4 * i + 2];
    return { gray, number: this.frameNumber };
  }

  // ── gestures ──

  gestures() {
    const pointers = new Map();
    let last = null, pinch = null;
    const stage = this.stage;
    stage.addEventListener("pointerdown", (e) => {
      if (e.target.closest("button")) return;
      stage.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      stage.classList.add("dragging");
      last = [e.clientX, e.clientY];
      if (pointers.size === 2) { const [a, b] = [...pointers.values()]; pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), base: this.target }; }
    });
    stage.addEventListener("pointermove", (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      const r = stage.getBoundingClientRect();
      if (pointers.size >= 2 && pinch) {
        const [a, b] = [...pointers.values()];
        this.zoomBy(Math.hypot(a[0] - b[0], a[1] - b[1]) / pinch.d, pinch.base);
        return;
      }
      // The picture follows the finger: the view turns the other way.
      this.turn(-(e.clientX - last[0]) / r.width, -(e.clientY - last[1]) / r.height);
      last = [e.clientX, e.clientY];
    });
    const up = (e) => { pointers.delete(e.pointerId); if (pointers.size < 2) pinch = null; if (!pointers.size) stage.classList.remove("dragging"); else last = [...pointers.values()][0]; };
    stage.addEventListener("pointerup", up);
    stage.addEventListener("pointercancel", up);
    stage.addEventListener("wheel", (e) => { e.preventDefault(); this.zoomBy(Math.exp(-e.deltaY * 0.002)); }, { passive: false });
  }

  // ── the drive ──

  clampPan(pan) { return this.wraps ? pan : Math.min(this.ranges.pan[1], Math.max(this.ranges.pan[0], pan)); }

  turn(x, y) {
    const m = this.model, t = { ...this.target }, pitch = m.pitch(t.tilt) * Math.PI / 180;
    t.pan = this.clampPan(m.pan(m.yaw(t.pan) + x * m.viewHfov(t.zoom) / Math.max(0.2, Math.cos(pitch))));
    t.tilt = Math.min(this.ranges.tilt[1], Math.max(this.ranges.tilt[0], m.tilt(m.pitch(t.tilt) - y * m.viewVfov(t.zoom))));
    this.set(t);
  }

  zoomBy(factor, base) {
    const m = this.model;
    if (!m) return;
    const start = base || this.target;
    const zoom = Math.min(this.ranges.zoom[1], Math.max(this.ranges.zoom[0], m.zoomForFocal(m.focal(start.zoom) * Math.max(0.05, factor))));
    this.set({ ...this.target, zoom });
  }

  set(pose, step = false) {
    if (!this.model) return;
    if (pose.pan === this.target.pan && pose.tilt === this.target.tilt && pose.zoom === this.target.zoom) return;
    // A drag or a zoom while the view animates home: theirs from now on.
    if (!step) { this.animating = null; this.animatingTo = null; }
    this.target = pose;
    this.lastChange = Date.now();
    this.follow();
    this.send();
    this.poll();
  }

  // The camera's picture of a reported pose, and where to send it for its picture to be at a pose: the offset learnt
  // between the two (per turning way: the gears' play).
  shifted(pose, sign, way) {
    const m = this.model, o = this.offset;
    return { pan: m.pan(m.yaw(pose.pan) + sign * (o.yaw[way] || 0)), tilt: m.tilt(m.pitch(pose.tilt) + sign * (o.pitch[way] || 0)), zoom: pose.zoom };
  }
  picture(pose, way) { return this.shifted(pose, 1, way ?? (this.target.pan >= this.livePose.pan ? 1 : -1)); }
  reported(pose) { return this.shifted(pose, -1, pose.pan >= this.camera.pan ? 1 : -1); }

  turnSize() { return 360 / Math.abs(this.model.ap); }
  unwrap(pan, near) { const t = this.turnSize(); return pan + t * Math.round((near - pan) / t); }

  // The latest target to the camera, one goto at a time.
  async send() {
    const goal = this.animatingTo || this.target;
    if (this.sending || (this.sent && goal.pan === this.sent.pan && goal.tilt === this.sent.tilt && goal.zoom === this.sent.zoom)) return;
    this.sending = true;
    this.sent = goal;
    const pose = this.reported(goal);
    const [lo, hi] = this.ranges.pan, turn = hi - lo;
    if (this.wraps) pose.pan -= turn * Math.floor((pose.pan - lo) / turn);
    try { await this._hass.callWS({ type: "ptz_360/goto", entity_id: this.entity, ...pose }); } catch { /* the next one */ }
    this.sending = false;
    if (this.alive) this.send();
  }

  // The camera's position several times a second until it has settled where it was sent.
  async poll() {
    if (this.polling) return;
    this.polling = true;
    let still = 0, last = null;
    while (this.alive && still < 3) {
      try {
        const s = await this._hass.callWS({ type: "ptz_360/status", entity_id: this.entity });
        const near = this.wraps ? { ...s, pan: this.unwrap(s.pan, this.reported(this.target).pan) } : s;
        const pose = this.picture({ pan: near.pan, tilt: near.tilt, zoom: near.zoom });
        // Some cameras keep saying "moving" once still: an unchanged position counts as stopped.
        const unchanged = last && s.pan === last.pan && s.tilt === last.tilt && s.zoom === last.zoom;
        last = s;
        this.camera = pose;
        this.history.push([Date.now(), pose]);
        this.history = this.history.filter(([t]) => Date.now() - t < 12000);
        const arrived = (s.moving === false || unchanged) && !this.sending && Date.now() - this.lastChange > 1000;
        still = arrived ? still + 1 : 0;
      } catch { /* try again */ }
      await sleep(150);
    }
    // Stopped well short of the view (at its limits): the view goes where the camera is.
    const m = this.model;
    if (this.alive && m && !this.animatingTo) {
      const short = Math.abs(m.yaw(this.camera.pan) - m.yaw(this.target.pan)) > 0.25 * m.viewHfov(this.target.zoom)
        || Math.abs(m.pitch(this.camera.tilt) - m.pitch(this.target.tilt)) > 0.25 * m.viewVfov(this.target.zoom)
        || Math.abs(Math.log(m.focal(this.camera.zoom) / m.focal(this.target.zoom))) > 0.05;
      if (short) { this.target = this.camera; this.sent = this.camera; }
    }
    this.polling = false;
  }

  // Follows a move frame by frame, until the camera has settled and the picture with it.
  async follow() {
    if (this.tracking || !this.model) return;
    this.tracking = true;
    this.confirmed = false;
    const move = ++this.move, startPose = this.livePose, m = this.model;
    try {
      let frame = this.grab(), seen = frame.number;
      let anchor = { straight: await this.ask("straighten", { gray: frame.gray, zoom: startPose.zoom }), pose: startPose };
      let placed = { scale: 1, dx: 0, dy: 0 }, last = null, matched = 0, failures = 0, stillFrames = 0, unconfirmed = false;
      let lastFrame = Date.now();
      this.paintFrame(startPose);
      this.following = true;
      for (;;) {
        await sleep(25);
        if (!this.alive) return;
        const now = Date.now();
        if (this.frameNumber === seen) {
          // The video stalls now and then: wait for the next frame (found again after the gap); gone 8 s, it's down.
          if (!this.polling && now - lastFrame > 8000) break;
          continue;
        }
        const gap = now - lastFrame;
        lastFrame = now;
        frame = this.grab();
        seen = frame.number;
        const recent = this.history.filter(([t]) => now - t < 1500).map(([, p]) => p.zoom);
        const zooming = recent.length > 1 && Math.max(...recent) - Math.min(...recent) > 0.002;
        const r = await this.ask("locate", { gray: frame.gray, anchor: anchor.straight, anchorPose: anchor.pose, prior: placed, zooming });
        // Right after a stall the camera may have turned far: a weak match then is more likely wrong than right.
        const measured = r.found && !(gap > 300 && r.found.score < 0.5) ? r.found : null;
        let pose;
        if (measured) {
          let raw = { ...r.pose, pan: this.unwrap(r.pose.pan, anchor.pose.pan) };
          placed = { scale: measured.scale, dx: measured.dx, dy: measured.dy };
          const far = measured.scale > 1.4 || measured.scale < 0.7 || Math.abs(measured.dx) > GW * 0.25 || Math.abs(measured.dy) > GH * 0.25;
          matched++;
          let locked = false;
          // Every few frames (and before a new anchor, and until a lost pose is confirmed): where the panorama says it is.
          if (matched % 4 === 0 || far || unconfirmed) {
            const lock = await this.ask("panorama", { straight: r.straight, pose: raw });
            if (lock) {
              const found = { ...lock.pose, pan: this.unwrap(lock.pose.pan, raw.pan) }, gain = unconfirmed ? 1 : zooming ? 0.15 : 0.3;
              raw = { pan: raw.pan + (found.pan - raw.pan) * gain, tilt: raw.tilt + (found.tilt - raw.tilt) * gain,
                zoom: m.zoomForFocal(m.focal(raw.zoom) * Math.pow(m.focal(found.zoom) / m.focal(raw.zoom), gain)) };
              anchor = { straight: r.straight, pose: raw };
              placed = { scale: 1, dx: 0, dy: 0 };
              locked = true;
              unconfirmed = false;
            }
          }
          if (far && !locked) { anchor = { straight: r.straight, pose: raw }; placed = { scale: 1, dx: 0, dy: 0 }; }
          pose = last ? { ...raw, pan: this.unwrap(raw.pan, last.pan) } : raw;
          const moving = !last || m.degreesBetween(pose, last) > 0.003 * m.viewHfov(pose.zoom) || Math.abs(Math.log(m.focal(pose.zoom) / m.focal(last.zoom))) > 0.003;
          stillFrames = moving ? 0 : stillFrames + 1;
          last = pose;
          failures = 0;
        } else {
          // No match: not drawn at a guess. The first miss after a stall, or the fourth in a row: found again in the
          // panorama around where the camera says it is; failing that, after four, placed there, unconfirmed.
          failures++;
          let found = null;
          if (failures >= 4 || (failures === 1 && gap > 300)) found = await this.ask("relocalize", { gray: frame.gray, candidates: [this.camera, last || this.camera] });
          if (found) { pose = { ...found.pose, pan: this.unwrap(found.pose.pan, this.camera.pan) }; unconfirmed = false; anchor = { straight: found.straight, pose }; }
          else if (failures < 4) continue;
          else { pose = this.camera; unconfirmed = true; anchor = { straight: await this.ask("straighten", { gray: frame.gray, zoom: pose.zoom }), pose }; }
          placed = { scale: 1, dx: 0, dy: 0 };
          last = null;
          stillFrames = 0;
          failures = 0;
        }
        this.paintFrame(pose);
        if (!this.polling && now - this.lastChange > 500 && stillFrames >= 3) break;
        if (!this.polling && now - this.lastChange > 10000) break;
      }
    } finally {
      this.tracking = false;
      this.following = false;
    }
    // Settled: the live video takes over where the last frame was; the view glides onto it.
    const settled = this.paintedPose || startPose;
    this.livePose = settled;
    this.glide(settled);
    this.check(settled, this.target.pan >= startPose.pan ? 1 : -1, move);
  }

  // The frame just grabbed, drawn at `pose` (the canvas is the live texture while following).
  paintFrame(pose) {
    // No picture yet (the stream still starting): nothing to draw, the view moves over the panorama alone.
    this.liveFrame = this.sourceReady();
    if (this.liveFrame) this.view.setLive(this.grabCanvas);
    this.paintedPose = pose;
  }

  // Beside the live view, once settled: the still picture found once more in the panorama. How far that is from the
  // camera's report teaches the offset; confirmed, the place may be repainted; off, the view and the live picture move
  // together (the picture stays put on screen).
  async check(settled, way, move) {
    // A frame from after the camera stopped (the one on show may be from halfway, the video stalling). None within 2 s:
    // nothing to check.
    const stopped = this.frameNumber;
    for (let waited = 0; this.frameNumber === stopped && waited < 2000; waited += 50) await sleep(50);
    if (!this.alive || this.tracking || move !== this.move || this.frameNumber === stopped) return;
    const frame = this.grab();
    const found = await this.ask("relocalize", { gray: frame.gray, candidates: [settled, this.camera] });
    if (!found || this.tracking || move !== this.move) return;
    const m = this.model, pose = { ...found.pose, pan: this.unwrap(found.pose.pan, settled.pan) };
    const dp = m.pitch(pose.tilt) - m.pitch(this.camera.tilt), dy = m.yaw(this.unwrap(pose.pan, this.camera.pan)) - m.yaw(this.camera.pan);
    // One wrong match must not move the offset: only a confident one counts (a correlation of 0.7, well past the 0.55
    // a match needs), the offset is the median of the last seven, and once there are three, one more than 1.5° from
    // their median is set aside. (`camera` has the offset learnt so far in it: what was measured, whole.)
    const o = this.offset;
    o.samples = o.samples || {};
    const kept = o.samples[way] || [];
    const sample = [(o.yaw[way] || 0) + dy, (o.pitch[way] || 0) + dp], med = median(kept);
    // Never more than 3° either (a picture from before the camera stopped, a report not caught up): not an offset.
    if (found.score >= 0.7 && Math.abs(sample[0]) < 3 && Math.abs(sample[1]) < 3
        && (kept.length < 3 || (Math.abs(sample[0] - med[0]) < 1.5 && Math.abs(sample[1] - med[1]) < 1.5))) {
      kept.push(sample);
      o.samples[way] = kept.slice(-7);
      const now = median(o.samples[way]);
      o.yaw[way] = now[0];
      o.pitch[way] = now[1];
      // A first answer for one way stands for the other too (most of the offset is the same).
      if (!o.samples[-way]) { o.yaw[-way] = now[0]; o.pitch[-way] = now[1]; }
      try { localStorage.setItem(this.offsetKey, JSON.stringify(o)); } catch { /* not kept */ }
    }
    this.confirmed = true;
    if (m.degreesBetween(pose, settled) > 0.1) {
      this.target = { pan: this.target.pan + pose.pan - settled.pan, tilt: this.target.tilt + pose.tilt - settled.tilt,
        zoom: m.zoomForFocal(m.focal(this.target.zoom) * m.focal(pose.zoom) / m.focal(settled.zoom)) };
      this.sent = this.target;
      this.livePose = pose;
    }
    this.repaint(this.livePose, false);
  }

  // The live picture at `pose` into the panorama (shown at once; kept in this browser now and then).
  repaint(pose, fromVideo) {
    this.view.setLive(fromVideo ? this.source : this.grabCanvas);
    this.view.paint(this.model, this.layout, pose, this.videoAspect());
    clearTimeout(this.keepTimer);
    this.keepTimer = setTimeout(() => {
      this.view.readPanorama().toBlob((b) => b && kept.set(this.panoKey, b), "image/jpeg", 0.9);
    }, 5000);
  }

  // The view onto where the live picture is, over a quarter of a second (only the view: the camera isn't sent).
  glide(pose) {
    const m = this.model, start = this.target, began = Date.now();
    const end = this.wraps ? { ...pose, pan: this.unwrap(pose.pan, start.pan) } : pose;
    let expected = start;
    const step = () => {
      if (!this.alive || this.following || this.target !== expected) return;
      const t = Math.min(1, (Date.now() - began) / 250), e = t * t * (3 - 2 * t);
      expected = { pan: start.pan + (end.pan - start.pan) * e, tilt: start.tilt + (end.tilt - start.tilt) * e,
        zoom: m.zoomForFocal(m.focal(start.zoom) * Math.pow(m.focal(end.zoom) / m.focal(start.zoom), e)) };
      this.target = expected;
      this.sent = expected;
      if (t < 1) requestAnimationFrame(step);
    };
    step();
  }

  // Home: the view eases there (longer the further it goes, at most a second); the camera is sent there once.
  async goHome() {
    if (!this.model) return;
    let home = this.info.home;
    if (!home) {
      try { home = await this._hass.callWS({ type: "ptz_360/home", entity_id: this.entity }); this.info.home = home; } catch { return; }
    }
    const m = this.model, start = this.target, goal = this.picture(home, home.pan >= start.pan ? 1 : -1);
    const end = this.wraps ? { ...goal, pan: this.unwrap(goal.pan, start.pan) } : goal;
    const span = Math.max(m.degreesBetween(start, end) / m.viewHfov(start.zoom), Math.abs(Math.log(m.focal(end.zoom) / m.focal(start.zoom))) / Math.log(2));
    const duration = Math.min(1000, Math.max(350, 350 + span * 200)), began = Date.now();
    const token = {};
    this.animating = token;
    this.animatingTo = end;
    let expected = this.target;
    const step = () => {
      if (!this.alive || this.animating !== token || this.target !== expected) return;
      const t = Math.min(1, (Date.now() - began) / duration), e = t * t * (3 - 2 * t);
      this.set({ pan: start.pan + (end.pan - start.pan) * e, tilt: start.tilt + (end.tilt - start.tilt) * e,
        zoom: m.zoomForFocal(m.focal(start.zoom) * Math.pow(m.focal(end.zoom) / m.focal(start.zoom), e)) }, true);
      expected = this.target;
      if (t < 1) requestAnimationFrame(step); else { this.animating = null; this.animatingTo = null; }
    };
    step();
  }
}

customElements.define("ptz-360-card", Ptz360Card);
window.customCards = window.customCards || [];
window.customCards.push({ type: "ptz-360-card", name: "PTZ 360", description: "An ONVIF PTZ camera as a 360° view: drag to look around." });
