"""One self-contained results page for a run: 3D scene (colour, changes, every heat map,
object boxes and labels), heat-map figures, object sheets and the per-pair findings, with a
pair picker. Open the HTML file in any browser; nothing else is needed.

    python -m uncertainscene.viz.site results/change/<run_id> out.html [--steps p1_ p2_]

3D data per step comes from `scene3d.json` (version 2 steps) or, for older steps, from the
Plotly figure inside their `viewer.html`.
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys
from pathlib import Path

import numpy as np

from .scene3d import Points, _b64, _hex_rgb, _scale_rgb

IMAGES = [("splat_compare.png", "Gaussian splats: before and after rendered from the same camera"),
          ("position_map.png", "Where each object is: 90% ranges, top-down"),
          ("position_closeups.png", "Close-ups of each object's range as clips are added"),
          ("position_precision.png", "How the range shrinks with more views and clips"),
          ("objects.png", "Every object"), ("object_overlays.png", "Object boundaries in the frames"),
          ("heatmaps.png", "Heat maps"), ("base_placement.png", "Where a robot could stand"),
          ("overlays.png", "Changes in the frames"), ("crops.png", "Changed objects")]


def _decode(v):
    """Plotly array: list, or {'dtype', 'bdata'} (base64 typed array)."""
    if isinstance(v, dict) and "bdata" in v:
        return np.frombuffer(base64.b64decode(v["bdata"]), dtype=np.dtype(v["dtype"])).tolist()
    return v


def _layer_meta(name: str) -> tuple[str | None, str]:
    rec = "A" if ("A before" in name or name.startswith("A ")) else "B" if ("B after" in name or name.startswith("B ")) else None
    kind = "colour" if "(colour)" in name else "heat" if name.startswith("heat map") else "change"
    return rec, kind


def from_viewer_html(path: Path, max_points: int = 16000, max_heat: int = 8000, fields_on: str = "B") -> dict:
    s = path.read_text()
    i = s.index("Plotly.newPlot(")
    j = s.index("[", i)
    data, _ = json.JSONDecoder().raw_decode(s[j:])
    pts = Points()
    layers, objects = [], []
    for t in data:
        name = t.get("name", "")
        if t.get("type") != "scatter3d":
            continue
        x, y, z = (np.asarray(_decode(t.get(k, [])), dtype=object) for k in "xyz")
        mk = t.get("marker", {})
        col = _decode(mk.get("color"))
        if "text" in t.get("mode", "") and t.get("text"):
            texts = t["text"] if isinstance(t["text"], list) else [t["text"]] * len(x)
            cols = col if isinstance(col, list) else [col] * len(x)
            hov = t.get("hovertext") or [""] * len(x)
            grp = name.replace("objects: ", "")
            for k in range(len(x)):
                lab = str(texts[k])
                rec = lab[0] if lab[:1] in ("A", "B") and lab[1:2].isdigit() else None
                objects.append({"center": [round(float(x[k]), 4), round(float(y[k]), 4), round(float(z[k]), 4)],
                                "label": lab, "color": cols[k] if isinstance(cols[k], str) else "#444",
                                "hover": hov[k] if isinstance(hov, list) else hov, "group": grp, "rec": rec})
            continue
        if t.get("mode") == "lines":
            P = np.array([[np.nan if v is None else v for v in (a, b, c)] for a, b, c in zip(x, y, z)], float)
            if np.isfinite(P).all(1).sum() >= 2:
                layers.append({"name": name, "kind": "line", "rec": None, "visible": True, "pts": pts.add(P),
                               "color": (t.get("line") or {}).get("color", "#ff7f0e")})
            continue
        if name.startswith("unknown") or not len(x):
            continue
        X = np.stack([x.astype(float), y.astype(float), z.astype(float)], 1)
        n = len(X)
        rec, kind = _layer_meta(name)
        cap = max_heat if kind == "heat" else max_points
        rng = np.random.default_rng(n)      # same n -> same picks, so heat maps share coordinates
        sel = np.sort(rng.choice(n, cap, replace=False)) if n > cap else np.arange(n)
        nice = (name.replace("heat map: ", "").replace("A before", "before").replace("B after", "after")
                .replace("appearance (", "looks different ("))
        layer = {"name": nice, "kind": kind, "rec": rec if kind != "heat" else fields_on,
                 "size": float(mk.get("size", 2)), "visible": t.get("visible", True) is True, "pts": pts.add(X[sel])}
        if isinstance(col, list) and len(col) == n and mk.get("colorscale"):
            vals = np.asarray(col, float)
            lo, hi = mk.get("cmin"), mk.get("cmax")
            rgb, stops = _scale_rgb(vals[sel], mk["colorscale"], lo, hi)
            lo = float(np.nanmin(vals)) if lo is None else lo
            hi = float(np.nanmax(vals)) if hi is None else hi
            layer.update(rgb=_b64(rgb), legend={"lo": round(float(lo), 3), "hi": round(float(hi), 3), "stops": stops})
        elif isinstance(col, list) and len(col) == n:
            layer["rgb"] = _b64(_hex_rgb([col[k] for k in sel], len(sel)))
        else:
            layer["color"] = col if isinstance(col, str) else "#888888"
        layers.append(layer)
    return {"points": pts.tables, "layers": layers, "objects": objects}


def _img(path: Path, max_w: int = 1000, quality: int = 55) -> str:
    from PIL import Image

    im = Image.open(path).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _md_to_html(md: str) -> str:
    """Tiny Markdown subset (headings, tables, bullets, bold) for the per-pair notes."""
    out, table = [], []

    def inline(t):
        t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        return re.sub(r"`(.+?)`", r"<code>\1</code>", t)

    def flush():
        if table:
            rows = [r for r in table if not re.match(r"^\|[\s\-|]+\|$", r)]
            html = "<table>" + "".join(
                "<tr>" + "".join(f"<{'th' if i == 0 else 'td'}>{inline(c.strip())}</{'th' if i == 0 else 'td'}>"
                                 for c in r.strip("|").split("|")) + "</tr>" for i, r in enumerate(rows)) + "</table>"
            out.append(html)
            table.clear()
    for line in md.splitlines():
        if line.startswith("|"):
            table.append(line)
            continue
        flush()
        if line.startswith("!["):
            continue
        if line.startswith("#"):
            lvl = min(len(line) - len(line.lstrip("#")) + 1, 4)
            out.append(f"<h{lvl}>{inline(line.lstrip('#').strip())}</h{lvl}>")
        elif line.startswith("- "):
            out.append(f"<li>{inline(line[2:])}</li>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    flush()
    return "\n".join(out)


def load_step(d: Path, with_3d: bool = True) -> dict:
    from ..experiments.catalog import describe, rec_name

    m = json.loads((d / "metrics.json").read_text()) if (d / "metrics.json").exists() else {}
    info = describe(d.name, m)
    recs = m.get("clips") or [m.get("a"), m.get("b")]
    st = {"name": d.name, "title": info["title"], "question": info["question"], "phase": info["phase"],
          "phase_title": info["phase_title"], "type": m.get("pair_type"),
          "recordings": [rec_name(r) for r in recs if r], "variant": m.get("variant"),
          "metrics": {k: m.get(k) for k in ("changed_frac", "obj_changed", "obj_compared", "obj_total",
                                             "field_scale_m_per_unit", "field_uncertainty_cm_median",
                                             "field_reachable_frac", "field_interactable_frac", "n_per_video",
                                             "frames_total", "n_changes", "pos_radius90_cm_median",
                                             "pos_nis_median", "matched_objects")},
          "images": [], "scene": None}
    for f, cap in IMAGES:
        if (d / f).exists():
            st["images"].append({"caption": cap, "src": _img(d / f)})
    rep = d / "report.md"
    st["notes"] = _md_to_html(rep.read_text()) if rep.exists() else ""
    if with_3d:
        if (d / "scene3d.json").exists():
            from .scene3d import load as load_scene

            st["scene"] = load_scene(d)
        elif (d / "viewer.html").exists():
            try:
                st["scene"] = from_viewer_html(d / "viewer.html", fields_on=m.get("fields_on", "B"))
            except Exception as e:  # noqa: BLE001 - a broken viewer must not stop the page
                st["scene_error"] = str(e)[:200]
        if st["scene"]:
            lab = {"A": f"before ({rec_name(m['a'])})" if m.get("a") else "before",
                   "B": f"after ({rec_name(m['b'])})" if m.get("b") else "after"}
            if m.get("clips"):
                lab = {str(i + 1): f"clip {i + 1} ({rec_name(c)})" for i, c in enumerate(m["clips"])}
            st["scene"]["recs"] = lab
    return st


FEATURED = ("p1_", "p1b_", "1-main_", "1b-splat_", "2-position_")


def decimate(scene: dict, keep: float) -> dict:
    """Keep about `keep` of every point table used by point layers (line tables untouched), with
    the colours of every layer that uses the table thinned the same way."""
    if keep >= 1 or not scene:
        return scene
    line_tabs = {l["pts"] for l in scene["layers"] if l.get("kind") == "line"} | \
                {o["ring"] for o in scene.get("objects", []) if o.get("ring") is not None}
    sel = {}
    tables = []
    for t, tab in enumerate(scene["points"]):
        if t in line_tabs or tab["n"] < 200:
            tables.append(tab)
            continue
        q = np.frombuffer(base64.b64decode(tab["q"]), np.int16).reshape(-1, 3)
        idx = np.arange(0, len(q), max(1, int(round(1 / keep))))
        sel[t] = idx
        tables.append({**tab, "n": int(len(idx)), "q": _b64(q[idx])})
    layers = []
    for l in scene["layers"]:
        l = dict(l)
        if l["pts"] in sel and l.get("rgb"):
            l["rgb"] = _b64(np.frombuffer(base64.b64decode(l["rgb"]), np.uint8).reshape(-1, 3)[sel[l["pts"]]])
        layers.append(l)
    return {**scene, "points": tables, "layers": layers}


def build(run_dirs: list[Path], out: Path, prefixes: tuple = FEATURED, title: str = "Scene01 Change Viewer",
          summary_md: str = "", max_mb: float = 15.5, standalone: bool = True, sidecar: bool = False,
          scene_mb: float = 15.0) -> Path:
    """One page for the given runs (newest first). Steps are grouped by phase in the picker,
    each with its plain title; the 3D view, figures and findings are embedded.

    sidecar=True writes each step's 3D scene at full density to scenes/<i>.js next to the page,
    loaded only when that step is opened (for the published artifact, which can carry extra
    files); otherwise every scene is embedded and thinned to fit max_mb."""
    steps = []
    for rd in run_dirs:
        for d in sorted(Path(rd).iterdir()):
            if d.is_dir() and d.name.startswith(prefixes) and (d / "metrics.json").exists():
                st = load_step(d)
                st["run"] = Path(rd).name
                steps.append(st)
    if sidecar:
        sdir = out.parent / "scenes"
        sdir.mkdir(parents=True, exist_ok=True)
        for old in sdir.glob("*.js"):
            old.unlink()
        for i, st in enumerate(steps):
            if st["scene"]:
                sc, keep = st["scene"], 1.0
                while True:
                    txt = json.dumps(decimate(sc, keep), separators=(",", ":"))
                    if len(txt) < scene_mb * 1024**2 or keep < 0.1:
                        break
                    keep *= 0.75
                (sdir / f"{i}.js").write_text(f"window.__S({i},{txt});")
                st["scene"], st["scene_src"] = None, f"scenes/{i}.js"
    keep = 1.0
    while True:
        st2 = [{**st, "scene": decimate(st["scene"], keep)} for st in steps]
        payload = json.dumps({"steps": st2, "summary": _md_to_html(summary_md)}, separators=(",", ":"))
        if len(payload) < (max_mb - 0.1) * 1024**2 or keep < 0.08:
            break
        keep *= 0.7
        if keep < 0.35:                 # past this, drop figures before thinning the 3D further
            for st in steps:
                st["images"] = st["images"][:2]
    html = TEMPLATE.replace("__TITLE__", title).replace("__DATA__", payload.replace("</", "<\\/"))
    if standalone:      # a file opened directly needs its own document head (the artifact host adds one)
        html = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
                'content="width=device-width,initial-scale=1"></head><body>' + html + "</body></html>")
    out.write_text(html)
    mb = len(html.encode()) / 1024**2
    if mb > max_mb:
        print(f"warning: {out} is {mb:.1f} MB (limit {max_mb})")
    return out


TEMPLATE = r"""<title>__TITLE__</title>
<style>
/* Layout: header with the run picker and plain-language question, view switch, then a
   full-width 3D canvas with layer chips; figures and findings in tabs. */
