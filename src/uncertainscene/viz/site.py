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

IMAGES = [("objects.png", "Every object"), ("object_overlays.png", "Object boundaries in the frames"),
          ("heatmaps.png", "Heat maps"), ("base_placement.png", "Where a robot could stand"),
          ("overlays.png", "Changes in the frames"), ("crops.png", "Changed objects")]


def _decode(v):
    """Plotly array: list, or {'dtype', 'bdata'} (base64 typed array)."""
    if isinstance(v, dict) and "bdata" in v:
        return np.frombuffer(base64.b64decode(v["bdata"]), dtype=np.dtype(v["dtype"])).tolist()
    return v


def _r(a, d=3):
    return [None if v is None else round(float(v), d) for v in a]


def from_viewer_html(path: Path, max_points: int = 4500) -> dict:
    s = path.read_text()
    i = s.index("Plotly.newPlot(")
    j = s.index("[", i)
    data, _ = json.JSONDecoder().raw_decode(s[j:])
    layers, objects = [], []
    rng = np.random.default_rng(0)
    for t in data:
        name = t.get("name", "")
        if t.get("type") != "scatter3d":
            continue
        x, y, z = (_decode(t.get(k, [])) for k in "xyz")
        mk = t.get("marker", {})
        col = _decode(mk.get("color"))
        if "text" in t.get("mode", "") and t.get("text"):
            texts = t["text"] if isinstance(t["text"], list) else [t["text"]] * len(x)
            cols = col if isinstance(col, list) else [col] * len(x)
            hov = t.get("hovertext") or [""] * len(x)
            for k in range(len(x)):
                objects.append({"center": [x[k], y[k], z[k]], "label": texts[k], "color": cols[k],
                                "hover": hov[k] if isinstance(hov, list) else hov, "group": name})
            continue
        if t.get("mode") == "lines":
            layers.append({"name": name, "x": x, "y": y, "z": z, "line": t.get("line", {}), "visible": True})
            continue
        if name.startswith("unknown"):
            continue
        n = len(x)
        rng = np.random.default_rng(n)      # same n -> same picks, so heat maps share coordinates
        sel = np.sort(rng.choice(n, min(n, max_points), replace=False)) if n > max_points else np.arange(n)
        pick = (lambda a: [a[k] for k in sel]) if n else (lambda a: a)
        layer = {"name": name, "size": mk.get("size", 2), "visible": t.get("visible", True) is True}
        xyz = (_r(pick(x)), _r(pick(y)), _r(pick(z)))
        if isinstance(col, list) and len(col) == n and mk.get("colorscale"):
            layer.update(color=_r(pick(col), 2), colorscale=mk["colorscale"], cmin=mk.get("cmin"), cmax=mk.get("cmax"))
            ref = next((k for k, l in enumerate(layers) if l.get("colorscale") and "x" in l and l["x"] == xyz[0]), None)
            if ref is not None:          # heat maps share one point set: store coordinates once
                layer["ref"] = ref
                layers.append(layer)
                continue
        elif isinstance(col, list) and len(col) == n:
            layer["color"] = pick(col)
        else:
            layer["color"] = col
        layer["x"], layer["y"], layer["z"] = xyz
        layers.append(layer)
    return {"layers": layers, "objects": objects}


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
    m = json.loads((d / "metrics.json").read_text()) if (d / "metrics.json").exists() else {}
    st = {"name": d.name, "type": m.get("pair_type"), "a": m.get("a"), "b": m.get("b"), "variant": m.get("variant"),
          "metrics": {k: m.get(k) for k in ("changed_frac", "obj_changed", "obj_compared", "obj_total",
                                             "field_scale_m_per_unit", "field_uncertainty_cm_median",
                                             "field_reachable_frac", "field_interactable_frac", "n_per_video",
                                             "frames_total", "n_changes")},
          "images": [], "scene": None}
    for f, cap in IMAGES:
        if (d / f).exists():
            st["images"].append({"caption": cap, "src": _img(d / f)})
    rep = d / "report.md"
    st["notes"] = _md_to_html(rep.read_text()) if rep.exists() else ""
    if with_3d:
        if (d / "scene3d.json").exists():
            st["scene"] = json.loads((d / "scene3d.json").read_text())
        elif (d / "viewer.html").exists():
            try:
                st["scene"] = from_viewer_html(d / "viewer.html")
            except Exception as e:  # noqa: BLE001 - a broken viewer must not stop the page
                st["scene_error"] = str(e)[:200]
    return st


