"""Appearance features (DINOv2) and open-vocabulary labels (CLIP), both small and frozen.

These are measuring instruments: pretrained, never fitted on this project's data or labels.
"""

from __future__ import annotations

import numpy as np

_CACHE = {}

VOCAB = [
    "cushion", "pillow", "neck pillow", "blanket", "throw", "book", "stack of books", "glass vase",
    "vase", "mug", "cup", "coaster", "guitar", "baseball cap", "hat", "table tennis bat", "remote control",
    "phone", "bottle", "bag", "box", "shoe", "plant", "picture frame", "lamp", "toy", "towel", "jacket",
    "laptop", "headphones", "keys", "sofa", "armchair", "rug", "floor", "wall", "door", "shelf",
    "power socket", "cable",
]
BACKGROUND = {"sofa", "armchair", "rug", "floor", "wall", "door", "shelf", "power socket", "cable"}


def _device():
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


def dino_features(images: np.ndarray, model_name: str = "facebook/dinov2-small", batch: int = 8) -> np.ndarray:
    """[S, h, w, C] float16 L2-normalised patch features for uint8 images [S, H, W, 3]."""
    import torch
    from transformers import AutoModel

    if model_name not in _CACHE:
        _CACHE[model_name] = AutoModel.from_pretrained(model_name).to(_device()).eval()
    model = _CACHE[model_name]
    S, H, W, _ = images.shape
    h, w = H // 14, W // 14
    mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
    out = []
    for i in range(0, S, batch):
        x = torch.from_numpy(images[i:i + batch, : h * 14, : w * 14]).permute(0, 3, 1, 2).float() / 255
        x = ((x - mean) / std).to(_device())
        with torch.no_grad():
            tok = model(pixel_values=x).last_hidden_state[:, 1:]       # drop CLS
        f = torch.nn.functional.normalize(tok.float(), dim=-1).reshape(len(x), h, w, -1)
        out.append(f.half().cpu().numpy())
    return np.concatenate(out)


def clip_labels(crops: list, vocab: list[str] = VOCAB, model_name: str = "openai/clip-vit-base-patch32"):
    """Top label and probability for each PIL crop, plus the full probability table."""
    import torch
    from transformers import CLIPModel, CLIPProcessor

    if model_name not in _CACHE:
        _CACHE[model_name] = (CLIPModel.from_pretrained(model_name).to(_device()).eval(),
                              CLIPProcessor.from_pretrained(model_name))
    model, proc = _CACHE[model_name]
    if not crops:
        return [], np.zeros((0, len(vocab)))
    inp = proc(text=[f"a photo of a {v}" for v in vocab], images=crops, return_tensors="pt", padding=True)
    inp = {k: v.to(_device()) for k, v in inp.items()}
    with torch.no_grad():
        probs = model(**inp).logits_per_image.softmax(-1).float().cpu().numpy()
    top = [(vocab[int(p.argmax())], float(p.max())) for p in probs]
    return top, probs
