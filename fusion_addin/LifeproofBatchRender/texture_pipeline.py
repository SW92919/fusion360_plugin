# Fusion 360: apply color-set images via document appearances OR root decals.
# Edit the name sets below to match your template .f3d (Appearance / Decal names in Browser).

from __future__ import annotations

import base64
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

import adsk.core  # type: ignore
import adsk.fusion  # type: ignore

from visibility_apply import (
    _body_hide_for_batch,
    _mesh_hide_for_batch,
    _occurrence_should_hide_batch,
)

# --- User template: exact Fusion names for each slot (Appearance mode) ---
# Client allowlist — only these appearances receive batch color images; foam,
# steel, paint, and all other document appearances are left unchanged.
#
# Matching is NORMALIZED (see ``appearance_name_slot``): spacing and separator
# drift don't matter, so "Vinyl Skin - 1", "Vinyl Skin-1" and "Vinyl_1" all map
# to slot 1. The client's models are inconsistent across files:
#   Treads Plus Bullnose - Appearance (2).f3d → "Vinyl Skin-1" / "Vinyl Skin-2"
#   Treads Plus Bullnose - Appearance1.f3d    → "Vinyl"        / "Vinyl Skin - 2"
#   Treads Plus Bullnose - Appearance2.f3d    → "Vinyl Skin - 1" / "Vinyl Skin - 2"
# All three are handled by the entries below.
SLOT1_APPEARANCE_NAMES: FrozenSet[str] = frozenset(
    {
        "Vinyl_1",
        "Vinyl Skin - 1",
        "Vinyl Skin-1",
        "Vinyl",
    }
)
SLOT2_APPEARANCE_NAMES: FrozenSet[str] = frozenset(
    {
        "Vinyl_2",
        "Vinyl Skin - 2",
        "Vinyl Skin-2",
    }
)


def _normalize_appearance_slot_name(name: str) -> str:
    """Collapse spacing / separator drift for slot-name comparison.

    Lowercases, folds NBSP, and reduces any run of space / underscore / hyphen
    to a single ``-`` so ``"Vinyl Skin - 1"``, ``"Vinyl Skin-1"`` and
    ``"Vinyl_1"`` all normalize to ``"vinyl-skin-1"`` / ``"vinyl-1"``.
    """
    s = (name or "").strip().lower().replace(" ", " ")
    s = re.sub(r"[\s_\-]+", "-", s)
    return s.strip("-")


_SLOT1_APPEARANCE_NAMES_NORM: FrozenSet[str] = frozenset(
    _normalize_appearance_slot_name(n) for n in SLOT1_APPEARANCE_NAMES
)
_SLOT2_APPEARANCE_NAMES_NORM: FrozenSet[str] = frozenset(
    _normalize_appearance_slot_name(n) for n in SLOT2_APPEARANCE_NAMES
)


def appearance_name_slot(name: str) -> Optional[int]:
    """Return 1 or 2 if ``name`` matches a slot-1 / slot-2 appearance, else None."""
    n = _normalize_appearance_slot_name(name)
    if not n:
        return None
    if n in _SLOT1_APPEARANCE_NAMES_NORM:
        return 1
    if n in _SLOT2_APPEARANCE_NAMES_NORM:
        return 2
    return None

# --- User template: exact Fusion names for each slot (Decal mode) ---
SLOT1_DECAL_NAMES: FrozenSet[str] = frozenset(
    {
        "Batch_Decal_1",
        "Wood_Decal_1",
        "Honeycomb-1",
        "Honeycomb_1",
        "Decal_1",
        "Decal-1",
        "Slot_1",
        "Slot1",
    }
)
SLOT2_DECAL_NAMES: FrozenSet[str] = frozenset(
    {
        "Batch_Decal_2",
        "Wood_Decal_2",
        "Honeycomb-2",
        "Honeycomb_2",
        "Decal_2",
        "Decal-2",
        "Slot_2",
        "Slot2",
    }
)

# When the named matches above produce zero updates, fall back to positional
# matching (first decal -> slot 1, second decal -> slot 2). Disable to force
# strict naming.
DECAL_POSITIONAL_FALLBACK: bool = True

# Strict per-name control: only SLOT1/SLOT2 appearance names are updated.
# Do not push slot 1 into every textured appearance (foam, Pine, steel, etc.).
APPEARANCE_BROADCAST: bool = False

# When True, every body in the model is force-assigned to a single "carrier"
# appearance whose texture is swapped per color set. This gives a real
# 100% coverage even when the template's appearances are procedural /
# read-only / refuse changeTextureImage. The original body->appearance
# mapping is restored at the end of the batch. Set to False if you want
# the original mixed materials preserved (some bodies will not change).
FORCE_BODY_COVERAGE: bool = True

# When True, batch decals (chain faces + Scale Plane XY) texture visible tread
# bodies even when a library carrier is available. Keeps manual-decal-like
# placement instead of blanketing the whole assembly with one carrier UV.
BODY_COVERAGE_VIA_DECALS: bool = True

# When True, every face of every body gets its own decal so the image wraps
# the WHOLE model — top, sides, ends, curved nose, etc. When False (default),
# only the largest planar face per body receives a decal.
#
# Set this to False unless you really need 100+ decals: each color-set change
# rewrites the imageFilename of every active decal, and on this build that
# triggers a synchronous re-projection + viewport refresh per decal, which is
# what made a 6-image batch take >1 hour. With one decal per body (~14
# decals on the stair tread template) the same batch finishes in minutes,
# and the bottom-face/inner-face decals were never visible to the camera
# anyway so the rendered output looks the same or better.
BATCH_DECAL_ALL_FACES: bool = False

# All decals this pipeline creates are named with this prefix so we can locate
# and delete them at end-of-batch without touching decals authored in the .f3d.
BATCH_DECAL_NAME_PREFIX: str = "LifeproofBatchDecal_"

# Multiplier applied when computing uniform Scale Plane XY (see
# ``BATCH_DECAL_SCALE_COVER_FACTOR``). Legacy name kept for docs only.
DECAL_OVERSIZE_FACTOR: float = 2

# When True, decals are TILED across the face's longest direction in a grid
# of step-sized cells, instead of one decal centered on the face. This is
# the workaround for Fusion builds that ignore every decal-sizing API:
# place many small default-sized decals next to each other and let them
# collectively cover the face.
# NOTE: ignored when ``BATCH_DECAL_CHAIN_FACES`` is True — chain-wrapped
# decals must be one per face, not a tile grid.
BATCH_DECAL_TILE: bool = False

# Grid step in cm for tiling. Each decal is a ~5 cm patch; with image
# slicing OFF every tile shows the full swatch. 4 cm is denser than 5 cm and
# reduces bare slivers on curved/trimmed faces (square nose, wear layer).
BATCH_DECAL_TILE_STEP_CM: float = 4.0

# Hard cap on tile decals per single face. A large plank/tread show face
# needs a full grid (a ~120 × 15 cm face is ~30 × 4 = ~120 patches at 4 cm)
# or it only partially covers. Set high enough for full single-face cover.
BATCH_DECAL_MAX_TILES_PER_FACE: int = 120

# Hard cap on total batch decals across the WHOLE model — the anti-freeze
# safety valve (each decal = 1 create + N color-set swaps + 1 teardown, all
# synchronous re-projections). This is THE speed↔coverage knob:
#   * lower (e.g. 80)  → faster, but long parts may show bare gaps;
#   * higher (e.g. 800)→ fuller coverage, slower (can freeze on weak PCs).
# 500 + largest-face-first + gap-fill retries: better automatic coverage on
# unknown client models without per-.f3d appearance prep.
BATCH_DECAL_MAX_TOTAL: int = 20

# When a tile placement fails (off-face), try alternate on-surface anchors
# before counting a skip. Helps curved noses and chamfered tread edges.
BATCH_DECAL_CREATE_RETRIES: int = 5

# After the main tile grid on a face, place extra single decals at unused
# anchor points when any tile failed — fills holes without manual model prep.
BATCH_DECAL_GAP_FILL_ON_FAIL: bool = True
BATCH_DECAL_GAP_FILL_MAX_PER_FACE: int = 12

# Faces smaller than this (cm² of bounding-box area) are skipped entirely —
# they are slivers/fillets that are not meaningfully visible and only burn
# the decal budget.
BATCH_DECAL_MIN_FACE_AREA_CM2: float = 1.5

# Skip faces whose normal points clearly downward — the render camera is a
# top angle, so plank/foam-pad/nose UNDERSIDES are never seen. Decaling them
# wastes budget that should fill the visible top (which was showing light
# patches where its decals ran out). Threshold = z component of the unit
# normal: -1 straight down, 0 horizontal, +1 up. -0.25 keeps top + sides +
# forward-curving nose, drops only clearly-downward faces.
BATCH_DECAL_SKIP_DOWN_FACING: bool = True
BATCH_DECAL_DOWN_FACE_THRESHOLD: float = -0.25

# Faces whose longest side is <= this (cm) get exactly ONE decal instead of
# a grid: a single ~5 cm patch already covers them (end caps, short returns),
# so gridding them just wastes the budget and freezes the UI.
BATCH_DECAL_SINGLE_DECAL_MAX_CM: float = 7.0

# Skip strongly curved faces (e.g. the rounded nose-front return). Decals
# project flat, so tiling a tight curve smears the image into streaks. With
# this on, such faces get NO decals and the wood-tone neutral shows there
# instead — a clean undertone reads far better than smeared texture. The
# threshold is the max angle (degrees) the face normal may swing across the
# face before it's treated as "too curved"; higher = more tolerant (fewer
# faces skipped). 55° keeps gentle chamfers textured, drops tight rounds.
BATCH_DECAL_SKIP_CURVED_FACES: bool = False
BATCH_DECAL_CURVED_FACE_MAX_DEG: float = 55.0

# When True, each tile decal shows only its own (iu, iv) cell of the source
# image, so the tiles on a face reassemble into ONE big copy of the swatch.
# That reconstruction is fragile: any face that is curved, trimmed, or only
# partially covered (off-face skips) ends up showing a scrambled / half-
# missing photo — the "messy / wrong color / half textured" results on the
# moulding parts. When False (default) every tile shows the FULL image, so
# the part reads as a clean, uniform repeating texture (exactly how real
# flooring / trim renders look) and partial coverage degrades gracefully
# instead of looking broken. Set True only for flat slab-like models where
# you specifically want one giant copy of the swatch stretched across.
BATCH_DECAL_TILE_SLICE_IMAGE: bool = False

# Pump Fusion's UI message loop every N decal operations so the application
# stays responsive. Tighter = smoother UI, marginally more overhead.
BATCH_DECAL_UI_PUMP_INTERVAL: int = 8

# Fusion DECAL dialog "Z Angle" — used on flat/chain-wrapped faces (End Cap strips).
BATCH_DECAL_Z_ANGLE_DEG: float = 90.0

# Z angle on curved tops when chain faces is off (align-grain handles orientation).
BATCH_DECAL_Z_ANGLE_CURVED_DEG: float = 0.0

# Re-apply cached transform after each imageFilename swap (Fusion may reset UV).
# Ignored when BATCH_DECAL_RECREATE_ON_COLOR_SWAP is True.
BATCH_DECAL_REAPPLY_TRANSFORM_ON_IMAGE_SWAP: bool = True

# Recreate batch decals per color set (full DecalInput orientation + scale).
# Reliable when ``decal.transform`` is read-only after ``add()``.
BATCH_DECAL_RECREATE_ON_COLOR_SWAP: bool = True

# Fusion DECAL dialog "Chain Faces" — wrap the decal onto connected faces of
# the body (top flat + curved nose, etc.). Requires a single primary face in
# ``DecalInput.faces``; defaults to True in recent Fusion API builds.
# When True, tiling is disabled (one scaled decal per face).
# Also enables one chain-wrapped decal per body on the primary show face
# (largest planar top — not end caps).
BATCH_DECAL_CHAIN_FACES: bool = True

# Disabled — chain ON + corrected auto-fit covers connected show faces (End Cap incl.).
BATCH_DECAL_EXTRUSION_PROFILE_TOKENS: Tuple[str, ...] = ()

# Min |n·Y| for extrusion top-face whitelist (flat exterior tops only).
BATCH_DECAL_EXTRUSION_TOP_MIN_Y_DOT: float = 0.95

# Auto-fit: legacy ``2.0 * axis_len`` undershot render ~2× on this Fusion build.
# Use ``1.0 * axis_len`` as default decal footprint for fit (UI=1 ≈ old UI=2).
BATCH_DECAL_FIT_DEFAULT_AXIS_MULTIPLIER: float = 1.0

# Uniform Scale Plane XY via ``DecalInput.transform`` axis magnitudes (same
# effect as the Fusion gizmo / Keep Aspect Ratio — NOT separate width/height).
BATCH_DECAL_USE_SCALE_PLANE_XY: bool = True

# Fusion DECAL dialog **Scale Plane XY** slider — uniform multiplier applied
# after auto-fit to the body bbox in decal-local axes (manual bullnose ~1.8).
# Overridden per batch from the plugin UI when set.
BATCH_DECAL_SCALE_PLANE_XY: float = 1

# Set by controller from UI; None = use ``BATCH_DECAL_SCALE_PLANE_XY`` above.
_RUNTIME_DECAL_SCALE_PLANE_XY: Optional[float] = None


def get_decal_scale_plane_xy() -> float:
    if _RUNTIME_DECAL_SCALE_PLANE_XY is not None:
        return float(_RUNTIME_DECAL_SCALE_PLANE_XY)
    return float(BATCH_DECAL_SCALE_PLANE_XY)


def set_runtime_decal_scale_plane_xy(value: Optional[float]) -> None:
    global _RUNTIME_DECAL_SCALE_PLANE_XY
    _RUNTIME_DECAL_SCALE_PLANE_XY = value

# Margin on auto-fit before the UI multiplier (1.0 = none).
BATCH_DECAL_SCALE_AUTO_FIT_MARGIN: float = 1.0

# Skip end-cap / cut faces at the start & end of long bodies (normal parallel
# to the body's longest axis). Show faces (top, nose, back, sides) are kept.
BATCH_DECAL_SKIP_END_CAP_FACES: bool = True
BATCH_DECAL_END_CAP_NORMAL_DOT: float = 0.82

# Skip long vertical side faces (tread front/back, width-facing bands). Uses
# world Z-up (Fusion default) plus body-local bbox heuristics.
BATCH_DECAL_SKIP_VERTICAL_SIDES: bool = True
BATCH_DECAL_VERTICAL_SIDE_LENGTH_DOT: float = 0.35
BATCH_DECAL_VERTICAL_SIDE_THICKNESS_DOT: float = 0.55
BATCH_DECAL_SKIP_WORLD_VERTICAL: bool = True
BATCH_DECAL_WORLD_VERTICAL_Z_DOT: float = 0.35

# Primary chain-decal anchor: tread walking surface (+Y or +Z in this template).
BATCH_DECAL_PRIMARY_USE_WORLD_UP: bool = True
BATCH_DECAL_PRIMARY_MIN_WORLD_UP_DOT: float = 0.65
# Nearly flat tread (rejects 45° chamfers). Relaxed 0.65; strict tier uses 0.85.
BATCH_DECAL_PRIMARY_MIN_FLAT_Z_DOT: float = 0.65
BATCH_DECAL_PRIMARY_STRICT_SHOW_DOT: float = 0.85
# Never anchor chain decals on sliver/chamfer faces.
BATCH_DECAL_PRIMARY_MIN_FACE_AREA_CM2: float = 100.0
# Plank/Nose tread tops face ±Y in this assembly (Y-up show surfaces).
BATCH_DECAL_PRIMARY_ALLOW_Y_SHOW: bool = True

# When True, rotate decal local X to body length. Curved tops: before Z=0.
# Flat chain-wrapped tops: after Z=90 so orientation works for all body poses.
BATCH_DECAL_ALIGN_GRAIN_TO_LENGTH: bool = True

# False = align local X (Fusion grain / image U); True = align local Y (Height).
BATCH_DECAL_ALIGN_GRAIN_USE_Y_AXIS: bool = False

# Probe template decals for logging; chain/Z inherit flags below are separate.
BATCH_DECAL_INHERIT_TEMPLATE_DECAL: bool = True

# Do not copy template Chain Faces — geometry/curvature decides (template uses multi-face).
BATCH_DECAL_INHERIT_TEMPLATE_CHAIN: bool = False

# Do not copy template Z — flat+chain uses BATCH_DECAL_Z_ANGLE_DEG instead.
BATCH_DECAL_INHERIT_TEMPLATE_Z_ANGLE: bool = False

# When no template hint: disable chain faces on curved show faces (arched 3-in-1 tops).
BATCH_DECAL_CHAIN_FACES_CURVED_MAX_DEG: float = 25.0

# Keep batch decals after run so you can inspect EDIT DECAL in Fusion (debug).
SKIP_BATCH_DECAL_CLEANUP: bool = False

# When chain-faces scaling, floor required span to the body bbox's two largest axes.
BATCH_DECAL_SCALE_USE_BBOX_FLOOR: bool = True

# Panoramic color-set images (e.g. 2500×685): max aspect boost on auto-fit scale.
BATCH_DECAL_PANORAMIC_SCALE_MAX: float = 6.0

# Legacy alias — use BATCH_DECAL_SCALE_PLANE_XY instead.
BATCH_DECAL_SCALE_COVER_FACTOR: float = 1.8

# When chain-faces mode is on, place one decal on the largest show body per
# occurrence (Plank, Nose, …) instead of every sub-body (foam, wear, paint).
BATCH_DECAL_ONE_PER_OCCURRENCE: bool = True

# Body / occurrence name tokens that are never primary show surfaces.
BATCH_DECAL_BODY_SKIP_KEYWORDS: Tuple[str, ...] = (
    "foam",
    "pad",
    "substrate",
    "light",
    "lcd",
    "main light",
    "paint",
)

# Appearance-name fragments that must never receive wood swatches (broadcast or
# neutralization). Foam/substrate stay white/grey in renders and in the .f3d.
PROTECTED_SUBSTRATE_APPEARANCE_FRAGMENTS: Tuple[str, ...] = (
    "foam",
    "substrate",
)

# Prefer these exact body names (Browser) over area / appearance heuristics.
BATCH_DECAL_BODY_PREFER_NAMES: Tuple[str, ...] = ("Body1",)
BATCH_DECAL_BODY_PREFER_NAME_RANK: float = 100.0

# Hard map: occurrence label → body name (Option B — no scoring guesswork).
# Keys match Fusion occurrence names, e.g. ``Plank:1``, ``Nose1.125:1``.
BATCH_DECAL_OCCURRENCE_BODY: Dict[str, str] = {
    "Plank:1": "Body1",
    "Nose1.125:1": "Body1",
}

# Square Nose / similar: tread nose bodies live at document root (not Nose1.125:1).
# Exact name match only; try in order until decal create succeeds.
BATCH_DECAL_ROOT_ANCHOR_BODIES: Tuple[str, ...] = (
    "Nose Wear Layer",
    "Nose",
)

# Match main show body from .f3d filename (e.g. ``End Cap - Decal.f3d`` → ``End Cap:1``).
BATCH_DECAL_MAIN_BODY_FROM_FILENAME: bool = True

# Delete pre-existing decals on the main-body component before batch placement.
BATCH_DECAL_REMOVE_EXISTING_ON_MAIN: bool = True

# Occurrence name tokens that never receive batch decals (Track keeps template decal).
BATCH_DECAL_OCCURRENCE_SKIP_KEYWORDS: Tuple[str, ...] = (
    "track",
    "rail",
    "foam pad",
    "main light",
)

# Prefer wear-layer bodies (steel shell) when no name / occurrence rule applies.
BATCH_DECAL_BODY_PREFER_KEYWORDS: Tuple[str, ...] = (
    "steel",
    "satin",
    "striped",
)

# When True, the plugin also rewrites ``imageFilename`` on every user-
# authored decal in the .f3d (anything not named ``LifeproofBatchDecal_*``).
# Disable when you have many user-authored decals (e.g. 15+ on a single
# template) — they bloat the per-color-set swap loop and aren't necessary
# once the tile coverage is good. Re-enable if you intentionally maintain
# hand-placed decals you want auto-swapped.
UPDATE_USER_AUTHORED_DECALS: bool = False

# Library appearance keywords used to source a carrier when nothing in the
# active design accepts texture updates. Order matters: items earlier in the
# tuple are tried before later ones, so wood-flavoured raster appearances
# are preferred to abstract patterns.
CARRIER_LIBRARY_KEYWORDS: Tuple[str, ...] = (
    "wood",
    "hardwood",
    "flooring",
    "fabric",
    "carpet",
    "stone",
    "tile",
    "ceramic",
    "masonry",
    "wall covering",
    "ground",
    # Raster-friendly stock packs often use these tokens:
    "plastic",
    "vinyl",
    "laminat",
    "laminate",
    "leather",
    "paint",
    "rubber",
)

# When probing ``materialLibraries`` / ``appearanceLibraries``, prefer containers
# that ship with Fusion (Favorites, Fusion 360 library, …) before huge third-party
# PBR libraries, so we do not burn the probe budget on assets that never accept
# ``changeTextureImage`` on the design copy.
CARRIER_LIBRARY_CONTAINER_PRIORITY: Tuple[str, ...] = (
    "favorites",
    "fusion 360",
    "fusion360",
    "autodesk",
    "appearance",
    "wood",
    "floor",
    "ceramic",
    "wall",
    "sample",
    "legacy",
)

# Display name of the carrier appearance once copied into the design. Kept
# constant so we can clean it up between batch runs without leaking dozens
# of copies.
CARRIER_APPEARANCE_NAME: str = "LifeproofBatchCarrier"

# Library appearances to try as carrier sources. We no longer require
# ``_appearance_has_texture_slot`` on the *library* object — many Fusion
# builds report false even when ``addByCopy`` + ``changeTextureImage``
# works on the *design* copy (that mismatch produced ``tried 0`` in logs).
#
# ``0`` = probe up to ``MAX_LIBRARY_CARRIER_HARD_CAP`` entries (sorted:
# Fusion/Favorites libraries first, then keyword-ranked appearance names).
# Set a positive integer to stop earlier on enormous libraries.
MAX_LIBRARY_CARRIER_PROBES: int = 0
MAX_LIBRARY_CARRIER_HARD_CAP: int = 25000

# When True, after the batch finishes, every body's original appearance is
# restored, returning the document to its pre-render look. When False, the
# carrier appearance stays applied and you see the textured result live in
# the Fusion viewport (good for debugging "did the plank actually change").
RESTORE_BODY_APPEARANCES_ON_FINISH: bool = True

# When True, ``hide:`` / ``show:`` rules in component/body descriptions are
# applied whenever a named view is rendered. When False, visibility stays
# exactly as in the .f3d right after open — no extra bodies turn on for a
# view. The add-in never creates new geometry; unexpected solids are from
# the model file or from these visibility rules.
APPLY_NAMED_VIEW_VISIBILITY: bool = False

# With force-coverage + decal mode, *_1 was previously pushed into both the
# carrier appearance (whole part) and the decal (one face only). That looks
# like a rectangular "patch" that does not cover the full surface. When True,
# *_1 updates only the carrier; decals receive *_2 only (second image /
# labels). Set False if you intentionally want the main color on a decal.
CARRIER_SUPPRESS_DECAL_SLOT1: bool = True

# Carrier appearance: absolute U/V offsets after each slot-1 image swap (Image branch knobs in Fusion).
# Leave at 0 when using CARRIER_TEXTURE_IMAGE_SHIFT_PX (bitmap roll is more reliable across Fusion builds).
CARRIER_TEXTURE_U_OFFSET: float = 0.0
CARRIER_TEXTURE_V_OFFSET: float = 0.0

# Roll slot-1 raster horizontally before ``changeTextureImage`` on the carrier (main wrap on long trims).
# Positive shifts content right in image space (often moves the visible band along the part). Requires Pillow:
# Fusion → Tools → Add-Ins → Scripts → ``python -m pip install pillow`` in Fusion's Python if needed.
CARRIER_TEXTURE_IMAGE_SHIFT_PX: int = 120

# Decal origin nudge in model cm if ``decal.transform`` is read/write on your Fusion build.
DECAL_TEXTURE_ORIGIN_OFFSET_X_CM: float = 0.0
DECAL_TEXTURE_ORIGIN_OFFSET_Y_CM: float = 0.0

# Decal image: same horizontal roll for decal slot rasters (usually *_2 when CARRIER_SUPPRESS_DECAL_SLOT1).
DECAL_TEXTURE_IMAGE_SHIFT_PX: int = 0

_decal_shift_temp_paths: List[str] = []
_decal_shift_pil_warning_emitted: bool = False

# Maps id(decal) -> (ix, iy, nx, ny) so we can slice each color-set image
# the same way when updating per-decal imageFilename. Populated by the tile
# loop in create_batch_decals_for_all_bodies; cleared by cleanup_batch_decals.
_TILE_METADATA: dict = {}

# Cached Decal.transform after placement — reapplied when imageFilename changes.
_DECAL_TRANSFORM_CACHE: dict = {}

# Placement record for recreate-on-swap (face, body, chain, template hint, …).
_DECAL_PLACEMENT_CACHE: dict = {}