:root{--bg:#f6f7f5;--panel:#ffffff;--text:#1b1d1c;--muted:#5f6662;--line:#dfe3df;--accent:#1f6f5c;--chip:#ecefec;--warn:#a65d00;
 --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141716;--panel:#1c201f;--text:#e8ebe9;--muted:#a1aaa5;--line:#2f3532;--accent:#5fc3a6;--chip:#262b29;--warn:#e7a54b;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#141716;--panel:#1c201f;--text:#e8ebe9;--muted:#a1aaa5;--line:#2f3532;--accent:#5fc3a6;--chip:#262b29;--warn:#e7a54b;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1400px;margin:0 auto;padding-inline:16px;padding-block:12px 32px}
header{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;padding-block:6px 10px;border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0;font-weight:650;letter-spacing:.01em}
#heatpick.on{border-color:var(--accent);outline:1px solid var(--accent)}
select{font:inherit;padding:7px 9px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--text);max-width:100%;min-width:0;flex:1 1 380px}
.q{margin:12px 0 4px;display:grid;gap:4px}
.q .phase{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.q h2{font-size:20px;margin:0;text-wrap:balance;font-weight:650}
.q p{margin:0;color:var(--muted);max-width:75ch}
.recs{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.recs span{font-size:13px;background:var(--chip);border-radius:6px;padding:2px 8px}
.tabs,.views{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.tabs button,.views button{font:inherit;font-size:14px;border:1px solid var(--line);background:var(--chip);color:var(--text);padding:6px 12px;border-radius:999px;cursor:pointer}
.tabs button.on,.views button.on{background:var(--accent);border-color:var(--accent);color:var(--panel)}
button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.meta{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}.meta span{background:var(--chip);border-radius:999px;padding:2px 10px;font-size:13px;font-variant-numeric:tabular-nums}
.layers{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.layers label{display:inline-flex;gap:6px;align-items:center;background:var(--chip);border-radius:8px;padding:3px 9px;font-size:13px;cursor:pointer}
.layers .grp{font-size:12px;color:var(--muted);width:100%;margin-top:4px}
#stage{position:relative;width:100%;height:68vh;min-height:380px;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;touch-action:none}
#stage canvas{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab}
#legend{position:absolute;right:10px;bottom:10px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:12px;max-width:60%}
#legend .bar{height:10px;width:180px;max-width:100%;border-radius:3px;margin:3px 0}
#legend .ends{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums;color:var(--muted)}
#empty{position:absolute;inset:0;display:grid;place-items:center;color:var(--muted);padding:16px;text-align:center}
.hint{color:var(--muted);font-size:13px;margin:6px 0}
.ctrl{display:flex;flex-wrap:wrap;gap:12px;align-items:center;font-size:13px;color:var(--muted);margin:6px 0}
figure{margin:0 0 18px}figure img{width:100%;height:auto;border-radius:8px;border:1px solid var(--line);background:#fff}
figcaption{color:var(--muted);font-size:13px;margin:4px 0}
.notes{max-width:110ch;min-width:0}.notes table{border-collapse:collapse;font-size:13px;display:block;overflow-x:auto;margin:8px 0}
.notes td,.notes th{border:1px solid var(--line);padding:4px 7px;text-align:left;vertical-align:top}
.notes code{font-family:var(--mono);background:var(--chip);padding:0 4px;border-radius:4px;font-size:.9em}
[hidden]{display:none!important}
</style>
<div class="wrap">
<header><h1>__TITLE__</h1><select id="pick" aria-label="Choose a run"></select></header>
<div class="q"><div class="phase" id="phase"></div><h2 id="ttl"></h2><p id="question"></p></div>
<div class="recs" id="recs"></div>
<div class="tabs" id="tabs" role="tablist"><button data-t="t3d" class="on">3D scene</button><button data-t="timg">Figures</button><button data-t="tnotes">Findings for this run</button><button data-t="tsum">All runs</button></div>
<section id="t3d">
 <div class="views" id="views" aria-label="What to show"></div>
 <div class="meta" id="meta"></div>
 <div class="layers" id="layers"></div>
 <div id="stage"><canvas id="cv" aria-label="3D point cloud"></canvas><div id="legend" hidden></div><div id="empty" hidden></div></div>
 <div class="ctrl"><label>Point size <input id="psize" type="range" min="1" max="7" step="1" value="3"></label>
  <label><input id="labels" type="checkbox" checked> object names</label><label><input id="boxes" type="checkbox" checked> object boxes</label><label><input id="fill" type="checkbox" checked> filled</label>
  <label><input id="rings" type="checkbox" checked> position ranges</label><button id="reset" type="button">Reset view</button></div>
 <p class="hint">Drag to rotate, scroll or pinch to zoom, right-drag or shift-drag to pan, double-click to reset.</p>
</section>
<section id="timg" hidden></section><section id="tnotes" class="notes" hidden></section><section id="tsum" class="notes" hidden></section>
</div>
<script>
const D=__DATA__;
const $=id=>document.getElementById(id);
const pick=$('pick');
// picker grouped by phase, in run order
const groups={};D.steps.forEach((s,i)=>{const k=s.run+' | '+s.phase_title;(groups[k]=groups[k]||[]).push(i)});
Object.entries(groups).forEach(([k,ids])=>{const g=document.createElement('optgroup');g.label=k;ids.forEach(i=>{const o=document.createElement('option');o.value=i;o.textContent=D.steps[i].title;g.appendChild(o)});pick.appendChild(g)});
$('tsum').innerHTML=D.summary||'<p class="hint">No summary yet.</p>';
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#tabs button').forEach(x=>x.classList.toggle('on',x===b));
 ['t3d','timg','tnotes','tsum'].forEach(id=>$(id).hidden=id!==b.dataset.t);if(b.dataset.t==='t3d')resize()});
function b64(s){const bin=atob(s),u=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);return u}
function hex(c){c=(c||'#888').replace('#','');if(c.length<6)c='888888';return[parseInt(c.slice(0,2),16),parseInt(c.slice(2,4),16),parseInt(c.slice(4,6),16)]}
function css(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim()}
// ---- decode a scene once ----
function decode(sc){if(sc._dec)return sc._dec;
 const T=sc.points.map(t=>{const q=new Int16Array(b64(t.q).buffer),n=t.n,P=new Float32Array(n*3);
  for(let i=0;i<n;i++)for(let a=0;a<3;a++){const v=q[i*3+a];P[i*3+a]=v===-32768?NaN:t.lo[a]+t.sc[a]*(v+32500)}return P});
 const L=sc.layers.map(l=>{const P=T[l.pts],n=P.length/3;let C;
  if(l.rgb){C=b64(l.rgb)}else{C=new Uint8Array(n*3);const c=hex(typeof l.color==='string'?l.color:'#888');for(let i=0;i<n;i++){C[i*3]=c[0];C[i*3+1]=c[1];C[i*3+2]=c[2]}}
  return{...l,P,C,n}});
 const O=(sc.objects||[]).map(o=>({...o,ring:o.ring!=null?T[o.ring]:null}));
 // robust centre and radius from colour layers
 const src=L.filter(l=>l.kind==='colour');const pool=(src.length?src:L).flatMap(l=>{const a=[];for(let i=0;i<l.n;i+=7)if(!isNaN(l.P[i*3]))a.push([l.P[i*3],l.P[i*3+1],l.P[i*3+2]]);return a});
 const med=a=>{a=a.slice().sort((x,y)=>x-y);return a[Math.floor(a.length/2)]||0};
 const c=[0,1,2].map(k=>med(pool.map(p=>p[k])));const d=pool.map(p=>Math.hypot(p[0]-c[0],p[1]-c[1],p[2]-c[2])).sort((x,y)=>x-y);
 const R=d[Math.floor(d.length*0.9)]||1;
 sc._dec={L,O,c,R};return sc._dec}
// ---- renderer: z-buffered points in a canvas, no WebGL needed ----
const cv=$('cv'),ctx=cv.getContext('2d');let W=0,H=0,dpr=1;
// start from the first camera's viewpoint
const cam={yaw:0,pitch:0,zoom:1,pan:[0,0]};let cur=null,vis={},opts={labels:true,boxes:true,fill:true,rings:true,size:3};
function resize(){const r=$('stage').getBoundingClientRect();dpr=Math.min(window.devicePixelRatio||1,2);W=Math.max(1,Math.round(r.width*dpr));H=Math.max(1,Math.round(r.height*dpr));cv.width=W;cv.height=H;draw()}
new ResizeObserver(resize).observe($('stage'));
function proj(dec){const cy=Math.cos(cam.yaw),sy=Math.sin(cam.yaw),cp=Math.cos(cam.pitch),sp=Math.sin(cam.pitch);
 const Dz=dec.R*3,f=0.42*Math.min(W,H)*cam.zoom*Dz/dec.R,c=dec.c,px=cam.pan[0]*dpr,py=cam.pan[1]*dpr;
 return(x,y,z)=>{x-=c[0];y-=c[1];z-=c[2];const x1=x*cy-z*sy,z1=x*sy+z*cy;const y2=y*cp-z1*sp,z2=y*sp+z1*cp;const dz=z2+Dz;
  if(dz<=Dz*0.05)return null;return[W/2+px+f*x1/dz,H/2+py+f*y2/dz,dz]}}
let raf=0;function draw(){if(!raf)raf=requestAnimationFrame(()=>{raf=0;render()})}
function render(){const s=D.steps[cur];ctx.clearRect(0,0,W,H);if(!s||!s.scene){$('empty').hidden=false;$('empty').textContent=s&&s._loading?'Loading the 3D scene…':s&&s.scene_error?('3D data could not be read: '+s.scene_error):'No 3D data for this run.';$('legend').hidden=true;return}
 $('empty').hidden=true;const dec=decode(s.scene),P=proj(dec);
 const img=ctx.createImageData(W,H),buf=new Uint32Array(img.data.buffer),zb=new Float32Array(W*H).fill(Infinity);
 const sz=Math.max(1,Math.round(opts.size*dpr*0.8));let any=false;
 dec.L.forEach((l,k)=>{if(!vis[k]||l.kind==='line')return;any=true;const S=Math.max(1,Math.round((l.size||2)/2*sz));
  for(let i=0;i<l.n;i++){const x=l.P[i*3];if(isNaN(x))continue;const p=P(x,l.P[i*3+1],l.P[i*3+2]);if(!p)continue;
   const u0=Math.round(p[0]-S/2),v0=Math.round(p[1]-S/2);if(u0>=W||v0>=H||u0+S<0||v0+S<0)continue;
   const col=(255<<24)|(l.C[i*3+2]<<16)|(l.C[i*3+1]<<8)|l.C[i*3];
   for(let dv=0;dv<S;dv++){const v=v0+dv;if(v<0||v>=H)continue;for(let du=0;du<S;du++){const u=u0+du;if(u<0||u>=W)continue;const q=v*W+u;if(p[2]<zb[q]){zb[q]=p[2];buf[q]=col}}}}});
 ctx.putImageData(img,0,0);
 // lines (moves, position ranges per clip count)
 dec.L.forEach((l,k)=>{if(!vis[k]||l.kind!=='line')return;ctx.strokeStyle=l.color||'#ff7f0e';ctx.lineWidth=1.5*dpr;ctx.beginPath();let pen=false;
  for(let i=0;i<l.n;i++){const x=l.P[i*3];const p=isNaN(x)?null:P(x,l.P[i*3+1],l.P[i*3+2]);if(!p){pen=false;continue}pen?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]);pen=true}ctx.stroke()});
 if(vis.objects){const recOk=o=>!o.rec||vis.rec==null||vis.rec===o.rec;const E=[[0,1],[1,3],[3,2],[2,0],[4,5],[5,7],[7,6],[6,4],[0,4],[1,5],[2,6],[3,7]];
  const fg=css('--text'),bg=css('--panel');ctx.font=`${12*dpr}px system-ui,sans-serif`;ctx.lineJoin='round';
  const own=dec.L.some((l,k)=>vis[k]&&l.kind==='objects'),colOf=o=>own&&o.pal?o.pal:o.color;
  if(opts.boxes&&opts.fill){const F=[[0,1,3,2],[4,5,7,6],[0,1,5,4],[2,3,7,6],[0,2,6,4],[1,3,7,5]],faces=[];
   dec.O.forEach(o=>{if(!recOk(o)||!o.corners)return;const c=o.corners.map(q=>P(q[0],q[1],q[2]));
    F.forEach(f=>{if(f.every(i=>c[i]))faces.push([f.reduce((a,i)=>a+c[i][2],0)/4,f.map(i=>c[i]),colOf(o)])})});
   faces.sort((a,b)=>b[0]-a[0]);ctx.globalAlpha=0.16;faces.forEach(([,q,col])=>{ctx.fillStyle=col;ctx.beginPath();ctx.moveTo(q[0][0],q[0][1]);for(let i=1;i<4;i++)ctx.lineTo(q[i][0],q[i][1]);ctx.closePath();ctx.fill()});ctx.globalAlpha=1}
  dec.O.forEach(o=>{if(!recOk(o))return;ctx.strokeStyle=colOf(o);
   if(opts.boxes&&o.corners){const c=o.corners.map(q=>P(q[0],q[1],q[2]));ctx.lineWidth=1.4*dpr;ctx.beginPath();E.forEach(([a,b])=>{if(c[a]&&c[b]){ctx.moveTo(c[a][0],c[a][1]);ctx.lineTo(c[b][0],c[b][1])}});ctx.stroke()}
   if(opts.rings&&o.ring){ctx.lineWidth=1.2*dpr;ctx.setLineDash([4*dpr,3*dpr]);ctx.beginPath();let pen=false;for(let i=0;i<o.ring.length/3;i++){const x=o.ring[i*3];const p=isNaN(x)?null:P(x,o.ring[i*3+1],o.ring[i*3+2]);if(!p){pen=false;continue}pen?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]);pen=true}ctx.stroke();ctx.setLineDash([])}
   const p=P(o.center[0],o.center[1],o.center[2]);if(!p)return;ctx.fillStyle=colOf(o);ctx.beginPath();ctx.arc(p[0],p[1],3.5*dpr,0,7);ctx.fill();
   if(opts.labels){const t=o.label+(o.status&&o.status!=='unchanged'&&o.status!=='matched'?` (${o.status})`:'');ctx.lineWidth=3*dpr;ctx.strokeStyle=bg;ctx.strokeText(t,p[0]+6*dpr,p[1]-6*dpr);ctx.fillStyle=fg;ctx.fillText(t,p[0]+6*dpr,p[1]-6*dpr)}})}
 // legend for an active heat map
 const hl=dec.L.find((l,k)=>vis[k]&&l.legend);const lg=$('legend');
 if(hl){lg.hidden=false;lg.innerHTML=`<div>${hl.name}</div><div class="bar" style="background:linear-gradient(90deg,${hl.legend.stops.join(',')})"></div><div class="ends"><span>${hl.legend.lo}</span><span>${hl.legend.hi}</span></div>`}else lg.hidden=true;
 if(!any&&!vis.objects){$('empty').hidden=false;$('empty').textContent='Nothing switched on. Pick a view above or tick a layer.'}}
