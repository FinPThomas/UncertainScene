"""Plain-language names for recordings, pair kinds, phases and steps.

Every results page (RESULTS.md, the viewer, each step's report.md) takes its titles from here,
so a reader always sees what a run compares and what question it answers, never only a code.
"""

from __future__ import annotations

REC = {  # label -> (lighting, state)
    "scene01_dark_before_a": ("dark", "before"), "scene01_dark_before_b": ("dark", "before"),
    "scene01_dark_before_c": ("dark", "before"), "scene01_light_before_a": ("light", "before"),
    "scene01_light_before_b": ("light", "before"), "scene01_light_after_a": ("light", "after"),
    "scene01_light_after_b": ("light", "after"),
}

# pair kind -> (short title, the question a pair of this kind answers, slug used in folder names)
KIND = {
    "static": ("Nothing moved, same lighting",
               "How many false alarms does it raise when nothing changed?", "no-change"),
    "lighting": ("Nothing moved, light vs dark",
                 "Does a lighting change alone cause false alarms?", "no-change-light-vs-dark"),
    "change": ("Objects moved, same lighting",
               "Does it find the objects that really moved, were added or removed?", "moved"),
    "change+lighting": ("Objects moved and lighting changed",
                        "Does it still find the real changes when the lighting also changes?", "moved-and-light-changed"),
}

# version 2 phases, in the order they run. Folder names start with the key.
PHASES = {
    "1-main": ("Main comparisons",
               "One or two pairs of every kind, with every output (objects, heat maps, 3D, scene report)."),
    "1b-splat": ("Gaussian splats",
                 "Do splats of each recording render well, and does comparing before and after from the exact same "
                 "camera separate real changes from noise?"),
    "1c-objects": ("Whole objects",
                   "With a detector that finds whole objects and names them, is every object one piece in the point "
                   "cloud, correctly labelled, and matched as the same object across recordings?"),
    "1d-objects": ("Whole objects, recordings re-aligned",
                   "After run 10: once the after recording is lined up on the objects that did not move, does each "
                   "object get one verdict, are pieces one object, and are the object clouds clean? (light and "
                   "dark streams)"),
    "2-position": ("Where objects are, and how sure",
                   "How precisely is each object located, and does the range shrink with more clips of the same scene?"),
    "3-allpairs": ("Every other pair",
                   "More pairs of each kind, so false alarms and real detections can be counted properly."),
    "4-frames": ("How many frames are enough",
                 "Does the result change with 28, 100 or 185 frames per recording instead of every frame (~370), or fewer or more SAM 2 views?"),
    "5-robot": ("Robot variants",
                "How do reach and approach maps change with a shorter or longer arm, a lower mount, a robot that cannot drive, or a bigger gripper?"),
    "6-repeat": ("Repeatability",
                 "Same pairs with different frame picks, and the reversed direction: are results stable?"),
}

# version 1 (first overnight run, 2026-10-10_scene01-heatmaps): folder prefix -> (title, question)
PHASES_V1 = {
    "p1b": ("Light vs dark", "Every light/dark contrast, for the lighting report."),
    "p1": ("Main comparisons", "The six main pairs with every output."),
    "p2": ("Dense versions", "Main pairs with as many frames as the GPU fits."),
    "p3": ("Robot variants", "Reach and approach maps for different robots."),
    "p4": ("Ablations", "Does the visibility test or the appearance test matter?"),
    "p5": ("Every other pair", "Remaining pairs for more evidence per kind."),
    "p6": ("How many frames are enough", "Fewer frames per recording."),
    "p7": ("Repeatability", "Same pairs with different frame picks."),
    "summary": ("Summaries", "Tables across all finished steps."),
}

VARIANT = {
    "base": "", "reversed": "reversed direction (after compared with before)",
    "short_arm": "short arm (0.7 m reach)", "long_arm": "long arm (1.3 m reach)",
    "low_mount": "arm mounted low (0.45 m)", "fixed_base": "robot cannot drive (stays where the camera operator stood)",
    "table_mount": "arm mounted at table height", "mobile_closer": "robot base 0.6 m closer",
    "big_gripper": "bigger gripper", "fields_on_A": "heat maps of the scene before the change",
    "dense": "dense (more frames)", "no_visibility": "without the visibility test",
    "no_appearance": "without the appearance test", "naive_nn": "plain nearest-point comparison, no visibility test",
    "short_operator": "assuming the phone was held lower", "tall_operator": "assuming the phone was held higher",
    "objects": "whole objects found by a detector, then tracked",
    "objects2": "whole objects, recordings re-aligned on unchanged objects (clean clouds, one verdict per object)",
}


def pair_type(a: str, b: str) -> str:
    (la, sa), (lb, sb) = REC[a], REC[b]
    if sa == sb:
        return "static" if la == lb else "lighting"
    return "change" if la == lb else "change+lighting"


def rec_name(label: str) -> str:
    """scene01_light_before_a -> 'light, before, take a'."""
    if label not in REC:
        return label
    light, state = REC[label]
    return f"{light}, {state}, take {label[-1]}"


def rec_slug(label: str) -> str:
    """scene01_light_before_a -> 'light-before-a'."""
    return label.replace("scene01_", "").replace("_", "-")


def variant_name(v: str | None) -> str:
    if not v:
        return ""
    if v in VARIANT:
        return VARIANT[v]
    if v.startswith("n") and v[1:].isdigit():
        return f"{v[1:]} frames per recording"
    if v.startswith("key") and v[3:].isdigit():
        return f"{v[3:]} SAM 2 views per recording"
    if v.startswith("offset"):
        return f"different frame picks (repeat {v[6:]})"
    if v.startswith("clips"):
        return v.replace("clips", "clips: ")
    return v.replace("_", " ")


def phase_of(step: str) -> tuple[str, str, str]:
    """(phase key, title, question) for a step folder name, version 1 or 2."""
    for k, (t, q) in PHASES.items():
        if step.startswith(k):
            return k, t, q
    for k, (t, q) in PHASES_V1.items():
        if step.startswith(k + "_") or step == k:
            return k, t, q
    return "other", "Other", ""


def describe(step: str, m: dict) -> dict:
    """Title, question and phase for one finished step (from its folder name and metrics)."""
    if m.get("title"):
        ph = phase_of(step)
        return {"phase": ph[0], "phase_title": ph[1], "phase_question": ph[2], "title": m["title"],
                "question": m.get("question", ""), "kind": m.get("pair_type")}
    kind = m.get("pair_type")
    a, b = m.get("a"), m.get("b")
    ph = phase_of(step)
    if kind in KIND and a and b:
        t, q, _ = KIND[kind]
        title = f"{t}: {rec_name(a)} vs {rec_name(b)}"
        v = variant_name(m.get("variant"))
        if v:
            title += f" ({v})"
    else:
        title, q = step.replace("_", " "), ""
    return {"phase": ph[0], "phase_title": ph[1], "phase_question": ph[2], "title": title, "question": q, "kind": kind}


def step_name(phase: str, a: str, b: str, variant: str = "") -> str:
    """Folder name: <phase>_<variant>_<kind>_<rec a>_vs_<rec b>, e.g.
    1-main_moved_light-before-a_vs_light-after-a."""
    v = f"{variant}_" if variant else ""
    return f"{phase}_{v}{KIND[pair_type(a, b)][2]}_{rec_slug(a)}_vs_{rec_slug(b)}"