# Maps id(decal) -> appearance slot (1 or 2) of the body the decal sits on,
# derived from that body's ORIGINAL appearance name (Vinyl Skin-1 -> 1,
# Vinyl Skin-2 -> 2). Lets update_batch_decal_images route the _1 image onto
# slot-1 bodies and the _2 image onto slot-2 bodies (e.g. dark front / light
# back) even though every body is textured via decals, not appearances.
# Default when a decal id is absent: slot 1.
_DECAL_SLOT: Dict[int, int] = {}


class TemplateDecalHint:
    """Settings read from a user-authored decal before batch replacement."""

    __slots__ = ("chain_faces", "z_angle_deg", "name")

    def __init__(
        self,
        chain_faces: Optional[bool] = None,
        z_angle_deg: Optional[float] = None,
        name: str = "",
    ) -> None:
        self.chain_faces = chain_faces
        self.z_angle_deg = z_angle_deg
        self.name = name or ""


class DecalPlacementRecord:
    """Enough state to recreate a batch decal with the same orientation."""

    __slots__ = (
        "decals_collection",
        "face",
        "body",
        "decal_name",
        "chain_faces",
        "template_hint",
        "center_override",
    )

    def __init__(
        self,
        decals_collection: Any,
        face: Any,
        body: Optional[Any],
        decal_name: str,
        chain_faces: Optional[bool],
        template_hint: Optional[TemplateDecalHint],
        center_override: Optional[adsk.core.Point3D] = None,
    ) -> None:
        self.decals_collection = decals_collection
        self.face = face
        self.body = body
        self.decal_name = decal_name
        self.chain_faces = chain_faces
        self.template_hint = template_hint
        self.center_override = center_override

# Temp PNG paths produced by _slice_image_for_tile. Deleted at batch end.
_TILE_TEMP_PATHS: List[str] = []

# 1×1 PNG (base64) used only to discover a library carrier when the client's
# color-set raster (often JPG) is rejected by ``changeTextureImage`` everywhere,
# but PNG is still accepted (observed on some Fusion builds).
_MIN_PROBE_PNG_B64: str = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _win_path(path: Path) -> str:
    """Fusion on Windows accepts normalized paths with backslashes."""
    return os.path.normpath(str(path.resolve()))


def _write_carrier_probe_png() -> Optional[str]:
    """Write a tiny valid PNG to a temp file; used only for carrier discovery."""
    try:
        raw = base64.b64decode(_MIN_PROBE_PNG_B64)
        fd, path = tempfile.mkstemp(suffix=".png", prefix="lifeproof_batch_probe_")
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        return path
    except Exception:
        return None


def _unlink_silent(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except Exception:
        pass


def clear_decal_shift_temp_files() -> None:
    """Call at batch start/end to remove PIL-generated decal shift rasters."""
    global _decal_shift_temp_paths, _decal_shift_pil_warning_emitted
    for p in _decal_shift_temp_paths:
        _unlink_silent(p)
    _decal_shift_temp_paths.clear()
    _decal_shift_pil_warning_emitted = False


def _roll_image_horizontal_to_temp_png(src: Path, dx_px: int) -> Optional[str]:
    global _decal_shift_pil_warning_emitted
    if dx_px == 0:
        return None
    try:
        from PIL import Image # type: ignore
    except ImportError:
        if not _decal_shift_pil_warning_emitted:
            _decal_shift_pil_warning_emitted = True
        return None
    try:
        im = Image.open(str(src)).convert("RGB")
        w, h = im.size
        if w < 2 or h < 1:
            return None
        dx_px = int(dx_px) % w
        if dx_px == 0:
            return None
        out = Image.new("RGB", (w, h))
        out.paste(im.crop((w - dx_px, 0, w, h)), (0, 0))
        out.paste(im.crop((0, 0, w - dx_px, h)), (dx_px, 0))
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="lifeproof_texshift_")
        os.close(fd)
        out.save(tmp, format="PNG")
        _decal_shift_temp_paths.append(tmp)
        return tmp
    except Exception:
        return None


def _shifted_raster_path_or_original(src: Optional[Path], shift_px: int) -> Optional[Path]:
    """Return a temp path with horizontal roll applied, or the original path if shift is 0 / PIL fails."""
    if not src or not src.is_file():
        return None
    px = int(shift_px)
    if not px:
        return src
    tmp = _roll_image_horizontal_to_temp_png(src, px)
    if tmp:
        return Path(tmp)
    return src


def _win_path_for_decal_image(src: Optional[Path]) -> Optional[str]:
    """Return path for Decal.imageFilename, optionally with horizontal bitmap roll."""
    effective = _shifted_raster_path_or_original(src, int(DECAL_TEXTURE_IMAGE_SHIFT_PX))
    if not effective:
        return None
    return _win_path(effective)


def _texture_image_path_candidates(image_path: str) -> List[str]:
    """Fusion builds differ on slash direction; try a small stable set."""
    norm = os.path.normpath(image_path)
    out: List[str] = []
    seen: Set[str] = set()
    for p in (norm, norm.replace("\\", "/"), norm.replace("/", "\\")):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _library_container_rank(container_name: str) -> int:
    ln = (container_name or "").lower()
    for idx, sub in enumerate(CARRIER_LIBRARY_CONTAINER_PRIORITY):
        if sub in ln:
            return idx
    return len(CARRIER_LIBRARY_CONTAINER_PRIORITY)


def _carrier_probe_cap(n_candidates: int) -> int:
    if MAX_LIBRARY_CARRIER_PROBES <= 0:
        return min(n_candidates, MAX_LIBRARY_CARRIER_HARD_CAP)
    return min(n_candidates, MAX_LIBRARY_CARRIER_HARD_CAP, MAX_LIBRARY_CARRIER_PROBES)


def _sorted_carrier_library_candidates(
    app: adsk.core.Application,
) -> Tuple[List[Tuple[str, Any]], int]:
    """Fusion stock libraries first, then keyword-ranked appearance names.

    Returns ``([(label, libAppearance), ...], total_unique_count)``.
    """
    rows: List[Tuple[int, int, int, str, Any]] = []
    seen: Set[Tuple[str, str]] = set()

    for coll_attr, coll_offset in (
        ("materialLibraries", 0),
        ("appearanceLibraries", 50_000_000),
    ):
        coll = getattr(app, coll_attr, None)
        if coll is None:
            continue
        try:
            nlibs = coll.count
        except Exception:
            continue
        for li in range(nlibs):
            try:
                lib = coll.item(li)
                lib_apps = lib.appearances
            except Exception:
                continue
            lib_name = lib.name or ""
            c_rank = _library_container_rank(lib_name)
            try:
                n_apps = lib_apps.count
            except Exception:
                continue
            for ai in range(n_apps):
                try:
                    lib_ap = lib_apps.item(ai)
                except Exception:
                    continue
                ap_name = lib_ap.name or ""
                dedupe = (lib_name.lower(), ap_name.lower())
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                lname = ap_name.lower()
                keyword_idx = -1
                for idx, kw in enumerate(CARRIER_LIBRARY_KEYWORDS):
                    if kw in lname:
                        keyword_idx = idx
                        break
                priority = keyword_idx if keyword_idx >= 0 else len(CARRIER_LIBRARY_KEYWORDS)
                label = "{} / {}".format(lib_name, ap_name)
                stable = coll_offset + li * 500_000 + ai
                rows.append((c_rank, priority, stable, label, lib_ap))

    rows.sort(key=lambda x: (x[0], x[1], x[2]))
    slim = [(r[3], r[4]) for r in rows]
    return slim, len(slim)


_NON_COLOR_BRANCH_HINTS: Tuple[str, ...] = (
    "bump",
    "normal",
    "rough",
    "metal",
    "specular",
    "gloss",
    "displace",
    "height",
    "ao",
    "ambient_occlusion",
    "opacity",
    "transparency",
    "emission",
    "anisotropy",
    "cutout",
    "mask",
)


def _branch_label_is_color(parent_names: Tuple[str, ...]) -> bool:
    """True unless the texture's branch path clearly identifies it as a
    non-visible channel (bump / normal / roughness / metallic / etc.).

    We rely on the procedural-name blacklist (``_is_procedural_carrier_name``)
    to reject Pine / limestone / steel / paint / foam etc. by *appearance*
    name. That leaves this per-slot filter with one job: reject swaps that
    *clearly* landed on a non-color branch. Anything else — including
    properties with unnamed parents or names Fusion doesn't tag with
    "color"/"albedo" — is accepted, because real raster diffuse slots in
    Fusion libraries often have generic names like ``value``, ``Image``,
    ``texture``, or empty strings on some builds.
    """
    if not parent_names:
        return True
    joined = " ".join(parent_names).lower()
    if any(neg in joined for neg in _NON_COLOR_BRANCH_HINTS):
        return False
    return True


def _apply_image_to_appearance_textures_detailed(
    appearance: adsk.fusion.Appearance, image_path: str
) -> Tuple[int, int]:
    """
    Call changeTextureImage on every AppearanceTexture reachable from this
    appearance, at any nesting depth.

    Modern Fusion PBR appearances (the bulk of materialLibraries) expose
    their texture as ``AppearanceTextureProperty.value`` rather than as a
    top-level ``AppearanceTexture`` on ``appearanceProperties`` — a flat
    ``AppearanceTexture.cast(prop)`` over the top level misses them and
    returns 0, which is what made every library probe fail in the field.
    """
    try:
        props = appearance.appearanceProperties
    except Exception:
        return 0, 0
    path_opts = _texture_image_path_candidates(image_path)
    changed = 0
    color_changed = 0
    seen: Set[int] = set()

    def _try_change_on(tex: Any) -> bool:
        method = getattr(tex, "changeTextureImage", None)
        if not callable(method):
            return False
        for cand in path_opts:
            try:
                if method(cand):
                    return True
            except Exception:
                continue
        return False

    def _record(parents: Tuple[str, ...]) -> None:
        nonlocal changed, color_changed
        changed += 1
        if _branch_label_is_color(parents):
            color_changed += 1

    def walk(coll: Any, parents: Tuple[str, ...], depth: int) -> None:
        if coll is None or depth > 28:
            return
        try:
            n = coll.count
        except Exception:
            return
        for i in range(n):
            try:
                p = coll.item(i)
            except Exception:
                continue
            try:
                k = id(p)
                if k in seen:
                    continue
                seen.add(k)
            except Exception:
                pass
            try:
                pname = p.name or ""
            except Exception:
                pname = ""
            child_parents = parents + (pname,) if pname else parents
            try:
                tex = adsk.core.AppearanceTexture.cast(p)
                if tex is not None and _try_change_on(tex):
                    _record(child_parents)
                    continue
            except Exception:
                pass
            try:
                tprop = adsk.core.AppearanceTextureProperty.cast(p)
                if tprop is not None:
                    inner = getattr(tprop, "value", None)
                    if inner is not None and _try_change_on(inner):
                        _record(child_parents)
                    inner_props = getattr(inner, "properties", None) if inner is not None else None
                    if inner_props is not None:
                        walk(inner_props, child_parents, depth + 1)
                    continue
            except Exception:
                pass
            if _try_change_on(p):
                _record(child_parents)
                continue
            try:
                child = getattr(p, "properties", None)
                if child is not None:
                    walk(child, child_parents, depth + 1)
            except Exception:
                pass

    walk(props, (), 0)
    return changed, color_changed


def _apply_image_to_appearance_textures(appearance: adsk.fusion.Appearance, image_path: str) -> int:
    """Backwards-compatible wrapper that returns total slot updates only."""
    total, _ = _apply_image_to_appearance_textures_detailed(appearance, image_path)
    return total


def _is_u_texture_offset_name(name: str, *, bare_ok: bool = False) -> bool:
    n = (name or "").strip().lower()
    if not n or "rotation" in n or "angle" in n:
        return False
    if bare_ok and n == "offset":
        return True
    if re.search(r"\bu[\s_-]*offset\b", n) or re.search(r"\boffset[\s_-]*u\b", n):
        return True
    if "horizontal" in n and "offset" in n:
        return True
    return False


def _is_v_texture_offset_name(name: str, *, bare_ok: bool = False) -> bool:
    n = (name or "").strip().lower()
    if not n or "rotation" in n or "angle" in n:
        return False
    if bare_ok and n in ("offset y", "offsety", "y offset"):
        return True
    if re.search(r"\bv[\s_-]*offset\b", n) or re.search(r"\boffset[\s_-]*v\b", n):
        return True
    if "vertical" in n and "offset" in n:
        return True
    return False


def _appearance_texture_child_property_lists(prop: Any) -> List[Any]:
    subs: List[Any] = []
    seen: Set[int] = set()

    def add(ch: Any) -> None:
        if ch is None:
            return
        try:
            k = id(ch)
            if k in seen:
                return
            seen.add(k)
        except Exception:
            pass
        subs.append(ch)

    try:
        atp = adsk.core.AppearanceTextureProperty.cast(prop)
        if atp is not None and atp.value is not None:
            ch = getattr(atp.value, "properties", None)
            add(ch)
    except Exception:
        pass
    try:
        tex = adsk.core.AppearanceTexture.cast(prop)
        if tex is not None:
            ch = getattr(tex, "properties", None)
            add(ch)
    except Exception:
        pass
    return subs


def _apply_texture_uv_offsets_to_appearance(
    appearance: adsk.fusion.Appearance,
    u_target: float,
    v_target: float,
) -> Tuple[int, List[str]]:
    lines: List[str] = []
    if abs(u_target) < 1e-12 and abs(v_target) < 1e-12:
        return 0, lines
    n_ok = 0

    def walk(props: Any, in_texture_branch: bool, depth: int) -> None:
        nonlocal n_ok
        if props is None or depth > 28:
            return
        try:
            n = props.count
        except Exception:
            return
        for i in range(n):
            try:
                p = props.item(i)
            except Exception:
                continue
            label = p.name or ""
            fp = adsk.core.FloatProperty.cast(p)
            if fp is not None:
                try:
                    if _is_u_texture_offset_name(label, bare_ok=in_texture_branch):
                        fp.value = float(u_target)
                        n_ok += 1
                    elif _is_v_texture_offset_name(label, bare_ok=in_texture_branch):
                        fp.value = float(v_target)
                        n_ok += 1
                except Exception as ex:
                    lines.append('UV "{}": {}'.format(label, ex))
            for sub in _appearance_texture_child_property_lists(p):
                walk(sub, True, depth + 1)

    try:
        walk(appearance.appearanceProperties, False, 0)
    except Exception as ex:
        lines.append("Carrier UV walk failed: {}".format(ex))
    if n_ok == 0 and not lines and (abs(u_target) > 1e-12 or abs(v_target) > 1e-12):
        lines.append(
            "Carrier UV: no matching U/V offset floats — set CARRIER_TEXTURE_U_OFFSET/V to 0 "
            "or edit offsets in the template"
        )
    return n_ok, lines


def apply_appearance_color_set(
    design: adsk.fusion.Design,
    slot1: Optional[Path],
    slot2: Optional[Path],
) -> Tuple[int, List[str]]:
    """
    For each document appearance whose name is in SLOT*_APPEARANCE_NAMES,
    push the corresponding raster path into texture slots.
    Returns (total_texture_updates, log_lines).
    """
    lines: List[str] = []
    total = 0
    apps = design.appearances
    for i in range(apps.count):
        ap = apps.item(i)
        target_path: Optional[str] = None
        slot = appearance_name_slot(ap.name)
        if slot == 1 and slot1:
            target_path = _win_path(slot1)
        elif slot == 2 and slot2:
            target_path = _win_path(slot2)
        if not target_path:
            continue
        n = _apply_image_to_appearance_textures(ap, target_path)
        total += n
        lines.append('Appearance "{}": {} texture(s) updated'.format(ap.name, n))
    return total, lines


def _nudge_decal_texture_origin(decal: "adsk.fusion.Decal") -> Optional[str]:
    ox = float(DECAL_TEXTURE_ORIGIN_OFFSET_X_CM)
    oy = float(DECAL_TEXTURE_ORIGIN_OFFSET_Y_CM)
    if abs(ox) < 1e-12 and abs(oy) < 1e-12:
        return None
    m = getattr(decal, "transform", None)
    if m is None:
        return None
    try:
        origin, x_axis, y_axis, z_axis = m.getAsCoordinateSystem()
    except Exception as ex:
        return str(ex)

    def _unit(v: adsk.core.Vector3D) -> Optional[Tuple[float, float, float]]:
        L = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
        if L < 1e-12:
            return None
        return (v.x / L, v.y / L, v.z / L)

    try:
        u_x = _unit(x_axis)
        u_y = _unit(y_axis)
        if u_x is None or u_y is None:
            return "degenerate decal axes"
        nx = u_x[0] * ox + u_y[0] * oy
        ny = u_x[1] * ox + u_y[1] * oy
        nz = u_x[2] * ox + u_y[2] * oy
        new_origin = adsk.core.Point3D.create(origin.x + nx, origin.y + ny, origin.z + nz)
        new_m = adsk.core.Matrix3D.create()
        new_m.setToAlignCoordinateSystems(new_origin, x_axis, y_axis, z_axis)
        decal.transform = new_m
    except Exception as ex:
        return str(ex)
    return None


def _set_decal_image(decal: "adsk.fusion.Decal", image_path: str) -> Tuple[bool, str]:
    try:
        decal.imageFilename = image_path
        return True, ""
    except Exception as ex:
        return False, str(ex)


def _collect_all_decals(design: adsk.fusion.Design) -> List[adsk.fusion.Decal]:
    """Return every Decal in the design (root + every sub-component), deduped.

    Decals can live on the root component **or inside any nested component**
    (e.g. ``Plank:1 → Decals``). Walking only ``root.decals`` would miss those
    and leave their ``imageFilename`` untouched, so the render keeps the
    original placeholder texture on the affected surface.
    """
    decals: List[adsk.fusion.Decal] = []
    seen: set = set()

    def _drain(comp: adsk.fusion.Component) -> None:
        try:
            key = comp.id
        except Exception:
            key = id(comp)
        if key in seen:
            return
        seen.add(key)
        try:
            comp_decals = comp.decals
        except Exception:
            return
        try:
            n = comp_decals.count
        except Exception:
            return
        for j in range(n):
            try:
                decals.append(comp_decals.item(j))
            except Exception:
                continue

    try:
        _drain(design.rootComponent)
    except Exception:
        return decals
    try:
        occs = design.rootComponent.allOccurrences
    except Exception:
        return decals
    for i in range(occs.count):
        try:
            comp = occs.item(i).component
        except Exception:
            continue
        _drain(comp)
    return decals


def _entity_is_visible(entity: Any, *, default: bool = True) -> bool:
    """Best-effort "is this shown in the document right now".

    Prefers the resolved ``isVisible`` (accounts for parent occurrences),
    then falls back to the ``isLightBulbOn`` toggle. Returns ``default`` if
    neither property is queryable, so we never wrongly drop geometry on
    Fusion builds that don't expose them.
    """
    for attr in ("isVisible", "isLightBulbOn"):
        try:
            v = getattr(entity, attr)
        except Exception:
            continue
        if v is None:
            continue
        return bool(v)
    return default


def _face_area(face: adsk.fusion.BRepFace) -> float:
    try:
        a = face.area
        if a is not None:
            return float(a)
    except Exception:
        pass
    try:
        bbox = face.boundingBox
        if bbox is None:
            return 0.0
        dx = bbox.maxPoint.x - bbox.minPoint.x
        dy = bbox.maxPoint.y - bbox.minPoint.y
        dz = bbox.maxPoint.z - bbox.minPoint.z
        sides = sorted((abs(dx), abs(dy), abs(dz)), reverse=True)
        return sides[0] * sides[1]
    except Exception:
        return 0.0


def _face_up_score(face: adsk.fusion.BRepFace) -> float:
    """Z component of the face's unit normal at its center: +1 up, 0 sideways,
    -1 down. Used to skip undersides the top camera can't see. Returns 0.0
    (neutral, not skipped) when the normal can't be computed.
    """
    try:
        ev = face.evaluator
        if ev is None:
            return 0.0
        ok, prange = ev.parametricRange()
        if not ok or prange is None:
            return 0.0
        u = (prange.minPoint.x + prange.maxPoint.x) / 2.0
        v = (prange.minPoint.y + prange.maxPoint.y) / 2.0
        ok2, n = ev.getNormalAtParameter(adsk.core.Point2D.create(u, v))
        if not ok2 or n is None:
            return 0.0
        mag = math.sqrt(n.x * n.x + n.y * n.y + n.z * n.z)
        if mag <= 1e-9:
            return 0.0
        return n.z / mag
    except Exception:
        return 0.0


def _face_curvature_spread_deg(face: adsk.fusion.BRepFace) -> float:
    """Max angle (degrees) the surface normal swings across the face.

    Planar faces return ~0. Tightly rounded faces (the nose-front return)
    return a large angle. Decals project flat, so a high spread means tiling
    the face will smear the image — callers skip such faces and let the
    neutral backdrop show instead. Returns 0.0 when it can't be computed
    (treated as flat / safe to texture).
    """
    try:
        if isinstance(face.geometry, adsk.core.Plane):
            return 0.0
    except Exception:
        pass
    try:
        ev = face.evaluator
        if ev is None:
            return 0.0
        ok, prange = ev.parametricRange()
        if not ok or prange is None:
            return 0.0
        u0, u1 = prange.minPoint.x, prange.maxPoint.x
        v0, v1 = prange.minPoint.y, prange.maxPoint.y
        if u1 <= u0 or v1 <= v0:
            return 0.0
        normals = []
        for fu in (0.1, 0.5, 0.9):
            for fv in (0.1, 0.5, 0.9):
                u = u0 + fu * (u1 - u0)
                v = v0 + fv * (v1 - v0)
                p2 = adsk.core.Point2D.create(u, v)
                try:
                    on = ev.isParameterOnFace(p2)
                except Exception:
                    on = True
                if not on:
                    continue
                ok2, n = ev.getNormalAtParameter(p2)
                if not ok2 or n is None:
                    continue
                mag = math.sqrt(n.x * n.x + n.y * n.y + n.z * n.z)
                if mag <= 1e-9:
                    continue
                normals.append((n.x / mag, n.y / mag, n.z / mag))
        max_deg = 0.0
        for i in range(len(normals)):
            for j in range(i + 1, len(normals)):
                dot = (
                    normals[i][0] * normals[j][0]
                    + normals[i][1] * normals[j][1]
                    + normals[i][2] * normals[j][2]
                )
                dot = max(-1.0, min(1.0, dot))
                deg = math.degrees(math.acos(dot))
                if deg > max_deg:
                    max_deg = deg
        return max_deg
    except Exception:
        return 0.0


def _largest_planar_face(body: adsk.fusion.BRepBody) -> Optional[adsk.fusion.BRepFace]:
    """Pick the largest planar face. Falls back to the largest face overall if
    no planar face exists (some bodies are entirely cylindrical / spline)."""
    best_planar: Optional[adsk.fusion.BRepFace] = None
    best_planar_area = 0.0
    best_any: Optional[adsk.fusion.BRepFace] = None
    best_any_area = 0.0
    try:
        faces = body.faces
        n = faces.count
    except Exception:
        return None
    for i in range(n):
        try:
            f = faces.item(i)
        except Exception:
            continue
        area = _face_area(f)
        if area > best_any_area:
            best_any_area = area
            best_any = f
        try:
            is_plane = isinstance(f.geometry, adsk.core.Plane)
        except Exception:
            is_plane = False
        if is_plane and area > best_planar_area:
            best_planar_area = area
            best_planar = f
    return best_planar or best_any


def _vector_length(v: adsk.core.Vector3D) -> float:
    return math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)


def _body_bbox_dims_cm(
    body: adsk.fusion.BRepBody,
) -> Tuple[float, float, float]:
    """Body axis-aligned bounding box extents (cm)."""
    try:
        bbox = body.boundingBox
        if bbox is None:
            return 0.0, 0.0, 0.0
        dx = abs(bbox.maxPoint.x - bbox.minPoint.x)
        dy = abs(bbox.maxPoint.y - bbox.minPoint.y)
        dz = abs(bbox.maxPoint.z - bbox.minPoint.z)
        return dx, dy, dz
    except Exception:
        return 0.0, 0.0, 0.0


def _body_unit_length_axis(
    body: adsk.fusion.BRepBody,
) -> Tuple[float, float, float]:
    """Unit vector along the body's longest bbox axis (plank length)."""
    length, _width, _thickness = _body_bbox_axes(body)
    return length


def _body_bbox_axes(
    body: adsk.fusion.BRepBody,
) -> Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]:
    """Unit vectors along length (longest), width (mid), thickness (shortest) bbox axes."""
    dx, dy, dz = _body_bbox_dims_cm(body)
    ranked = sorted(
        ((dx, (1.0, 0.0, 0.0)), (dy, (0.0, 1.0, 0.0)), (dz, (0.0, 0.0, 1.0))),
        key=lambda t: t[0],
        reverse=True,
    )
    return ranked[0][1], ranked[1][1], ranked[2][1]


