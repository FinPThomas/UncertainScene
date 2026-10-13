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

# wider list for labelling every object in the scene (SAM 2 inventory)
VOCAB_ALL = sorted(set(VOCAB) | {
    "acoustic guitar", "guitar case", "ukulele", "sofa cushion", "seat cushion", "woven cushion", "patterned cushion",
    "grey cushion", "throw blanket", "folded blanket", "jumper", "hoodie", "coat", "scarf", "sock", "slipper",
    "backpack", "handbag", "tote bag", "paper bag", "cardboard box", "basket", "storage box", "magazine",
    "notebook", "picture", "poster", "framed print", "postcard", "greeting card", "mirror", "clock", "candle",
    "candle holder", "tea light", "bowl", "plate", "glass", "wine glass", "water bottle", "jar", "flower pot",
    "houseplant", "flowers", "tv remote", "game controller", "tablet", "charger", "earphones", "speaker",
    "lamp shade", "floor lamp", "side table", "coffee table", "stool", "chair", "cupboard", "drawer", "radiator",
    "window", "curtain", "skirting board", "light switch", "plug", "extension lead", "carpet", "wooden floor",
    "tiled floor", "ceiling", "door frame", "wooden panel", "wooden crate", "leather sofa", "sofa arm",
    "sofa back", "teddy bear", "soft toy", "ball", "tennis ball", "table tennis ball", "glasses", "sunglasses",
    "wallet", "pen", "a4 paper sheet", "envelope", "tissue box", "cushion cover", "cap"})


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


def _embeds(out):
    """Projected CLIP embeddings from get_text_features / get_image_features: a tensor in older
    transformers, an output object whose pooler_output is the projected embedding in 5.x."""
    return (out if isinstance(out, __import__("torch").Tensor) else out.pooler_output).float()


def clip_inventory(crops: list, vocab: list[str] = VOCAB_ALL, model_name: str = "openai/clip-vit-large-patch14",
                   batch: int = 32, groups: list[list[int]] | None = None) -> tuple[list, np.ndarray]:
    """Top-3 (label, probability) per PIL crop and L2-normalised CLIP image embeddings [N, D]
    (the embeddings also serve re-identification across recordings). With `groups` (lists of
    crop indices, e.g. several views of one object), one result per group from the mean of
    its views' embeddings, so the whole object is labelled, not one view of it."""
    import torch
    from transformers import CLIPModel, CLIPProcessor

    if not crops:
        return [], np.zeros((0, 768), np.float32)
    if model_name not in _CACHE:
        _CACHE[model_name] = (CLIPModel.from_pretrained(model_name).to(_device()).eval(),
                              CLIPProcessor.from_pretrained(model_name))
    model, proc = _CACHE[model_name]
    if not crops:
        return [], np.zeros((0, 1), np.float32)
    with torch.no_grad():
        t = proc(text=[f"a photo of a {v}" for v in vocab], return_tensors="pt", padding=True)
        tf = model.get_text_features(**{k: v.to(_device()) for k, v in t.items()})
        tf = torch.nn.functional.normalize(_embeds(tf), dim=-1)
        embs = []
        for i in range(0, len(crops), batch):
            px = proc(images=crops[i:i + batch], return_tensors="pt")["pixel_values"].to(_device())
            embs.append(torch.nn.functional.normalize(_embeds(model.get_image_features(pixel_values=px)), dim=-1))
        imf = torch.cat(embs)
        if groups is not None:
            imf = torch.nn.functional.normalize(torch.stack([imf[g].mean(0) for g in groups]), dim=-1)
        probs = (100 * imf @ tf.T).softmax(-1).cpu().numpy()
        tops = []
        for p in probs:
            o = np.argsort(-p)[:3]
            tops.append([(vocab[j], float(p[j])) for j in o])
    return tops, imf.cpu().numpy().astype(np.float32)


def clip_text(texts: list[str], model_name: str = "openai/clip-vit-large-patch14") -> np.ndarray:
    """L2-normalised CLIP text embeddings [N, D] (same model as clip_inventory)."""
    import torch
    from transformers import CLIPModel, CLIPProcessor

    if model_name not in _CACHE:
        _CACHE[model_name] = (CLIPModel.from_pretrained(model_name).to(_device()).eval(),
                              CLIPProcessor.from_pretrained(model_name))
    model, proc = _CACHE[model_name]
    with torch.no_grad():
        t = proc(text=texts, return_tensors="pt", padding=True)
        tf = model.get_text_features(**{k: v.to(_device()) for k, v in t.items()})
        tf = torch.nn.functional.normalize(_embeds(tf), dim=-1)
    return tf.cpu().numpy().astype(np.float32)


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
