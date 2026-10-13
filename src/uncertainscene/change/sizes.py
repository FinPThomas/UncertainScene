"""Typical sizes of household objects, for the metric scale (change.floor.object_scale).

One entry per label CLIP can give (features.VOCAB_ALL, canonical names): the largest and
second-largest dimension in metres of a typical product, and the log-sd of size across
products (how much the real thing varies: tennis balls barely, cushions and bags a lot).
Values are from common product dimensions (retail listings and standards: A4 paper, UK socket
plates, ball sports rules), set once, never fitted to this scene. Labels without an entry
(walls, floors, curtains, cables...) carry no size information.
"""

# label: (largest m, second m, log-sd)
SIZES = {
    "mug": (0.12, 0.095, 0.15), "cup": (0.10, 0.08, 0.20), "glass": (0.12, 0.07, 0.25),
    "wine glass": (0.20, 0.08, 0.15), "coaster": (0.10, 0.10, 0.10), "bowl": (0.16, 0.16, 0.25),
    "plate": (0.26, 0.26, 0.10), "jar": (0.12, 0.08, 0.30), "bottle": (0.25, 0.07, 0.20),
    "water bottle": (0.24, 0.07, 0.15), "vase": (0.25, 0.12, 0.35), "glass vase": (0.30, 0.12, 0.35),
    "candle": (0.10, 0.07, 0.40), "tea light": (0.04, 0.04, 0.10), "candle holder": (0.12, 0.10, 0.50),
    "guitar": (1.00, 0.38, 0.06), "ukulele": (0.55, 0.18, 0.10), "guitar case": (1.08, 0.42, 0.06),
    "baseball cap": (0.28, 0.20, 0.12), "hat": (0.32, 0.30, 0.20), "table tennis bat": (0.26, 0.15, 0.05),
    "table tennis ball": (0.04, 0.04, 0.02), "tennis ball": (0.067, 0.067, 0.03), "ball": (0.22, 0.22, 0.30),
    "book": (0.23, 0.16, 0.20), "magazine": (0.28, 0.21, 0.08), "notebook": (0.21, 0.15, 0.15),
    "a4 paper sheet": (0.297, 0.21, 0.02), "envelope": (0.22, 0.11, 0.20), "postcard": (0.148, 0.105, 0.03),
    "greeting card": (0.15, 0.105, 0.20), "poster": (0.60, 0.42, 0.40), "picture frame": (0.30, 0.25, 0.40),
    "mirror": (0.60, 0.40, 0.50), "clock": (0.30, 0.30, 0.30),
    "neck pillow": (0.30, 0.28, 0.12), "cushion": (0.45, 0.45, 0.15), "pillow": (0.60, 0.42, 0.12),
    "teddy bear": (0.30, 0.20, 0.40), "soft toy": (0.25, 0.15, 0.50), "toy": (0.20, 0.15, 0.60),
    "remote control": (0.18, 0.05, 0.15), "game controller": (0.16, 0.10, 0.10), "phone": (0.15, 0.072, 0.06),
    "tablet": (0.25, 0.17, 0.12), "laptop": (0.33, 0.23, 0.10), "headphones": (0.20, 0.17, 0.15),
    "speaker": (0.20, 0.10, 0.40), "keys": (0.08, 0.04, 0.30), "wallet": (0.11, 0.09, 0.15),
    "glasses": (0.14, 0.05, 0.08), "sunglasses": (0.14, 0.05, 0.08), "pen": (0.14, 0.012, 0.08),
    "light switch": (0.086, 0.086, 0.05), "power socket": (0.146, 0.086, 0.05), "plug": (0.05, 0.045, 0.20),
    "shoe": (0.28, 0.10, 0.10), "slipper": (0.27, 0.10, 0.10), "sock": (0.25, 0.09, 0.20),
    "jumper": (0.60, 0.50, 0.30), "hoodie": (0.65, 0.55, 0.30), "coat": (0.90, 0.55, 0.30),
    "backpack": (0.45, 0.30, 0.12), "handbag": (0.32, 0.22, 0.25), "tote bag": (0.40, 0.38, 0.15),
    "paper bag": (0.32, 0.24, 0.25), "bag": (0.40, 0.30, 0.40), "box": (0.30, 0.20, 0.60),
    "cardboard box": (0.35, 0.25, 0.50), "storage box": (0.40, 0.30, 0.40), "basket": (0.35, 0.25, 0.35),
    "wooden crate": (0.45, 0.30, 0.30), "tissue box": (0.24, 0.12, 0.12),
    "plant": (0.40, 0.30, 0.60), "flower pot": (0.15, 0.15, 0.40), "lamp": (0.45, 0.25, 0.35),
    "lamp shade": (0.35, 0.30, 0.40), "floor lamp": (1.60, 0.40, 0.15), "radiator": (1.00, 0.60, 0.40),
    "chair": (0.85, 0.50, 0.15), "stool": (0.55, 0.40, 0.25), "side table": (0.55, 0.45, 0.25),
    "coffee table": (1.00, 0.55, 0.25), "armchair": (0.90, 0.85, 0.15), "sofa": (2.00, 0.90, 0.15),
}


def size_prior(label: str | None):
    """(largest m, second m, log-sd) for a label, or None when its size says nothing."""
    return SIZES.get(label) if label else None