def _vec3_dot(
    a: Tuple[float, float, float], b: Tuple[float, float, float]
) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vec3_cross(
    a: Tuple[float, float, float], b: Tuple[float, float, float]
) -> Tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _vec3_normalize(
    v: Tuple[float, float, float],
) -> Optional[Tuple[float, float, float]]:
    mag = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if mag <= 1e-9:
        return None
    return (v[0] / mag, v[1] / mag, v[2] / mag)


def _vec3_from_vector3d(v: adsk.core.Vector3D) -> Tuple[float, float, float]:
    return (float(v.x), float(v.y), float(v.z))


def _appearance_snap_name_map(
    snap: Optional[List[Tuple[Any, Any]]],
) -> Dict[int, str]:
    """Map ``id(body)`` → appearance name from a pre-neutralization snapshot."""
    out: Dict[int, str] = {}
    if not snap:
        return out
    for body, ap in snap:
        try:
            out[id(body)] = (ap.name if ap is not None else "") or ""
        except Exception:
            out[id(body)] = ""
    return out


def _body_filter_haystack(
    body: adsk.fusion.BRepBody,
    label: str,
    original_appearance_names: Optional[Dict[int, str]] = None,
) -> str:
    """Name tokens for skip/prefer — uses pre-neutralization appearance when given."""
    try:
        name = (body.name or "").lower()
    except Exception:
        name = ""
    label_l = (label or "").lower()
    ap_name = ""
    if original_appearance_names is not None:
        orig = original_appearance_names.get(id(body))
        if orig:
            ap_name = orig.lower()
    if not ap_name:
        try:
            ap = body.appearance
            ap_name = (ap.name if ap is not None else "").lower()
        except Exception:
            ap_name = ""
    return " ".join((name, label_l, ap_name))


def _normalize_occurrence_name(label: str) -> str:
    """``End Cap:1`` → ``End Cap``."""
    s = (label or "").strip()
    m = re.match(r"^(.+):\d+$", s)
    return (m.group(1) if m else s).strip()


def _main_product_token_from_model_path(model_path: Optional[Path]) -> str:
    """Primary product name from ``End Cap - Decal.f3d`` → ``End Cap``."""
    if model_path is None:
        return ""
    stem = (model_path.stem or "").strip()
    if not stem:
        return ""
    s = stem
    # ``Treads Plus Bullnose - Appearance1`` → ``Treads Plus Bullnose``
    s = re.sub(r"\s*-\s*Appearance\s*\d+\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*-\s*(Decal|Appearance|Appearances)\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+Appearance\s*\d+\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+Decal\s*$", "", s, flags=re.IGNORECASE)
    return s.strip()


def _normalize_product_name_key(name: str) -> str:
    """Lowercase key with spaces/hyphens/underscores collapsed for matching."""
    s = (name or "").strip().lower()
    return re.sub(r"[\s\-_]+", "-", s).strip("-")


def _name_matches_product_token(name: str, token: str) -> bool:
    if not name or not token:
        return False
    n = _normalize_product_name_key(name)
    t = _normalize_product_name_key(token)
    return n == t or n.startswith(t + "-") or n.startswith(t)


def _normalize_fusion_body_name(name: str) -> str:
    """Collapse Fusion browser spacing (incl. NBSP) for name comparisons."""
    s = (name or "").strip().replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s)


def _body_base_name(name: str) -> str:
    """``Body1 (7)`` / ``Body1(7)`` / ``body1`` → ``Body1``."""
    s = _normalize_fusion_body_name(name)
    if not s:
        return ""
    m = re.match(r"^(.+?)\s*\(\s*\d+\s*\)\s*$", s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m2 = re.match(r"^(.+?)\(\s*\d+\s*\)\s*$", s, re.IGNORECASE)
    if m2:
        return m2.group(1).strip()
    return s


def _body_name_matches_map_anchor(name: str, anchor: str) -> bool:
    """Case-insensitive ``Body1`` ≡ ``Body1 (7)`` ≡ ``Body1(7)``."""
    if not name or not anchor:
        return False
    return _body_base_name(name).lower() == _body_base_name(anchor).lower()


def _body_name_exact_matches(name: str, expected: str) -> bool:
    """Exact body name match (ignores Fusion ``Body1 (1)`` copy suffix)."""
    return _body_name_matches_map_anchor(name, expected)


def _body_name_matches_forced(
    body: adsk.fusion.BRepBody,
    expected: str,
    *,
    exact: bool,
) -> bool:
    try:
        actual = (body.name or "").strip()
    except Exception:
        actual = ""
    if exact:
        return _body_name_exact_matches(actual, expected)
    return _name_matches_product_token(actual, expected)


def _find_body_matching_token(
    comp: adsk.fusion.Component,
    main_token: str,
) -> Optional[str]:
    """First body on ``comp`` whose name matches the filename product token."""
    if not main_token:
        return None
    try:
        bodies = comp.bRepBodies
        n = bodies.count
    except Exception:
        return None
    for bi in range(n):
        try:
            body = bodies.item(bi)
            bname = body.name or ""
        except Exception:
            continue
        if _name_matches_product_token(bname, main_token):
            return bname
    return None


def _main_token_is_treads_plus_assembly(main_token: str) -> bool:
    key = _normalize_product_name_key(main_token)
    return bool(key) and "treads-plus" in key


def _body_matches_root_anchor(name: str) -> bool:
    for anchor in BATCH_DECAL_ROOT_ANCHOR_BODIES:
        if _body_name_exact_matches(name, anchor):
            return True
    return False


def _find_body_by_exact_anchor_name(
    comp: adsk.fusion.Component,
    anchor: str,
) -> Optional[adsk.fusion.BRepBody]:
    if not anchor:
        return None
    try:
        bodies = comp.bRepBodies
        n = bodies.count
    except Exception:
        return None
    for bi in range(n):
        try:
            body = bodies.item(bi)
            bname = body.name or ""
        except Exception:
            continue
        if not _body_name_exact_matches(bname, anchor):
            continue
        if _body_hide_for_batch(body, include_face_uv_pins=False):
            continue
        if not _entity_is_visible(body, default=True):
            continue
        return body
    return None


def _find_body_by_anchor_names(
    comp: adsk.fusion.Component,
    anchor_names: Tuple[str, ...],
) -> Optional[str]:
    """First visible anchor body on ``comp`` (exact name match)."""
    for anchor in anchor_names:
        body = _find_body_by_exact_anchor_name(comp, anchor)
        if body is not None:
            try:
                return body.name or anchor
            except Exception:
                return anchor
    return None


def _resolve_root_batch_anchor_body(
    comp: adsk.fusion.Component,
    main_token: str,
) -> Optional[str]:
    """Root anchor: filename body match (L-End Cap) or tread nose body names."""
    matched = _find_body_matching_token(comp, main_token)
    if matched:
        return matched
    if _main_token_is_treads_plus_assembly(main_token):
        return _find_body_by_anchor_names(comp, BATCH_DECAL_ROOT_ANCHOR_BODIES)
    return None


def _occurrence_forced_body_name_from_map(occurrence_label: str) -> Optional[str]:
    """Explicit multi-part map (Bullnose Plank / Nose)."""
    if not BATCH_DECAL_OCCURRENCE_BODY:
        return None
    label = (occurrence_label or "").strip()
    if not label:
        return None
    direct = BATCH_DECAL_OCCURRENCE_BODY.get(label)
    if direct:
        return direct
    label_l = label.lower()
    for key, body_name in BATCH_DECAL_OCCURRENCE_BODY.items():
        if key.lower() == label_l:
            return body_name
    return None


def _occurrence_label_in_explicit_map(occurrence_label: str) -> bool:
    return _occurrence_forced_body_name_from_map(occurrence_label) is not None


def _occurrence_is_batch_target(
    occurrence_label: str,
    main_token: str,
    comp: Optional[adsk.fusion.Component] = None,
) -> bool:
    """True when this occurrence should get the batch color-set decal."""
    label = (occurrence_label or "").strip()
    if not label:
        return False
    if label == "(root)":
        if not BATCH_DECAL_MAIN_BODY_FROM_FILENAME or not main_token or comp is None:
            return False
        return _resolve_root_batch_anchor_body(comp, main_token) is not None
    if _occurrence_label_in_explicit_map(label):
        return True
    if not BATCH_DECAL_MAIN_BODY_FROM_FILENAME:
        return True
    if not main_token:
        return True
    occ = _normalize_occurrence_name(label)
    hay = occ.lower()
    for kw in BATCH_DECAL_OCCURRENCE_SKIP_KEYWORDS:
        if kw.lower() in hay:
            return False
    return _name_matches_product_token(occ, main_token)


def _resolve_forced_body_name(
    occurrence_label: str,
    main_token: str,
    comp: adsk.fusion.Component,
) -> Optional[str]:
    """Pinned body for chain decal — map, filename token, or body name scan."""
    mapped = _occurrence_forced_body_name_from_map(occurrence_label)
    if mapped:
        return mapped
    label = (occurrence_label or "").strip()
    if label == "(root)" and BATCH_DECAL_MAIN_BODY_FROM_FILENAME and main_token:
        return _resolve_root_batch_anchor_body(comp, main_token)
    if BATCH_DECAL_MAIN_BODY_FROM_FILENAME and main_token:
        if _occurrence_is_batch_target(occurrence_label, main_token, comp):
            occ = _normalize_occurrence_name(occurrence_label)
            if _name_matches_product_token(occ, main_token):
                return occ
            matched = _find_body_matching_token(comp, main_token)
            if matched:
                return matched
    return None


def _read_decal_chain_faces_flag(decal: adsk.fusion.Decal) -> Optional[bool]:
    for attr in ("isChainFaces", "chainFaces"):
        try:
            return bool(getattr(decal, attr))
        except Exception:
            continue
    return None


def _read_decal_z_angle_deg(decal: adsk.fusion.Decal) -> Optional[float]:
    for attr in ("zAngle", "z_angle", "angleZ"):
        try:
            val = getattr(decal, attr)
            if val is not None:
                return float(val)
        except Exception:
            continue
    return None


def _probe_template_decals_on_component(
    comp: adsk.fusion.Component,
) -> Optional[TemplateDecalHint]:
    """Read chain/Z from the largest user decal on ``comp`` (before removal)."""
    if not BATCH_DECAL_INHERIT_TEMPLATE_DECAL:
        return None
    try:
        decals = comp.decals
        n = decals.count
    except Exception:
        return None
    best: Optional[TemplateDecalHint] = None
    best_score = -1.0
    for i in range(n):
        try:
            d = decals.item(i)
        except Exception:
            continue
        try:
            d_name = d.name or ""
        except Exception:
            d_name = ""
        if d_name.startswith(BATCH_DECAL_NAME_PREFIX):
            continue
        score = 1.0
        try:
            m = d.transform
            if m is not None:
                _o, x_axis, y_axis, _z = m.getAsCoordinateSystem()
                score = max(_vector_length(x_axis), _vector_length(y_axis))
        except Exception:
            pass
        hint = TemplateDecalHint(
            chain_faces=_read_decal_chain_faces_flag(d),
            z_angle_deg=_read_decal_z_angle_deg(d),
            name=d_name,
        )
        if score > best_score:
            best_score = score
            best = hint
    return best


def _resolve_chain_faces_for_decal(
    face: adsk.fusion.BRepFace,
    body: Optional[adsk.fusion.BRepBody],
    template_hint: Optional[TemplateDecalHint],
) -> bool:
    if (
        BATCH_DECAL_INHERIT_TEMPLATE_CHAIN
        and template_hint is not None
        and template_hint.chain_faces is not None
    ):
        return bool(template_hint.chain_faces)
    if not BATCH_DECAL_CHAIN_FACES:
        return False
    if face is not None:
        spread = _face_curvature_spread_deg(face)
        if spread > float(BATCH_DECAL_CHAIN_FACES_CURVED_MAX_DEG):
            return False
    return True


def _resolve_decal_grain_orientation(
    face: adsk.fusion.BRepFace,
    body: Optional[adsk.fusion.BRepBody],
    use_chain: bool,
    template_hint: Optional[TemplateDecalHint],
) -> Tuple[float, bool, bool]:
    """Return ``(z_angle_deg, use_align_grain, align_after_z)`` for this face."""
    use_align = bool(BATCH_DECAL_ALIGN_GRAIN_TO_LENGTH) and body is not None
    if (
        BATCH_DECAL_INHERIT_TEMPLATE_Z_ANGLE
        and template_hint is not None
        and template_hint.z_angle_deg is not None
    ):
        z_deg = float(template_hint.z_angle_deg)
        if use_align and use_chain:
            return z_deg, True, True
        if use_align and not use_chain and abs(z_deg) < 1e-9:
            return z_deg, True, False
        return z_deg, False, False
    if use_chain:
        # Z=90 first for chain wrap; align X to body length after (body orientation varies).
        return float(BATCH_DECAL_Z_ANGLE_DEG), use_align, True
    return float(BATCH_DECAL_Z_ANGLE_CURVED_DEG), use_align, False


def _record_decal_placement(
    decal: adsk.fusion.Decal,
    record: DecalPlacementRecord,
) -> None:
    try:
        _DECAL_PLACEMENT_CACHE[id(decal)] = record
    except Exception:
        pass


def _remove_existing_decals_on_component(
    comp: adsk.fusion.Component,
    lines: List[str],
    label: str,
) -> Optional[TemplateDecalHint]:
    """Remove user-authored decals on main body; return probed template hint."""
    template_hint = _probe_template_decals_on_component(comp)
    if template_hint and template_hint.name:
        parts: List[str] = []
        if template_hint.chain_faces is not None:
            parts.append("chain={}".format(template_hint.chain_faces))
        if template_hint.z_angle_deg is not None:
            parts.append("Z={:.1f}°".format(template_hint.z_angle_deg))
        if parts:
            inherit_note = ""
            if not (
                BATCH_DECAL_INHERIT_TEMPLATE_CHAIN
                or BATCH_DECAL_INHERIT_TEMPLATE_Z_ANGLE
            ):
                inherit_note = " (reference only — using geometry rules)"
            lines.append(
                "  {}: template decal '{}' → {}{}".format(
                    label, template_hint.name, ", ".join(parts), inherit_note
                )
            )
    if not BATCH_DECAL_REMOVE_EXISTING_ON_MAIN:
        return template_hint
    removed = 0
    try:
        decals = comp.decals
        n = decals.count
    except Exception:
        return template_hint
    for i in range(n - 1, -1, -1):
        try:
            d = decals.item(i)
        except Exception:
            continue
        try:
            d_name = d.name or ""
        except Exception:
            d_name = ""
        if d_name.startswith(BATCH_DECAL_NAME_PREFIX):
            continue
        try:
            d.deleteMe()
            removed += 1
        except Exception:
            continue
    if removed:
        lines.append(
            "  {}: removed {} existing decal(s) before batch placement".format(
                label, removed
            )
        )
    return template_hint


def _occurrence_forced_body_name(
    occurrence_label: str,
    main_token: str = "",
    comp: Optional[adsk.fusion.Component] = None,
) -> Optional[str]:
    """Return pinned body name for this occurrence, if any."""
    if comp is not None:
        return _resolve_forced_body_name(occurrence_label, main_token, comp)
    mapped = _occurrence_forced_body_name_from_map(occurrence_label)
    if mapped:
        return mapped
    if BATCH_DECAL_MAIN_BODY_FROM_FILENAME and main_token:
        occ = _normalize_occurrence_name(occurrence_label)
        if _name_matches_product_token(occ, main_token):
            return occ
    return None


def _body_name_matches(body: adsk.fusion.BRepBody, expected: str) -> bool:
    try:
        actual = (body.name or "").strip()
    except Exception:
        actual = ""
    if _body_matches_root_anchor(expected):
        return _body_name_exact_matches(actual, expected)
    return _name_matches_product_token(actual, expected)


def _forced_body_uses_exact_match(label: str, forced_body: Optional[str]) -> bool:
    if not forced_body:
        return False
    if _occurrence_label_in_explicit_map(label):
        return True
    if label == "(root)" and _body_matches_root_anchor(forced_body):
        return True
    return False


def _find_body_by_map_anchor_name(
    comp: adsk.fusion.Component,
    anchor: str,
) -> Optional[adsk.fusion.BRepBody]:
    """Visible body named ``Body1`` / ``Body1 (7)`` on ``comp``."""
    if not anchor:
        return None
    try:
        bodies = comp.bRepBodies
        n = bodies.count
    except Exception:
        return None
    for bi in range(n):
        try:
            body = bodies.item(bi)
            bname = body.name or ""
        except Exception:
            continue
        if not _body_name_matches_map_anchor(bname, anchor):
            continue
        if not _entity_is_visible(body, default=True):
            continue
        return body
    return None


def _body_decal_rank_multiplier(
    body: adsk.fusion.BRepBody,
    label: str,
    original_appearance_names: Optional[Dict[int, str]] = None,
) -> float:
    """Rank bodies for per-occurrence decal anchor selection."""
    try:
        body_name = (body.name or "").strip()
    except Exception:
        body_name = ""
    for prefer in BATCH_DECAL_BODY_PREFER_NAMES:
        if _body_name_matches_map_anchor(body_name, prefer):
            return float(BATCH_DECAL_BODY_PREFER_NAME_RANK)
    haystack = _body_filter_haystack(body, label, original_appearance_names)
    for kw in BATCH_DECAL_BODY_PREFER_KEYWORDS:
        if kw.lower() in haystack:
            return 4.0
    return 1.0


def _face_normal_unit(
    face: adsk.fusion.BRepFace,
) -> Optional[Tuple[float, float, float]]:
    pt = _on_face_point(face)
    if pt is None:
        return None
    return _face_normal_at_point(face, pt)


def _is_world_vertical_face(face: adsk.fusion.BRepFace) -> bool:
    """True when face normal is horizontal in Fusion Z-up (vertical wall)."""
    if not BATCH_DECAL_SKIP_WORLD_VERTICAL:
        return False
    normal = _face_normal_unit(face)
    if normal is None:
        return False
    return abs(normal[2]) < float(BATCH_DECAL_WORLD_VERTICAL_Z_DOT)


def _tread_show_alignment(
    normal: Tuple[float, float, float],
) -> float:
    """How much the face normal points toward a show surface (+Z or ±Y tread)."""
    z_up = max(0.0, normal[2])
    if not BATCH_DECAL_PRIMARY_ALLOW_Y_SHOW:
        return z_up
    y_show = abs(normal[1])
    if y_show >= float(BATCH_DECAL_PRIMARY_MIN_WORLD_UP_DOT):
        return max(z_up, y_show)
    return z_up


def _is_vertical_side_face(
    face: adsk.fusion.BRepFace,
    body: adsk.fusion.BRepBody,
) -> bool:
    """True for side bands — not tread tops facing +Z or ±Y (Full Front show)."""
    normal = _face_normal_unit(face)
    if normal is None:
        return False
    if _tread_show_alignment(normal) >= float(BATCH_DECAL_PRIMARY_MIN_WORLD_UP_DOT):
        return False
    if abs(normal[2]) < float(BATCH_DECAL_WORLD_VERTICAL_Z_DOT):
        return True
    if not BATCH_DECAL_SKIP_VERTICAL_SIDES:
        return False
    length, _width, thickness = _body_bbox_axes(body)
    dot_len = abs(_vec3_dot(normal, length))
    dot_thick = abs(_vec3_dot(normal, thickness))
    return (
        dot_len < float(BATCH_DECAL_VERTICAL_SIDE_LENGTH_DOT)
        and dot_thick < float(BATCH_DECAL_VERTICAL_SIDE_THICKNESS_DOT)
        and abs(normal[2]) < float(BATCH_DECAL_WORLD_VERTICAL_Z_DOT)
    )


def _is_chamfer_or_slope_face(
    face: adsk.fusion.BRepFace,
) -> bool:
    """True when the normal is not axis-aligned (45° chamfers, etc.)."""
    normal = _face_normal_unit(face)
    if normal is None:
        return True
    peak = max(abs(normal[0]), abs(normal[1]), abs(normal[2]))
    return peak < float(BATCH_DECAL_PRIMARY_STRICT_SHOW_DOT)


def _qualifies_primary_anchor_face(
    face: adsk.fusion.BRepFace,
    body: adsk.fusion.BRepBody,
) -> bool:
    """Stricter filter for chain-mode tread-top anchor."""
    if _face_area(face) < float(BATCH_DECAL_PRIMARY_MIN_FACE_AREA_CM2):
        return False
    if _is_chamfer_or_slope_face(face):
        return False
    if body is not None and _is_end_cap_face(face, body):
        return False
    if body is not None and _is_vertical_side_face(face, body):
        return False
    if BATCH_DECAL_SKIP_DOWN_FACING and (
        _face_up_score(face) < BATCH_DECAL_DOWN_FACE_THRESHOLD
    ):
        return False
    if BATCH_DECAL_SKIP_CURVED_FACES and (
        _face_curvature_spread_deg(face) > BATCH_DECAL_CURVED_FACE_MAX_DEG
    ):
        return False
    if not BATCH_DECAL_PRIMARY_USE_WORLD_UP:
        return True
    normal = _face_normal_unit(face)
    if normal is None:
        return False
    align = _tread_show_alignment(normal)
    if align < float(BATCH_DECAL_PRIMARY_MIN_WORLD_UP_DOT):
        return False
    return align >= float(BATCH_DECAL_PRIMARY_STRICT_SHOW_DOT)


def _body_bbox_show_plane_floor_cm(
    body: adsk.fusion.BRepBody,
    face_normal: Optional[Tuple[float, float, float]] = None,
) -> Tuple[float, float]:
    """Length × width in the tread show plane — lower bound for chain-face scale."""
    dx, dy, dz = _body_bbox_dims_cm(body)
    if face_normal is None:
        ranked = sorted((dx, dy, dz), reverse=True)
        return ranked[0], ranked[1]

    nx, ny, nz = face_normal
    abs_n = (abs(nx), abs(ny), abs(nz))
    if abs_n[1] >= max(abs_n[0], abs_n[2]) and abs_n[1] >= 0.5:
        in_plane = (dx, dz)
    elif abs_n[2] >= max(abs_n[0], abs_n[1]) and abs_n[2] >= 0.5:
        in_plane = (dx, dy)
    elif abs_n[0] >= 0.5:
        in_plane = (dy, dz)
    else:
        ranked = sorted((dx, dy, dz), reverse=True)
        return ranked[0], ranked[1]
    return max(in_plane), min(in_plane)


def _face_exterior_along_normal(
    face: adsk.fusion.BRepFace,
    body: adsk.fusion.BRepBody,
) -> float:
    """Positive when the face points outward from the body bbox center."""
    pt = _on_face_point(face)
    normal = _face_normal_unit(face)
    if pt is None or normal is None:
        return 0.0
    try:
        bbox = body.boundingBox
        if bbox is None:
            return 0.0
        cx = (bbox.minPoint.x + bbox.maxPoint.x) * 0.5
        cy = (bbox.minPoint.y + bbox.maxPoint.y) * 0.5
        cz = (bbox.minPoint.z + bbox.maxPoint.z) * 0.5
        vx = pt.x - cx
        vy = pt.y - cy
        vz = pt.z - cz
        return vx * normal[0] + vy * normal[1] + vz * normal[2]
    except Exception:
        return 0.0


def _primary_show_face_score(
    face: adsk.fusion.BRepFace,
    body: adsk.fusion.BRepBody,
) -> float:
    """Rank faces for tread-top anchoring (area × show alignment × exterior)."""
    area = _face_area(face)
    normal = _face_normal_unit(face)
    score = area
    if normal is not None:
        align = _tread_show_alignment(normal)
        score *= 1.0 + 4.0 * align
        if align >= float(BATCH_DECAL_PRIMARY_STRICT_SHOW_DOT):
            score *= 2.0
    exterior = _face_exterior_along_normal(face, body)
    if exterior > 0.0:
        score *= 1.5
    elif exterior < 0.0:
        score *= 0.25
    try:
        if isinstance(face.geometry, adsk.core.Plane):
            score *= 1.25
    except Exception:
        pass
    return score


def _face_show_diagnostic_line(
    face: adsk.fusion.BRepFace,
    body: adsk.fusion.BRepBody,
) -> str:
    normal = _face_normal_unit(face)
    if normal is None:
        return "normal=?"
    show = _tread_show_alignment(normal)
    return "normal=({:+.2f},{:+.2f},{:+.2f}) show={:.2f} |n·z|={:.2f} |n·y|={:.2f}".format(
        normal[0],
        normal[1],
        normal[2],
        show,
        abs(normal[2]),
        abs(normal[1]),
    )


def _is_end_cap_face(
    face: adsk.fusion.BRepFace,
    body: adsk.fusion.BRepBody,
) -> bool:
    """True for short cut faces at the ends of a long body (plank length axis)."""
    if not BATCH_DECAL_SKIP_END_CAP_FACES:
        return False
    pt = _on_face_point(face)
    if pt is None:
        return False
    normal = _face_normal_at_point(face, pt)
    if normal is None:
        return False
    lx, ly, lz = _body_unit_length_axis(body)
    dot = abs(normal[0] * lx + normal[1] * ly + normal[2] * lz)
    return dot >= float(BATCH_DECAL_END_CAP_NORMAL_DOT)


def _face_qualifies_for_decal(
    face: adsk.fusion.BRepFace,
    body: Optional[adsk.fusion.BRepBody] = None,
) -> bool:
    """Shared filters for show faces (not end caps / vertical sides / undersides)."""
    if _face_area(face) < BATCH_DECAL_MIN_FACE_AREA_CM2:
        return False
    if body is not None and _is_end_cap_face(face, body):
        return False
    if body is not None and _is_vertical_side_face(face, body):
        return False
    if BATCH_DECAL_SKIP_DOWN_FACING and (
        _face_up_score(face) < BATCH_DECAL_DOWN_FACE_THRESHOLD
    ):
        return False
    if BATCH_DECAL_SKIP_CURVED_FACES and (
        _face_curvature_spread_deg(face) > BATCH_DECAL_CURVED_FACE_MAX_DEG
    ):
        return False
    return True


def _pick_relaxed_face_for_forced_body(
    body: adsk.fusion.BRepBody,
    *,
    permissive: bool = False,
) -> Optional[adsk.fusion.BRepFace]:
    """Fallback for occurrence-mapped bodies (curved bullnose, split tops)."""
    min_area = float(BATCH_DECAL_MIN_FACE_AREA_CM2)
    best: Optional[adsk.fusion.BRepFace] = None
    best_score = 0.0
    largest_any: Optional[adsk.fusion.BRepFace] = None
    largest_area = 0.0
    try:
        faces = body.faces
        n = faces.count
    except Exception:
        return None
    for i in range(n):
        try:
            f = faces.item(i)
        except Exception:
            continue
        area = _face_area(f)
        if area > largest_area:
            largest_area = area
            largest_any = f
        if area < min_area:
            continue
        if not permissive:
            if _is_vertical_side_face(f, body):
                continue
            if BATCH_DECAL_SKIP_DOWN_FACING and (
                _face_up_score(f) < BATCH_DECAL_DOWN_FACE_THRESHOLD
            ):
                continue
        normal = _face_normal_unit(f)
        align = _tread_show_alignment(normal) if normal is not None else 0.0
        score = area * (0.25 + align)
        if score > best_score:
            best_score = score
            best = f
    if best is not None:
        return best
    if permissive and largest_any is not None:
        return largest_any
    return largest_any


def _pick_primary_show_face_for_body(
    body: adsk.fusion.BRepBody,
    *,
    occurrence_mapped: bool = False,
) -> Optional[adsk.fusion.BRepFace]:
    """Best tread-top anchor — large +Z or ±Y show face (Plank + Nose)."""
    strict_best: Optional[adsk.fusion.BRepFace] = None
    strict_score = 0.0
    relaxed_best: Optional[adsk.fusion.BRepFace] = None
    relaxed_score = 0.0
    try:
        faces = body.faces
        n = faces.count
    except Exception:
        return None
    for i in range(n):
        try:
            f = faces.item(i)
        except Exception:
            continue
        if _face_area(f) < float(BATCH_DECAL_PRIMARY_MIN_FACE_AREA_CM2):
            continue
        if _is_chamfer_or_slope_face(f):
            continue
        if _is_end_cap_face(f, body):
            continue
        if _is_vertical_side_face(f, body):
            continue
        if BATCH_DECAL_SKIP_DOWN_FACING and (
            _face_up_score(f) < BATCH_DECAL_DOWN_FACE_THRESHOLD
        ):
            continue
        normal = _face_normal_unit(f)
        if normal is None:
            continue
        align = _tread_show_alignment(normal)
        if align < float(BATCH_DECAL_PRIMARY_MIN_WORLD_UP_DOT):
            continue
        score = _primary_show_face_score(f, body)
        if align >= float(BATCH_DECAL_PRIMARY_STRICT_SHOW_DOT):
            if score > strict_score:
                strict_score = score
                strict_best = f
        elif score > relaxed_score:
            relaxed_score = score
            relaxed_best = f
    result = strict_best if strict_best is not None else relaxed_best
    if result is not None or not occurrence_mapped:
        return result
    result = _pick_relaxed_face_for_forced_body(body, permissive=False)
    if result is not None:
        return result
    return _pick_relaxed_face_for_forced_body(body, permissive=True)


def _body_corners_cm(body: adsk.fusion.BRepBody) -> List[adsk.core.Point3D]:
    """Eight corners of the body bounding box."""
    out: List[adsk.core.Point3D] = []
    try:
        bbox = body.boundingBox
        if bbox is None:
            return out
        mn, mx = bbox.minPoint, bbox.maxPoint
        for x in (mn.x, mx.x):
            for y in (mn.y, mx.y):
                for z in (mn.z, mx.z):
                    out.append(adsk.core.Point3D.create(x, y, z))
    except Exception:
        pass
    return out


def _is_extrusion_profile_product(
    model_path: Optional[Path],
    main_token: str,
) -> bool:
    """End Cap–style extrusions: chain off, exterior top face set."""
    parts: List[str] = []
    if main_token:
        parts.append(main_token)
    if model_path is not None:
        try:
            parts.append(model_path.stem or "")
        except Exception:
            pass
    hay = " ".join(parts).lower()
    for tok in BATCH_DECAL_EXTRUSION_PROFILE_TOKENS:
        if tok.lower() in hay:
            return True
    return False


def _chain_faces_for_product(
    model_path: Optional[Path],
    main_token: str,
    occurrence_label: str,
) -> bool:
    """Chain ON for tread/bullnose; OFF for extrusion-profile products."""
    if not BATCH_DECAL_CHAIN_FACES:
        return False
    if _is_extrusion_profile_product(model_path, main_token):
        return False
    if _occurrence_label_in_explicit_map(occurrence_label):
        return True
    hay = ((occurrence_label or "") + " " + (main_token or "")).lower()
    for kw in ("nose", "bullnose", "plank", "tread"):
        if kw in hay:
            return True
    return bool(BATCH_DECAL_CHAIN_FACES)


def _adjacent_faces_same_body(
    face: adsk.fusion.BRepFace,
) -> List[adsk.fusion.BRepFace]:
    """Faces sharing an edge with ``face``."""
    out: List[adsk.fusion.BRepFace] = []
    seen: set = set()
    try:
        edges = face.edges
        n_e = edges.count
    except Exception:
        return out
    for ei in range(n_e):
        try:
            edge = edges.item(ei)
            adj_faces = edge.faces
            n_f = adj_faces.count
        except Exception:
            continue
        for fi in range(n_f):
            try:
                nf = adj_faces.item(fi)
            except Exception:
                continue
            try:
                key = id(nf)
            except Exception:
                key = None
            if nf is face or key in seen:
                continue
            if key is not None:
                seen.add(key)
            out.append(nf)
    return out


def _face_qualifies_extrusion_top(
    face: adsk.fusion.BRepFace,
    body: adsk.fusion.BRepBody,
) -> bool:
    """Exterior flat +Y top band — no slopes, cuts, or cavity walls."""
    if _face_area(face) < float(BATCH_DECAL_MIN_FACE_AREA_CM2):
        return False
    if _is_end_cap_face(face, body):
        return False
    if _is_vertical_side_face(face, body):
        return False
    if BATCH_DECAL_SKIP_DOWN_FACING and (
        _face_up_score(face) < BATCH_DECAL_DOWN_FACE_THRESHOLD
    ):
        return False
    normal = _face_normal_unit(face)
    if normal is None:
        return False
    if abs(normal[1]) < float(BATCH_DECAL_EXTRUSION_TOP_MIN_Y_DOT):
        return False
    if _face_exterior_along_normal(face, body) <= 0.0:
        return False
    return True


def _pick_extrusion_top_faces(
    body: adsk.fusion.BRepBody,
    seed: Optional[adsk.fusion.BRepFace],
) -> List[adsk.fusion.BRepFace]:
    """Connected exterior +Y tops (chain-off multi-face decal, no inner/cut)."""
    if seed is None:
        seed = _pick_primary_show_face_for_body(body, occurrence_mapped=True)
    if seed is None:
        return []
    picked: List[adsk.fusion.BRepFace] = []
    seen: set = set()
    queue: List[adsk.fusion.BRepFace] = [seed]
    while queue:
        f = queue.pop(0)
        try:
            fid = id(f)
        except Exception:
            continue
        if fid in seen:
            continue
        seen.add(fid)
        if not _face_qualifies_extrusion_top(f, body):
            continue
        picked.append(f)
        for nb in _adjacent_faces_same_body(f):
            try:
                nid = id(nb)
            except Exception:
                continue
            if nid not in seen:
                queue.append(nb)
    picked.sort(key=lambda ff: _face_area(ff), reverse=True)
    return picked


def _apply_bbox_floor_to_decal_needs(
    need_x: float,
    need_y: float,
    x_axis: adsk.core.Vector3D,
    y_axis: adsk.core.Vector3D,
    body: adsk.fusion.BRepBody,
    face_normal: Optional[Tuple[float, float, float]],
) -> Tuple[float, float]:
    """Apply body bbox floor along decal long/short axes (not blind X/Y)."""
    floor_long, floor_short = _body_bbox_show_plane_floor_cm(body, face_normal)
    length = _body_unit_length_axis(body)
    x_u = _vec3_normalize(_vec3_from_vector3d(x_axis))
    y_u = _vec3_normalize(_vec3_from_vector3d(y_axis))
    if x_u is None or y_u is None:
        return max(need_x, floor_long), max(need_y, floor_short)
    x_al = abs(_vec3_dot(x_u, length))
    y_al = abs(_vec3_dot(y_u, length))
    if y_al >= x_al:
        return max(need_x, floor_short), max(need_y, floor_long)
    return max(need_x, floor_long), max(need_y, floor_short)


def _decal_axes_extents_needed_cm(
    trf: adsk.core.Matrix3D,
    face: adsk.fusion.BRepFace,
    body: Optional[adsk.fusion.BRepBody],
    *,
    chain_faces: Optional[bool] = None,
) -> Tuple[float, float]:
    """Span along decal X/Y required to cover body (chain faces) or face."""
    use_chain = (
        bool(BATCH_DECAL_CHAIN_FACES)
        if chain_faces is None
        else bool(chain_faces)
    )
    try:
        origin, x_axis, y_axis, _z_axis = trf.getAsCoordinateSystem()
    except Exception:
        return _face_long_dims(face)

    x_len = _vector_length(x_axis)
    y_len = _vector_length(y_axis)
    if x_len < 1e-9 or y_len < 1e-9:
        return _face_long_dims(face)

    x_u = adsk.core.Vector3D.create(
        x_axis.x / x_len, x_axis.y / x_len, x_axis.z / x_len
    )
    y_u = adsk.core.Vector3D.create(
        y_axis.x / y_len, y_axis.y / y_len, y_axis.z / y_len
    )

    points: List[adsk.core.Point3D] = []
    if body is not None and use_chain:
        points = _body_corners_cm(body)
    if not points:
        try:
            bbox = face.boundingBox
            if bbox is not None:
                mn, mx = bbox.minPoint, bbox.maxPoint
                for x in (mn.x, mx.x):
                    for y in (mn.y, mx.y):
                        for z in (mn.z, mx.z):
                            points.append(adsk.core.Point3D.create(x, y, z))
        except Exception:
            pass
    if not points:
        return _face_long_dims(face)

    x_vals: List[float] = []
    y_vals: List[float] = []
    for p in points:
        vx = p.x - origin.x
        vy = p.y - origin.y
        vz = p.z - origin.z
        x_vals.append(vx * x_u.x + vy * x_u.y + vz * x_u.z)
        y_vals.append(vx * y_u.x + vy * y_u.y + vz * y_u.z)
    need_x = max(x_vals) - min(x_vals)
    need_y = max(y_vals) - min(y_vals)
    if body is not None and BATCH_DECAL_SCALE_USE_BBOX_FLOOR:
        face_normal = _face_normal_unit(face)
        need_x, need_y = _apply_bbox_floor_to_decal_needs(
            need_x, need_y, x_axis, y_axis, body, face_normal
        )
    if need_x < 0.1 or need_y < 0.1:
        return _face_long_dims(face)
    return need_x, need_y


def _batch_decal_one_per_body_chain() -> bool:
    """One chain-wrapped decal per body on the primary show face."""
    return bool(BATCH_DECAL_CHAIN_FACES) and not bool(BATCH_DECAL_ALL_FACES)


def _body_is_show_surface(
    body: adsk.fusion.BRepBody,
    label: str,
    original_appearance_names: Optional[Dict[int, str]] = None,
    *,
    trust_map_anchor: bool = False,
) -> bool:
    """Skip foam pads, substrate (bamboo/paint/vinyl), lights, etc."""
    if trust_map_anchor:
        try:
            haystack = (body.name or "").lower()
        except Exception:
            haystack = ""
    else:
        haystack = _body_filter_haystack(body, label, original_appearance_names)
    for kw in BATCH_DECAL_BODY_SKIP_KEYWORDS:
        k = kw.lower()
        if k in haystack:
            return False
    return True


_PROTECTED_SUBSTRATE_BODY_KEYWORDS: Tuple[str, ...] = (
    "foam",
    "pad",
    "substrate",
)


def _body_is_protected_substrate_body(
    body: adsk.fusion.BRepBody,
    label: str = "",
) -> bool:
    """True for foam-pad / substrate bodies that must keep white/grey materials."""
    haystack = _body_filter_haystack(body, label, None)
    for kw in _PROTECTED_SUBSTRATE_BODY_KEYWORDS:
        if kw in haystack:
            return True
    return False


def _image_pixel_size(image_path: Optional[Path]) -> Optional[Tuple[int, int]]:
    """Return (width, height) for fit logging / aspect hints."""
    if not image_path or not image_path.is_file():
        return None
    try:
        from PIL import Image # type: ignore

        with Image.open(str(image_path)) as im:
            return int(im.size[0]), int(im.size[1])
    except Exception:
        return None


def _matrix_from_axes(
    origin: adsk.core.Point3D,
    x_axis: adsk.core.Vector3D,
    y_axis: adsk.core.Vector3D,
    z_axis: adsk.core.Vector3D,
) -> Tuple[Optional[adsk.core.Matrix3D], Optional[str]]:
    """Build a Matrix3D from a decal coordinate system (multi-method fallback)."""
    m = adsk.core.Matrix3D.create()
    for method_name in (
        "setWithCoordinateSystem",
        "setToCoordinateSystem",
        "setToAlignCoordinateSystems",
    ):
        try:
            method = getattr(m, method_name)
            method(origin, x_axis, y_axis, z_axis)
            return m, None
        except TypeError:
            continue
        except Exception as ex:
            return None, "{}: {}".format(method_name, ex)
    return None, "no coordinate-system setter on Matrix3D"


def _apply_xy_scale_to_transform(
    trf: adsk.core.Matrix3D,
    origin: adsk.core.Point3D,
    scale_xy: float,
) -> Tuple[Optional[adsk.core.Matrix3D], Optional[str]]:
    """Scale decal X/Y uniformly via transformBy (works when setToAlign fails)."""
    if scale_xy <= 1e-9:
        try:
            return trf.copy(), None
        except Exception:
            return trf, None

    scaled = trf.copy()
    last_err = "scale transformBy failed"

    scale_vec = adsk.core.Vector3D.create(scale_xy, scale_xy, 1.0)
    for method_name, args in (
        ("setWithScale", (scale_vec, origin)),
        ("setToScale", (scale_xy, origin)),
    ):
        try:
            scale_mat = adsk.core.Matrix3D.create()
            getattr(scale_mat, method_name)(*args)
            scaled.transformBy(scale_mat)
            return scaled, None
        except Exception as ex:
            last_err = "{}: {}".format(method_name, ex)

    try:
        _o, x_axis, y_axis, z_axis = trf.getAsCoordinateSystem()
        new_x = adsk.core.Vector3D.create(
            x_axis.x * scale_xy, x_axis.y * scale_xy, x_axis.z * scale_xy
        )
        new_y = adsk.core.Vector3D.create(
            y_axis.x * scale_xy, y_axis.y * scale_xy, y_axis.z * scale_xy
        )
        rebuilt, err = _matrix_from_axes(origin, new_x, new_y, z_axis)
        if rebuilt is not None:
            return rebuilt, None
        if err:
            last_err = err
    except Exception as ex:
        last_err = str(ex)
    return None, last_err


def _slice_image_for_tile(
    src_path: Path, ix: int, iy: int, nx: int, ny: int
) -> Optional[Path]:
    """Crop ``src_path`` to the (ix, iy) cell of an nx × ny grid.

    Returns the temp PNG path containing only that cell of the image. When
    each tile decal points to a different cell, the tiles arranged in their
    natural spatial grid display the source image as one continuous picture
    instead of repeating the full image once per tile.
    """
    try:
        from PIL import Image # type: ignore
    except ImportError:
        return None
    if nx < 1 or ny < 1 or not src_path or not src_path.is_file():
        return None
    try:
        im = Image.open(str(src_path)).convert("RGB")
        w, h = im.size
        if w < nx or h < ny:
            return None
        cw = w / nx
        ch = h / ny
        left = int(ix * cw)
        top = int(iy * ch)
        right = int(min((ix + 1) * cw, w))
        bottom = int(min((iy + 1) * ch, h))
        if right - left < 2 or bottom - top < 2:
            return None
        crop = im.crop((left, top, right, bottom))
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="lifeproof_tile_")
        os.close(fd)
        crop.save(tmp, format="PNG")
        _TILE_TEMP_PATHS.append(tmp)
        return Path(tmp)
    except Exception:
        return None