// ---- interaction ----
const ptrs=new Map();let last=null;
cv.addEventListener('pointerdown',e=>{cv.setPointerCapture(e.pointerId);ptrs.set(e.pointerId,[e.clientX,e.clientY,e.button,e.shiftKey]);cv.style.cursor='grabbing'});
cv.addEventListener('pointermove',e=>{if(!ptrs.has(e.pointerId))return;const pr=ptrs.get(e.pointerId);const dx=e.clientX-pr[0],dy=e.clientY-pr[1];
 if(ptrs.size===1){if(pr[2]===2||pr[3]||e.shiftKey){cam.pan[0]+=dx;cam.pan[1]+=dy}else{cam.yaw+=dx*0.008;cam.pitch=Math.max(-1.55,Math.min(1.55,cam.pitch-dy*0.008))}}
 else if(ptrs.size===2){const ids=[...ptrs.keys()],o=ptrs.get(ids[0]===e.pointerId?ids[1]:ids[0]);const d0=Math.hypot(pr[0]-o[0],pr[1]-o[1]),d1=Math.hypot(e.clientX-o[0],e.clientY-o[1]);
  if(d0>0)cam.zoom=Math.max(0.2,Math.min(30,cam.zoom*d1/d0));cam.pan[0]+=dx/2;cam.pan[1]+=dy/2}
 ptrs.set(e.pointerId,[e.clientX,e.clientY,pr[2],pr[3]]);draw()});