def build(run_dirs: list[Path], out: Path, prefixes: tuple = ("p1",), title: str = "Scene01 Change Viewer",
          summary_md: str = "") -> Path:
    steps = []
    for rd in run_dirs:
        for d in sorted(Path(rd).iterdir()):
            if d.is_dir() and d.name.startswith(prefixes) and (d / "metrics.json").exists():
                st = load_step(d)
                st["run"] = Path(rd).name
                steps.append(st)
    payload = json.dumps({"steps": steps, "summary": _md_to_html(summary_md)}, separators=(",", ":"))
    out.write_text(TEMPLATE.replace("__TITLE__", title).replace("__DATA__", payload.replace("</", "<\\/")))
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.2/plotly.min.js"></script>
<style>
:root{--bg:#fafaf8;--panel:#fff;--text:#1d1d1b;--muted:#6b6b66;--line:#e3e2dc;--accent:#2f6fde;--chip:#f0efe9}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#17171a;--panel:#202024;--text:#ececea;--muted:#a3a39d;--line:#34343a;--accent:#7aa7ff;--chip:#2a2a30}}
:root[data-theme="dark"]{--bg:#17171a;--panel:#202024;--text:#ececea;--muted:#a3a39d;--line:#34343a;--accent:#7aa7ff;--chip:#2a2a30}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0;font-weight:600}select{font:inherit;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--text);max-width:100%}
.tabs{display:flex;gap:6px;padding:10px 16px;flex-wrap:wrap;border-bottom:1px solid var(--line)}
.tabs button{font:inherit;border:1px solid var(--line);background:var(--chip);color:var(--text);padding:6px 12px;border-radius:999px;cursor:pointer}
.tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
main{padding:12px 16px;max-width:1500px;margin:0 auto}
#plot{width:100%;height:72vh;min-height:420px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 12px}.meta span{background:var(--chip);padding:3px 9px;border-radius:999px;font-size:13px}
.layers{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.layers label{background:var(--chip);padding:3px 9px;border-radius:8px;font-size:13px;cursor:pointer}
figure{margin:0 0 18px}figure img{width:100%;height:auto;border-radius:8px;border:1px solid var(--line);background:#fff}figcaption{color:var(--muted);font-size:13px;margin:4px 0}
table{border-collapse:collapse;margin:8px 0;font-size:13px;display:block;overflow-x:auto}td,th{border:1px solid var(--line);padding:4px 7px;text-align:left}
.hide{display:none}.muted{color:var(--muted)}code{background:var(--chip);padding:0 4px;border-radius:4px}
</style></head><body>
<header><h1>__TITLE__</h1><select id="pick" aria-label="pair"></select><span id="sub" class="muted"></span></header>
<div class="tabs" id="tabs"><button data-t="t3d" class="on">3D scene</button><button data-t="timg">Heat maps and objects</button><button data-t="tnotes">Findings</button><button data-t="tsum">Run summary</button></div>
<main>
<section id="t3d"><div class="meta" id="meta"></div><div class="layers" id="layers"></div><div id="plot"></div>
<p class="muted">Drag to rotate, scroll to zoom. Tick layers above to switch colour, change classes, each heat map and the labelled objects on and off.</p></section>
<section id="timg" class="hide"></section><section id="tnotes" class="hide"></section><section id="tsum" class="hide"></section>
</main>
<script>
const D=__DATA__;
const pick=document.getElementById('pick');
D.steps.forEach((s,i)=>{const o=document.createElement('option');o.value=i;o.textContent=`${s.name} (${s.type||''})`;pick.appendChild(o)});
document.getElementById('tsum').innerHTML=D.summary||'<p class="muted">No summary yet.</p>';
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#tabs button').forEach(x=>x.classList.toggle('on',x===b));
 ['t3d','timg','tnotes','tsum'].forEach(id=>document.getElementById(id).classList.toggle('hide',id!==b.dataset.t));if(b.dataset.t==='t3d')Plotly.Plots.resize('plot')});
function dark(){return matchMedia('(prefers-color-scheme: dark)').matches}
function traces(sc,state){const T=[];
 sc.layers.forEach((l,i)=>{if(!state[i])return;if(l.ref!=null){const r=sc.layers[l.ref];l={...l,x:r.x,y:r.y,z:r.z}}
  if(l.line){T.push({type:'scatter3d',mode:'lines',x:l.x,y:l.y,z:l.z,line:l.line,name:l.name,showlegend:false,hoverinfo:'skip'});return}
  const mk={size:l.size||2,color:l.color};if(l.colorscale){Object.assign(mk,{colorscale:l.colorscale,cmin:l.cmin,cmax:l.cmax,showscale:true,colorbar:{len:0.5,thickness:12}})}
  T.push({type:'scatter3d',mode:'markers',x:l.x,y:l.y,z:l.z,marker:mk,name:l.name,hoverinfo:'skip'})});
 if(state.objects&&sc.objects.length){const o=sc.objects;
  T.push({type:'scatter3d',mode:'markers+text',x:o.map(a=>a.center[0]),y:o.map(a=>a.center[1]),z:o.map(a=>a.center[2]),text:o.map(a=>a.label),
   hovertext:o.map(a=>a.hover||a.label),hoverinfo:'text',textfont:{size:11,color:dark()?'#eee':'#222'},marker:{size:5,color:o.map(a=>a.color)},name:'objects'});
  o.forEach(a=>{if(!a.corners)return;const c=a.corners,E=[[0,1],[1,3],[3,2],[2,0],[4,5],[5,7],[7,6],[6,4],[0,4],[1,5],[2,6],[3,7]];const x=[],y=[],z=[];
   E.forEach(([p,q])=>{x.push(c[p][0],c[q][0],null);y.push(c[p][1],c[q][1],null);z.push(c[p][2],c[q][2],null)});
   T.push({type:'scatter3d',mode:'lines',x,y,z,line:{color:a.color,width:3},showlegend:false,hoverinfo:'skip'})})}
 return T}
let cur=null,state={};
function draw(){const s=D.steps[cur],sc=s.scene;if(!sc){document.getElementById('plot').innerHTML='<p class="muted" style="padding:16px">No 3D data for this step.</p>';return}
 const lay={paper_bgcolor:'rgba(0,0,0,0)',font:{color:dark()?'#ddd':'#222'},margin:{l:0,r:0,t:0,b:0},showlegend:false,
  scene:{aspectmode:'data',camera:{up:{x:0,y:-1,z:0}},xaxis:{visible:false},yaxis:{visible:false},zaxis:{visible:false}}};
 Plotly.react('plot',traces(sc,state),lay,{responsive:true,displaylogo:false})}
function show(i){cur=i;const s=D.steps[i];
 document.getElementById('sub').textContent=`${s.a||''} vs ${s.b||''} · ${s.variant||''} · ${s.run||''}`;
 const m=s.metrics,chips=[];const f=(v,d=0)=>v==null?'–':v.toFixed(d);
 if(m.obj_changed!=null)chips.push(`changed objects ${m.obj_changed} of ${m.obj_compared}`);
 if(m.changed_frac!=null)chips.push(`point-level changed ${f(m.changed_frac*100,2)}%`);
 if(m.field_scale_m_per_unit!=null)chips.push(`scale ${f(m.field_scale_m_per_unit,3)} m/unit`);
 if(m.field_uncertainty_cm_median!=null)chips.push(`surface uncertainty ${f(m.field_uncertainty_cm_median,1)} cm`);
 if(m.field_interactable_frac!=null)chips.push(`interactable ${f(m.field_interactable_frac*100)}%`);
 document.getElementById('meta').innerHTML=chips.map(c=>`<span>${c}</span>`).join('');
 const L=document.getElementById('layers');L.innerHTML='';state={};
 if(s.scene){s.scene.layers.forEach((l,k)=>{if(l.line){state[k]=true;return}state[k]=!!l.visible;
   const lb=document.createElement('label');lb.innerHTML=`<input type="checkbox" ${state[k]?'checked':''}> ${l.name}`;
   lb.querySelector('input').onchange=e=>{state[k]=e.target.checked;draw()};L.appendChild(lb)});
  if(s.scene.objects.length){state.objects=true;const lb=document.createElement('label');lb.innerHTML=`<input type="checkbox" checked> objects (${s.scene.objects.length})`;
   lb.querySelector('input').onchange=e=>{state.objects=e.target.checked;draw()};L.appendChild(lb)}}
 draw();
 document.getElementById('timg').innerHTML=s.images.map(im=>`<figure><figcaption>${im.caption}</figcaption><img loading="lazy" src="${im.src}" alt="${im.caption}"></figure>`).join('')||'<p class="muted">No figures.</p>';
 document.getElementById('tnotes').innerHTML=s.notes||'<p class="muted">No notes.</p>'}
pick.onchange=e=>show(+e.target.value);
let h=0;try{h=+(localStorage.getItem('us-pick')||0)}catch(e){}
pick.addEventListener('change',e=>{try{localStorage.setItem('us-pick',e.target.value)}catch(_){}});
if(D.steps.length){h=Math.min(h,D.steps.length-1);pick.value=h;show(h)}
</script></body></html>
"""


if __name__ == "__main__":
    args = sys.argv[1:]
    pre = tuple(args[args.index("--steps") + 1:]) if "--steps" in args else ("p1",)
    args = args[:args.index("--steps")] if "--steps" in args else args
    print(build([Path(args[0])], Path(args[1]), pre))