def _tile_points_on_planar_face(
    face: adsk.fusion.BRepFace, step_cm: float
) -> List[Tuple[adsk.core.Point3D, int, int, int, int]]:
    """Yield a grid of points spanning the face's bbox in step_cm increments.

    Each point is then validated against the face's evaluator so we only
    keep points that are actually on the face surface — bbox of a non-
    rectangular face leaves grid points in empty space that Fusion would
    reject with "Input point not on primary face".
    """
    # Generate a 2D grid spanning the face's two longest bbox axes (the third
    # axis is the face's thickness / perpendicular). Each tile carries its
    # (iu, iv, nu, nv) so the per-tile image slice maps to the correct cell.
    # Returning the grid coords lets ``update_batch_decal_images`` slice each
    # color-set image into nu × nv pieces and assign one piece per tile,
    # which eliminates the visible "same image repeated" pattern.
    result: List[Tuple[adsk.core.Point3D, int, int, int, int]] = []
    try:
        bbox = face.boundingBox
        if bbox is None:
            return result
        dx = abs(bbox.maxPoint.x - bbox.minPoint.x)
        dy = abs(bbox.maxPoint.y - bbox.minPoint.y)
        dz = abs(bbox.maxPoint.z - bbox.minPoint.z)
        dims = [(dx, "x"), (dy, "y"), (dz, "z")]
        dims.sort(key=lambda t: t[0], reverse=True)
        d_u, axis_u = dims[0]
        d_v, axis_v = dims[1]
        d_w, axis_w = dims[2]
        step = max(step_cm, 1.0)
        nu = max(1, int(d_u / step) + 1)
        nv = max(1, int(d_v / step) + 1)
        # Cap nu * nv up-front so the slicing math (nu, nv) stays meaningful;
        # decimating the flat list later would scramble which slice maps to
        # which decal.
        cap = max(int(BATCH_DECAL_MAX_TILES_PER_FACE), 1)
        while nu * nv > cap:
            if nu > nv:
                nu -= 1
            else:
                nv -= 1
            if nu < 1 or nv < 1:
                nu = max(nu, 1)
                nv = max(nv, 1)
                break

        for iu in range(nu):
            for iv in range(nv):
                u_off = (iu * d_u / max(nu - 1, 1)) if nu > 1 else d_u / 2.0
                v_off = (iv * d_v / max(nv - 1, 1)) if nv > 1 else d_v / 2.0
                w_off = d_w / 2.0  # centre of the perpendicular dim
                px = bbox.minPoint.x
                py = bbox.minPoint.y
                pz = bbox.minPoint.z
                for off, axis in ((u_off, axis_u), (v_off, axis_v), (w_off, axis_w)):
                    if axis == "x":
                        px += off
                    elif axis == "y":
                        py += off
                    else:
                        pz += off
                pt = adsk.core.Point3D.create(px, py, pz)
                if _is_point_on_face_surface(face, pt):
                    result.append((pt, iu, iv, nu, nv))
    except Exception:
        pass
    return result


def _face_long_dims(face: adsk.fusion.BRepFace) -> Tuple[float, float]:
    """The face's two longest physical extents in cm (for tile-count sizing)."""
    try:
        bbox = face.boundingBox
        if bbox is None:
            return 0.0, 0.0
        dx = abs(bbox.maxPoint.x - bbox.minPoint.x)
        dy = abs(bbox.maxPoint.y - bbox.minPoint.y)
        dz = abs(bbox.maxPoint.z - bbox.minPoint.z)
        dims = sorted((dx, dy, dz), reverse=True)
        return dims[0], dims[1]
    except Exception:
        return 0.0, 0.0


def _tile_points_on_face_uv(
    face: adsk.fusion.BRepFace, step_cm: float
) -> List[Tuple[adsk.core.Point3D, int, int, int, int]]:
    """Grid the face in its own parametric (U/V) space via the evaluator.

    This is far more reliable than the world-axis bounding-box grid for thin,
    chamfered, or non-axis-aligned faces (e.g. the long ``Plastic - Matte
    (White)`` edge strip that rendered as a pale line): every sample comes
    straight from ``getPointAtParameter`` so it is guaranteed on the surface,
    and tile counts are derived from the face's real physical size so the
    image still assembles continuously across the face.
    """
    result: List[Tuple[adsk.core.Point3D, int, int, int, int]] = []
    try:
        ev = face.evaluator
        if ev is None:
            return result
        ok, prange = ev.parametricRange()
        if not ok or prange is None:
            return result
        u0 = prange.minPoint.x
        u1 = prange.maxPoint.x
        v0 = prange.minPoint.y
        v1 = prange.maxPoint.y
        if u1 <= u0 or v1 <= v0:
            return result

        d_u, d_v = _face_long_dims(face)
        step = max(float(step_cm), 1.0)
        # Cells sized ~step so neighbouring ~5 cm Fusion decals overlap and
        # leave no bare slivers; longest physical dim drives the U count.
        nu = max(1, int(d_u / step) + 1)
        nv = max(1, int(d_v / step) + 1)
        cap = max(int(BATCH_DECAL_MAX_TILES_PER_FACE), 1)
        while nu * nv > cap:
            if nu >= nv:
                nu -= 1
            else:
                nv -= 1
            if nu < 1 or nv < 1:
                nu = max(nu, 1)
                nv = max(nv, 1)
                break

        for iu in range(nu):
            for iv in range(nv):
                # Cell centre in parametric space — keeps the decal inside its
                # image slice and away from the trimmed face boundary.
                fu = (iu + 0.5) / nu
                fv = (iv + 0.5) / nv
                u = u0 + fu * (u1 - u0)
                v = v0 + fv * (v1 - v0)
                p2 = adsk.core.Point2D.create(u, v)
                try:
                    on = ev.isParameterOnFace(p2)
                except Exception:
                    on = True
                if not on:
                    continue
                try:
                    ok2, p3 = ev.getPointAtParameter(p2)
                except Exception:
                    ok2, p3 = False, None
                if ok2 and p3 is not None:
                    result.append((p3, iu, iv, nu, nv))
    except Exception:
        return result
    return result


def _point_key(p: adsk.core.Point3D) -> Tuple[float, float, float]:
    return (round(p.x, 4), round(p.y, 4), round(p.z, 4))


def _is_point_on_face_surface(
    face: adsk.fusion.BRepFace,
    pt: adsk.core.Point3D,
    tol_cm: float = 0.08,
) -> bool:
    """True when ``pt`` lies on (or very near) the face surface."""
    try:
        ev = face.evaluator
        if ev is None:
            return True
        ok, closest, dist = ev.getClosestPoint(pt)
        if ok and dist is not None:
            return float(dist) <= float(tol_cm)
    except Exception:
        pass
    return True


def _collect_decal_anchor_points(
    face: adsk.fusion.BRepFace,
    primary: Optional[adsk.core.Point3D] = None,
    *,
    step_cm: Optional[float] = None,
    max_points: int = 24,
) -> List[adsk.core.Point3D]:
    """Ordered on-surface anchors for decal placement / gap-fill retries."""
    step = max(float(step_cm or BATCH_DECAL_TILE_STEP_CM), 1.0)
    seen: set = set()
    out: List[adsk.core.Point3D] = []

    # API-provided on-face point — trust even when distance check is finicky.
    pon = _on_face_point(face)
    if pon is not None:
        key = _point_key(pon)
        if key not in seen:
            seen.add(key)
            out.append(pon)

    def _add(pt: Optional[adsk.core.Point3D]) -> None:
        if pt is None or len(out) >= max_points:
            return
        key = _point_key(pt)
        if key in seen:
            return
        if not _is_point_on_face_surface(face, pt):
            return
        seen.add(key)
        out.append(pt)

    _add(primary)
    _add(_on_face_point(face))

    for tile in _tile_points_on_face_uv(face, step):
        _add(tile[0])
    for tile in _tile_points_on_planar_face(face, step):
        _add(tile[0])

    # Finer UV corners / centres for curved or trimmed faces.
    try:
        ev = face.evaluator
        if ev is not None:
            ok, prange = ev.parametricRange()
            if ok and prange is not None:
                u0, u1 = prange.minPoint.x, prange.maxPoint.x
                v0, v1 = prange.minPoint.y, prange.maxPoint.y
                if u1 > u0 and v1 > v0:
                    for fu in (0.2, 0.35, 0.5, 0.65, 0.8):
                        for fv in (0.2, 0.35, 0.5, 0.65, 0.8):
                            u = u0 + fu * (u1 - u0)
                            v = v0 + fv * (v1 - v0)
                            p2 = adsk.core.Point2D.create(u, v)
                            try:
                                on = ev.isParameterOnFace(p2)
                            except Exception:
                                on = True
                            if not on:
                                continue
                            ok2, p3 = ev.getPointAtParameter(p2)
                            if ok2 and p3 is not None:
                                _add(p3)
    except Exception:
        pass
    return out


