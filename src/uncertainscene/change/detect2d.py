"""Whole-object instances from an open-vocabulary detector (YOLOE, text-prompted).

SAM 2's automatic masks are parts as often as objects (run 6: the guitar in up to 8 pieces,
cushions split into panels) and CLIP on masked crops gives weak labels (0.1-0.3, "skirting
board" for many pieces). YOLOE, prompted with a household vocabulary, returns one mask per
object instance with a class name. It is used in three ways (see segment.sam_tracks):

1. its instances seed SAM 2 tracks before the automatic masks, so an object is tracked whole;
2. automatic masks lying inside an instance of an object class are parts, not new objects;
3. on every kept frame each track collects label votes from the instance it overlaps, and
   tracks that keep lying inside one instance are joined to the track of that instance.

Weights come from the ultralytics GitHub release (no Hugging Face needed).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

WEIGHTS = "yoloe-11l-seg.pt"
RELEASE = "https://github.com/ultralytics/assets/releases/download/v8.3.0/"

# what the detector is asked to find. SUPPORT classes are surfaces other objects rest on: a mask
# inside a sofa is a cushion, not a part of the sofa, so only OBJECT classes absorb parts.
OBJECTS = [
    "cushion", "pillow", "neck pillow", "throw blanket", "book", "stack of books", "glass vase", "vase", "mug", "cup",
    "glass", "coaster", "guitar", "baseball cap", "hat", "table tennis bat", "remote control", "phone", "bottle",
    "bag", "paper bag", "box", "shoe", "plant", "picture frame", "lamp", "toy", "soft toy", "towel", "jacket",
    "laptop", "headphones", "basket", "bowl", "plate", "ball", "pen", "sock", "card", "candle", "clock", "speaker",
]
SUPPORT = ["sofa", "armchair", "rug", "shelf", "cabinet", "table", "side table", "chair", "radiator", "curtain",
           "window", "door"]
VOCAB = OBJECTS + SUPPORT
CANON = {"pillow": "cushion", "hat": "baseball cap", "stack of books": "book", "vase": "glass vase",
         "mug": "cup", "throw blanket": "blanket", "soft toy": "toy", "side table": "table"}


def canonical(label: str) -> str:
    return CANON.get(label, label)


def weights_path(name: str = WEIGHTS, cache: str | os.PathLike | None = None) -> Path:
    """Local copy of the weights, downloaded from the GitHub release once."""
    for p in (Path(name), Path(cache or ".") / name, Path.home() / ".cache" / "uncertainscene" / name):
        if p.exists():
            return p
    import urllib.request

    dst = Path(cache) / name if cache else Path.home() / ".cache" / "uncertainscene" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part")
    urllib.request.urlretrieve(RELEASE + name, tmp)
    tmp.rename(dst)
    return dst


class Detector:
    """`det(img) -> [dict(mask=bool[h, w] full-res, label, conf, box, object=bool)]`."""

    def __init__(self, weights: str | os.PathLike | None = None, vocab: list[str] | None = None,
                 imgsz: int = 1024, conf: float = 0.15, min_area: float = 0.0005, max_area: float = 0.6,
                 device: str | None = None):
        from PIL import Image

        pil_open = Image.open
        from ultralytics import YOLOE

        Image.open = pil_open        # ultralytics patches PIL's open (installs pi-heif on any failure); undo that
        self.vocab = list(vocab or VOCAB)
        weights = Path(weights or weights_path())
        self.model = YOLOE(str(weights))
        here = os.getcwd()
        try:      # YOLOE's text encoder (mobileclip_blt.ts) is looked for, or fetched, in the working directory
            os.chdir(weights.parent)
            self.model.set_classes(self.vocab, self.model.get_text_pe(self.vocab))
        finally:
            os.chdir(here)
        self.imgsz, self.conf, self.min_area, self.max_area = imgsz, conf, min_area, max_area
        self.device = device

    def __call__(self, img: np.ndarray) -> list[dict]:
        r = self.model.predict(img[..., ::-1].copy(), imgsz=self.imgsz, conf=self.conf, verbose=False,
                               retina_masks=True, device=self.device)[0]
        if r.masks is None:
            return []
        h, w = img.shape[:2]
        ms = r.masks.data.cpu().numpy() > 0.5
        if ms.shape[1:] != (h, w):
            from PIL import Image

            ms = np.stack([np.asarray(Image.fromarray(m.astype(np.uint8) * 255).resize((w, h), Image.Resampling.NEAREST))
                           > 127 for m in ms])
        out = []
        for m, c, p, b in zip(ms, r.boxes.cls.cpu().numpy(), r.boxes.conf.cpu().numpy(), r.boxes.xyxy.cpu().numpy()):
            a = m.sum() / (h * w)
            if not self.min_area <= a <= self.max_area:
                continue
            lab = self.vocab[int(c)]
            out.append({"mask": m, "label": canonical(lab), "raw_label": lab, "conf": float(p),
                        "box": tuple(int(x) for x in b), "object": lab in OBJECTS})
        return dedupe(out)


def dedupe(dets: list[dict], max_iou: float = 0.7) -> list[dict]:
    """One instance per object: of overlapping instances (IoU > max_iou, any class), keep the
    most confident (YOLOE's NMS is per class, so a cushion can also come back as a pillow)."""
    keep: list[dict] = []
    for d in sorted(dets, key=lambda d: -d["conf"]):
        if all(iou(d["mask"], k["mask"]) <= max_iou for k in keep):
            keep.append(d)
    return keep


def iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    return float(inter / max(a.sum() + b.sum() - inter, 1))


def inside(a: np.ndarray, b: np.ndarray) -> float:
    """Share of mask `a` that lies inside mask `b`."""
    return float(np.logical_and(a, b).sum() / max(a.sum(), 1))


def release() -> None:
    import gc

    gc.collect()
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass
