# Capture protocol: what to record and how

This is path P0. Everything later (3D reconstruction, change detection, uncertainty) can only be
as good as the video, and most failures come from capture rather than models: blur, no
parallax, lens switching, exposure jumps, or no way to check the answer. Follow this once
carefully and the recordings can be reused for every research path.

**Short version:** phone in landscape on the main 1× lens, 4K 30 fps, HDR off, exposure and
focus locked. Walk a slow arc around the shelf (do not just turn on the spot), 60 to 90 s per
sweep, starting and ending on a wide view that includes the floor. Two sweeps per scene state.
Keep a printed scale reference fixed in the scene. Measure and write down every change before
you record the "after" video.

## 1. What the pipeline needs from the video, and why

| Need | Why | How to get it |
|---|---|---|
| **Parallax** (the camera moves through space) | 3D needs the same point seen from different positions. Turning on the spot gives no depth. | Walk an arc; move the phone sideways and up/down, not just rotate. |
| **Sharp frames** | Blur ruins feature matching and object edges, which inflates boundary uncertainty. | Move slowly (about one step per 2 s), good light, avoid fast pans. |
| **Fixed camera intrinsics** | Reconstruction assumes one lens with one focal length. Zoom, lens switching or heavy stabilisation crop changes it mid-video. | Main 1× lens only, no zoom, standard stabilisation (not "action"/"enhanced"), auto-macro off. |
| **Stable exposure and colour** within a sweep | Brightness jumps look like changes and break matching. | Lock AE/AF (long-press on the screen until "AE/AF LOCK"); HDR video off. |
| **Overlap between the two recordings** | Change can only be judged where both recordings saw the area. | Cover the same area from similar distances in both; follow the same rough route, but from shifted positions. |
| **Static anchors with texture** | The two recordings are aligned on what did not change. | Keep the shelf frame, walls, floor, posters, the scale reference visible and unchanged. |
| **Metric scale** | A phone video has no scale; "moved 30 cm" and "radius ± 1 cm" need one. | A printed reference (A4 sheet, or a printed checkerboard) fixed in the scene, never moved. |
| **Floor visible** | Floor plane is used for the robot reach map (P5) and to orient the scene. | Start and end each sweep tilted down enough to show floor and the shelf base. |
| **Each object seen from 3+ directions** | Needed for its 3D shape, size and size uncertainty. | Arc covers left, front, right of the shelf; one close pass. |
| **Ground truth** | Without a written answer key nothing can be scored. | Measure changes with a tape before the "after" recording (§7). |

## 2. Phone settings

| Setting | Use | Avoid |
|---|---|---|
| Resolution / frame rate | **4K at 30 fps** (1080p at 30 fps if storage is tight) | 60 fps (no benefit, double size), slow motion, time-lapse |
| Lens | **Main 1×** for every recording | 0.5× ultrawide, 2×/3× tele, digital zoom; iPhone: turn off *Settings → Camera → Macro Control* auto-switching (or keep ≥ 20 cm from objects) |
| HDR video | **Off** (iPhone: *Settings → Camera → Record Video → HDR Video* off) | HLG / Dolby Vision (washed-out colour on most tools) |
| Exposure / focus | **Lock** before pressing record, re-lock for each recording | Auto exposure pumping as you turn toward a window |
| Stabilisation | Standard (default) | Action mode, "enhanced", cinematic/portrait mode, filters, beauty modes |
| Orientation | **Landscape**, same for all recordings | Rotating mid-video |
| Format | Phone default (`.mov` HEVC on iPhone, `.mp4` on Android) is fine | Re-encoding: never send through WhatsApp/Messages/email (they compress). Copy the original file by cable, AirDrop or Google Drive/Photos "original quality". |
| Optional: LiDAR (iPhone Pro) | A Record3D or Polycam capture of one or two states gives depth and poses to check our reconstruction against | Not required; normal video is the main input |

## 3. Length and camera path

**Per sweep: 60 to 90 seconds.** At the 2 to 5 frames per second we sample, that gives 150 to
400 frames, which is plenty for reconstruction and cheap on Colab. Longer adds little; shorter
than 30 s usually leaves gaps.

**Per scene state: 2 sweeps** (take `a` and take `b`):

- **Sweep a (wide arc), about 1.5 m from the shelf.** Start at the far left looking at the whole
  shelf tilted down to include the floor. Walk a half-circle to the far right at a steady pace,
  keeping the shelf centred. Halfway, raise the phone to about head height for a few seconds,
  then lower to about waist height. End on a wide view including the floor.
- **Sweep b (close pass), about 0.6 to 0.8 m from the shelf.** Go along each shelf level slowly,
  left to right, then right to left one level down. This gives object detail for sizes and
  boundaries. Start and end with a 3 s wide view so it links to sweep a.

**Different viewpoints between before and after** (the task requires it, and it tests that
viewpoint change is not mistaken for scene change): in the "after" recording follow the same
kind of route but shift it, e.g. start 30 to 50 cm to the side, hold the phone higher or lower,
or walk the arc in the opposite direction. Keep at least 60 to 80 % of the scene covered in
both.

Tips: hold the phone with two hands at chest height, elbows in; walk heel-to-toe; pause one
second at each end; do not walk in front of your own light source (your shadow is a change).

## 4. The scene

Aim for **8 to 15 objects** on and around the shelf, chosen to test different things:

| Object type | Examples | What it tests |
|---|---|---|
| Rigid, textured | book, cereal box, patterned tin | Easy case; baseline accuracy |
| Rigid, plain / textureless | white mug, plain bowl | Weak features; boundary and size uncertainty (the "cup radius ± x" case) |
| Small | pen, remote, phone charger | Detection limit |
| Two similar objects | two mugs of different colour, or two similar books | Re-identification: which one moved |
| Deformable | blanket, towel, hoodie, cushion | Deformation heat map (P4) |
| Shiny or transparent (only from pair 3 on) | glass, metal bottle | Known hard case; report as such |
| Floor objects | bag, shoe, box on the floor near the shelf | Robot reach map, "new obstacle" case |
| Fixed anchors (never move) | shelf frame, wall poster, the scale reference, a plant pot | Registration between recordings |

**Scale reference:** tape a printed A4 sheet (21.0 × 29.7 cm), ideally with a printed
checkerboard on it, flat on a visible surface (side of the shelf, the wall, or the floor in
front). It stays in exactly the same place for every recording of a scene.

Keep out of frame: people and pets moving, TV or monitor screens that are on, mirrors (or
cover them), windows with moving things outside if avoidable, swaying curtains.

## 5. What stays the same and what changes

| Keep the same in every recording of a pair | Change on purpose |
|---|---|
| Room, shelf, furniture (unless that is the change) | The objects listed as changes for that pair |
| Phone, lens, resolution, settings | Camera route and viewpoint (shifted, §3) |
| Scale reference and other anchors, untouched | Lighting, **only** on pairs marked as lighting pairs |
| Lighting (on same-lighting pairs): same lamps, curtains in the same position, record within ~30 min so daylight barely shifts | |
| Everything not listed as a change, untouched (don't tidy!) | |

Between "before" and "after", change only what you write down. Anything else that moves by
accident (a chair nudged) either goes into the ground truth or you re-record.

## 6. Recording set (test matrix)

Each pair = "before" recording + changes + "after" recording, each with sweeps a and b.

| Pair | Changes | Lighting | Tests | Priority |
|---|---|---|---|---|
| `pair00_static` | **Nothing changes**; just re-record from a shifted route | same | False-change rate from viewpoint and noise alone (the most important control) | must |
| `pair01` | 4 to 6 mixed changes: one object moved ~30 cm, one moved ~10 cm, one rotated in place ~90°, one removed, one added, one deformed (blanket folded) | same | Minimum deliverables (moved / added / removed, labels) | must |
| `pair02` | Different set of 4 to 6 changes, incl. swapping the two similar objects | **different** (e.g. before: ceiling light on; after: daylight only or a single side lamp) | Lighting robustness, re-identification | must |
| `pair03` | Displacement ladder: four objects moved by about 2, 5, 10 and 20 cm, plus one rotated ~15° | same | Smallest detectable change; is the stated uncertainty honest? | should |
| `pair04` | Deformables only: blanket folded, towel bunched, cushion squashed, hoodie moved and crumpled | same | Deformation heat map vs noise floor | should |
| `pair05` | 2 to 3 changes, but in the "after" recording deliberately skip one shelf end and put one object behind another | same | Visibility: does the system say "unknown" rather than "removed"? | should |
| `pair06_light` | **Nothing changes**, lighting changes | different | Lighting-only false changes | nice |
| `pair07` | Floor changes: a bag and a box added in front of the shelf, a chair moved | same | "New obstacle" case and robot reach map | nice |

Pairs can share recordings to save time: the "after" of one pair can be the "before" of the
next, as long as nothing else is touched in between.

**Minimum to start:** pair00, pair01, pair02 = 6 recordings × 2 sweeps ≈ 12 to 18 minutes of
video. **Full set:** about 16 recordings ≈ 35 to 45 minutes.

## 7. Ground truth (the answer key)

Do this **after making the changes and before recording "after"**:

1. Before changing anything, take one or two still photos of the shelf from the front.
2. For each moved object, measure where it was and where it is to the nearest cm from a fixed
   edge (e.g. "left edge of shelf 2 to mug centre: 12 cm → 41 cm; front edge to mug centre
   10 cm → 10 cm"), and roughly the rotation in degrees.
3. Measure the true size of objects you want sizes for (a mug's diameter and height, a book's
   width and height). This is what calibrates the "cup radius ± 1 cm" uncertainty.
4. Take one still photo after the changes.
5. Write it into `data/ground_truth/<pair>.json` (format in `data/README.md`), or just send
   the notes and photos and I'll write the file.

Do not leave tape marks or labels on objects or the shelf: they would show up as changes.
Mark positions by measuring from edges instead.

For the robot reach map: put a small piece of tape on the floor where an imaginary robot base
would stand, keep it there for every recording, and note the robot's reach you want assumed
(e.g. 0.85 m).

## 8. File naming and hand-over

- Name: `<pair>_<before|after>_<a|b>.<ext>`, e.g. `pair01_before_a.mov`. Rename on the
  computer, keep the original file otherwise untouched.
- One line per recording in a notes file: time, lighting, anything odd ("dog walked through at
  0:40").
- Hand-over: put the files somewhere I can read them (the project files, or a folder on your
  laptop I can access) and they get uploaded to the repo's `data` release with checksums, so
  every Colab account downloads exactly the same bytes. Raw videos never go in git.

## 9. Checklist for each recording

- [ ] Lens 1×, 4K 30 fps, HDR off, landscape
- [ ] Exposure/focus locked on the scene
- [ ] Scale reference in place and untouched; robot base tape in place
- [ ] Nobody else in the room; screens off; curtains still
- [ ] Sweep a: wide arc, floor at start and end, height change halfway, 60 to 90 s
- [ ] Sweep b: close pass along each level, wide view at start and end
- [ ] (After recordings) route shifted from "before"; ground truth written first
- [ ] Play back: no blur streaks, no lens jump, no exposure jump; re-record if in doubt
- [ ] Named, noted, original file copied without compression