def _on_face_point(face: adsk.fusion.BRepFace) -> Optional[adsk.core.Point3D]:
    """Return a Point3D guaranteed to lie on the face surface.

    Fusion rejects ``createInput`` with "Input point is not located on primary
    face" whenever the supplied point isn't strictly on the face — which is
    common for fillets, the curved nose, and any non-convex face whose
    bounding-box center is in empty space. ``BRepFace.pointOnFace`` is the
    API's documented "give me any point on the face" helper.
    """
    try:
        p = face.pointOnFace
        if p is not None:
            return p
    except Exception:
        pass
    try:
        ev = face.evaluator
        if ev is not None:
            ok, prange = ev.parametricRange()
            if ok and prange is not None:
                u_mid = (prange.minPoint.x + prange.maxPoint.x) / 2.0
                v_mid = (prange.minPoint.y + prange.maxPoint.y) / 2.0
                ok2, p = ev.getPointAtParameter(adsk.core.Point2D.create(u_mid, v_mid))
                if ok2 and p is not None:
                    return p
    except Exception:
        pass
    try:
        bbox = face.boundingBox
        if bbox is not None:
            return adsk.core.Point3D.create(
                (bbox.minPoint.x + bbox.maxPoint.x) / 2.0,
                (bbox.minPoint.y + bbox.maxPoint.y) / 2.0,
                (bbox.minPoint.z + bbox.maxPoint.z) / 2.0,
            )
    except Exception:
        pass
    return None


def _face_normal_at_point(
    face: adsk.fusion.BRepFace,
    point: adsk.core.Point3D,
) -> Optional[Tuple[float, float, float]]:
    """Unit surface normal at ``point`` on ``face``, or None."""
    try:
        ev = face.evaluator
        if ev is None:
            return None
        param: Optional[adsk.core.Point2D] = None
        try:
            ok, param = ev.getParameterAtPoint(point)
        except Exception:
            ok = False
        if not ok or param is None:
            ok, prange = ev.parametricRange()
            if not ok or prange is None:
                return None
            param = adsk.core.Point2D.create(
                (prange.minPoint.x + prange.maxPoint.x) / 2.0,
                (prange.minPoint.y + prange.maxPoint.y) / 2.0,
            )
        ok2, n = ev.getNormalAtParameter(param)
        if not ok2 or n is None:
            return None
        mag = math.sqrt(n.x * n.x + n.y * n.y + n.z * n.z)
        if mag <= 1e-9:
            return None
        return (n.x / mag, n.y / mag, n.z / mag)
    except Exception:
        return None


def _batch_decal_use_tiling() -> bool:
    """Tiling and chain-faces conflict — chain faces wins (one decal per face)."""
    return bool(BATCH_DECAL_TILE) and not bool(BATCH_DECAL_CHAIN_FACES)


def _build_scale_fit_diag_lines(
    *,
    need_x: float,
    need_y: float,
    default_x_cm: float,
    default_y_cm: float,
    fit_x: float,
    fit_y: float,
    fit_base: float,
    panoramic_mult: float,
    axis_mult: float,
    margin: float,
    ui_mult: float,
    scale_xy: float,
    image_path: Optional[Path],
    chain_faces: bool,
) -> List[str]:
    """Summary lines: model span (cm), Fusion default decal (cm), image (px), scale."""
    model_long = max(need_x, need_y)
    model_short = min(need_x, need_y)
    decal_long = max(default_x_cm, default_y_cm)
    decal_short = min(default_x_cm, default_y_cm)
    ratio_long = model_long / max(decal_long, 1e-9)

    px = _image_pixel_size(image_path)
    if px is not None:
        img_w, img_h = px
        img_long = max(img_w, img_h)
        img_short = min(img_w, img_h)
        img_line = "image={}x{}px (long={}px short={}px)".format(
            img_w, img_h, img_long, img_short
        )
    else:
        img_line = "image=(unavailable)"

    lines = [
        "  DIAG scale fit: model_need={:.1f}x{:.1f}cm (long={:.1f} short={:.1f}) chain={}".format(
            need_x, need_y, model_long, model_short, "on" if chain_faces else "off"
        ),
        "    default_decal={:.2f}x{:.2f}cm (long={:.2f} short={:.2f}) "
        "model_long/decal_long={:.2f}".format(
            default_x_cm, default_y_cm, decal_long, decal_short, ratio_long
        ),
        "    {} fit_x={:.3f} fit_y={:.3f} fit_base={:.3f} axis_mult={:.1f}".format(
            img_line, fit_x, fit_y, fit_base, axis_mult
        ),
    ]
    if abs(panoramic_mult - 1.0) > 1e-6:
        lines.append("    panoramic_mult={:.3f}".format(panoramic_mult))
    lines.append(
        "    margin={:.2f} ui_mult={:.2f} -> scale_xy={:.3f}".format(
            margin, ui_mult, scale_xy
        )
    )
    return lines


def _apply_scale_plane_xy_to_matrix(
    trf: adsk.core.Matrix3D,
    face: adsk.fusion.BRepFace,
    body: Optional[adsk.fusion.BRepBody] = None,
    image_path: Optional[Path] = None,
    *,
    chain_faces: Optional[bool] = None,
) -> Tuple[Optional[str], float, Optional[adsk.core.Matrix3D], float, float, List[str]]:
    """Uniform Scale Plane XY via transform axis scaling (not width/height).

    Auto-fits to body bbox in decal-local X/Y after orientation, then applies
    ``BATCH_DECAL_SCALE_PLANE_XY``. Returns ``(err, scale_xy, trf, need_x, need_y, diag)``.
    """
    need_x = 0.0
    need_y = 0.0
    empty_diag: List[str] = []
    use_chain = (
        bool(BATCH_DECAL_CHAIN_FACES)
        if chain_faces is None
        else bool(chain_faces)
    )
    try:
        origin, x_axis, y_axis, z_axis = trf.getAsCoordinateSystem()
    except Exception as ex:
        return "scale plane XY: {}".format(ex), 1.0, None, need_x, need_y, empty_diag

    x_len = _vector_length(x_axis)
    y_len = _vector_length(y_axis)
    if x_len < 1e-9 or y_len < 1e-9:
        return "degenerate decal axes", 1.0, None, need_x, need_y, empty_diag

    need_x, need_y = _decal_axes_extents_needed_cm(
        trf, face, body, chain_faces=use_chain
    )
    if need_x < 0.1 or need_y < 0.1:
        return None, 1.0, None, need_x, need_y, empty_diag

    axis_mult = max(float(BATCH_DECAL_FIT_DEFAULT_AXIS_MULTIPLIER), 1e-9)
    default_x_cm = axis_mult * x_len
    default_y_cm = axis_mult * y_len
    fit_x = need_x / max(default_x_cm, 1e-9)
    fit_y = need_y / max(default_y_cm, 1e-9)
    fit_base = max(fit_x, fit_y)
    fit = fit_base
    panoramic_mult = 1.0
    px = _image_pixel_size(image_path)
    if px is not None:
        img_w, img_h = px
        if img_w > 0 and img_h > 0:
            img_ar = float(img_w) / float(img_h)
            body_ar = need_x / max(need_y, 0.1)
            if img_ar > body_ar * 1.15:
                cap = max(float(BATCH_DECAL_PANORAMIC_SCALE_MAX), 1.0)
                panoramic_mult = min(img_ar / max(body_ar, 0.1), cap)
                fit *= panoramic_mult

    margin = max(float(BATCH_DECAL_SCALE_AUTO_FIT_MARGIN), 0.01)
    ui_mult = max(get_decal_scale_plane_xy(), 0.01)
    scale_xy = fit * margin * ui_mult
    if scale_xy < 1e-6:
        scale_xy = 1.0

    diag = _build_scale_fit_diag_lines(
        need_x=need_x,
        need_y=need_y,
        default_x_cm=default_x_cm,
        default_y_cm=default_y_cm,
        fit_x=fit_x,
        fit_y=fit_y,
        fit_base=fit_base,
        panoramic_mult=panoramic_mult,
        axis_mult=axis_mult,
        margin=margin,
        ui_mult=ui_mult,
        scale_xy=scale_xy,
        image_path=image_path,
        chain_faces=use_chain,
    )

    scaled_trf, scale_err = _apply_xy_scale_to_transform(trf, origin, scale_xy)
    if scaled_trf is not None:
        return None, scale_xy, scaled_trf, need_x, need_y, diag
    return scale_err or "scale plane XY apply failed", scale_xy, None, need_x, need_y, diag


def _decal_x_axis_angle_in_face_plane_deg(
    m: adsk.core.Matrix3D,
) -> Optional[float]:
    """Approximate in-plane rotation of decal local X (degrees) for logging."""
    try:
        _o, x_axis, _y_axis, z_axis = m.getAsCoordinateSystem()
        x = _vec3_normalize(_vec3_from_vector3d(x_axis))
        n = _vec3_normalize(_vec3_from_vector3d(z_axis))
        if x is None or n is None:
            return None
        dot = _vec3_dot(x, n)
        px = (
            x[0] - dot * n[0],
            x[1] - dot * n[1],
            x[2] - dot * n[2],
        )
        pm = math.sqrt(px[0] * px[0] + px[1] * px[1] + px[2] * px[2])
        if pm < 1e-9:
            return None
        px = (px[0] / pm, px[1] / pm, px[2] / pm)
        ref = (1.0, 0.0, 0.0)
        rdot = _vec3_dot(ref, n)
        rx = (
            ref[0] - rdot * n[0],
            ref[1] - rdot * n[1],
            ref[2] - rdot * n[2],
        )
        rm = math.sqrt(rx[0] * rx[0] + rx[1] * rx[1] + rx[2] * rx[2])
        if rm < 1e-9:
            ref = (0.0, 0.0, 1.0)
            rdot = _vec3_dot(ref, n)
            rx = (
                ref[0] - rdot * n[0],
                ref[1] - rdot * n[1],
                ref[2] - rdot * n[2],
            )
            rm = math.sqrt(rx[0] * rx[0] + rx[1] * rx[1] + rx[2] * rx[2])
        if rm < 1e-9:
            return None
        rx = (rx[0] / rm, rx[1] / rm, rx[2] / rm)
        sin_a = _vec3_dot(_vec3_cross(rx, px), n)
        cos_a = _vec3_dot(rx, px)
        return math.degrees(math.atan2(sin_a, cos_a))
    except Exception:
        return None


def _apply_entity_transform(
    entity: Any,
    trf: adsk.core.Matrix3D,
) -> Optional[str]:
    """Assign ``transform`` on DecalInput or Decal."""
    try:
        entity.transform = trf.copy()
        return None
    except Exception as ex:
        return "transform assign: {}".format(ex)


def _cache_decal_transform(decal: adsk.fusion.Decal, trf: adsk.core.Matrix3D) -> None:
    try:
        _DECAL_TRANSFORM_CACHE[id(decal)] = trf.copy()
    except Exception:
        pass


def _reapply_cached_decal_transform(decal: adsk.fusion.Decal) -> Optional[str]:
    trf = _DECAL_TRANSFORM_CACHE.get(id(decal))
    if trf is None:
        return None
    return _apply_entity_transform(decal, trf)


def _decal_readback_log_line(decal: adsk.fusion.Decal) -> str:
    """One-line API read-back for summary logging (chain faces + scale proxy)."""
    parts: List[str] = []
    for attr in ("isChainFaces", "chainFaces"):
        try:
            parts.append("{}={}".format(attr, getattr(decal, attr)))
            break
        except Exception:
            continue
    try:
        m = decal.transform
        if m is not None:
            _o, x_axis, y_axis, _z = m.getAsCoordinateSystem()
            parts.append(
                "decalSizeXY=({:.2f}x{:.2f} cm)".format(
                    _vector_length(x_axis) * 2.0,
                    _vector_length(y_axis) * 2.0,
                )
            )
            uv_deg = _decal_x_axis_angle_in_face_plane_deg(m)
            if uv_deg is not None:
                parts.append("grainAngle={:.1f}°".format(uv_deg))
    except Exception:
        parts.append("transform=unreadable")
    return ", ".join(parts) if parts else "(no read-back)"


def _set_decal_input_chain_faces(decal_input: Any, chain_faces: bool) -> bool:
    """Set Fusion ``DecalInput`` chain-faces flag; tolerate API name variants."""
    for attr in ("isChainFaces", "chainFaces"):
        try:
            setattr(decal_input, attr, bool(chain_faces))
            return True
        except Exception:
            continue
    return False


def _apply_align_grain_to_body_length(
    trf: adsk.core.Matrix3D,
    face: adsk.fusion.BRepFace,
    center: adsk.core.Point3D,
    body: adsk.fusion.BRepBody,
    *,
    align_y_axis: bool = False,
) -> adsk.core.Matrix3D:
    """Rotate decal in the face plane so local X or Y follows plank length."""
    normal = _face_normal_at_point(face, center)
    if normal is None:
        return trf
    length = _body_unit_length_axis(body)
    dot_nl = _vec3_dot(normal, length)
    proj = (
        length[0] - dot_nl * normal[0],
        length[1] - dot_nl * normal[1],
        length[2] - dot_nl * normal[2],
    )
    target_axis = _vec3_normalize(proj)
    if target_axis is None:
        return trf
    try:
        _origin, x_axis, y_axis, _z_axis = trf.getAsCoordinateSystem()
    except Exception:
        return trf
    if align_y_axis:
        current = _vec3_normalize(_vec3_from_vector3d(y_axis))
    else:
        current = _vec3_normalize(_vec3_from_vector3d(x_axis))
    if current is None:
        return trf
    cross = _vec3_cross(current, target_axis)
    sin_a = _vec3_dot(cross, normal)
    cos_a = max(-1.0, min(1.0, _vec3_dot(current, target_axis)))
    angle = math.atan2(sin_a, cos_a)
    if abs(angle) < 1e-6:
        return trf
    try:
        z_axis = adsk.core.Vector3D.create(normal[0], normal[1], normal[2])
        rot = adsk.core.Matrix3D.create()
        rot.setToRotation(angle, z_axis, center)
        aligned = trf.copy()
        aligned.transformBy(rot)
        return aligned
    except Exception:
        return trf


def _configure_decal_create_input(
    decal_input: Any,
    face: adsk.fusion.BRepFace,
    center: adsk.core.Point3D,
    body: Optional[adsk.fusion.BRepBody] = None,
    image_path: Optional[Path] = None,
    *,
    chain_faces: Optional[bool] = None,
    template_hint: Optional[TemplateDecalHint] = None,
) -> Tuple[Optional[str], Optional[float], Optional[str], Optional[adsk.core.Matrix3D], List[str], Optional[bool]]:
    """Apply chain faces, grain align, Z angle, and Scale Plane XY on ``DecalInput``.

    Returns ``(error, scale_xy_factor, scale_warning, final_transform, scale_diag, use_chain)``.
    """
    empty_diag: List[str] = []
    use_chain = (
        _resolve_chain_faces_for_decal(face, body, template_hint)
        if chain_faces is None
        else bool(chain_faces)
    )
    _set_decal_input_chain_faces(decal_input, use_chain)

    z_deg, use_align, align_after_z = _resolve_decal_grain_orientation(
        face, body, use_chain, template_hint
    )
    needs_transform = (
        BATCH_DECAL_USE_SCALE_PLANE_XY
        or abs(z_deg) > 1e-9
        or use_align
    )
    transform = getattr(decal_input, "transform", None)
    if transform is None:
        if needs_transform:
            return "DecalInput.transform not available", None, None, None, empty_diag, use_chain
        return None, None, None, None, empty_diag, use_chain

    try:
        trf = transform.copy()
    except Exception as ex:
        return "transform copy: {}".format(ex), None, None, None, empty_diag, use_chain

    if use_align and not align_after_z:
        trf = _apply_align_grain_to_body_length(
            trf,
            face,
            center,
            body,
            align_y_axis=bool(BATCH_DECAL_ALIGN_GRAIN_USE_Y_AXIS),
        )

    if abs(z_deg) > 1e-9:
        normal = _face_normal_at_point(face, center)
        if normal is None:
            return "face normal unavailable for Z angle", None, None, None, empty_diag, use_chain
        try:
            z_axis = adsk.core.Vector3D.create(normal[0], normal[1], normal[2])
            rot = adsk.core.Matrix3D.create()
            rot.setToRotation(math.radians(z_deg), z_axis, center)
            trf.transformBy(rot)
        except Exception as ex:
            return "Z angle transform: {}".format(ex), None, None, None, empty_diag, use_chain

    if use_align and align_after_z:
        trf = _apply_align_grain_to_body_length(
            trf,
            face,
            center,
            body,
            align_y_axis=bool(BATCH_DECAL_ALIGN_GRAIN_USE_Y_AXIS),
        )

    scale_xy: Optional[float] = None
    scale_warn: Optional[str] = None
    scale_diag: List[str] = []
    need_x = 0.0
    need_y = 0.0
    if BATCH_DECAL_USE_SCALE_PLANE_XY:
        scale_err, scale_xy, scaled_trf, need_x, need_y, scale_diag = (
            _apply_scale_plane_xy_to_matrix(
                trf, face, body, image_path, chain_faces=use_chain
            )
        )
        if scaled_trf is not None:
            trf = scaled_trf
        elif scale_err:
            scale_warn = scale_err
    if scale_xy is not None and scale_warn is None:
        scale_warn = "scale_xy={:.2f} need={:.0f}x{:.0f}cm".format(
            scale_xy, need_x, need_y
        )

    assign_err = _apply_entity_transform(decal_input, trf)
    if assign_err:
        return assign_err, scale_xy, scale_warn, trf, scale_diag, use_chain
    return None, scale_xy, scale_warn, trf, scale_diag, use_chain


def _create_decal_on_face(
    decals_collection: Any,
    image_win_path: str,
    face: adsk.fusion.BRepFace,
    decal_name: str,
    center_override: Optional[adsk.core.Point3D] = None,
    body: Optional[adsk.fusion.BRepBody] = None,
    image_path: Optional[Path] = None,
    *,
    faces: Optional[List[adsk.fusion.BRepFace]] = None,
    chain_faces: Optional[bool] = None,
    template_hint: Optional[TemplateDecalHint] = None,
    record_placement: bool = True,
) -> Tuple[Optional[adsk.fusion.Decal], str, List[str]]:
    """Create one decal on ``face`` (or ``faces``) using ``createInput``.

    Applies chain faces, Z angle, and Scale Plane XY via ``DecalInput`` before
    ``add``. When ``center_override`` is provided, the decal is placed at that
    point (tiling grid only when tiling is enabled); otherwise the face centre.
    """
    face_list = list(faces) if faces else [face]
    primary = face_list[0]
    retries = max(1, int(BATCH_DECAL_CREATE_RETRIES))
    anchors = _collect_decal_anchor_points(
        primary,
        center_override,
        max_points=max(retries, 8),
    )
    if not anchors:
        return None, "no on-face point", []

    last_err = "no on-face point"
    last_diag: List[str] = []
    for center in anchors[:retries]:
        try:
            decal_input = decals_collection.createInput(
                image_win_path, face_list, center
            )
        except Exception as ex:
            last_err = "createInput: {}".format(ex)
            continue
        if decal_input is None:
            last_err = "createInput returned None"
            continue

        orient_err, _scale_xy, scale_warn, final_trf, scale_diag, use_chain = _configure_decal_create_input(
            decal_input,
            primary,
            center,
            body,
            image_path,
            chain_faces=chain_faces,
            template_hint=template_hint,
        )
        if orient_err:
            last_err = orient_err
            continue
        last_diag = scale_diag

        try:
            decal = decals_collection.add(decal_input)
        except Exception as ex:
            last_err = "decals.add: {}".format(ex)
            continue
        if decal is None:
            last_err = "decals.add returned None"
            continue

        if final_trf is not None:
            _cache_decal_transform(decal, final_trf)
            post_err = _apply_entity_transform(decal, final_trf)
            if post_err:
                scale_warn = (scale_warn + "; " if scale_warn else "") + "post-add " + post_err

        try:
            decal.name = decal_name
        except Exception:
            pass
        if record_placement:
            _record_decal_placement(
                decal,
                DecalPlacementRecord(
                    decals_collection=decals_collection,
                    face=primary,
                    body=body,
                    decal_name=decal_name,
                    chain_faces=use_chain,
                    template_hint=template_hint,
                    center_override=center_override if center_override is not None else center,
                ),
            )
        if scale_warn:
            return decal, scale_warn, scale_diag
        return decal, "", scale_diag
    return None, last_err, last_diag


