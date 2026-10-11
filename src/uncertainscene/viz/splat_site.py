"""Browser viewer for real 3D Gaussian splats: one HTML page with an inline WebGL2 renderer
(projected 2D covariances, Gaussian falloff, back-to-front sort in a Web Worker), switching
and flicker between splats, orbit/pan/zoom, opacity filter and size slider.

    python -m uncertainscene.viz.splat_site out_dir before=a.splat after=b.splat [--title T] [--notes N]

Input is the antimatter15 `.splat` format (32 bytes per Gaussian: xyz f32, linear scale f32,
rgba u8, quaternion u8 as q*128+128 in w,x,y,z order), world frame OpenCV/VGGT (y down).
Each splat keeps its `max_gaussians` most visible Gaussians (opacity x geometric-mean scale)
and is written in a compact quantised layout (17 bytes per Gaussian), by default as base64
inside `splats/<name>.js` (loaded with a <script> tag, safe on any static host), or as
`splats/<name>.bin` fetched by the page (`fmt="bin"`).
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
from pathlib import Path

import numpy as np

ROW = 32


def read_splat(path: Path) -> dict:
    """Parse a .splat file into arrays: pos (N,3), scale (N,3), rgba (N,4) u8, quat (N,4) u8."""
    d = np.fromfile(path, dtype=np.uint8)
    d = d[: len(d) // ROW * ROW].reshape(-1, ROW)
    return {"pos": d[:, 0:12].copy().view(np.float32), "scale": d[:, 12:24].copy().view(np.float32),
            "rgba": d[:, 24:28].copy(), "quat": d[:, 28:32].copy()}


def keep_visible(s: dict, max_gaussians: int) -> dict:
    """The `max_gaussians` Gaussians with the largest opacity x geometric-mean scale."""
    n = len(s["pos"])
    if n <= max_gaussians:
        return s
    vis = s["rgba"][:, 3] / 255.0 * np.exp(np.log(np.maximum(s["scale"], 1e-12)).mean(1))
    idx = np.sort(np.argpartition(-vis, max_gaussians)[:max_gaussians])
    return {k: v[idx] for k, v in s.items()}


def encode(s: dict) -> tuple[bytes, dict]:
    """Quantised structure-of-arrays: pos u16 x3 over the bounding box, log-scale u8 x3,
    rgba u8 x4, quat u8 x4. Returns (bytes, metadata needed to decode)."""
    pos, ls = s["pos"].astype(np.float64), np.log(np.maximum(s["scale"], 1e-12)).astype(np.float64)
    lo, hi = pos.min(0), pos.max(0)
    hi = np.where(hi > lo, hi, lo + 1e-6)
    slo, shi = float(ls.min()), float(max(ls.max(), ls.min() + 1e-6))
    q = np.round((pos - lo) / (hi - lo) * 65535).astype("<u2")
    qs = np.round((ls - slo) / (shi - slo) * 255).astype(np.uint8)
    buf = q.tobytes() + qs.tobytes() + s["rgba"].tobytes() + s["quat"].tobytes()
    return buf, {"n": len(pos), "lo": lo.tolist(), "hi": hi.tolist(), "slo": slo, "shi": shi}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", name.lower()).strip("_") or "splat"


def build(splats: dict[str, Path], out_dir: Path, title: str = "Scene01 Splat Viewer", cameras: dict | None = None,
          max_gaussians: int = 300_000, notes: str = "", fmt: str = "js") -> Path:
    """Write `out_dir/splat_viewer.html` and `out_dir/splats/<name>.{js,bin}`; return the HTML path.

    cameras: optional {name: {"c2w": 4x4 camera-to-world (OpenCV), "fov_y": degrees}}; the first one
    is the start view. Default start view: camera at the origin looking along +z, y down, fov_y 55.
    """
    out_dir = Path(out_dir)
    (out_dir / "splats").mkdir(parents=True, exist_ok=True)
    manifest, centre = [], None
    for name, path in splats.items():
        raw = read_splat(Path(path))
        s = keep_visible(raw, max_gaussians)
        if centre is None:
            centre = np.median(s["pos"], 0).tolist()
        buf, meta = encode(s)
        slug = _slug(name)
        if fmt == "bin":
            rel = f"splats/{slug}.bin"
            (out_dir / rel).write_bytes(buf)
        else:
            rel = f"splats/{slug}.js"
            b64 = base64.b64encode(buf).decode()
            (out_dir / rel).write_text(f"(window.SPLAT_DATA=window.SPLAT_DATA||{{}})[{json.dumps(slug)}]=\"{b64}\";\n")
        manifest.append({"name": name, "key": slug, "file": rel, "kind": fmt, "total": len(raw["pos"]), **meta})
    cams = cameras or {"Recording camera": {"c2w": np.eye(4).tolist(), "fov_y": 55.0}}
    cams = [{"name": k, "c2w": np.asarray(v["c2w"], float).tolist(), "fov_y": float(v.get("fov_y", 55.0))}
            for k, v in cams.items()]
    config = {"splats": manifest, "centre": centre or [0, 0, 1], "cameras": cams}
    page = (TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__NOTES__", html.escape(notes))
            .replace("__CONFIG__", json.dumps(config).replace("</", "<\\/")))
    out = out_dir / "splat_viewer.html"
    out.write_text(page)
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #f6f5f2; --surface: #ffffff; --text: #1d1d1f; --muted: #5f6368; --border: #d9d7d2;
  --accent: #2f6fdb; --accent-text: #ffffff; --canvas-bg: #e9e7e3;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #141416; --surface: #1e1e21; --text: #ececee; --muted: #a0a0a8; --border: #34343a;
    --accent: #6d9cf0; --accent-text: #0d1220; --canvas-bg: #0c0c0e;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #141416; --surface: #1e1e21; --text: #ececee; --muted: #a0a0a8; --border: #34343a;
  --accent: #6d9cf0; --accent-text: #0d1220; --canvas-bg: #0c0c0e;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { margin: 0; overflow-x: hidden; }
body { background: var(--bg); color: var(--text); font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.25rem; margin: 0 0 8px; font-weight: 600; }
.bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 8px 0; }
button { font: inherit; padding: 6px 12px; border-radius: 8px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); cursor: pointer; touch-action: manipulation; }
button[aria-pressed="true"] { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
label { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 0.9rem; }
input[type=range] { width: 120px; accent-color: var(--accent); }
.stage { position: relative; width: 100%; background: var(--canvas-bg); border: 1px solid var(--border);
  border-radius: 10px; overflow: hidden; }
canvas { display: block; width: 100%; height: min(72vh, 75vw); touch-action: none; cursor: grab; }
#overlay { position: absolute; left: 10px; top: 8px; padding: 2px 8px; border-radius: 6px; font-weight: 600;
  background: var(--surface); color: var(--text); opacity: 0.85; pointer-events: none; }
#status { position: absolute; right: 10px; bottom: 8px; font-size: 0.8rem; color: var(--muted); pointer-events: none; }
.caption { color: var(--muted); font-size: 0.9rem; margin-top: 8px; }
.caption p { margin: 4px 0; }
.help { font-size: 0.8rem; }
</style>
</head>
<body>
<main>
<h1>__TITLE__</h1>
<div class="bar" id="splatButtons"></div>
<div class="bar">
  <button id="flicker" aria-pressed="false">Flicker</button>
  <button id="reset">Reset view</button>
  <span id="camButtons" class="bar" style="margin:0"></span>
  <button id="theme" title="Toggle light/dark">Theme</button>
</div>
<div class="bar">
  <label>Opacity filter <input id="opacity" type="range" min="0" max="0.95" step="0.01" value="0"><span id="opacityVal">0.00</span></label>
  <label>Size <input id="size" type="range" min="0.5" max="2" step="0.05" value="1"><span id="sizeVal">1.00x</span></label>
</div>
<div class="stage">
  <canvas id="c"></canvas>
  <div id="overlay"></div>
  <div id="status">loading...</div>
</div>
<div class="caption">
  <p>__NOTES__</p>
  <p id="counts"></p>
  <p class="help">Drag to orbit, right-drag or shift-drag (two fingers on touch) to pan, wheel or pinch to zoom. Keys 1/2/... switch splats, F flickers, R resets.</p>
</div>
</main>
<script>
const CONFIG = __CONFIG__;
const canvas = document.getElementById("c");
const gl = canvas.getContext("webgl2", {antialias: false, premultipliedAlpha: true, alpha: false});
const statusEl = document.getElementById("status");
if (!gl) statusEl.textContent = "WebGL2 is not available in this browser.";

// ---------- small linear algebra (row-major 3x3 as arrays of 9) ----------
const mul3 = (a, b) => { const r = new Array(9); for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++)
  r[3*i+j] = a[3*i]*b[j] + a[3*i+1]*b[3+j] + a[3*i+2]*b[6+j]; return r; };
const mv3 = (a, v) => [a[0]*v[0]+a[1]*v[1]+a[2]*v[2], a[3]*v[0]+a[4]*v[1]+a[5]*v[2], a[6]*v[0]+a[7]*v[1]+a[8]*v[2]];
const axisAngle = (ax, t) => { const n = Math.hypot(...ax) || 1, x = ax[0]/n, y = ax[1]/n, z = ax[2]/n,
  c = Math.cos(t), s = Math.sin(t), C = 1 - c;
  return [c+x*x*C, x*y*C-z*s, x*z*C+y*s, y*x*C+z*s, c+y*y*C, y*z*C-x*s, z*x*C-y*s, z*y*C+x*s, c+z*z*C]; };

// ---------- decode splat data ----------
function b64bytes(s) { const bin = atob(s), n = bin.length, u = new Uint8Array(n);
  for (let i = 0; i < n; i++) u[i] = bin.charCodeAt(i); return u; }
function loadScript(src) { return new Promise((res, rej) => { const el = document.createElement("script");
  el.src = src; el.onload = res; el.onerror = () => rej(new Error("failed to load " + src)); document.head.appendChild(el); }); }
async function loadBytes(m) {
  if (m.kind === "bin") { const r = await fetch(m.file); if (!r.ok) throw new Error(m.file + ": " + r.status);
    return new Uint8Array(await r.arrayBuffer()); }
  await loadScript(m.file); const u = b64bytes(window.SPLAT_DATA[m.key]); delete window.SPLAT_DATA[m.key]; return u;
}
function decode(m, u) {
  const n = m.n, pos = new Float32Array(3*n), q16 = new Uint16Array(u.buffer.slice(0, 6*n));
  for (let i = 0; i < 3*n; i++) { const k = i % 3; pos[i] = m.lo[k] + q16[i] / 65535 * (m.hi[k] - m.lo[k]); }
  const so = 6*n, co = so + 3*n, qo = co + 4*n, W = 1024, H = Math.ceil(2*n / W);
  // texA: 2 RGBA32F texels per Gaussian [x y z c00] [c01 c02 c11 c12]; texB: 1 texel [rgb24 alpha c22 0]
  const texA = new Float32Array(W * H * 4), texB = new Float32Array(W * Math.ceil(n / W) * 4);
  const opac = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const s = [0, 1, 2].map(k => Math.exp(m.slo + u[so + 3*i + k] / 255 * (m.shi - m.slo)));
    let w = (u[qo+4*i] - 128) / 128, x = (u[qo+4*i+1] - 128) / 128, y = (u[qo+4*i+2] - 128) / 128, z = (u[qo+4*i+3] - 128) / 128;
    const qn = Math.hypot(w, x, y, z) || 1; w /= qn; x /= qn; y /= qn; z /= qn;
    const R = [1-2*(y*y+z*z), 2*(x*y-w*z), 2*(x*z+w*y), 2*(x*y+w*z), 1-2*(x*x+z*z), 2*(y*z-w*x),
               2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y)];
    const M = [R[0]*s[0], R[1]*s[1], R[2]*s[2], R[3]*s[0], R[4]*s[1], R[5]*s[2], R[6]*s[0], R[7]*s[1], R[8]*s[2]];
    const c00 = M[0]*M[0]+M[1]*M[1]+M[2]*M[2], c01 = M[0]*M[3]+M[1]*M[4]+M[2]*M[5], c02 = M[0]*M[6]+M[1]*M[7]+M[2]*M[8],
          c11 = M[3]*M[3]+M[4]*M[4]+M[5]*M[5], c12 = M[3]*M[6]+M[4]*M[7]+M[5]*M[8], c22 = M[6]*M[6]+M[7]*M[7]+M[8]*M[8];
    const a = 8*i; texA[a] = pos[3*i]; texA[a+1] = pos[3*i+1]; texA[a+2] = pos[3*i+2]; texA[a+3] = c00;
    texA[a+4] = c01; texA[a+5] = c02; texA[a+6] = c11; texA[a+7] = c12;
    const b = 4*i, col = co + 4*i;
    // pack rgb into one float losslessly (24 bits fit a float32 mantissa), alpha separately, cov22
    texB[b] = u[col] * 65536 + u[col+1] * 256 + u[col+2]; texB[b+1] = u[col+3] / 255; texB[b+2] = c22;
    opac[i] = u[col+3] / 255;
  }
  return {n, pos, opac, texA, texB, W, H, HB: Math.ceil(n / W)};
}

// ---------- GL setup ----------
const VS = `#version 300 es
precision highp float; precision highp int;
uniform highp sampler2D uA; uniform highp sampler2D uB;
uniform mat4 uView; uniform vec2 uFocal; uniform vec2 uViewport; uniform float uSize;
in vec2 aCorner; in uint aIndex;
out vec4 vColor; out vec2 vPos;
void main() {
  int i = int(aIndex);
  ivec2 ta = ivec2((2*i) & 1023, (2*i) >> 10);
  vec4 t0 = texelFetch(uA, ta, 0), t1 = texelFetch(uA, ta + ivec2(1, 0), 0);
  vec4 t2 = texelFetch(uB, ivec2(i & 1023, i >> 10), 0);
  vec4 cam = uView * vec4(t0.xyz, 1.0);
  gl_Position = vec4(0.0, 0.0, 2.0, 1.0);
  if (cam.z < 0.02) return;
  vec2 uv = uFocal * cam.xy / cam.z;                       // pixels from centre, y down
  if (any(greaterThan(abs(uv), 1.3 * uViewport * 0.5 + 64.0))) return;
  mat3 S = mat3(t0.w, t1.x, t1.y,  t1.x, t1.z, t1.w,  t1.y, t1.w, t2.z) * (uSize * uSize);
  vec3 r0 = vec3(uView[0][0], uView[1][0], uView[2][0]);
  vec3 r1 = vec3(uView[0][1], uView[1][1], uView[2][1]);
  vec3 r2 = vec3(uView[0][2], uView[1][2], uView[2][2]);
  float iz = 1.0 / cam.z;
  vec3 m0 = uFocal.x * iz * (r0 - cam.x * iz * r2);
  vec3 m1 = uFocal.y * iz * (r1 - cam.y * iz * r2);
  float a = dot(m0, S * m0) + 0.3, b = dot(m0, S * m1), c = dot(m1, S * m1) + 0.3;
  float mid = 0.5 * (a + c), rad = length(vec2(0.5 * (a - c), b));
  float l1 = mid + rad, l2 = mid - rad;
  if (l2 <= 0.0) return;
  vec2 d = (abs(b) < 1e-12) ? (a >= c ? vec2(1.0, 0.0) : vec2(0.0, 1.0)) : normalize(vec2(b, l1 - a));
  vec2 major = min(sqrt(2.0 * l1), 1024.0) * d;
  vec2 minor = min(sqrt(2.0 * l2), 1024.0) * vec2(-d.y, d.x);
  vec2 off = aCorner.x * major + aCorner.y * minor;        // pixels, y down
  vec2 p = uv + off;
  gl_Position = vec4(2.0 * p.x / uViewport.x, -2.0 * p.y / uViewport.y, 0.0, 1.0);
  float rgb = t2.x;
  vColor = vec4(floor(rgb / 65536.0), mod(floor(rgb / 256.0), 256.0), mod(rgb, 256.0), 255.0 * t2.y) / 255.0;
  vPos = aCorner;
}`;
const FS = `#version 300 es
precision highp float;
in vec4 vColor; in vec2 vPos; out vec4 frag;
void main() {
  float A = -dot(vPos, vPos);
  if (A < -4.0) discard;
  float B = exp(A) * vColor.a;
  frag = vec4(B * vColor.rgb, B);
}`;
function shader(type, src) { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; }
let prog, loc = {}, idxBuf, vao;
if (gl) {
  prog = gl.createProgram(); gl.attachShader(prog, shader(gl.VERTEX_SHADER, VS)); gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, FS));
  gl.linkProgram(prog); if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
  gl.useProgram(prog);
  for (const k of ["uA", "uB", "uView", "uFocal", "uViewport", "uSize"]) loc[k] = gl.getUniformLocation(prog, k);
  vao = gl.createVertexArray(); gl.bindVertexArray(vao);
  const qb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, qb);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-2, -2, 2, -2, 2, 2, -2, 2]), gl.STATIC_DRAW);
  const ac = gl.getAttribLocation(prog, "aCorner"); gl.enableVertexAttribArray(ac); gl.vertexAttribPointer(ac, 2, gl.FLOAT, false, 0, 0);
  idxBuf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, idxBuf);
  const ai = gl.getAttribLocation(prog, "aIndex"); gl.enableVertexAttribArray(ai);
  gl.vertexAttribIPointer(ai, 1, gl.UNSIGNED_INT, 0, 0); gl.vertexAttribDivisor(ai, 1);
  gl.uniform1i(loc.uA, 0); gl.uniform1i(loc.uB, 1);
  gl.disable(gl.DEPTH_TEST); gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
}
function makeTex(data, w, h) { const t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, w, h, 0, gl.RGBA, gl.FLOAT, data);
  for (const p of [gl.TEXTURE_MIN_FILTER, gl.TEXTURE_MAG_FILTER]) gl.texParameteri(gl.TEXTURE_2D, p, gl.NEAREST);
  for (const p of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T]) gl.texParameteri(gl.TEXTURE_2D, p, gl.CLAMP_TO_EDGE);
  return t; }

// ---------- sort worker (16-bit counting sort, far to near) ----------
const WORKER = `
const sets = {};
onmessage = (e) => {
  const m = e.data;
  if (m.type === "add") { sets[m.key] = {pos: m.pos, opac: m.opac}; return; }
  const s = sets[m.key]; if (!s) return;
  const {pos, opac} = s, n = opac.length, v = m.view, thr = m.thr;
  const depth = new Float32Array(n), keep = new Uint32Array(n); let k = 0, lo = Infinity, hi = -Infinity;
  for (let i = 0; i < n; i++) {
    if (opac[i] < thr) continue;
    const z = v[2]*pos[3*i] + v[6]*pos[3*i+1] + v[10]*pos[3*i+2] + v[14];
    if (z < 0.02) continue;
    depth[k] = z; keep[k++] = i; if (z < lo) lo = z; if (z > hi) hi = z;
  }
  const B = 65536, counts = new Uint32Array(B), sc = (B - 1) / Math.max(hi - lo, 1e-9), bin = new Uint32Array(k);
  for (let j = 0; j < k; j++) { const b = B - 1 - ((depth[j] - lo) * sc | 0); bin[j] = b; counts[b]++; }
  for (let b = 1; b < B; b++) counts[b] += counts[b - 1];
  const out = new Uint32Array(k);
  for (let j = k - 1; j >= 0; j--) out[--counts[bin[j]]] = keep[j];
  postMessage({key: m.key, idx: out, id: m.id}, [out.buffer]);
};`;
const worker = new Worker(URL.createObjectURL(new Blob([WORKER], {type: "text/javascript"})));

// ---------- state ----------
const sets = {}; let active = null, flickerTimer = null, sortBusy = false, sortDirty = true, sortId = 0;
let drawCount = 0, drawnKey = null;
const centre = CONFIG.centre.slice();
let cam = {}, pivot = centre.slice(), fovY = 55;
function setCamera(c) {   // camera-to-world: Rc columns = camera axes, C = position
  const M = c.c2w; cam.R = [M[0][0], M[0][1], M[0][2], M[1][0], M[1][1], M[1][2], M[2][0], M[2][1], M[2][2]];
  cam.C = [M[0][3], M[1][3], M[2][3]]; fovY = c.fov_y; pivot = centre.slice(); sortDirty = true; requestDraw();
}
function viewMatrix() {   // world-to-camera as column-major mat4
  const R = cam.R, C = cam.C, t = [0, 1, 2].map(j => -(R[j]*C[0] + R[3+j]*C[1] + R[6+j]*C[2]));
  return new Float32Array([R[0], R[1], R[2], 0, R[3], R[4], R[5], 0, R[6], R[7], R[8], 0, t[0], t[1], t[2], 1]);
}
const ui = {opacity: document.getElementById("opacity"), size: document.getElementById("size")};

worker.onmessage = (e) => {
  const m = e.data; sortBusy = false;
  if (sets[m.key] && m.key === active) {
    gl.bindBuffer(gl.ARRAY_BUFFER, idxBuf); gl.bufferData(gl.ARRAY_BUFFER, m.idx, gl.DYNAMIC_DRAW);
    drawCount = m.idx.length; drawnKey = m.key; requestDraw(true);
  }
  if (sortDirty) requestSort();
};
function requestSort() {
  if (!active || !sets[active]) return;
  if (sortBusy) { sortDirty = true; return; }
  sortBusy = true; sortDirty = false;
  worker.postMessage({type: "sort", key: active, view: viewMatrix(), thr: +ui.opacity.value, id: ++sortId});
}

let raf = 0;
function requestDraw(noSort) { if (!noSort) sortDirty = true; if (!raf) raf = requestAnimationFrame(draw); }
function resize() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2), w = Math.round(canvas.clientWidth * dpr), h = Math.round(canvas.clientHeight * dpr);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
}
function draw() {
  raf = 0; if (!gl) return; resize();
  if (sortDirty) requestSort();
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--canvas-bg").trim();
  const rgb = bg.length === 7 ? [1, 3, 5].map(i => parseInt(bg.slice(i, i + 2), 16) / 255) : [0, 0, 0];
  gl.viewport(0, 0, canvas.width, canvas.height); gl.clearColor(rgb[0], rgb[1], rgb[2], 1); gl.clear(gl.COLOR_BUFFER_BIT);
  const s = sets[drawnKey]; if (!s || !drawCount) return;
  const f = 0.5 * canvas.height / Math.tan(fovY * Math.PI / 360);
  gl.useProgram(prog); gl.bindVertexArray(vao);
  gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, s.tA);
  gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, s.tB);
  gl.uniformMatrix4fv(loc.uView, false, viewMatrix()); gl.uniform2f(loc.uFocal, f, f);
  gl.uniform2f(loc.uViewport, canvas.width, canvas.height); gl.uniform1f(loc.uSize, +ui.size.value);
  gl.drawArraysInstanced(gl.TRIANGLE_FAN, 0, 4, drawCount);
}

// ---------- UI ----------
const btns = {};
const overlay = document.getElementById("overlay");
function setActive(key) {
  if (!sets[key]) return; active = key;
  for (const k in btns) btns[k].setAttribute("aria-pressed", k === key ? "true" : "false");
  overlay.textContent = CONFIG.splats.find(m => m.key === key).name;
  sortDirty = true; requestSort();
}
CONFIG.splats.forEach((m, i) => {
  const b = document.createElement("button"); b.textContent = `${m.name} (${i + 1})`; b.disabled = true;
  b.onclick = () => { stopFlicker(); setActive(m.key); }; btns[m.key] = b;
  document.getElementById("splatButtons").appendChild(b);
});
CONFIG.cameras.forEach((c, i) => { if (CONFIG.cameras.length < 2) return; const b = document.createElement("button");
  b.textContent = c.name; b.onclick = () => setCamera(c); document.getElementById("camButtons").appendChild(b); });
const flickBtn = document.getElementById("flicker");
function stopFlicker() { if (flickerTimer) clearInterval(flickerTimer); flickerTimer = null; flickBtn.setAttribute("aria-pressed", "false"); }
function toggleFlicker() {
  if (flickerTimer) return stopFlicker();
  const keys = CONFIG.splats.map(m => m.key).filter(k => sets[k]); if (keys.length < 2) return;
  flickBtn.setAttribute("aria-pressed", "true");
  flickerTimer = setInterval(() => setActive(keys[(keys.indexOf(active) + 1) % keys.length]), 700);
}
flickBtn.onclick = toggleFlicker;
document.getElementById("reset").onclick = () => setCamera(CONFIG.cameras[0]);
document.getElementById("theme").onclick = () => { const r = document.documentElement;
  const dark = r.dataset.theme ? r.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  r.dataset.theme = dark ? "light" : "dark"; requestDraw(true); };
ui.opacity.oninput = () => { document.getElementById("opacityVal").textContent = (+ui.opacity.value).toFixed(2); sortDirty = true; requestSort(); };
ui.size.oninput = () => { document.getElementById("sizeVal").textContent = (+ui.size.value).toFixed(2) + "x"; requestDraw(true); };
window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  const i = parseInt(e.key, 10) - 1;
  if (i >= 0 && i < CONFIG.splats.length) { stopFlicker(); setActive(CONFIG.splats[i].key); }
  else if (e.key === "f" || e.key === "F") toggleFlicker();
  else if (e.key === "r" || e.key === "R") setCamera(CONFIG.cameras[0]);
});
window.addEventListener("resize", () => requestDraw(true));
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => requestDraw(true));

// ---------- camera controls ----------
function orbit(dx, dy) {
  const k = 0.005, yaw = axisAngle([0, -1, 0], -dx * k), right = [cam.R[0], cam.R[3], cam.R[6]];
  const Q = mul3(axisAngle(right, -dy * k), yaw);
  const d = [0, 1, 2].map(j => cam.C[j] - pivot[j]), nd = mv3(Q, d);
  cam.C = [0, 1, 2].map(j => pivot[j] + nd[j]); cam.R = mul3(Q, cam.R); requestDraw();
}
function pan(dx, dy) {
  const dist = Math.hypot(...[0, 1, 2].map(j => cam.C[j] - pivot[j]));
  const s = dist * 2 * Math.tan(fovY * Math.PI / 360) / canvas.clientHeight;
  for (let j = 0; j < 3; j++) { const t = -(cam.R[3*j] * dx + cam.R[3*j+1] * dy) * s; cam.C[j] += t; pivot[j] += t; }
  requestDraw();
}
function zoom(f) { for (let j = 0; j < 3; j++) cam.C[j] = pivot[j] + (cam.C[j] - pivot[j]) * f; requestDraw(); }
const ptrs = new Map(); let mode = null, last = null;
canvas.addEventListener("contextmenu", e => e.preventDefault());
canvas.addEventListener("pointerdown", (e) => {
  canvas.setPointerCapture(e.pointerId); ptrs.set(e.pointerId, [e.clientX, e.clientY]);
  mode = ptrs.size >= 2 ? "two" : (e.button === 2 || e.shiftKey ? "pan" : "orbit"); last = null;
});
canvas.addEventListener("pointermove", (e) => {
  if (!ptrs.has(e.pointerId)) return; ptrs.set(e.pointerId, [e.clientX, e.clientY]);
  const pts = [...ptrs.values()];
  if (mode === "two" && pts.length >= 2) {
    const c = [(pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2], d = Math.hypot(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1]);
    if (last) { pan(c[0] - last.c[0], c[1] - last.c[1]); if (d > 0 && last.d > 0) zoom(last.d / d); }
    last = {c, d};
  } else if (mode !== "two") {
    const p = pts[0]; if (last) (mode === "pan" ? pan : orbit)(p[0] - last.c[0], p[1] - last.c[1]); last = {c: p};
  }
});
const up = (e) => { ptrs.delete(e.pointerId); last = null; if (ptrs.size === 0) mode = null; else if (mode === "two") mode = "pan"; };
canvas.addEventListener("pointerup", up); canvas.addEventListener("pointercancel", up);
canvas.addEventListener("wheel", (e) => { e.preventDefault(); zoom(Math.exp(e.deltaY * 0.0012)); }, {passive: false});

// ---------- load ----------
setCamera(CONFIG.cameras[0]);
(async () => {
  if (!gl) return;
  const counts = [];
  for (const m of CONFIG.splats) {
    statusEl.textContent = `loading ${m.name}...`;
    try {
      const d = decode(m, await loadBytes(m));
      d.tA = makeTex(d.texA, d.W, d.H); d.tB = makeTex(d.texB, d.W, d.HB); delete d.texA; delete d.texB;
      worker.postMessage({type: "add", key: m.key, pos: d.pos, opac: d.opac});
      sets[m.key] = d; btns[m.key].disabled = false; if (!active) setActive(m.key);
      counts.push(`${m.name}: ${m.n.toLocaleString()} of ${m.total.toLocaleString()} Gaussians shown`);
      document.getElementById("counts").textContent = counts.join(" · ");
    } catch (err) { console.error(err); counts.push(`${m.name}: failed to load (${err.message})`); }
  }
  statusEl.textContent = "";
  window.SPLATS_READY = true;
})();
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("splats", nargs="+", help="name=path.splat, in display order")
    ap.add_argument("--title", default="Scene01 Splat Viewer")
    ap.add_argument("--notes", default="")
    ap.add_argument("--max-gaussians", type=int, default=300_000)
    ap.add_argument("--fmt", choices=["js", "bin"], default="js")
    a = ap.parse_args(argv)
    splats = dict(s.split("=", 1) for s in a.splats)
    out = build({k: Path(v) for k, v in splats.items()}, a.out_dir, a.title, None, a.max_gaussians, a.notes, a.fmt)
    print(out)
    for f in sorted((a.out_dir / "splats").iterdir()):
        print(f"  {f.relative_to(a.out_dir)}  {f.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