const up=e=>{ptrs.delete(e.pointerId);cv.style.cursor='grab'};cv.addEventListener('pointerup',up);cv.addEventListener('pointercancel',up);
cv.addEventListener('contextmenu',e=>e.preventDefault());
cv.addEventListener('wheel',e=>{e.preventDefault();cam.zoom=Math.max(0.2,Math.min(30,cam.zoom*Math.exp(-e.deltaY*0.0015)));draw()},{passive:false});
function resetCam(){cam.yaw=0;cam.pitch=0;cam.zoom=1;cam.pan=[0,0];draw()}
cv.addEventListener('dblclick',resetCam);$('reset').onclick=resetCam;
$('psize').oninput=e=>{opts.size=+e.target.value;draw()};
['labels','boxes','fill','rings'].forEach(k=>$(k).onchange=e=>{opts[k]=e.target.checked;draw()});
// ---- views: one recording alone, or the comparison ----
function setView(v){const s=D.steps[cur];if(!s.scene)return;const L=s.scene.layers;vis={objects:true,rec:null};
 L.forEach((l,k)=>{if(l.kind==='line')vis[k]=v==='compare'||v==='all'?true:l.visible;
  else if(v.startsWith('rec:')){vis[k]=l.kind==='colour'&&l.rec===v.slice(4)}
  else if(v.startsWith('obj:')){vis[k]=l.kind==='objects'&&(l.rec==null||l.rec===v.slice(4))}
  else if(v==='compare'){vis[k]=(l.kind==='colour'&&l.rec===(L.find(x=>x.kind==='colour')||{}).rec)||l.kind==='change'}
  else if(v.startsWith('heat:')){vis[k]=String(k)===v.slice(5)}});
 if(v.startsWith('rec:')||(v.startsWith('obj:')&&v.slice(4)!=='all'))vis.rec=v.slice(4);
 document.querySelectorAll('#views button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
 const hp=$('heatpick');if(hp){hp.value=v.startsWith('heat:')?v:'';hp.classList.toggle('on',v.startsWith('heat:'))}syncChips();draw()}
function syncChips(){document.querySelectorAll('#layers input[data-k]').forEach(i=>{const k=i.dataset.k;i.checked=k==='objects'?!!vis.objects:!!vis[k]})}
window.__S=(i,sc)=>{D.steps[i].scene=sc;D.steps[i]._loading=false;if(cur===i)show(i)};
function show(i){cur=i;const s=D.steps[i];
 if(!s.scene&&s.scene_src&&!s._loading){s._loading=true;const el=document.createElement('script');el.src=s.scene_src;
  el.onerror=()=>{s._loading=false;s.scene_src=null;s.scene_error='the 3D file did not load';if(cur===i)show(i)};document.head.appendChild(el)}$('phase').textContent=`${s.phase_title} · ${s.run}`;$('ttl').textContent=s.title;
 $('question').textContent=s.question?('What this run tests: '+s.question):'';
 $('recs').innerHTML=(s.recordings||[]).map((r,k)=>`<span>${s.type==='clips'?'clip '+(k+1):(k?'after':'before')}: ${r}</span>`).join('');
 const m=s.metrics,f=(v,d=0)=>v==null?'–':(+v).toFixed(d),chips=[];
 if(m.obj_changed!=null)chips.push(`changed objects ${m.obj_changed} of ${m.obj_compared}`);
 if(m.changed_frac!=null)chips.push(`changed surface ${f(m.changed_frac*100,1)}%`);
 if(m.pos_radius90_cm_median!=null)chips.push(`objects located to ±${f(m.pos_radius90_cm_median,1)} cm`);
 if(m.pos_nis_median!=null)chips.push(`consistency ${f(m.pos_nis_median,1)} (3 = honest)`);
 if(m.field_scale_m_per_unit!=null)chips.push(`scale ${f(m.field_scale_m_per_unit,3)} m/unit`);
 if(m.field_uncertainty_cm_median!=null)chips.push(`surface uncertainty ${f(m.field_uncertainty_cm_median,1)} cm`);
 $('meta').innerHTML=chips.map(c=>`<span>${c}</span>`).join('');
 const V=$('views'),Lc=$('layers');V.innerHTML='';Lc.innerHTML='';
 if(s.scene){const sc=s.scene,recs=sc.recs||{},L=sc.layers;const btn=(v,t)=>{const b=document.createElement('button');b.type='button';b.dataset.v=v;b.textContent=t;b.onclick=()=>setView(v);V.appendChild(b)};
  [...new Set(L.filter(l=>l.kind==='colour').map(l=>l.rec))].forEach(r=>btn('rec:'+r,`Only ${(recs[r]||r).replace(/ \(.*/,'')}`));
  L.filter(l=>l.kind==='objects').forEach(l=>btn('obj:'+(l.rec==null?'all':l.rec),l.rec==null?'Objects, one colour each':`Objects only, ${(recs[l.rec]||l.rec).replace(/ \(.*/,'')}`));
  if(L.some(l=>l.kind==='change'))btn('compare','Changes between them');
  const heats=L.map((l,k)=>[l,k]).filter(([l])=>l.kind==='heat');
  if(heats.length){const sel=document.createElement('select');sel.id='heatpick';sel.setAttribute('aria-label','Heat map');sel.style.flex='0 1 auto';
   sel.innerHTML='<option value="">Heat map…</option>'+heats.map(([l,k])=>`<option value="heat:${k}">${l.name}</option>`).join('');
   sel.onchange=e=>{if(e.target.value)setView(e.target.value)};V.appendChild(sel)}
  const chip=(k,t)=>{const lb=document.createElement('label');lb.innerHTML=`<input type="checkbox" data-k="${k}"> ${t}`;lb.querySelector('input').onchange=e=>{if(k==='objects')vis.objects=e.target.checked;else vis[k]=e.target.checked;vis.rec=null;draw()};Lc.appendChild(lb)};
  L.forEach((l,k)=>{if(l.kind!=='heat')chip(k,l.name)});if((sc.objects||[]).length)chip('objects',`objects (${sc.objects.length})`);
  const first=L.find(l=>l.kind==='colour');setView(first?'rec:'+first.rec:'compare')}
 else draw();
 $('timg').innerHTML=s.images.map(im=>`<figure><figcaption>${im.caption}</figcaption><img loading="lazy" src="${im.src}" alt="${im.caption}"></figure>`).join('')||'<p class="hint">No figures.</p>';
 $('tnotes').innerHTML=s.notes||'<p class="hint">No notes.</p>'}
pick.onchange=e=>{show(+e.target.value);try{localStorage.setItem('us-pick2',e.target.value)}catch(_){}};
let h=null;try{h=localStorage.getItem('us-pick2')}catch(e){}
if(D.steps.length){let i=h!=null&&D.steps[+h]?+h:D.steps.findIndex(s=>s.type==='change');if(i<0)i=0;pick.value=i;show(i)}
resize();
</script>
"""


if __name__ == "__main__":
    args = sys.argv[1:]
    pre = tuple(args[args.index("--steps") + 1:]) if "--steps" in args else FEATURED
    args = args[:args.index("--steps")] if "--steps" in args else args
    print(build([Path(args[0])], Path(args[1]), pre))