def create_batch_decals_for_all_bodies(
    design: adsk.fusion.Design,
    image_path: Path,
    appearance_snap: Optional[List[Tuple[Any, Any]]] = None,
    model_path: Optional[Path] = None,
) -> Tuple[List[adsk.fusion.Decal], List[str]]:
    """Project ``image_path`` onto main show bodies as chain-wrapped decals.

    When ``model_path`` is given and ``BATCH_DECAL_MAIN_BODY_FROM_FILENAME`` is
    on, only occurrences matching the file stem (e.g. ``End Cap - Decal.f3d`` →
    ``End Cap:1``) are decaled; secondary parts (Track, …) keep template decals.
    """
    lines: List[str] = []
    created: List[adsk.fusion.Decal] = []
    _DECAL_SLOT.clear()
    if not image_path or not image_path.is_file():
        lines.append("Decal projection skipped: image not found ({})".format(image_path))
        return created, lines

    main_token = _main_product_token_from_model_path(model_path)
    if BATCH_DECAL_MAIN_BODY_FROM_FILENAME and main_token:
        lines.append(
            "Main body from filename: '{}' (only matching occurrence(s) get batch decal)".format(
                main_token
            )
        )
    use_tiling = _batch_decal_use_tiling()
    one_per_body = _batch_decal_one_per_body_chain()
    if one_per_body and BATCH_DECAL_ONE_PER_OCCURRENCE:
        mode_label = "one/occurrence+chain"
    elif one_per_body:
        mode_label = "one/body+chain"
    else:
        mode_label = "per-face"
    lines.append(
        "Decal placement: flat Z={:.1f}° curved Z={:.1f}° chain≤{:.0f}° spread, "
        "Chain default={}, Scale Plane XY={:.2f}, align grain={} ({} axis), "
        "template inherit chain={} z={}, mode={}, tiling={}, axis_fit_mult={:.1f}".format(
            float(BATCH_DECAL_Z_ANGLE_DEG),
            float(BATCH_DECAL_Z_ANGLE_CURVED_DEG),
            float(BATCH_DECAL_CHAIN_FACES_CURVED_MAX_DEG),
            "on" if BATCH_DECAL_CHAIN_FACES else "off",
            float(get_decal_scale_plane_xy()),
            "on" if BATCH_DECAL_ALIGN_GRAIN_TO_LENGTH else "off",
            "Y" if BATCH_DECAL_ALIGN_GRAIN_USE_Y_AXIS else "X",
            "on" if BATCH_DECAL_INHERIT_TEMPLATE_CHAIN else "off",
            "on" if BATCH_DECAL_INHERIT_TEMPLATE_Z_ANGLE else "off",
            mode_label,
            "on" if use_tiling else "off",
            float(BATCH_DECAL_FIT_DEFAULT_AXIS_MULTIPLIER),
        )
    )

    image_win = _win_path(image_path)
    readback_logged = False
    scale_diag_logged = False
    original_appearance_names = _appearance_snap_name_map(appearance_snap)

    # Per-face candidates (tiling / non-chain) OR one entry per body (chain).
    candidates: List[Tuple[float, Any, Any, str]] = []
    body_candidates: List[Tuple[float, Any, Any, str, Any, Optional[TemplateDecalHint]]] = []
    comp_template_hints: Dict[int, Optional[TemplateDecalHint]] = {}

    def _handle_component(
        comp: adsk.fusion.Component, label: str
    ) -> None:
        if not _occurrence_is_batch_target(label, main_token, comp):
            if label and label != "(root)":
                lines.append(
                    "  {}: skipped (secondary part — keeping template decal(s))".format(
                        label
                    )
                )
            return

        local_picks: List[Tuple[float, Any, Any, str, Any, Optional[TemplateDecalHint]]] = []
        template_hint = _remove_existing_decals_on_component(comp, lines, label)
        comp_template_hints[id(comp)] = template_hint
        forced_body = _resolve_forced_body_name(label, main_token, comp)
        explicit_map = _occurrence_label_in_explicit_map(label)
        if forced_body and one_per_body:
            if _occurrence_label_in_explicit_map(label):
                lines.append(
                    "  {}: explicit map → anchor '{}' only".format(label, forced_body)
                )
            elif label == "(root)" and _body_matches_root_anchor(forced_body):
                if not _main_token_is_treads_plus_assembly(main_token or ""):
                    lines.append(
                        "  {}: tread root anchor → '{}' only".format(
                            label, forced_body
                        )
                    )
            elif main_token:
                lines.append(
                    "  {}: main body (token '{}') → anchor '{}' only".format(
                        label, main_token, forced_body
                    )
                )
            else:
                lines.append(
                    "  {}: occurrence map → anchor '{}' only".format(label, forced_body)
                )
        try:
            decals_coll = comp.decals
        except Exception as ex:
            lines.append("  {}: decals collection error: {}".format(label, ex))
            return
        exact_forced = _forced_body_uses_exact_match(label, forced_body)

        if (
            label == "(root)"
            and _main_token_is_treads_plus_assembly(main_token or "")
            and one_per_body
        ):
            for anchor in BATCH_DECAL_ROOT_ANCHOR_BODIES:
                if len(created) >= BATCH_DECAL_MAX_TOTAL:
                    lines.append(
                        "  Global decal budget {} reached".format(
                            BATCH_DECAL_MAX_TOTAL
                        )
                    )
                    break
                body = _find_body_by_exact_anchor_name(comp, anchor)
                if body is None:
                    continue
                body_label = "{}/{}".format(label, anchor)
                if not _body_is_show_surface(
                    body, body_label, original_appearance_names
                ):
                    continue
                primary = _pick_primary_show_face_for_body(
                    body, occurrence_mapped=True
                )
                if primary is None:
                    lines.append(
                        "  {}: no show tread face (area>={:.0f} cm², show>={:.2f})".format(
                            body_label,
                            float(BATCH_DECAL_PRIMARY_MIN_FACE_AREA_CM2),
                            float(BATCH_DECAL_PRIMARY_MIN_WORLD_UP_DOT),
                        )
                    )
                    continue
                lines.append(
                    "  {}: tread root anchor → '{}' only".format(label, anchor)
                )
                name = "{}{}".format(BATCH_DECAL_NAME_PREFIX, len(created))
                chain_note = ""
                spread = _face_curvature_spread_deg(primary)
                resolved_chain = _resolve_chain_faces_for_decal(
                    primary, body, template_hint
                )
                if not resolved_chain and spread > float(
                    BATCH_DECAL_CHAIN_FACES_CURVED_MAX_DEG
                ):
                    chain_note = ", chain=off (curved {:.0f}°)".format(spread)
                elif resolved_chain:
                    chain_note = ", chain=on (flat)"
                else:
                    chain_note = ", chain=off"
                decal, err, scale_diag = _create_decal_on_face(
                    decals_coll,
                    image_win,
                    primary,
                    name,
                    center_override=None,
                    body=body,
                    image_path=image_path,
                    template_hint=template_hint,
                )
                if decal is None:
                    lines.append(
                        "  {}: chain decal failed ({})".format(body_label, err)
                    )
                    continue
                created.append(decal)
                nonlocal scale_diag_logged, readback_logged
                if scale_diag and not scale_diag_logged:
                    lines.extend(scale_diag)
                    scale_diag_logged = True
                rb = _decal_readback_log_line(decal)
                extra = " ({})".format(err) if err else ""
                face_diag = _face_show_diagnostic_line(primary, body)
                lines.append(
                    "  {}: 1 chain decal on primary face ({:.0f} cm², {}){}{}{}".format(
                        body_label,
                        _face_area(primary),
                        face_diag,
                        extra,
                        chain_note,
                        ", " + rb if rb else "",
                    )
                )
                if not readback_logged:
                    lines.append("  Decal API read-back (first): {}".format(rb))
                    readback_logged = True
                break
            return

        try:
            bodies = comp.bRepBodies
            n_bodies = bodies.count
        except Exception:
            return
        for bi in range(n_bodies):
            try:
                body = bodies.item(bi)
            except Exception:
                continue
            try:
                bname = body.name or ""
            except Exception:
                bname = ""
            is_map_anchor = bool(
                explicit_map
                and forced_body
                and _body_name_matches_map_anchor(bname, forced_body)
            )
            if forced_body and one_per_body and not is_map_anchor and not _body_name_matches_forced(
                body, forced_body, exact=exact_forced
            ):
                continue
            if _body_hide_for_batch(body, include_face_uv_pins=False) and not is_map_anchor:
                continue
            # Skip bodies the designer hid in the .f3d — they are not in the
            # render, so spending the decal budget on them leaves the
            # visible geometry bare.
            if not _entity_is_visible(body, default=True):
                continue
            body_label = "{}/{}".format(label, bname or "?")

            if BATCH_DECAL_ALL_FACES:
                try:
                    faces = body.faces
                    n_faces = faces.count
                except Exception as ex:
                    lines.append("  {}: faces error: {}".format(body_label, ex))
                    continue
                body_ok = 0
                body_fail = 0
                first_err = ""
                for fi in range(n_faces):
                    try:
                        face = faces.item(fi)
                    except Exception:
                        body_fail += 1
                        continue
                    name = "{}{}".format(BATCH_DECAL_NAME_PREFIX, len(created))
                    decal, info, _scale_diag = _create_decal_on_face(decals_coll, image_win, face, name)
                    if decal is None:
                        body_fail += 1
                        if not first_err:
                            first_err = info
                        continue
                    created.append(decal)
                    body_ok += 1
                lines.append(
                    "  {}: {} / {} face(s) decaled{}".format(
                        body_label,
                        body_ok,
                        body_ok + body_fail,
                        "" if not first_err else " (first failure: {})".format(first_err),
                    )
                )
            else:
                if one_per_body:
                    is_forced = is_map_anchor or (
                        forced_body is not None
                        and _body_name_matches_forced(
                            body, forced_body, exact=exact_forced
                        )
                    )
                    if forced_body and one_per_body and not is_forced:
                        continue
                    if not _body_is_show_surface(
                        body,
                        body_label,
                        original_appearance_names,
                        trust_map_anchor=is_map_anchor,
                    ):
                        continue
                    primary = _pick_primary_show_face_for_body(
                        body, occurrence_mapped=is_forced
                    )
                    if primary is None:
                        lines.append(
                            "  {}: no show tread face (area>={:.0f} cm², show>={:.2f})".format(
                                body_label,
                                float(BATCH_DECAL_PRIMARY_MIN_FACE_AREA_CM2),
                                float(BATCH_DECAL_PRIMARY_MIN_WORLD_UP_DOT),
                            )
                        )
                        continue
                    face_score = _primary_show_face_score(primary, body)
                    body_rank = _body_decal_rank_multiplier(
                        body, body_label, original_appearance_names
                    )
                    pick = (
                        face_score * body_rank,
                        primary,
                        decals_coll,
                        body_label,
                        body,
                        template_hint,
                    )
                    if is_forced:
                        pick = (pick[0] + 1e12,) + pick[1:]
                    if BATCH_DECAL_ONE_PER_OCCURRENCE:
                        local_picks.append(pick)
                    else:
                        body_candidates.append(pick)
                    continue
                # Phase 1: collect worthwhile faces (per-face mode).
                try:
                    faces = body.faces
                    n_faces = faces.count
                except Exception as ex:
                    lines.append("  {}: faces error: {}".format(body_label, ex))
                    continue
                for fi in range(n_faces):
                    try:
                        f = faces.item(fi)
                    except Exception:
                        continue
                    if not _face_qualifies_for_decal(f, body):
                        continue
                    candidates.append(
                        (_face_area(f), f, decals_coll, body_label)
                    )

        if BATCH_DECAL_ONE_PER_OCCURRENCE and one_per_body and local_picks:
            best = max(local_picks, key=lambda t: t[0])
            body_candidates.append(best)
            skipped = len(local_picks) - 1
            if skipped > 0:
                lines.append(
                    "  {}: one decal on largest show body (skipped {} sub-bodies)".format(
                        label, skipped
                    )
                )
        elif (
            explicit_map
            and forced_body
            and one_per_body
            and BATCH_DECAL_ONE_PER_OCCURRENCE
            and not local_picks
        ):
            anchor_body = _find_body_by_map_anchor_name(comp, forced_body)
            if anchor_body is None:
                body_names: List[str] = []
                try:
                    for bi in range(bodies.count):
                        body_names.append(bodies.item(bi).name or "?")
                except Exception:
                    pass
                msg = (
                    "  {}: occurrence map '{}' — no body named {} found".format(
                        label, forced_body, forced_body
                    )
                )
                if body_names:
                    msg += " (bodies: {})".format(", ".join(body_names))
                lines.append(msg)
            else:
                body_label = "{}/{}".format(label, anchor_body.name or forced_body)
                if not _body_is_show_surface(
                    anchor_body,
                    body_label,
                    original_appearance_names,
                    trust_map_anchor=True,
                ):
                    lines.append(
                        "  {}: map anchor skipped (non-show body keyword)".format(
                            body_label
                        )
                    )
                else:
                    primary = _pick_primary_show_face_for_body(
                        anchor_body, occurrence_mapped=True
                    )
                    if primary is None:
                        lines.append(
                            "  {}: no show tread face (area>={:.0f} cm², show>={:.2f})".format(
                                body_label,
                                float(BATCH_DECAL_PRIMARY_MIN_FACE_AREA_CM2),
                                float(BATCH_DECAL_PRIMARY_MIN_WORLD_UP_DOT),
                            )
                        )
                    else:
                        face_score = _primary_show_face_score(primary, anchor_body)
                        body_rank = _body_decal_rank_multiplier(
                            anchor_body, body_label, original_appearance_names
                        )
                        pick = (
                            face_score * body_rank + 1e12,
                            primary,
                            decals_coll,
                            body_label,
                            anchor_body,
                            template_hint,
                        )
                        body_candidates.append(pick)
        elif forced_body and one_per_body and BATCH_DECAL_ONE_PER_OCCURRENCE:
            lines.append(
                "  {}: occurrence map '{}' — no qualifying show face on that body".format(
                    label, forced_body
                )
            )

    try:
        _handle_component(design.rootComponent, "(root)")
    except Exception as ex:
        lines.append("Root component decal pass failed: {}".format(ex))

    try:
        occs = design.rootComponent.allOccurrences
        n_occ = occs.count
    except Exception as ex:
        n_occ = 0
        lines.append("allOccurrences error: {}".format(ex))

    for oi in range(n_occ):
        try:
            occ = occs.item(oi)
        except Exception:
            continue
        if _occurrence_should_hide_batch(occ):
            continue
        # Skip occurrences the designer HID in the .f3d (alternate tread /
        # riser configurations, etc.). They are not in the render, so
        # decaling them just burns the whole budget and leaves the visible
        # bullnose bare — exactly the "no texture" symptom.
        if not _entity_is_visible(occ, default=True):
            lines.append(
                "  {}: skipped (hidden in document)".format(occ.name)
            )
            continue
        try:
            comp = occ.component
        except Exception:
            continue
        _handle_component(comp, occ.name)

    # ---- Phase 2a: one chain-wrapped decal per body (primary show face) ----
    if body_candidates:
        body_candidates.sort(key=lambda t: t[0], reverse=True)
        for _score, face, decals_coll, body_label, body, template_hint in body_candidates:
            if len(created) >= BATCH_DECAL_MAX_TOTAL:
                lines.append(
                    "  Global decal budget {} reached".format(BATCH_DECAL_MAX_TOTAL)
                )
                break
            name = "{}{}".format(BATCH_DECAL_NAME_PREFIX, len(created))
            chain_note = ""
            if face is not None:
                spread = _face_curvature_spread_deg(face)
                resolved_chain = _resolve_chain_faces_for_decal(
                    face, body, template_hint
                )
                if not resolved_chain and spread > float(
                    BATCH_DECAL_CHAIN_FACES_CURVED_MAX_DEG
                ):
                    chain_note = ", chain=off (curved {:.0f}°)".format(spread)
                elif resolved_chain:
                    chain_note = ", chain=on (flat)"
                else:
                    chain_note = ", chain=off"
            decal, err, scale_diag = _create_decal_on_face(
                decals_coll,
                image_win,
                face,
                name,
                center_override=None,
                body=body,
                image_path=image_path,
                template_hint=template_hint,
            )
            if decal is None:
                lines.append(
                    "  {}: chain decal failed ({})".format(body_label, err)
                )
                continue
            created.append(decal)
            if scale_diag and not scale_diag_logged:
                lines.extend(scale_diag)
                scale_diag_logged = True
            rb = _decal_readback_log_line(decal)
            extra = ""
            if err:
                extra = " ({})".format(err)
            face_diag = _face_show_diagnostic_line(face, body)
            lines.append(
                "  {}: 1 chain decal on primary face ({:.0f} cm², {}){}{}{}".format(
                    body_label,
                    _face_area(face),
                    face_diag,
                    extra,
                    chain_note,
                    ", " + rb if rb else "",
                )
            )
            if not readback_logged:
                lines.append("  Decal API read-back (first): {}".format(rb))
                readback_logged = True
            if (
                BATCH_DECAL_UI_PUMP_INTERVAL > 0
                and len(created) % BATCH_DECAL_UI_PUMP_INTERVAL == 0
            ):
                try:
                    adsk.doEvents()
                except Exception:
                    pass

    # ---- Phase 2b: per-face decals (tiling or non-chain fallback) ----
    # candidates is empty in BATCH_DECAL_ALL_FACES mode (that path creates
    # decals itself above), so this is a no-op there.
    if candidates:
        # Total visible face area drives a PROPORTIONAL split of the budget:
        # every visible face gets tiles ∝ its share of the area, so a few
        # huge faces (the nose) can no longer eat the whole budget and leave
        # the plank bare. Uniform density everywhere instead of all-or-none.
        total_area = 0.0
        for _a, _f, _dc, _bl in candidates:
            total_area += max(0.0, float(_a))
        if total_area <= 0.0:
            total_area = 1.0

        candidates.sort(key=lambda t: t[0], reverse=True)
        per_body_ok: dict = {}
        per_body_fail: dict = {}
        per_body_faces: dict = {}
        first_err = ""
        budget_hit = False
        for _area, face, decals_coll, body_label in candidates:
            if len(created) >= BATCH_DECAL_MAX_TOTAL:
                budget_hit = True
                break
            try:
                is_plane = isinstance(face.geometry, adsk.core.Plane)
            except Exception:
                is_plane = False

            d_long = _face_long_dims(face)[0]
            big = (
                use_tiling
                and d_long > BATCH_DECAL_SINGLE_DECAL_MAX_CM
            )
            step_eff = BATCH_DECAL_TILE_STEP_CM
            if big:
                # This face's fair share of the global budget (by area),
                # clamped to [1, per-face cap].
                share = BATCH_DECAL_MAX_TOTAL * (float(_area) / total_area)
                target = int(round(share))
                target = max(1, min(target, BATCH_DECAL_MAX_TILES_PER_FACE))
                # Spread `target` tiles over the WHOLE face by deriving an
                # effective step from it (never denser than the base step),
                # so coverage is uniform — not a dense patch + bare strip.
                step_eff = max(
                    BATCH_DECAL_TILE_STEP_CM,
                    math.sqrt(max(float(_area), 1.0) / float(target)),
                )
                tile_specs = _tile_points_on_face_uv(face, step_eff)
                if not tile_specs and is_plane:
                    tile_specs = _tile_points_on_planar_face(face, step_eff)
                if not tile_specs:
                    p = _on_face_point(face)
                    tile_specs = [(p, 0, 0, 1, 1)] if p is not None else []
                # If the grid still overshoots, thin it EVENLY (taking the
                # first N would cluster decals at one end → a strip).
                if len(tile_specs) > target:
                    n_have = len(tile_specs)
                    keep = sorted(
                        {
                            int(round(i * (n_have - 1) / max(target - 1, 1)))
                            for i in range(target)
                        }
                    )
                    tile_specs = [tile_specs[i] for i in keep]
            else:
                # Small face: one ~5 cm decal already covers it.
                p = _on_face_point(face)
                tile_specs = [(p, 0, 0, 1, 1)] if p is not None else []

            if not tile_specs:
                continue

            remaining = BATCH_DECAL_MAX_TOTAL - len(created)
            if remaining <= 0:
                budget_hit = True
                break
            if len(tile_specs) > remaining:
                tile_specs = tile_specs[:remaining]

            used_face = False
            face_fail = 0
            tried_anchors: set = set()
            for tile_pt, iu, iv, nu, nv in tile_specs:
                if tile_pt is not None:
                    tried_anchors.add(_point_key(tile_pt))
                name = "{}{}".format(BATCH_DECAL_NAME_PREFIX, len(created))
                slice_path = (
                    _slice_image_for_tile(image_path, iu, iv, nu, nv)
                    if BATCH_DECAL_TILE_SLICE_IMAGE and nu * nv > 1
                    else None
                )
                decal_image_str = (
                    _win_path(slice_path) if slice_path else image_win
                )
                decal, err, scale_diag = _create_decal_on_face(
                    decals_coll,
                    decal_image_str,
                    face,
                    name,
                    center_override=tile_pt,
                    image_path=image_path,
                )
                if decal is None:
                    face_fail += 1
                    per_body_fail[body_label] = (
                        per_body_fail.get(body_label, 0) + 1
                    )
                    if not first_err:
                        first_err = err
                    continue
                created.append(decal)
                if scale_diag and not scale_diag_logged:
                    lines.extend(scale_diag)
                    scale_diag_logged = True
                _TILE_METADATA[id(decal)] = (iu, iv, nu, nv)
                per_body_ok[body_label] = per_body_ok.get(body_label, 0) + 1
                used_face = True
                if not readback_logged:
                    lines.append(
                        "  Decal API read-back (first): {}".format(
                            _decal_readback_log_line(decal)
                        )
                    )
                    readback_logged = True
                if (
                    BATCH_DECAL_UI_PUMP_INTERVAL > 0
                    and len(created) % BATCH_DECAL_UI_PUMP_INTERVAL == 0
                ):
                    try:
                        adsk.doEvents()
                    except Exception:
                        pass

            if (
                use_tiling
                and BATCH_DECAL_GAP_FILL_ON_FAIL
                and face_fail > 0
                and len(created) < BATCH_DECAL_MAX_TOTAL
            ):
                gap_cap = max(1, int(BATCH_DECAL_GAP_FILL_MAX_PER_FACE))
                gap_added = 0
                for anchor in _collect_decal_anchor_points(face, step_cm=step_eff if big else None):
                    if gap_added >= gap_cap:
                        break
                    if len(created) >= BATCH_DECAL_MAX_TOTAL:
                        break
                    if _point_key(anchor) in tried_anchors:
                        continue
                    tried_anchors.add(_point_key(anchor))
                    name = "{}{}".format(BATCH_DECAL_NAME_PREFIX, len(created))
                    decal, err, scale_diag = _create_decal_on_face(
                        decals_coll,
                        image_win,
                        face,
                        name,
                        center_override=anchor,
                        image_path=image_path,
                    )
                    if decal is None:
                        if not first_err:
                            first_err = err
                        continue
                    created.append(decal)
                    if scale_diag and not scale_diag_logged:
                        lines.extend(scale_diag)
                        scale_diag_logged = True
                    per_body_ok[body_label] = per_body_ok.get(body_label, 0) + 1
                    used_face = True
                    gap_added += 1
                    if (
                        BATCH_DECAL_UI_PUMP_INTERVAL > 0
                        and len(created) % BATCH_DECAL_UI_PUMP_INTERVAL == 0
                    ):
                        try:
                            adsk.doEvents()
                        except Exception:
                            pass

            if used_face:
                per_body_faces[body_label] = (
                    per_body_faces.get(body_label, 0) + 1
                )

        for bl in sorted(set(per_body_ok) | set(per_body_fail)):
            ok = per_body_ok.get(bl, 0)
            fail = per_body_fail.get(bl, 0)
            if ok == 0:
                lines.append(
                    "  {}: no decals (budget spent on larger faces "
                    "elsewhere)".format(bl)
                )
            else:
                extra = " across {} face(s)".format(per_body_faces.get(bl, 0))
                if fail:
                    extra += " ({} off-face skipped)".format(fail)
                lines.append(
                    "  {}: {} {} decal(s) added{}".format(
                        bl,
                        ok,
                        "scaled" if not use_tiling else "tile",
                        extra,
                    )
                )
        if budget_hit:
            lines.append(
                "  Global decal budget {} reached — smaller faces skipped "
                "(anti-freeze cap)".format(BATCH_DECAL_MAX_TOTAL)
            )

    # Tag each created decal with the appearance slot of the body it sits on so
    # update_batch_decal_images can route the _1 vs _2 raster. Resolve the body's
    # ORIGINAL appearance robustly: match by entityToken first (Fusion hands back
    # a fresh wrapper each traversal, so id() is NOT stable across the snapshot
    # and the decal body), then the id map, then a live read (valid when
    # appearance neutralization is off).
    orig_by_token: Dict[str, str] = {}
    for snap_body, snap_ap in (appearance_snap or []):
        try:
            tok = snap_body.entityToken
        except Exception:
            tok = None
        if tok:
            try:
                orig_by_token[tok] = (snap_ap.name if snap_ap is not None else "") or ""
            except Exception:
                orig_by_token[tok] = ""

    def _orig_appearance_name_for_body(body: Any) -> str:
        if body is None:
            return ""
        try:
            tok = body.entityToken
        except Exception:
            tok = None
        if tok and orig_by_token.get(tok):
            return orig_by_token[tok]
        nm = original_appearance_names.get(id(body), "")
        if nm:
            return nm
        try:
            ap = body.appearance
            return (ap.name if ap is not None else "") or ""
        except Exception:
            return ""

    def _face_appearance_slot(face: Any) -> Tuple[Optional[int], str]:
        """Slot from the decal's anchor face appearance (skins are often pinned
        per-face, not on the body). Returns (slot_or_None, appearance_name)."""
        if face is None:
            return None, ""
        try:
            fa = face.appearance
            fname = (fa.name if fa is not None else "") or ""
        except Exception:
            fname = ""
        return appearance_name_slot(fname), fname

    n_slot2 = 0
    for d in created:
        rec = _DECAL_PLACEMENT_CACHE.get(id(d))
        body = rec.body if rec is not None else None
        ap_name = _orig_appearance_name_for_body(body)
        slot = appearance_name_slot(ap_name)
        src = "body"
        if slot is None and rec is not None:
            fslot, fname = _face_appearance_slot(rec.face)
            if fslot is not None:
                slot, ap_name, src = fslot, fname, "face"
        if slot not in (1, 2):
            slot = 1
        _DECAL_SLOT[id(d)] = slot
        if slot == 2:
            n_slot2 += 1
        try:
            bn = body.name if body is not None else "?"
        except Exception:
            bn = "?"
        lines.append(
            "  Slot tag: decal on body '{}' appearance '{}' (via {}) → slot {}".format(
                bn, ap_name or "(none)", src, slot
            )
        )
    # Also log every snapshot body that wears a slot appearance, so we can see
    # whether a slot-2 surface exists at all (and on which body).
    for snap_body, snap_ap in (appearance_snap or []):
        try:
            ap_nm = (snap_ap.name if snap_ap is not None else "") or ""
        except Exception:
            ap_nm = ""
        s = appearance_name_slot(ap_nm)
        if s:
            try:
                bn = snap_body.name
            except Exception:
                bn = "?"
            lines.append(
                "  Slot map: body '{}' wears '{}' → slot {}".format(bn, ap_nm, s)
            )
    lines.append(
        "Slot routing: {} of {} decal(s) tagged slot-2 (receive _2 image).".format(
            n_slot2, len(created)
        )
    )

    lines.insert(0, "Batch decal projection: {} decal(s) created".format(len(created)))
    return created, lines


def _decal_image_for_slot(
    decal: adsk.fusion.Decal,
    slot1_image: Optional[Path],
    slot2_image: Optional[Path],
) -> Optional[Path]:
    """Per-decal color-set image: slot-2 bodies get the _2 image, all others _1.

    Falls back to the _1 image when a decal has no slot-2 image available so a
    missing/one-image color set never leaves a body untextured.
    """
    slot = _DECAL_SLOT.get(id(decal), 1)
    if slot == 2 and slot2_image is not None and slot2_image.is_file():
        return slot2_image
    return slot1_image


def update_batch_decal_images(
    decals: List[adsk.fusion.Decal],
    slot1_image: Path,
    slot2_image: Optional[Path] = None,
) -> Tuple[int, List[str], List[adsk.fusion.Decal]]:
    """Update every tracked batch decal for a new color set.

    Each decal receives the image for ITS body's appearance slot: slot-2 bodies
    (e.g. ``Vinyl Skin-2``) get ``slot2_image`` (the ``_2`` raster); every other
    decal gets ``slot1_image`` (the ``_1`` raster). ``slot2_image`` may be None
    (one-image models) — those decals fall back to ``slot1_image``.

    When ``BATCH_DECAL_RECREATE_ON_COLOR_SWAP`` is True, each decal is deleted
    and recreated with full orientation/scale (reliable on read-only Decal API).
    Otherwise falls back to ``imageFilename`` swap + cached transform re-apply.
    """
    if BATCH_DECAL_RECREATE_ON_COLOR_SWAP:
        return _update_batch_decals_via_recreate(decals, slot1_image, slot2_image)
    n, lines = _update_batch_decals_via_image_swap(decals, slot1_image, slot2_image)
    return n, lines, decals


def _resolve_recreate_face(record: DecalPlacementRecord) -> Any:
    """Re-pick anchor face from body; cached ``record.face`` goes stale after CS01."""
    body = record.body
    if body is not None:
        face = _pick_primary_show_face_for_body(body, occurrence_mapped=True)
        if face is not None:
            return face
    return record.face


def _update_batch_decals_via_recreate(
    decals: List[adsk.fusion.Decal],
    slot1_image: Path,
    slot2_image: Optional[Path] = None,
) -> Tuple[int, List[str], List[adsk.fusion.Decal]]:
    lines: List[str] = []
    if not slot1_image or not slot1_image.is_file():
        lines.append("Decal update skipped: image not found ({})".format(slot1_image))
        return 0, lines, decals
    out: List[adsk.fusion.Decal] = []
    n_slot2 = 0
    pump_every = max(int(BATCH_DECAL_UI_PUMP_INTERVAL), 1)
    for idx, d in enumerate(decals):
        old_id = id(d)
        slot = _DECAL_SLOT.get(old_id, 1)
        image_path = _decal_image_for_slot(d, slot1_image, slot2_image)
        if image_path is slot2_image and slot2_image is not None:
            n_slot2 += 1
        image_win = _win_path(image_path)
        record = _DECAL_PLACEMENT_CACHE.get(old_id)
        meta = _TILE_METADATA.get(old_id)
        try:
            dn = d.name or "?"
        except Exception:
            dn = "?"
        if record is None:
            lines.append(
                "  Decal '{}': recreate skipped (no placement cache)".format(dn)
            )
            out.append(d)
            continue
        face = _resolve_recreate_face(record)
        new_decal, err, _scale_diag = _create_decal_on_face(
            record.decals_collection,
            image_win,
            face,
            record.decal_name,
            center_override=record.center_override,
            body=record.body,
            image_path=image_path,
            chain_faces=record.chain_faces,
            template_hint=record.template_hint,
        )
        if new_decal is None:
            ok, swap_err = _set_decal_image(d, image_win)
            if ok:
                lines.append(
                    "  Decal '{}': recreate FAILED ({}); kept via imageFilename swap".format(
                        dn, err
                    )
                )
                out.append(d)
            else:
                lines.append(
                    "  Decal '{}': recreate FAILED ({}); swap fallback FAILED ({})".format(
                        dn, err, swap_err
                    )
                )
                out.append(d)
            if (idx + 1) % pump_every == 0:
                try:
                    adsk.doEvents()
                except Exception:
                    pass
            continue
        try:
            d.deleteMe()
        except Exception as ex:
            lines.append(
                "  Decal '{}': recreated but old delete failed ({})".format(dn, ex)
            )
        _DECAL_PLACEMENT_CACHE.pop(old_id, None)
        _DECAL_TRANSFORM_CACHE.pop(old_id, None)
        _TILE_METADATA.pop(old_id, None)
        _DECAL_SLOT.pop(old_id, None)
        _DECAL_SLOT[id(new_decal)] = slot
        if meta is not None:
            _TILE_METADATA[id(new_decal)] = meta
        try:
            new_name = new_decal.name or record.decal_name
        except Exception:
            new_name = record.decal_name
        rb = _decal_readback_log_line(new_decal)
        msg = "  Decal '{}': recreated".format(new_name)
        if err:
            msg += " ({})".format(err)
        if rb:
            msg += " — {}".format(rb)
        lines.append(msg)
        out.append(new_decal)
        if (idx + 1) % pump_every == 0:
            try:
                adsk.doEvents()
            except Exception:
                pass
    slot2_note = " ({} on _2 image)".format(n_slot2) if n_slot2 else ""
    lines.insert(
        0,
        "Batch decal update: {} / {} recreate(s){}".format(
            len(out), len(decals), slot2_note
        ),
    )
    return len(out), lines, out


def _update_batch_decals_via_image_swap(
    decals: List[adsk.fusion.Decal],
    slot1_image: Path,
    slot2_image: Optional[Path] = None,
) -> Tuple[int, List[str]]:
    """Swap ``imageFilename`` on every tracked decal. Each decal receives the
    raster for ITS body's appearance slot (slot-2 bodies get ``slot2_image``,
    all others ``slot1_image``). When per-decal grid metadata exists in
    ``_TILE_METADATA``, each decal receives its own slice so tiles assemble
    into one continuous image.
    """
    lines: List[str] = []
    if not slot1_image or not slot1_image.is_file():
        lines.append("Decal update skipped: image not found ({})".format(slot1_image))
        return 0, lines
    n = 0
    n_slot2 = 0
    pump_every = max(int(BATCH_DECAL_UI_PUMP_INTERVAL), 1)
    slice_cache: dict = {}
    for idx, d in enumerate(decals):
        image_path = _decal_image_for_slot(d, slot1_image, slot2_image)
        if image_path is slot2_image and slot2_image is not None:
            n_slot2 += 1
        base_target = _win_path(image_path)
        meta = _TILE_METADATA.get(id(d))
        if (
            BATCH_DECAL_TILE_SLICE_IMAGE
            and meta is not None
            and meta[2] * meta[3] > 1
        ):
            cache_key = (id(image_path), meta)
            cached = slice_cache.get(cache_key)
            if cached is None:
                iu, iv, nu, nv = meta
                sp = _slice_image_for_tile(image_path, iu, iv, nu, nv)
                cached = _win_path(sp) if sp else base_target
                slice_cache[cache_key] = cached
            target = cached
        else:
            target = base_target
        try:
            d.imageFilename = target
            n += 1
            try:
                dn = d.name or "?"
            except Exception:
                dn = "?"
            had_cache = id(d) in _DECAL_TRANSFORM_CACHE
            if BATCH_DECAL_REAPPLY_TRANSFORM_ON_IMAGE_SWAP:
                re_err = _reapply_cached_decal_transform(d)
                if re_err:
                    lines.append(
                        "  Decal '{}': transform re-apply FAILED ({})".format(
                            dn, re_err
                        )
                    )
                elif not had_cache:
                    lines.append(
                        "  Decal '{}': transform re-apply skipped (no cache)".format(
                            dn
                        )
                    )
            rb = _decal_readback_log_line(d)
            if rb:
                lines.append("  Decal '{}' after swap: {}".format(dn, rb))
        except Exception as ex:
            try:
                dn = d.name
            except Exception:
                dn = "?"
            lines.append("  Decal '{}': imageFilename FAILED ({})".format(dn, ex))
        if (idx + 1) % pump_every == 0:
            try:
                adsk.doEvents()
            except Exception:
                pass
    slot2_note = " ({} on _2 image)".format(n_slot2) if n_slot2 else ""
    lines.insert(
        0,
        "Batch decal update: {} / {} imageFilename swap(s){}".format(
            n, len(decals), slot2_note
        ),
    )
    return n, lines


def update_user_authored_decals(
    design: adsk.fusion.Design,
    image_path: Path,
    exclude_decal_ids: Optional[set] = None,
) -> Tuple[int, List[str]]:
    """Swap ``imageFilename`` on every decal in the design that was NOT created
    by this batch run.

    Use case: when ``BODY_COVERAGE_VIA_DECALS`` is on this build, the auto-
    created batch decals stay at Fusion's default ~5 cm size (the sizing API
    is locked on some installs). If the user manually places one large decal
    in Fusion's UI per body — sized by dragging handles, which always works —
    those decals are persisted in the .f3d and we just update their image
    per color set here.
    """
    lines: List[str] = []
    if not image_path or not image_path.is_file():
        lines.append("User-decal update skipped: image not found ({})".format(image_path))
        return 0, lines
    exclude = exclude_decal_ids or set()
    target = _win_path(image_path)
    decals = _collect_all_decals(design)
    n = 0
    user_count = 0
    pump_every = max(int(BATCH_DECAL_UI_PUMP_INTERVAL), 1)
    for d in decals:
        try:
            if id(d) in exclude:
                continue
        except Exception:
            pass
        try:
            d_name = d.name or ""
        except Exception:
            d_name = ""
        # Also skip anything our pipeline already created — defensive against
        # the exclude_ids set being stale (decals re-fetched between calls).
        if d_name.startswith(BATCH_DECAL_NAME_PREFIX):
            continue
        user_count += 1
        try:
            d.imageFilename = target
            n += 1
            lines.append("  User decal '{}': image -> {}".format(d_name or "?", target))
        except Exception as ex:
            lines.append("  User decal '{}': FAILED ({})".format(d_name or "?", ex))
        if user_count % pump_every == 0:
            try:
                adsk.doEvents()
            except Exception:
                pass
    lines.insert(0, "User-authored decal update: {} / {} imageFilename swap(s)".format(n, user_count))
    return n, lines


def cleanup_batch_decals(decals: List[adsk.fusion.Decal]) -> int:
    """Delete every tracked decal so the .f3d isn't polluted after a batch.

    Also clears the per-tile slice metadata and removes the temp PNG slices
    produced by ``_slice_image_for_tile`` so Resources/Texture doesn't fill
    up with throwaway crops.
    """
    n = 0
    for d in decals:
        try:
            d.deleteMe()
            n += 1
        except Exception:
            pass
    _TILE_METADATA.clear()
    _DECAL_TRANSFORM_CACHE.clear()
    _DECAL_PLACEMENT_CACHE.clear()
    _DECAL_SLOT.clear()
    for p in _TILE_TEMP_PATHS:
        _unlink_silent(p)
    _TILE_TEMP_PATHS.clear()
    return n


def apply_decal_color_set(
    design: adsk.fusion.Design,
    slot1: Optional[Path],
    slot2: Optional[Path],
) -> Tuple[int, List[str]]:
    """
    Set Decal.imageFilename for decals whose names match SLOT*_DECAL_NAMES.

    Walks every component in the design (including sub-components such as
    ``Plank:1``) so decals authored inside occurrences are updated too.

    If ``DECAL_POSITIONAL_FALLBACK`` is enabled, missing slot assignments are
    filled positionally (first decal -> slot 1, second -> slot 2) **even when
    the named pass already matched another slot** — e.g. a sole decal named
    ``Honeycomb-2`` would otherwise receive only ``*_2`` and never ``*_1``.
    """
    lines: List[str] = []
    _need_pil = int(DECAL_TEXTURE_IMAGE_SHIFT_PX) != 0 or int(CARRIER_TEXTURE_IMAGE_SHIFT_PX) != 0
    if _need_pil:
        try:
            import PIL  # noqa: F401 # type: ignore
        except ImportError:
            lines.append(
                "Texture bitmap shift: install Pillow in Fusion's Python "
                "(Scripts → python -m pip install pillow) or set "
                "DECAL_TEXTURE_IMAGE_SHIFT_PX / CARRIER_TEXTURE_IMAGE_SHIFT_PX to 0"
            )
    total = 0

    decal_list = _collect_all_decals(design)
    slot1_assigned = False
    slot2_assigned = False

    for d in decal_list:
        target: Optional[str] = None
        which: Optional[str] = None
        if d.name in SLOT1_DECAL_NAMES and slot1:
            target = _win_path_for_decal_image(slot1)
            which = "slot 1"
        elif d.name in SLOT2_DECAL_NAMES and slot2:
            target = _win_path_for_decal_image(slot2)
            which = "slot 2"
        if not target:
            continue
        ok, err = _set_decal_image(d, target)
        if ok:
            total += 1
            lines.append('Decal "{}" ({}): image -> {}'.format(d.name, which, target))
            if which == "slot 1":
                slot1_assigned = True
            elif which == "slot 2":
                slot2_assigned = True
            nudge_err = _nudge_decal_texture_origin(d)
            if nudge_err:
                lines.append('Decal "{}" ({}): transform nudge — {}'.format(d.name, which, nudge_err))
        else:
            lines.append('Decal "{}" ({}): failed ({})'.format(d.name, which, err))

    if DECAL_POSITIONAL_FALLBACK and decal_list:
        if slot1 and not slot1_assigned:
            d = decal_list[0]
            p1 = _win_path_for_decal_image(slot1)
            if not p1:
                pass
            else:
                ok, err = _set_decal_image(d, p1)
                if ok:
                    total += 1
                    slot1_assigned = True
                    lines.append(
                        'Decal "{}" (positional slot 1): image -> {}'.format(d.name, p1)
                    )
                    nudge_err = _nudge_decal_texture_origin(d)
                    if nudge_err:
                        lines.append(
                            'Decal "{}" (positional slot 1): transform nudge — {}'.format(d.name, nudge_err)
                        )
                else:
                    lines.append('Decal "{}" (positional slot 1): failed ({})'.format(d.name, err))
        if slot2 and not slot2_assigned and len(decal_list) >= 2:
            d = decal_list[1]
            p2 = _win_path_for_decal_image(slot2)
            if p2:
                ok, err = _set_decal_image(d, p2)
                if ok:
                    total += 1
                    slot2_assigned = True
                    lines.append(
                        'Decal "{}" (positional slot 2): image -> {}'.format(d.name, p2)
                    )
                    nudge_err = _nudge_decal_texture_origin(d)
                    if nudge_err:
                        lines.append(
                            'Decal "{}" (positional slot 2): transform nudge — {}'.format(d.name, nudge_err)
                        )
                else:
                    lines.append('Decal "{}" (positional slot 2): failed ({})'.format(d.name, err))
        elif slot2 and not slot2_assigned and len(decal_list) == 1 and not slot1_assigned:
            d = decal_list[0]
            p2 = _win_path_for_decal_image(slot2)
            if p2:
                ok, err = _set_decal_image(d, p2)
                if ok:
                    total += 1
                    slot2_assigned = True
                    lines.append(
                        'Decal "{}" (positional slot 2, sole decal): image -> {}'.format(d.name, p2)
                    )
                    nudge_err = _nudge_decal_texture_origin(d)
                    if nudge_err:
                        lines.append(
                            'Decal "{}" (positional slot 2, sole decal): transform nudge — {}'.format(
                                d.name, nudge_err
                            )
                        )
                else:
                    lines.append(
                        'Decal "{}" (positional slot 2, sole decal): failed ({})'.format(d.name, err)
                    )

    return total, lines


def _appearance_is_protected_substrate(ap_name: str) -> bool:
    """True for foam/substrate appearances that must keep their original colour."""
    n = (ap_name or "").strip().lower()
    if not n:
        return False
    return any(frag in n for frag in PROTECTED_SUBSTRATE_APPEARANCE_FRAGMENTS)


def broadcast_to_textured_appearances(
    design: adsk.fusion.Design,
    slot1: Optional[Path],
) -> Tuple[int, List[str]]:
    """Push slot 1 into every appearance that already exposes a texture slot.

    Skips appearances with no AppearanceTexture properties so plain colour
    appearances (and most metals/plastics) are left untouched. Also skips
    foam/substrate appearances so pad and core layers stay white/grey. This is
    the "fill everything textureable" pass used to get 100% coverage when the
    template hasn't been named to match SLOT*_NAMES.
    """
    lines: List[str] = []
    if not slot1:
        return 0, lines
    img = _win_path(slot1)
    total = 0
    apps = design.appearances
    for i in range(apps.count):
        ap = apps.item(i)
        if _appearance_is_protected_substrate(ap.name):
            lines.append(
                'Appearance "{}" (broadcast): skipped (protected substrate)'.format(ap.name)
            )
            continue
        n = _apply_image_to_appearance_textures(ap, img)
        if n > 0:
            total += n
            lines.append('Appearance "{}" (broadcast): {} texture(s) updated'.format(ap.name, n))
    return total, lines


def _appearance_has_texture_slot(appearance: adsk.fusion.Appearance) -> bool:
    """True if the appearance exposes at least one AppearanceTexture at any depth.

    Walks ``AppearanceTextureProperty.value`` and nested property collections so
    modern PBR appearances (which hide the texture one level down) are detected.
    """
    seen: Set[int] = set()

    def walk(coll: Any, depth: int) -> bool:
        if coll is None or depth > 28:
            return False
        try:
            n = coll.count
        except Exception:
            return False
        for i in range(n):
            try:
                p = coll.item(i)
            except Exception:
                continue
            try:
                k = id(p)
                if k in seen:
                    continue
                seen.add(k)
            except Exception:
                pass
            try:
                if adsk.core.AppearanceTexture.cast(p):
                    return True
            except Exception:
                pass
            try:
                tprop = adsk.core.AppearanceTextureProperty.cast(p)
                if tprop is not None:
                    inner = getattr(tprop, "value", None)
                    if inner is not None:
                        return True
            except Exception:
                pass
            try:
                child = getattr(p, "properties", None)
                if child is not None and walk(child, depth + 1):
                    return True
            except Exception:
                pass
        return False

    try:
        return walk(appearance.appearanceProperties, 0)
    except Exception:
        return False


def _walk_bodies(design: adsk.fusion.Design):
    """Yield every BRepBody in the design (root component + all occurrences)."""
    try:
        for body in design.rootComponent.bRepBodies:
            yield body
    except Exception:
        return
    try:
        occs = design.rootComponent.allOccurrences
    except Exception:
        return
    for i in range(occs.count):
        try:
            comp = occs.item(i).component
        except Exception:
            continue
        try:
            for body in comp.bRepBodies:
                yield body
        except Exception:
            continue


def _existing_carrier(design: adsk.fusion.Design) -> Optional[adsk.fusion.Appearance]:
    """Return a previously created carrier appearance left in the design, if any."""
    apps = design.appearances
    for i in range(apps.count):
        ap = apps.item(i)
        if ap.name == CARRIER_APPEARANCE_NAME:
            return ap
    return None


def _try_changeTextureImage(
    appearance: adsk.fusion.Appearance, image_path: str
) -> int:
    """Verified update: returns slot count actually swapped (0 means refused)."""
    return _apply_image_to_appearance_textures(appearance, image_path)


# Autodesk procedural shader families (Wood / Stone / Metal / Concrete / etc.)
# accept changeTextureImage on auxiliary slots (bump, finish noise, color-name
# slots in the shader graph) without ever using a real raster diffuse — the
# visible color is generated procedurally, so the bitmap is ignored at render
# time. Skip them outright as carrier candidates.
#
# Substrings (lower-cased) — we substring-match rather than exact-match because
# the Fusion library ships variants like "Steel - Satin", "Steel - Satin1",
# "Steel - Striped", "Bamboo Light - Semigloss", " LCD (2000)" (note leading
# space) etc., and the design often copies them with the same prefix.
_PROCEDURAL_APPEARANCE_SUBSTRINGS: Tuple[str, ...] = (
    # Wood / bamboo (procedural Wood shader)
    "pine", "oak", "maple", "walnut", "cherry", "birch", "hickory",
    "ash", "beech", "fir", "spruce", "teak", "mahogany", "bamboo",
    # Stone family (procedural Stone shader)
    "limestone", "lime stone", "granite", "marble", "sandstone",
    "slate", "travertine", "quartz",
    # Metal family (procedural Metal shader)
    "steel", "iron", "aluminum", "aluminium", "copper", "brass",
    "bronze", "chrome", "gold", "silver", "nickel", "titanium",
    "zinc", "metal", "metallic",
    # Concrete / masonry (procedural)
    "concrete", "asphalt", "brick", "cinder",
    # Paint / generic solid colors (no raster diffuse)
    "paint", "foam",
    # Display assets the model uses as lit screens, not as wrap textures
    "lcd", "display - 4x20",
    # Light source dummy
    "light",
)


def _is_procedural_carrier_name(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return True  # unnamed appearance: don't risk it as carrier
    return any(sub in n for sub in _PROCEDURAL_APPEARANCE_SUBSTRINGS)


def _reset_carrier_to_neutral(
    carrier: adsk.fusion.Appearance, lines: List[str]
) -> None:
    """Park the carrier on a throwaway PNG so the first real per-color-set swap
    is seen as a genuine change.

    Without this, ``_find_verified_in_design_carrier`` leaves the carrier
    pointing at the very first color set's slot1 image. The batch loop then
    re-applies that same path and ``changeTextureImage`` returns False because
    nothing changed — surfacing as "Textures 0 for 1 color set(s)" in the
    summary for whichever color set happens to be processed first.
    """
    probe = _write_carrier_probe_png()
    if not probe:
        return
    try:
        _apply_image_to_appearance_textures(carrier, probe)
    except Exception as ex:
        lines.append("Carrier neutral-reset failed: {}".format(ex))
    finally:
        _unlink_silent(probe)


def _try_changeTextureImage_color(
    appearance: adsk.fusion.Appearance, image_path: str
) -> Tuple[int, int]:
    """Returns ``(total_slots, color_slots)``. A carrier is only useful when
    ``color_slots > 0`` — i.e. the swap landed in a base-color/diffuse/albedo
    branch, not in bump/normal/roughness/etc.
    """
    return _apply_image_to_appearance_textures_detailed(appearance, image_path)


def _find_verified_in_design_carrier(
    design: adsk.fusion.Design, test_image_win_path: str
) -> Optional[adsk.fusion.Appearance]:
    """Return the first design appearance whose **visible color** actually swaps.

    Two-pass: prefer raster-friendly names that almost always carry a real
    diffuse texture, then fall back to anything in the design that swaps a
    color slot. Procedural species (Pine/Oak/…) are excluded entirely —
    they accept changeTextureImage on bump but never change color.
    """
    apps = design.appearances
    preferred = (
        "Wood Flooring", "Hardwood", "Laminate", "Vinyl", "Carpet",
        "Wall Paint", "Concrete", "Brick", "Stone", "Tile", "Marble",
        "Plastic", "Fabric", "Leather", "Paper",
    )

    # Pass 1: name-prefix preference, color slot required.
    for i in range(apps.count):
        ap = apps.item(i)
        if ap.name == CARRIER_APPEARANCE_NAME or _is_procedural_carrier_name(ap.name):
            continue
        if not any(p.lower() in (ap.name or "").lower() for p in preferred):
            continue
        _, color = _try_changeTextureImage_color(ap, test_image_win_path)
        if color > 0:
            return ap

    # Pass 2: any non-procedural design appearance, color slot required.
    for i in range(apps.count):
        ap = apps.item(i)
        if ap.name == CARRIER_APPEARANCE_NAME or _is_procedural_carrier_name(ap.name):
            continue
        _, color = _try_changeTextureImage_color(ap, test_image_win_path)
        if color > 0:
            return ap
    return None


def _verified_library_carrier(
    design: adsk.fusion.Design, test_image_win_path: str
) -> Tuple[Optional[adsk.fusion.Appearance], str]:
    """Copy library appearances into the design until ``changeTextureImage`` works.

    We probe with ``addByCopy`` + real ``changeTextureImage`` tests.  We do
    **not** pre-filter with ``_appearance_has_texture_slot(lib_ap)`` — on
    many Fusion builds library-side ``appearanceProperties`` do not cast to
    ``AppearanceTexture`` even though the **copied** design appearance does,
    which previously yielded **zero** candidates and ``tried 0`` in logs.

    Candidates come from ``materialLibraries`` and (when available)
    ``appearanceLibraries``, deduplicated, with Fusion/Favorites containers
    ranked ahead of third-party PBR packs so the probe budget reaches assets
    that actually accept ``changeTextureImage``.
    """
    app = adsk.core.Application.get()
    try:
        candidates, total_listed = _sorted_carrier_library_candidates(app)
    except Exception as ex:
        return None, "library candidate sweep failed: {}".format(ex)

    if total_listed == 0:
        return None, "No library appearances found (empty libraries?)"

    stale = _existing_carrier(design)
    if stale is not None:
        try:
            stale.deleteMe()
        except Exception:
            pass

    last_error = ""
    tried = 0
    cap = _carrier_probe_cap(len(candidates))
    cap_suffix = ""
    if cap < len(candidates):
        cap_suffix = "; probe list capped at {} of {} (raise MAX_LIBRARY_CARRIER_HARD_CAP or MAX_LIBRARY_CARRIER_PROBES)".format(
            cap, len(candidates)
        )

    for label, lib_ap in candidates[:cap]:
        try:
            carrier = design.appearances.addByCopy(lib_ap, CARRIER_APPEARANCE_NAME)
        except Exception as ex:
            last_error = str(ex)
            continue
        if carrier is None:
            continue
        tried += 1
        n = 0
        color = 0
        try:
            n, color = _try_changeTextureImage_color(carrier, test_image_win_path)
        except Exception as ex:
            last_error = str(ex)
        if color > 0:
            return carrier, (
                "Carrier from library: {} (verified COLOR slot, {} total / {} color; "
                "listed {} unique appearances, {} addByCopy probes{})"
            ).format(label, n, color, total_listed, tried, cap_suffix)
        try:
            carrier.deleteMe()
        except Exception:
            pass

    # If the probe budget capped the first sweep, continue through the remainder
    # in the same Fusion-first / keyword order (fixes "400 probes, 530 listed").
    if cap < len(candidates):
        for label, lib_ap in candidates[cap:]:
            try:
                carrier = design.appearances.addByCopy(lib_ap, CARRIER_APPEARANCE_NAME)
            except Exception as ex:
                last_error = str(ex)
                continue
            if carrier is None:
                continue
            tried += 1
            try:
                n = _try_changeTextureImage(carrier, test_image_win_path)
            except Exception as ex:
                n = 0
                last_error = str(ex)
            if n > 0:
                return carrier, (
                    "Carrier from library: {} (verified, {} slot; listed {} unique appearances, "
                    "{} addByCopy probes{})"
                ).format(label, n, total_listed, tried, cap_suffix)
            try:
                carrier.deleteMe()
            except Exception:
                pass

    msg = (
        "No library appearance accepted texture (listed {} unique appearances, "
        "{} addByCopy probes{}{})"
    ).format(
        total_listed,
        tried,
        cap_suffix,
        "" if not last_error else "; last error: {}".format(last_error),
    )
    return None, msg


def _first_textured_design_appearance(
    design: adsk.fusion.Design,
) -> Optional[adsk.fusion.Appearance]:
    """First design appearance that exposes a texture slot (property-only).

    Wood species names are preferred (they almost always carry a real
    diffuse bitmap), then any non-carrier appearance with a texture slot.
    This is the cheap, build-independent fallback used whenever the
    verified ``changeTextureImage`` probes are unavailable or exhausted.
    """
    apps = design.appearances
    for name in ("Pine", "Oak", "Maple", "Walnut", "Cherry", "Birch", "Wood"):
        for i in range(apps.count):
            ap = apps.item(i)
            if ap.name == name and _appearance_has_texture_slot(ap):
                return ap
    for i in range(apps.count):
        ap = apps.item(i)
        if ap.name == CARRIER_APPEARANCE_NAME:
            continue
        if _appearance_has_texture_slot(ap):
            return ap
    return None


def get_or_create_carrier(
    design: adsk.fusion.Design,
    test_image: Optional[Path] = None,
) -> Tuple[Optional[adsk.fusion.Appearance], List[str]]:
    """Find or create a carrier appearance verified to accept texture swaps.

    When ``test_image`` is provided every candidate is proved by actually
    calling ``changeTextureImage`` against it; only candidates that return
    a non-zero update count are kept. When ``test_image`` is None we fall
    back to a property-only check (less reliable but cheap), preserving
    backwards compatibility with callers that don't have an image yet.
    """
    lines: List[str] = []

    # 1) Reuse a previously generated carrier across batch runs.
    existing = _existing_carrier(design)
    if existing is not None:
        if test_image is not None:
            n, color = _try_changeTextureImage_color(existing, _win_path(test_image))
            if color > 0:
                lines.append(
                    "Carrier reused from previous run: '{}' (verified COLOR slot, {} total / {} color)".format(
                        existing.name, n, color
                    )
                )
                return existing, lines
            lines.append(
                "Existing carrier '{}' had no color slot ({} non-color updates); recreating".format(
                    existing.name, n
                )
            )
            try:
                existing.deleteMe()
            except Exception:
                pass
        else:
            lines.append("Reusing existing carrier '{}' (untested)".format(existing.name))
            return existing, lines

    if test_image is not None:
        test_path = _win_path(test_image)

        in_design = _find_verified_in_design_carrier(design, test_path)
        if in_design is not None:
            lines.append(
                "Carrier verified in design: '{}' (texture swap accepted)".format(
                    in_design.name
                )
            )
            _reset_carrier_to_neutral(in_design, lines)
            return in_design, lines

        carrier, msg = _verified_library_carrier(design, test_path)
        lines.append(msg)
        if carrier is not None:
            _reset_carrier_to_neutral(carrier, lines)
            return carrier, lines

        # Some Fusion builds reject JPG/WEBP on AppearanceTexture but still accept
        # PNG on the same library asset. Discover the carrier with a PNG probe,
        # then verify the real color-set image applies to that carrier.
        probe_png = _write_carrier_probe_png()
        if probe_png:
            lines.append(
                "Carrier retry: built-in PNG probe (color-set raster was not accepted on trial copies)"
            )
            try:
                carrier_png, msg_png = _verified_library_carrier(design, probe_png)
                lines.append(msg_png)
                if carrier_png is not None:
                    n_user, color_user = _try_changeTextureImage_color(carrier_png, test_path)
                    if color_user > 0:
                        lines.append(
                            "Carrier OK: PNG probe unlocked library copy; user image applies ({} total / {} color)".format(
                                n_user, color_user
                            )
                        )
                        return carrier_png, lines
                    lines.append(
                        "Carrier found via PNG probe but user image landed on no color slot ({} non-color updates)".format(
                            n_user
                        )
                    )
                    try:
                        carrier_png.deleteMe()
                    except Exception:
                        pass
            finally:
                _unlink_silent(probe_png)

        # Verified discovery exhausted. Don't give up: the design clearly has
        # at least one working textured appearance (the main body renders its
        # texture), so reuse the same property-only scan as the no-image path.
        # Returning None here is what previously left FORCE_BODY_COVERAGE on
        # the per-name fallback, leaving solid appearances (e.g. the end-cap
        # "Paint - Metallic (Black)") permanently un-textured.
        fb = _first_textured_design_appearance(design)
        if fb is not None:
            lines.append(
                "Carrier fallback: verified probes failed; reusing design "
                "appearance with a texture slot: '{}'".format(fb.name)
            )
            _reset_carrier_to_neutral(fb, lines)
            return fb, lines

        return None, lines

    # No test image: cheap property-only fallback first.
    fb = _first_textured_design_appearance(design)
    if fb is not None:
        lines.append("Carrier in design (untested): '{}'".format(fb.name))
        return fb, lines

    # Final fallback: probe libraries with the built-in PNG so we still get a
    # real addByCopy carrier even when the caller had no color-set image yet.
    # Without this, FORCE_BODY_COVERAGE silently does nothing on templates
    # whose design appearances don't expose AppearanceTexture in this build.
    probe_png = _write_carrier_probe_png()
    if probe_png:
        try:
            carrier_png, msg_png = _verified_library_carrier(design, probe_png)
            lines.append(msg_png)
            if carrier_png is not None:
                lines.append(
                    "Carrier created from library via built-in PNG probe (no test_image): '{}'".format(
                        carrier_png.name
                    )
                )
                return carrier_png, lines
        finally:
            _unlink_silent(probe_png)

    return None, lines + ["No carrier candidate found in design (no test_image provided)"]


def capture_body_appearances(design: adsk.fusion.Design) -> List[Tuple[Any, Any]]:
    """Snapshot ``(body, body.appearance)`` pairs for every body."""
    snap: List[Tuple[Any, Any]] = []
    for body in _walk_bodies(design):
        try:
            snap.append((body, body.appearance))
        except Exception:
            pass
    return snap


# Disabled: client requires foam/steel/paint to stay as authored; only
# Vinyl_1 / Vinyl_2 (and Vinyl Skin variants) receive batch textures.
BATCH_DECAL_NEUTRALIZE_APPEARANCES: bool = False

# Also neutralize steel/satin/striped bodies (common on decal templates).
BATCH_DECAL_NEUTRALIZE_STEEL_LIKE: bool = True

# Appearance-name fragments that trigger neutralization (dark / decorative).
_NEUTRALIZE_APPEARANCE_FRAGMENTS = (
    "metallic",
    "(black)",
    "paint",
    "vinyl",
    "lcd",
    "display",
)

# Procedural steel bodies on decal templates — gaps used to show grey.
_STEEL_LIKE_APPEARANCE_FRAGMENTS = (
    "steel",
    "striped",
    "plastic - matte",
)

# Ranked preference for the neutral backdrop (earliest wins). Prefer wood /
# light matte so uncovered slivers read as undertone, not grey metal.
_NEUTRAL_TARGET_PREFERENCES = (
    "pine",
    "oak",
    "wood",
    "bamboo light",
    "plastic - matte (white)",
    "plastic - matte",
    "lime",
    "granite",
    "steel - satin",
    "satin",
)


def _appearance_should_neutralize(ap_name: str) -> bool:
    """True when a body appearance should be swapped to the wood-tone backdrop."""
    n = (ap_name or "").lower()
    if not n:
        return False
    if _appearance_is_protected_substrate(ap_name):
        return False
    if appearance_name_slot(ap_name) is not None:
        # Never neutralize a designer slot appearance (Vinyl Skin-1/-2, etc.),
        # regardless of spacing/separator drift.
        return False
    if any(w in n for w in ("pine", "oak", "wood", "maple", "walnut", "cherry", "birch", "hickory")):
        return False
    if any(k in n for k in _NEUTRALIZE_APPEARANCE_FRAGMENTS):
        return True
    if BATCH_DECAL_NEUTRALIZE_STEEL_LIKE and any(
        k in n for k in _STEEL_LIKE_APPEARANCE_FRAGMENTS
    ):
        return True
    return False


def _pick_neutral_appearance(design):
    """Pick a clean, light, matte in-design appearance as the decal backdrop.
    Honors ``_NEUTRAL_TARGET_PREFERENCES`` as a ranked priority (the name
    matching the earliest entry wins, regardless of design iteration order).
    """
    try:
        apps = design.appearances
        n = apps.count
    except Exception:
        return None
    best_rank = None
    cand_pref = None
    cand_any = None
    for i in range(n):
        try:
            a = apps.item(i)
            nm = (a.name or "").lower()
        except Exception:
            continue
        if any(k in nm for k in _NEUTRALIZE_APPEARANCE_FRAGMENTS):
            continue
        if cand_any is None:
            cand_any = a
        for rank, pref in enumerate(_NEUTRAL_TARGET_PREFERENCES):
            if pref in nm:
                if best_rank is None or rank < best_rank:
                    best_rank = rank
                    cand_pref = a
                break
    return cand_pref or cand_any


def neutralize_dark_body_appearances(
    design: adsk.fusion.Design,
) -> Tuple[List[Tuple[Any, Any]], List[str]]:
    """Swap dark/reflective body appearances on VISIBLE bodies to a neutral
    matte one so decal gaps don't show as a black/steel stripe. Returns
    ``(snap, log_lines)``; the snapshot feeds ``restore_body_appearances`` at
    end of batch. No-op when ``BATCH_DECAL_NEUTRALIZE_APPEARANCES`` is False.
    """
    lines: List[str] = []
    if not BATCH_DECAL_NEUTRALIZE_APPEARANCES:
        lines.append("Appearance neutralization: disabled (flag off)")
        return [], lines
    neutral = _pick_neutral_appearance(design)
    if neutral is None:
        lines.append(
            "Appearance neutralization skipped: no neutral appearance found"
        )
        return [], lines
    try:
        neutral_name = neutral.name
    except Exception:
        neutral_name = "?"

    snap: List[Tuple[Any, Any]] = []
    changed = 0
    for body in _walk_bodies(design):
        try:
            if not _entity_is_visible(body, default=True):
                continue
            if _body_is_protected_substrate_body(body):
                continue
            ap = body.appearance
            if ap is None:
                continue
            nm = ap.name or ""
            if not _appearance_should_neutralize(nm):
                continue
            snap.append((body, ap))
            body.appearance = neutral
            changed += 1
        except Exception:
            continue
    lines.append(
        "Appearance neutralization: {} body(ies) swapped to '{}'".format(
            changed, neutral_name
        )
    )
    return snap, lines


def _set_body_and_faces(
    body: Any, carrier: adsk.fusion.Appearance, label: str, lines: List[str]
) -> Tuple[bool, int, int]:
    """Force carrier on a body AND on each of its faces.

    Returns ``(body_ok, faces_ok, faces_fail)``. We always try faces even
    if the body-level setter throws, because face-level wins in Fusion's
    appearance cascade and is what we really need to override per-face
    pins (the most common reason a "force-assign body appearance"
    silently fails on the wood plank top face).
    """
    body_ok = False
    try:
        body.appearance = carrier
        body_ok = True
    except Exception as ex:
        lines.append('  Body "{}": body-level FAILED ({})'.format(label, ex))

    faces_ok = 0
    faces_fail = 0
    try:
        faces = body.faces
        face_count = faces.count
    except Exception:
        face_count = 0
    for fi in range(face_count):
        try:
            face = faces.item(fi)
        except Exception:
            faces_fail += 1
            continue
        try:
            face.appearance = carrier
            faces_ok += 1
        except Exception:
            faces_fail += 1
    if body_ok or faces_ok > 0:
        lines.append(
            '  Body "{}": body-level={}, faces {}/{} OK'.format(
                label, "OK" if body_ok else "skip", faces_ok, faces_ok + faces_fail
            )
        )
    return body_ok, faces_ok, faces_fail


def _apply_to_component_contents(
    comp: adsk.fusion.Component,
    carrier: adsk.fusion.Appearance,
    prefix: str,
    stats: dict,
    lines: List[str],
) -> None:
    """Force carrier on every BRep and mesh body inside ``comp``."""
    try:
        breps = comp.bRepBodies
        brep_count = breps.count
    except Exception:
        brep_count = 0
    for bi in range(brep_count):
        try:
            body = breps.item(bi)
            label = "{}/{}".format(prefix, body.name or "?")
        except Exception:
            continue
        if _body_hide_for_batch(body, include_face_uv_pins=False):
            lines.append('  Body "{}": skipped (batch helper surface)'.format(label))
            continue
        if not _body_is_show_surface(body, prefix):
            lines.append('  Body "{}": skipped (non-show / substrate body)'.format(label))
            continue
        body_ok, faces_ok, faces_fail = _set_body_and_faces(body, carrier, label, lines)
        stats["bodies_ok" if body_ok else "bodies_fail"] += 1
        stats["faces_ok"] += faces_ok
        stats["faces_fail"] += faces_fail

    try:
        meshes = comp.meshBodies
        mesh_count = meshes.count
    except Exception:
        mesh_count = 0
    for mi in range(mesh_count):
        try:
            mbody = meshes.item(mi)
            label = "{}/{} (mesh)".format(prefix, mbody.name or "?")
        except Exception:
            continue
        if _mesh_hide_for_batch(mbody):
            lines.append('  MeshBody "{}": skipped (batch helper surface)'.format(label))
            continue
        try:
            mbody.appearance = carrier
            stats["mesh_bodies_ok"] += 1
            lines.append('  MeshBody "{}": OK'.format(label))
        except Exception as ex:
            stats["mesh_bodies_fail"] += 1
            lines.append('  MeshBody "{}": FAILED ({})'.format(label, ex))


def apply_carrier_to_all_bodies(
    design: adsk.fusion.Design, carrier: adsk.fusion.Appearance
) -> Tuple[int, int, List[str]]:
    """Force ``carrier`` onto every paintable surface in the design.

    Covers all three levels of Fusion's appearance cascade:

    1. ``Occurrence.appearance`` - the component-level default for an
       instance. Lowest precedence.
    2. ``BRepBody.appearance`` / ``MeshBody.appearance`` - overrides the
       occurrence default.
    3. ``BRepFace.appearance`` - overrides the body default. Highest
       precedence and the level the user can pin in the Appearance
       dialog ("Apply to selected face"). This is the level that has
       been silently keeping the plank looking like Pine even after
       ``body.appearance = carrier``.

    Returns ``(bodies_success, bodies_failure, log_lines)`` so the
    existing controller signature keeps working. ``log_lines`` includes
    a per-body / per-occurrence proof of which surface picked up the
    carrier and which didn't.
    """
    stats = {
        "occurrences_ok": 0,
        "occurrences_fail": 0,
        "bodies_ok": 0,
        "bodies_fail": 0,
        "faces_ok": 0,
        "faces_fail": 0,
        "mesh_bodies_ok": 0,
        "mesh_bodies_fail": 0,
    }
    lines: List[str] = []

    _apply_to_component_contents(design.rootComponent, carrier, "(root)", stats, lines)

    try:
        occs = design.rootComponent.allOccurrences
        occ_count = occs.count
    except Exception as ex:
        occ_count = 0
        lines.append("  allOccurrences error: {}".format(ex))

    for oi in range(occ_count):
        try:
            occ = occs.item(oi)
            occ_name = occ.name
        except Exception:
            continue
        if _occurrence_should_hide_batch(occ):
            lines.append(
                '  Occurrence "{}": skipped (batch helper component)'.format(occ_name)
            )
            continue
        try:
            occ.appearance = carrier
            stats["occurrences_ok"] += 1
            lines.append('  Occurrence "{}": occ-level OK'.format(occ_name))
        except Exception as ex:
            stats["occurrences_fail"] += 1
            lines.append('  Occurrence "{}": occ-level FAILED ({})'.format(occ_name, ex))
        try:
            comp = occ.component
        except Exception as ex:
            lines.append('  Occurrence "{}".component error: {}'.format(occ_name, ex))
            continue
        _apply_to_component_contents(comp, carrier, occ_name, stats, lines)

    summary_line = (
        "Carrier coverage: occs {}/{} OK, bodies {}/{} OK, faces {}/{} OK, mesh {}/{} OK".format(
            stats["occurrences_ok"],
            stats["occurrences_ok"] + stats["occurrences_fail"],
            stats["bodies_ok"],
            stats["bodies_ok"] + stats["bodies_fail"],
            stats["faces_ok"],
            stats["faces_ok"] + stats["faces_fail"],
            stats["mesh_bodies_ok"],
            stats["mesh_bodies_ok"] + stats["mesh_bodies_fail"],
        )
    )
    lines.insert(0, summary_line)

    return stats["bodies_ok"], stats["bodies_fail"], lines


def audit_design_appearances(design: adsk.fusion.Design) -> List[str]:
    """Snapshot what appearance is currently on every body/face/occurrence.

    Used as a before/after diagnostic in the summary file so you can see
    exactly which surface still has its original appearance vs. which
    one is now showing the carrier.
    """
    out: List[str] = []

    def _audit_body(body: Any, label: str) -> None:
        try:
            body_ap = body.appearance
            body_ap_name = body_ap.name if body_ap else "(none)"
        except Exception as ex:
            body_ap_name = "(error: {})".format(ex)
        face_names: dict = {}
        try:
            for fi in range(body.faces.count):
                try:
                    f = body.faces.item(fi)
                    fa = f.appearance
                    nm = fa.name if fa else "(none)"
                except Exception:
                    nm = "(err)"
                face_names[nm] = face_names.get(nm, 0) + 1
        except Exception:
            pass
        face_str = ", ".join("{}x'{}'".format(c, n) for n, c in sorted(face_names.items()))
        out.append('  {}: body="{}", faces={{{}}}'.format(label, body_ap_name, face_str))

    try:
        for bi in range(design.rootComponent.bRepBodies.count):
            body = design.rootComponent.bRepBodies.item(bi)
            _audit_body(body, "(root)/{}".format(body.name or "?"))
    except Exception:
        pass

    try:
        occs = design.rootComponent.allOccurrences
        for oi in range(occs.count):
            occ = occs.item(oi)
            try:
                occ_ap = occ.appearance.name if occ.appearance else "(none)"
            except Exception:
                occ_ap = "(error)"
            out.append('  Occurrence "{}": appearance="{}"'.format(occ.name, occ_ap))
            try:
                for bi in range(occ.component.bRepBodies.count):
                    body = occ.component.bRepBodies.item(bi)
                    _audit_body(body, "{}/{}".format(occ.name, body.name or "?"))
            except Exception:
                pass
    except Exception:
        pass

    return out


def restore_body_appearances(snap: List[Tuple[Any, Any]]) -> int:
    """Restore the appearance pointers captured by ``capture_body_appearances``."""
    n = 0
    for body, appearance in snap:
        try:
            if appearance is not None:
                body.appearance = appearance
                n += 1
        except Exception:
            pass
    return n


def update_carrier_texture(
    carrier: adsk.fusion.Appearance, slot1: Optional[Path]
) -> Tuple[int, List[str]]:
    """Swap the carrier texture to ``slot1``, optional bitmap roll + U/V offsets."""
    lines: List[str] = []
    if not slot1 or carrier is None:
        return 0, lines
    px = int(CARRIER_TEXTURE_IMAGE_SHIFT_PX)
    effective = _shifted_raster_path_or_original(slot1, px)
    if effective is None:
        return 0, lines
    if px and slot1.is_file() and effective.resolve() == slot1.resolve():
        lines.append(
            "Carrier bitmap shift: need Pillow in Fusion's Python "
            "(Scripts → python -m pip install pillow), or set CARRIER_TEXTURE_IMAGE_SHIFT_PX to 0"
        )
    n = _apply_image_to_appearance_textures(carrier, _win_path(effective))
    nu, uv_lines = _apply_texture_uv_offsets_to_appearance(
        carrier, float(CARRIER_TEXTURE_U_OFFSET), float(CARRIER_TEXTURE_V_OFFSET)
    )
    lines.extend(uv_lines)
    if nu > 0:
        lines.append("Carrier UV offsets set ({} knob(s))".format(nu))
    if px and effective.resolve() != slot1.resolve():
        lines.append("Carrier bitmap shift {} px → temp PNG".format(px))
    return n, lines


def apply_color_set_for_open_design(
    mode: str,
    slot1: Optional[Path],
    slot2: Optional[Path],
    carrier: Optional[adsk.fusion.Appearance] = None,
) -> Tuple[int, List[str]]:
    """Apply textures using the requested pipeline against the active design.

    mode:
      - 'appearance' : update document appearances by name first; if
                       APPEARANCE_BROADCAST is True, also push slot 1 to
                       every appearance with a texture slot.
      - 'decal'      : update root decals (named match + positional
                       fallback), then run the appearance pass above.
                       This avoids "decal in the middle, wood everywhere
                       else" results when a template has both.

    When ``carrier`` is provided (force-coverage mode), only the carrier
    appearance + decals are updated. The template's mixed-material
    appearances are left alone because every body has already been
    re-pointed at the carrier.
    """
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return 0, ["No active design."]

    total = 0
    lines: List[str] = []

    if mode == "decal":
        d_slot1: Optional[Path] = slot1
        d_slot2: Optional[Path] = slot2
        if carrier is not None and CARRIER_SUPPRESS_DECAL_SLOT1:
            d_slot1 = None
            lines.append(
                "Decal pass: slot1 suppressed (CARRIER_SUPPRESS_DECAL_SLOT1) - "
                "main image applies via carrier appearance only"
            )
        d_total, d_lines = apply_decal_color_set(design, d_slot1, d_slot2)
        total += d_total
        lines += d_lines

    if carrier is not None:
        n, carrier_lines = update_carrier_texture(carrier, slot1)
        total += n
        lines.append('Carrier "{}": {} texture slot(s) updated'.format(carrier.name, n))
        for cl in carrier_lines:
            lines.append("  {}".format(cl))
        return total, lines

    a_total, a_lines = apply_appearance_color_set(design, slot1, slot2)
    total += a_total
    lines += a_lines

    if APPEARANCE_BROADCAST and slot1:
        b_total, b_lines = broadcast_to_textured_appearances(design, slot1)
        total += b_total
        lines += b_lines

    return total, lines


def apply_hybrid_appearance_for_batch(
    design: adsk.fusion.Design,
    slot1: Optional[Path],
    slot2: Optional[Path],
) -> Tuple[int, List[str]]:
    """Appearance pass for bodies not covered by batch decals (e.g. Bullnose nose)."""
    total = 0
    lines: List[str] = []
    a_total, a_lines = apply_appearance_color_set(design, slot1, slot2)
    total += a_total
    lines += a_lines
    if APPEARANCE_BROADCAST and slot1:
        b_total, b_lines = broadcast_to_textured_appearances(design, slot1)
        total += b_total
        lines += b_lines
    if total:
        lines.insert(
            0,
            "Hybrid appearance pass: {} texture slot(s) updated".format(total),
        )
    return total, lines


def list_appearance_and_decal_names(design: adsk.fusion.Design) -> Tuple[List[str], List[str]]:
    """Helper for UI: show names present in the document so users can edit SLOT*_ constants."""
    anames = [design.appearances.item(i).name for i in range(design.appearances.count)]
    dnames = [d.name for d in _collect_all_decals(design)]
    return anames, dnames


def design_has_slot1_target(design: adsk.fusion.Design) -> bool:
    """True if the document has at least one appearance matching slot-1 names."""
    apps = design.appearances
    for i in range(apps.count):
        if appearance_name_slot(apps.item(i).name) == 1:
            return True
    return False


def design_has_slot2_target(design: adsk.fusion.Design) -> bool:
    """True if the document has at least one appearance matching slot-2 names."""
    apps = design.appearances
    for i in range(apps.count):
        if appearance_name_slot(apps.item(i).name) == 2:
            return True
    return False


def design_has_named_appearance_slot(design: adsk.fusion.Design) -> bool:
    """True if the document has any slot-1 or slot-2 named appearance.

    When this is True for an appearance-mode model, the controller uses the
    clean per-name appearance swap (slot1 image on slot-1 appearance, slot2 on
    slot-2) instead of blanketing every body with the slot-1 image via force
    decals / carrier — which would hide the slot-2 surface.
    """
    apps = design.appearances
    for i in range(apps.count):
        if appearance_name_slot(apps.item(i).name) is not None:
            return True
    return False


def root_has_slot2_decal_target(design: adsk.fusion.Design) -> bool:
    decals = _collect_all_decals(design)
    for d in decals:
        if d.name in SLOT2_DECAL_NAMES:
            return True
    if DECAL_POSITIONAL_FALLBACK and len(decals) >= 2:
        return True
    return False
