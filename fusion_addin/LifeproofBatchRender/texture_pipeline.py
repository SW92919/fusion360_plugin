# Fusion 360: apply colour-set images to the designer's authored decals (decal mode)
# or to document appearances (appearance mode), and size the decals from the Render
# Instructions. The name sets below match the client's .f3d (Browser names).

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

import adsk.core  # type: ignore
import adsk.fusion  # type: ignore

# Mtime of THIS file at the moment it was imported. Fusion keeps one interpreter
# per session, so an edited file is not reloaded until the add-in is stopped and
# started again. Comparing this against the file's CURRENT mtime is the only
# reliable way to tell whether the code running is the code on disk — a stamp
# built from file mtimes alone reports "fresh" even while stale code executes.
try:
    MODULE_IMPORTED_MTIME: float = os.path.getmtime(__file__)
except Exception:
    MODULE_IMPORTED_MTIME = 0.0

import image_orient

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

_SLOT1_DECAL_NAMES_NORM: FrozenSet[str] = frozenset(
    _normalize_appearance_slot_name(n) for n in SLOT1_DECAL_NAMES
)
_SLOT2_DECAL_NAMES_NORM: FrozenSet[str] = frozenset(
    _normalize_appearance_slot_name(n) for n in SLOT2_DECAL_NAMES
)
_DECAL_TRAILING_SLOT_RE = re.compile(r"([12])\s*$")


def decal_name_slot(name: str) -> Optional[int]:
    """Return 1 or 2 for a decal name, else None — tolerant of naming.

    Matches the documented ``SLOT*_DECAL_NAMES`` (normalized: case/space/sep
    insensitive), then falls back to the trailing digit, so ANY decal whose name
    ends in 1 or 2 resolves — ``Decal-1``, ``Slot_2``, ``Pattern1``, ``grid 2``,
    ``Custom_A-1`` all work regardless of exact wording. Names with no trailing
    1/2 (e.g. ``Logo``) return None.
    """
    n = (name or "").strip()
    if not n:
        return None
    norm = _normalize_appearance_slot_name(n)
    if norm in _SLOT1_DECAL_NAMES_NORM:
        return 1
    if norm in _SLOT2_DECAL_NAMES_NORM:
        return 2
    m = _DECAL_TRAILING_SLOT_RE.search(n)
    if m:
        return int(m.group(1))
    return None


# When the named matches above produce zero updates, fall back to positional
# matching (first decal -> slot 1, second decal -> slot 2). Disable to force
# strict naming.
DECAL_POSITIONAL_FALLBACK: bool = True


# Centimetres of rendered extent per 1.0 of transform-axis magnitude.
#
# 1.0 → magnitude IS the full extent (9 in → |Y| = 22.86)
# 2.0 → magnitude is the HALF extent  (9 in → |Y| = 11.43)
#
# 1.0 is confirmed in Fusion (2026-10): the client's authored decals read back at
# the parts' real sizes (Bullnose nose 123.7 x 22.6 cm = its 9 in width), and the
# Render Instructions sizes written with Decal.redefine render at exactly that size
# (T-Molding at 1 in wide showed a 2.54 cm strip).
BATCH_DECAL_MAGNITUDE_CM_PER_UNIT: float = 1.0


# Per-model decal targets from the Render Instructions JSON, keyed by
# ``render_instructions.normalize_part_key(componentPartNumber)``. Each value is a
# ``DecalSizeSpec`` (width_cm / length_cm / slot / part_number). Set once per model
# by the controller; empty means "no JSON targets" and the legacy filename-derived
# targeting applies.
_DECAL_TARGETS_BY_PART: Dict[str, Any] = {}


# --- Authored-decal image swap ----------------------------------------------
# A decal's footprint is its transform: |X| x |Y| in cm (Bullnose plank 13.0 x
# 121.5), with the image's horizontal axis along X, and Fusion stretches whatever
# image it is given over that footprint. Most footprints run along Y (portrait)
# while the client's colour-set rasters are LANDSCAPE (2500x486 ...), so dropped in
# as-is the texture runs ACROSS the part. The raster is therefore turned 90 degrees
# whenever its long side would otherwise lie across the footprint's long side ...
DECAL_MATCH_AUTHORED_IMAGE_ORIENTATION: bool = True
# ... deciding from the footprint first, and from the designer's own image only
# when the footprint is not clearly elongated or unreadable (on Bullnose the two
# agree; its footprints are 9.3:1 and 5.5:1).
DECAL_ORIENT_BY_FOOTPRINT: bool = True
DECAL_FOOTPRINT_DECISIVE_RATIO: float = 1.5
# Also resample to the authored image's proportions. With the footprint fixed this
# does not move or resize the decal; it is kept because it is exactly what produced
# the approved Bullnose renders. Skipped when the authored image ran the other way
# to the footprint (fitting to it would only throw resolution away).
DECAL_CONFORM_TO_AUTHORED_ASPECT: bool = True
# Clockwise vs counter-clockwise only decides which END of the part the image
# starts from (a 180-degree difference). Flip if directional artwork reads backwards.
DECAL_IMAGE_ROTATE_CLOCKWISE: bool = True
# When an authored decal refuses a new image (its face reference is broken in the
# .f3d), hide it and create a stand-in with the SAME placement. Off = just report.
DECAL_REPLACE_UNWRITABLE_AUTHORED: bool = True
# Log one state line per authored decal per model.
DECAL_PROBE_LOG: bool = True

# --- Render Instructions decal size (deliverable 2) -------------------------
# Give every authored decal on a listed part the width x length its Render
# Instructions entry specifies, as the magnitudes of the decal's X/Y axes in cm.
# Decal.transform cannot be assigned; the API's documented route is
# Decal.redefine(matrix, faces, isChainFaces) with the timeline marker rolled to
# just before the decal. The designer's centre and axis directions are kept; the
# decal's longer side receives the JSON length. Off = keep the designer's sizes.
DECAL_APPLY_JSON_SIZE: bool = True
# A decal whose sides are within this ratio has no clear length direction, so its
# size is left alone (every decal in the client's models is 5:1 or longer).
DECAL_SIZE_MIN_ELONGATION: float = 1.2
# A JSON width more than this fraction narrower than the designer's decal may
# leave the part's edges bare, so it is reported.
DECAL_SIZE_NARROWER_WARN: float = 0.03
# Distance (cm) from a part's end that counts as "not reached".
DECAL_SIZE_END_GAP_CM: float = 0.3
# A resized decal whose length Fusion turned further than this (degrees, on the
# surface) would show the grain crooked, so the designer's size is put back.
DECAL_SIZE_MAX_TURN_DEG: float = 5.0
# How far (cm) Fusion may put a resized decal's centre ACROSS the part before the
# designer's size is put back. Wrapping decals come back shifted sideways (Square
# Nose plank 0.59 cm, Bullnose plank 0.14 cm, 2026-10-15) and Fusion shifts the
# designer's own placement the same way, so undoing gains nothing. Along the part
# the limit stays DECAL_SIZE_END_GAP_CM, since that is what leaves an end bare.
DECAL_SIZE_MAX_ACROSS_SHIFT_CM: float = 1.0
# When Fusion refuses a centre moved to the middle of the part, the JSON size is
# applied at the designer's centre instead only if at most this much (cm) of one
# end then stays bare (T-Molding: 0.7 cm; 3-in-1 would leave 58 cm, so not there).
DECAL_SIZE_FALLBACK_MAX_BARE_CM: float = 1.0

# Per-model bookkeeping for authored decals, keyed by (component id, index in that
# component's Decals folder). Reset by ``set_decal_targets`` for every model.
_AUTHORED_SLOT_BY_KEY: Dict[Tuple[str, int], int] = {}
_AUTHORED_IMAGE_BY_KEY: Dict[Tuple[str, int], Tuple[Any, Any, str]] = {}
_RETIRED_AUTHORED_KEYS: Set[Tuple[str, int]] = set()
_PROBED_DECAL_KEYS: Set[Tuple[str, int]] = set()
_SKIP_REPORTED_KEYS: Set[Tuple[str, int]] = set()
# Why each decal never renders AS THE DESIGNER SAVED IT ("" = it renders). Taken the
# first time a decal is seen, before anything is written, so a decal this add-in
# hid itself is never mistaken for one the designer switched off.
_DESIGNER_INACTIVE_BY_KEY: Dict[Tuple[str, int], str] = {}
# Stand-ins that failed verification and could not be removed: they carry the part
# from then on, but every colour set is flagged for a visual check.
_SUSPECT_STAND_IN_KEYS: Set[Tuple[str, int]] = set()
# Render Instructions sizes: decals already handled this model, sizes waiting for
# the stand-in of a decal whose own face link is broken, and whether the sizing
# pass has run for the open model.
_SIZED_KEYS: Set[Tuple[str, int]] = set()
_STANDIN_SIZE_BY_KEY: Dict[Tuple[str, int], Any] = {}
_JSON_SIZES_DONE: List[bool] = [False]
# Set when the timeline marker could not be put back: no further decal is sized.
_SIZING_ABORTED: List[bool] = [False]
# "The JSON size may not cover this part" notes, popped by the controller.
_DECAL_SIZE_WARNINGS: List[str] = []
_DECAL_SWAP_FAILURES: List[str] = []
_CURRENT_MODEL_PATH: Optional[Path] = None
_EMBEDDED_SIZES_BY_MODEL: Dict[Path, list] = {}
# Picking a decal's image among several embedded ones (see _pick_embedded_image),
# as natural-log differences of width/height: 0.03 ~ 3 %, 0.05 ~ 5 %.
_EMBEDDED_FOOTPRINT_MATCH_TOL: float = 0.03
_EMBEDDED_SIMILAR_TOL: float = 0.05


def set_decal_targets(
    targets: Optional[Dict[str, Any]], model_path: Optional[Path] = None
) -> None:
    """Install this model's JSON decal targets (part_key -> DecalSizeSpec).

    Called once per model, so it is also where the per-model authored-decal
    bookkeeping is reset — component ids and decal indexes mean nothing across
    documents.
    """
    global _DECAL_TARGETS_BY_PART, _CURRENT_MODEL_PATH
    _DECAL_TARGETS_BY_PART = dict(targets or {})
    _CURRENT_MODEL_PATH = Path(model_path) if model_path else None
    _AUTHORED_SLOT_BY_KEY.clear()
    _AUTHORED_IMAGE_BY_KEY.clear()
    _RETIRED_AUTHORED_KEYS.clear()
    _PROBED_DECAL_KEYS.clear()
    _SKIP_REPORTED_KEYS.clear()
    _DESIGNER_INACTIVE_BY_KEY.clear()
    _SUSPECT_STAND_IN_KEYS.clear()
    _SIZED_KEYS.clear()
    _STANDIN_SIZE_BY_KEY.clear()
    _JSON_SIZES_DONE[0] = False
    _SIZING_ABORTED[0] = False
    del _DECAL_SIZE_WARNINGS[:]
    del _DECAL_SWAP_FAILURES[:]
    _EMBEDDED_SIZES_BY_MODEL.clear()


def _part_key(name: str) -> str:
    """Normalize a Fusion browser name for comparison with a JSON part number."""
    try:
        from render_instructions import normalize_part_key

        return normalize_part_key(name or "")
    except Exception:
        return re.sub(r"[\s\-_]+", "-", (name or "").strip().lower()).strip("-")


def decal_target_for_names(*names: str) -> Optional[Any]:
    """First JSON decal target matching any of these occurrence/component/body names."""
    if not _DECAL_TARGETS_BY_PART:
        return None
    for name in names:
        if not name:
            continue
        spec = _DECAL_TARGETS_BY_PART.get(_part_key(name))
        if spec is not None:
            return spec
    return None


def _json_slot_for_component(comp: Any, occ: Any = None) -> Optional[int]:
    """Slot for an authored decal, from the component whose Decals folder holds it.

    Preferred over ``_json_slot_for_decal`` because it needs nothing from the decal
    itself — it works even when the decal's face cannot be resolved by the API.
    """
    if not _DECAL_TARGETS_BY_PART or comp is None:
        return None
    names: List[str] = []
    for getter in (
        lambda: occ.name,
        lambda: comp.name,
        lambda: comp.partNumber,
    ):
        try:
            value = getter()
        except Exception:
            continue
        if value:
            names.append(value)
    spec = decal_target_for_names(*names)
    slot = getattr(spec, "slot", None) if spec is not None else None
    return slot if slot in (1, 2) else None


def _json_spec_for_decal(decal: Any, comp: Any, occ: Any) -> Optional[Any]:
    """The Render Instructions entry (DecalSizeSpec) for the part a decal textures.

    Same routing as the slot: the owning component for a decal in a part's own
    Decals folder, the face's occurrence for one in the ROOT folder.
    """
    if not _DECAL_TARGETS_BY_PART:
        return None
    if occ is None:
        try:
            face = decal.faces[0]
            part_occ = face.assemblyContext if face is not None else None
        except Exception:
            return None
        if part_occ is None:
            return None
        getters = (
            lambda: part_occ.name,
            lambda: part_occ.component.name,
            lambda: part_occ.component.partNumber,
        )
    else:
        getters = (lambda: occ.name, lambda: comp.name, lambda: comp.partNumber)
    names: List[str] = []
    for getter in getters:
        try:
            value = getter()
        except Exception:
            continue
        if value:
            names.append(value)
    return decal_target_for_names(*names)


def _json_slot_for_decal_face(decal: Any) -> Tuple[Optional[int], bool]:
    """``(slot, face_readable)`` from the part a ROOT-folder decal's face is on.

    The root Decals folder says nothing about which part a decal textures, and
    the ROOT's own name must never be used: in most of the one-tone models it is
    the product name itself ("End Cap", "L-Cap" ...) and would match the trim for
    every root decal. The face does tell: a face on a sub-part is a proxy whose
    ``assemblyContext`` is that part's occurrence. A face with no assembly context
    lies on a root body — not a listed part.
    """
    try:
        face = decal.faces[0]
    except Exception:
        return None, False
    if face is None:
        return None, False
    if not _DECAL_TARGETS_BY_PART:
        return None, True
    try:
        occ = face.assemblyContext
    except Exception:
        occ = None
    if occ is None:
        return None, True
    names: List[str] = []
    for getter in (
        lambda: occ.name,
        lambda: occ.component.name,
        lambda: occ.component.partNumber,
    ):
        try:
            value = getter()
        except Exception:
            continue
        if value:
            names.append(value)
    spec = decal_target_for_names(*names)
    slot = getattr(spec, "slot", None) if spec is not None else None
    return (slot if slot in (1, 2) else None), True


# Skip end-cap / cut faces at the start & end of long bodies (normal parallel
# to the body's longest axis). Show faces (top, nose, back, sides) are kept.
BATCH_DECAL_SKIP_END_CAP_FACES: bool = True
BATCH_DECAL_END_CAP_NORMAL_DOT: float = 0.82


# Body-NAME fragments marking the visible show surface. The client's models name
# it "<Part> Skin" — ``Nose Skin``, ``Plank Skin`` — or, in older files, "Nose Wear
# Layer". When a stand-in decal has to be placed (Bullnose's broken nose decal),
# the face under the designer's decal is picked from such a body first, so the
# texture lands on the visible surface rather than on an internal body.
BATCH_DECAL_BODY_PREFER_NAME_KEYWORDS: Tuple[str, ...] = ("skin", "wear layer", "wearlayer")


def _win_path(path: Path) -> str:
    """Fusion on Windows accepts normalized paths with backslashes."""
    return os.path.normpath(str(path.resolve()))


def _win_path_for_decal_image(src: Optional[Path]) -> Optional[str]:
    """Return the path to give Decal.imageFilename, or None if the file is missing."""
    return _win_path(src) if src and src.is_file() else None


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

    Which appearances are touched at all is decided by name (the slot lists
    above), so this per-slot filter has one job: reject swaps that *clearly*
    landed on a non-color branch. Anything else — including
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
            # Image attached as a connected texture on a Color/other property
            # (ColorProperty.connectedTexture). This is how the client's
            # Vinyl Skin-1/-2 appearances hold their photo — there is NO
            # standalone AppearanceTexture node, so the casts above miss it.
            try:
                if getattr(p, "hasConnectedTexture", False):
                    ct = p.connectedTexture
                    if ct is not None:
                        if _try_change_on(ct):
                            _record(child_parents)
                        ct_props = getattr(ct, "properties", None)
                        if ct_props is not None:
                            walk(ct_props, child_parents, depth + 1)
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


# When True, and an appearance image swap changes 0 textures, dump that
# appearance's full property tree to the summary so we can see exactly where the
# image node lives on this Fusion build (property name/type + whether it exposes
# changeTextureImage / a connected texture). Behavior-safe: logging only.
DIAG_DUMP_APPEARANCE_TREE: bool = True


def dump_appearance_property_tree(
    appearance: adsk.fusion.Appearance, max_depth: int = 12
) -> List[str]:
    """Return human-readable lines describing every property under an appearance.

    Flags each node with what the image-swap walk cares about:
      * ``changeTextureImage``          — node itself exposes the method
      * ``AppearanceTexture``           — castable to a texture object
      * ``AppearanceTextureProperty``   — a property whose ``.value`` is a texture
      * ``value=<Type>``                — the texture-property's current value type
    Used only for diagnostics when a swap reports 0 texture updates.
    """
    lines: List[str] = []
    seen: Set[int] = set()

    def _describe(p: Any) -> str:
        try:
            name = p.name or "?"
        except Exception:
            name = "?"
        try:
            otype = p.objectType or type(p).__name__
        except Exception:
            otype = type(p).__name__
        flags: List[str] = []
        if callable(getattr(p, "changeTextureImage", None)):
            flags.append("changeTextureImage")
        try:
            tex = adsk.core.AppearanceTexture.cast(p)
        except Exception:
            tex = None
        if tex is not None:
            flags.append("AppearanceTexture")
            if callable(getattr(tex, "changeTextureImage", None)):
                flags.append("tex.changeTextureImage")
        try:
            tprop = adsk.core.AppearanceTextureProperty.cast(p)
        except Exception:
            tprop = None
        if tprop is not None:
            flags.append("AppearanceTextureProperty")
            try:
                val = tprop.value
                flags.append("value={}".format(type(val).__name__ if val is not None else "None"))
                if val is not None and callable(getattr(val, "changeTextureImage", None)):
                    flags.append("value.changeTextureImage")
            except Exception as ex:
                flags.append("value_err={}".format(ex))
        try:
            if getattr(p, "hasConnectedTexture", False):
                flags.append("hasConnectedTexture")
                ct = p.connectedTexture
                if ct is not None:
                    flags.append("connectedTexture={}".format(type(ct).__name__))
                    try:
                        ct_otype = ct.objectType
                        if ct_otype:
                            flags.append("ct.objectType={}".format(ct_otype))
                    except Exception:
                        pass
                    if callable(getattr(ct, "changeTextureImage", None)):
                        flags.append("ct.changeTextureImage")
        except Exception as ex:
            flags.append("ct_err={}".format(ex))
        return "{} : {} [{}]".format(name, otype, ", ".join(flags) if flags else "-")

    def walk(coll: Any, depth: int, prefix: str) -> None:
        if coll is None or depth > max_depth:
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
            lines.append("{}{}".format(prefix, _describe(p)))
            for sub in _appearance_texture_child_property_lists(p):
                walk(sub, depth + 1, prefix + "  ")
            try:
                child = getattr(p, "properties", None)
                if child is not None and child is not coll:
                    walk(child, depth + 1, prefix + "  ")
            except Exception:
                pass

    try:
        walk(appearance.appearanceProperties, 0, "")
    except Exception as ex:
        lines.append("(property-tree dump failed: {})".format(ex))
    return lines or ["(no properties enumerated)"]


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

    # Snapshot the matching appearances BEFORE editing any of them.
    # ``changeTextureImage`` can localize a library-linked appearance, which
    # inserts a copy into ``design.appearances`` and shifts the live indices
    # mid-loop — that caused Skin-1 to be visited twice and Skin-2 to be skipped
    # on later color sets. Collecting targets first (this loop never modifies),
    # then editing the fixed ``targets`` list, avoids that. Every matching
    # appearance is updated (incl. duplicate-named ones) so whichever instance a
    # body references receives the image; duplicate log lines are cosmetic.
    #
    # NOTE: do NOT dedupe by ``id(ap)`` — the unmatched appearances are released
    # each iteration and CPython reuses their addresses, so ``id`` collides
    # across different appearances and wrongly skips the Vinyl slots.
    targets: List[Tuple[Any, int, str]] = []
    for i in range(apps.count):
        try:
            ap = apps.item(i)
        except Exception:
            continue
        try:
            ap_name = ap.name or ""
        except Exception:
            ap_name = ""
        slot = appearance_name_slot(ap_name)
        if slot == 1 and slot1:
            target_path = _win_path(slot1)
        elif slot == 2 and slot2:
            target_path = _win_path(slot2)
        else:
            continue
        targets.append((ap, slot, target_path))

    for ap, slot, target_path in targets:
        try:
            ap_name = ap.name
        except Exception:
            ap_name = "?"
        n = _apply_image_to_appearance_textures(ap, target_path)
        total += n
        lines.append('Appearance "{}": {} texture(s) updated'.format(ap_name, n))
        if n == 0 and DIAG_DUMP_APPEARANCE_TREE:
            lines.append(
                '  DIAG property tree for "{}" (slot {}) — why changeTextureImage found 0:'.format(
                    ap_name, slot
                )
            )
            for tree_line in dump_appearance_property_tree(ap):
                lines.append("    " + tree_line)
    return total, lines


def _set_decal_image(decal: "adsk.fusion.Decal", image_path: str) -> Tuple[bool, str]:
    try:
        decal.imageFilename = image_path
        return True, ""
    except Exception as ex:
        return False, str(ex)


def _collect_decals_with_owner(
    design: adsk.fusion.Design,
) -> List[Tuple[adsk.fusion.Decal, Any, Any, int]]:
    """Every Decal as ``(decal, owning_component, an_occurrence_or_None, index)``.

    ``index`` is the decal's position in its component's Decals folder; together
    with the component id it identifies the decal for the whole session.

    Decals can live on the root component **or inside any nested component**
    (e.g. ``Plank:1 → Decals``). Walking only ``root.decals`` would miss those
    and leave their ``imageFilename`` untouched, so the render keeps the
    original placeholder texture on the affected surface.

    The owner is recorded HERE, from the collection the decal was found in,
    because ``Decal`` has no ``parentComponent`` and going through
    ``decal.faces[0].body`` fails on a decal whose face the API cannot resolve
    (Bullnose's "dusk-cherry - 1 - Horizontal" reports ``faces == [None]``).
    """
    found: List[Tuple[adsk.fusion.Decal, Any, Any, int]] = []
    seen: set = set()

    def _drain(comp: adsk.fusion.Component, occ: Any) -> None:
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
                found.append((comp_decals.item(j), comp, occ, j))
            except Exception:
                continue

    try:
        _drain(design.rootComponent, None)
    except Exception:
        return found
    try:
        occs = design.rootComponent.allOccurrences
    except Exception:
        return found
    for i in range(occs.count):
        try:
            occ = occs.item(i)
            comp = occ.component
        except Exception:
            continue
        _drain(comp, occ)
    return found


def _collect_all_decals(design: adsk.fusion.Design) -> List[adsk.fusion.Decal]:
    """Return every Decal in the design (root + every sub-component), deduped."""
    return [d for d, _comp, _occ, _j in _collect_decals_with_owner(design)]


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


def _normalize_fusion_body_name(name: str) -> str:
    """Collapse Fusion browser spacing (incl. NBSP) for name comparisons."""
    s = (name or "").strip().replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s)


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


def audit_decal_body_is_show_only(
    body: adsk.fusion.BRepBody,
) -> Tuple[bool, str]:
    """Is this body a show-only 'Skin', or a full solid that includes cut ends?

    ``isChainFaces=True`` makes Fusion wrap the decal onto EVERY face of the body
    (Autodesk's own wording in Decal.redefine), so the add-in cannot stop the
    texture reaching the cut ends — the BODY decides the painted set.

    Bullnose renders correctly because its ``Nose Skin`` holds only the visible
    outer surface. A body that still contains end-cap faces will show texture on
    the cut ends no matter what this code does, so the model has to be fixed, not
    the pipeline. That makes this audit the fastest way to tell whether a given
    .f3d is ready.

    Returns ``(is_show_only, human_readable_summary)``.
    """
    try:
        faces = body.faces
        n = faces.count
    except Exception as ex:
        return False, "faces unreadable ({})".format(ex)
    if not n:
        return False, "no faces"

    n_end_cap = 0
    end_cap_area = 0.0
    total_area = 0.0
    for i in range(n):
        try:
            f = faces.item(i)
        except Exception:
            continue
        try:
            area = _face_area(f)
        except Exception:
            area = 0.0
        total_area += area
        try:
            if _is_end_cap_face(f, body):
                n_end_cap += 1
                end_cap_area += area
        except Exception:
            continue

    show_only = n_end_cap == 0
    verdict = "SHOW-ONLY (safe to chain-wrap)" if show_only else (
        "FULL SOLID — {} cut-end face(s), {:.0f} cm²: the texture WILL reach the "
        "cut ends. Needs a show-only skin body like Bullnose's 'Nose Skin'.".format(
            n_end_cap, end_cap_area
        )
    )
    return show_only, "{} face(s), {:.0f} cm² total, {} end-cap → {}".format(
        n, total_area, n_end_cap, verdict
    )


def _matrix_from_axes(
    origin: adsk.core.Point3D,
    x_axis: adsk.core.Vector3D,
    y_axis: adsk.core.Vector3D,
    z_axis: adsk.core.Vector3D,
) -> Tuple[Optional[adsk.core.Matrix3D], Optional[str]]:
    """Build a Matrix3D from a decal coordinate system (multi-method fallback).

    ``setWithCoordinateSystem`` returns a bool — Fusion rejects a coordinate system
    whose axes are degenerate or not usable. That result used to be discarded, so a
    rejected system returned an untouched identity matrix that then read as success
    and silently placed the decal at Fusion's default size and orientation.
    """
    m = adsk.core.Matrix3D.create()
    for method_name in (
        "setWithCoordinateSystem",
        "setToCoordinateSystem",
        "setToAlignCoordinateSystems",
    ):
        try:
            method = getattr(m, method_name)
            result = method(origin, x_axis, y_axis, z_axis)
            if result is False:
                return None, "{} rejected the coordinate system".format(method_name)
            return m, None
        except TypeError:
            continue
        except Exception as ex:
            return None, "{}: {}".format(method_name, ex)
    return None, "no coordinate-system setter on Matrix3D"


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


def _set_decal_input_chain_faces(decal_input: Any, chain_faces: bool) -> bool:
    """Set Fusion ``DecalInput`` chain-faces flag; tolerate API name variants."""
    for attr in ("isChainFaces", "chainFaces"):
        try:
            setattr(decal_input, attr, bool(chain_faces))
            return True
        except Exception:
            continue
    return False


def _authored_image_info(
    decal: Any, key: Tuple[str, int]
) -> Tuple[Optional[Tuple[int, int]], Optional[str], str]:
    """``(size, orientation, source)`` of the image the DESIGNER gave this decal.

    Cached per decal for the current model, and read BEFORE the first write — once
    we swap the image the decal reports ours instead. Three sources, most direct
    first; each can legitimately fail, so the result may be ``(None, None, ...)``.
    """
    if key in _AUTHORED_IMAGE_BY_KEY:
        return _AUTHORED_IMAGE_BY_KEY[key]

    size: Optional[Tuple[int, int]] = None
    source = "unknown"
    suffix = ""

    # 1) The file Fusion says the decal was created from, when it still exists.
    try:
        current = str(decal.imageFilename or "")
    except Exception:
        current = ""
    if current:
        suffix = Path(current).suffix.lower()
        try:
            if os.path.isfile(current):
                size = image_orient.image_size(Path(current))
                if size:
                    source = "decal image file"
        except Exception:
            size = None

    # 2) Ask the decal to save its image (documented for exactly this case).
    if size is None:
        tmp = image_orient.cache_root() / "authored" / "p{}_{}_{}.png".format(
            os.getpid(), len(_AUTHORED_IMAGE_BY_KEY), key[1]
        )
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            if tmp.exists():
                tmp.unlink()  # saveImage refuses to overwrite
            if decal.saveImage(str(tmp)):
                size = image_orient.image_size(tmp)
                if size:
                    source = "decal.saveImage"
        except Exception:
            size = None
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    orient = image_orient.orientation_of(size)

    # 3) The images embedded in the .f3d. Fusion stores them under new names, so
    #    which image belongs to which decal has to be inferred (see below).
    if size is None and _CURRENT_MODEL_PATH is not None:
        if _CURRENT_MODEL_PATH not in _EMBEDDED_SIZES_BY_MODEL:
            _EMBEDDED_SIZES_BY_MODEL[_CURRENT_MODEL_PATH] = image_orient.embedded_image_sizes(
                _CURRENT_MODEL_PATH
            )
        embedded = _EMBEDDED_SIZES_BY_MODEL[_CURRENT_MODEL_PATH]
        want = _image_suffix_key(suffix)
        same_type = [s for ext, s in embedded if want and _image_suffix_key(ext) == want] or [
            s for _ext, s in embedded
        ]
        e_size, e_orient, e_source = _pick_embedded_image(decal, same_type)
        if e_orient is not None:
            size, orient, source = e_size, e_orient, e_source

    info = (size, orient, source)
    _AUTHORED_IMAGE_BY_KEY[key] = info
    return info


def _image_suffix_key(suffix: str) -> str:
    s = (suffix or "").lower()
    return {".jpeg": ".jpg", ".tif": ".tiff"}.get(s, s)


def _decal_footprint_size(decal: Any) -> Optional[Tuple[float, float]]:
    """``(|X|, |Y|)`` of the decal's transform — the area it covers, in cm.

    Measured on Bullnose: plank 13.0 x 121.5, nose 123.7 x 22.6 — the parts' real
    visible width and length, so the magnitudes are the footprint itself and the
    image's horizontal axis runs along X.
    """
    try:
        _o, x_axis, y_axis, _z = decal.transform.getAsCoordinateSystem()
        mag_x, mag_y = _vector_length(x_axis), _vector_length(y_axis)
    except Exception:
        return None
    if mag_x <= 1e-9 or mag_y <= 1e-9:
        return None
    return mag_x, mag_y


def _decal_footprint_aspect(decal: Any) -> Optional[float]:
    """X/Y magnitude ratio of the decal's transform — the proportions it covers."""
    footprint = _decal_footprint_size(decal)
    return footprint[0] / footprint[1] if footprint else None


def _pick_embedded_image(
    decal: Any, candidates: List[Tuple[int, int]]
) -> Tuple[Optional[Tuple[int, int]], Optional[str], str]:
    """This decal's image among the .f3d's embedded images of its file type.

    In order of confidence: the only candidate; the candidate whose proportions
    the decal's footprint has (a designer placing an image without stretching it
    gives exactly that, and a coincidental 3% match is unlikely); any candidate
    when all are within 5% of each other; otherwise orientation alone, when every
    candidate agrees on it. Anything else is reported as unknown — never guessed.
    """
    if not candidates:
        return None, None, "unknown"
    if len(candidates) == 1:
        size = candidates[0]
        return size, image_orient.orientation_of(size), ".f3d embedded image"

    aspects = [(image_orient.aspect_of(s), s) for s in candidates]
    aspects = [(a, s) for a, s in aspects if a]
    footprint = _decal_footprint_aspect(decal)
    if footprint and aspects:
        scored = sorted(
            (min(abs(math.log(footprint / a)), abs(math.log(footprint * a))), s)
            for a, s in aspects
        )
        if scored[0][0] <= _EMBEDDED_FOOTPRINT_MATCH_TOL:
            size = scored[0][1]
            return (
                size,
                image_orient.orientation_of(size),
                ".f3d embedded image with the decal's proportions",
            )

    if aspects:
        ordered = sorted(aspects)
        if math.log(ordered[-1][0] / ordered[0][0]) <= _EMBEDDED_SIMILAR_TOL:
            size = ordered[len(ordered) // 2][1]
            return (
                size,
                image_orient.orientation_of(size),
                ".f3d embedded images ({} of near-identical proportions)".format(len(ordered)),
            )

    orients = {image_orient.orientation_of(s) for s in candidates}
    if len(orients) == 1 and None not in orients:
        return None, orients.pop(), ".f3d embedded images (orientation only)"
    return None, None, "unknown"


def _prepare_decal_image(
    base: str, decal: Any, key: Tuple[str, int]
) -> Tuple[str, str]:
    """Path to hand to ``decal.imageFilename`` plus a log note.

    Fusion stretches whatever image it is given over the decal's footprint, so the
    colour-set raster is turned 90 degrees when needed to put its long side along
    the footprint's long side. The footprint decides; the authored image's
    orientation is the fallback when the footprint is unreadable or square (on
    Bullnose the two agree for both decals). When they agree, the raster is also
    resampled to the authored proportions, as in the approved Bullnose renders.
    Falls back to ``base`` untouched whenever the direction cannot be determined.
    """
    if not DECAL_MATCH_AUTHORED_IMAGE_ORIENTATION:
        return base, "orientation matching off"
    incoming = image_orient.image_size(Path(base))
    if incoming is None:
        return base, "NOT ADJUSTED: cannot read size of {}".format(Path(base).name)

    auth_size, auth_orient, source = _authored_image_info(decal, key)
    footprint = _decal_footprint_size(decal) if DECAL_ORIENT_BY_FOOTPRINT else None
    fp_orient = image_orient.orientation_of(footprint)
    directional = (image_orient.PORTRAIT, image_orient.LANDSCAPE)
    # Only a clearly elongated footprint outranks the designer's image; a nearly
    # square one says little about which way the part runs.
    fp_decisive = (
        fp_orient in directional
        and max(footprint) / min(footprint) >= DECAL_FOOTPRINT_DECISIVE_RATIO
    )
    if fp_decisive:
        want = fp_orient
        agree = (auth_orient == fp_orient) if auth_orient in directional else None
    elif auth_orient in directional:
        want, agree = auth_orient, None
    else:
        return base, (
            "NOT ADJUSTED: neither the decal's footprint nor its authored image "
            "shows which way the texture runs"
        )

    rotate = image_orient.needs_rotation(want, image_orient.orientation_of(incoming))
    aspect = (
        image_orient.aspect_of(auth_size)
        if (DECAL_CONFORM_TO_AUTHORED_ASPECT and auth_size and agree is not False)
        else None
    )
    prepared, how = image_orient.prepared_copy(
        Path(base),
        rotate=rotate,
        clockwise=bool(DECAL_IMAGE_ROTATE_CLOCKWISE),
        target_aspect=aspect,
    )
    if prepared is None:
        return base, "NOT ADJUSTED: {}".format(how)

    out = image_orient.image_size(prepared) or incoming
    natural = (incoming[1], incoming[0]) if rotate else incoming
    # How far the texture is stretched across its SHORT side to fill the authored
    # area (1.00 = the client's image already had the designer's proportions).
    try:
        stretch = (min(out) / float(max(out))) / (min(natural) / float(max(natural)))
    except ZeroDivisionError:
        stretch = 1.0
    note = "footprint {} | authored {} ({}, via {}) | incoming {}x{} | {}{} -> {}x{} [{}]".format(
        "{:.1f}x{:.1f} cm ({})".format(footprint[0], footprint[1], fp_orient)
        if footprint
        else "unknown",
        "{}x{}".format(*auth_size) if auth_size else "size unknown",
        auth_orient,
        source,
        incoming[0],
        incoming[1],
        "rotated 90 " if rotate else "not rotated ",
        "+ fitted to authored proportions (x{:.2f} across)".format(stretch)
        if abs(stretch - 1.0) > 0.01
        else "",
        out[0],
        out[1],
        how,
    )
    if agree is False:
        note += (
            " | NOTE the authored image ran the other way to the footprint; "
            "followed the footprint"
        )
    return _win_path(prepared), note


def _find_face_at_point(
    comp: Any, point: Any, z_axis: Any
) -> Tuple[Optional[Any], List[str]]:
    """The face of ``comp`` a decal centred on ``point`` (normal ``z_axis``) sits on.

    Used only to rebuild a decal whose own face reference the API cannot resolve.
    A candidate must contain the point AND face the same way as the decal; a
    "... Skin" / wear-layer body wins over the solid beneath it. Returns
    ``(None, notes)`` rather than guessing when nothing qualifies or two bodies tie.
    """
    notes: List[str] = []
    try:
        z_len = math.sqrt(z_axis.x ** 2 + z_axis.y ** 2 + z_axis.z ** 2)
    except Exception:
        z_len = 0.0
    if z_len < 1e-9:
        return None, ["decal Z axis is degenerate"]

    try:
        face_type = adsk.fusion.BRepEntityTypes.BRepFaceEntityType
    except Exception:
        face_type = None

    for tol in (1e-4, 1e-3, 1e-2):
        faces: List[Any] = []
        if face_type is not None:
            try:
                hits = comp.findBRepUsingPoint(point, face_type, tol, False)
                for i in range(hits.count):
                    f = adsk.fusion.BRepFace.cast(hits.item(i))
                    if f is not None:
                        faces.append(f)
            except Exception as ex:
                notes.append("findBRepUsingPoint(tol={}): {}".format(tol, ex))
        if not faces:
            try:
                bodies = comp.bRepBodies
                for bi in range(bodies.count):
                    body_faces = bodies.item(bi).faces
                    for fi in range(body_faces.count):
                        f = body_faces.item(fi)
                        try:
                            if f.isPointOnFace(point, tol):
                                faces.append(f)
                        except Exception:
                            continue
            except Exception as ex:
                notes.append("face scan(tol={}): {}".format(tol, ex))

        ranked = []
        for f in faces:
            try:
                ok, n = f.evaluator.getNormalAtPoint(point)
                if not ok:
                    continue
                ndot = (n.x * z_axis.x + n.y * z_axis.y + n.z * z_axis.z) / z_len
                body_name = f.body.name or ""
                skin = any(
                    kw in _normalize_fusion_body_name(body_name).lower()
                    for kw in BATCH_DECAL_BODY_PREFER_NAME_KEYWORDS
                )
                area = float(f.area)
            except Exception:
                continue
            notes.append(
                "candidate body='{}' tol={} n.z={:+.3f} skin={} area={:.0f}cm2".format(
                    body_name, tol, ndot, skin, area
                )
            )
            if ndot >= 0.98:
                ranked.append((1 if skin else 0, area, body_name, f))
        if not ranked:
            continue
        ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
        best = ranked[0]
        rivals = [r for r in ranked[1:] if r[0] == best[0] and r[2] != best[2]]
        if rivals:
            notes.append(
                "ambiguous: '{}' and '{}' both qualify".format(best[2], rivals[0][2])
            )
            return None, notes
        return best[3], notes
    notes.append("no face of this component contains the decal's centre")
    return None, notes


def _replace_unwritable_decal(
    decal: Any,
    comp: Any,
    target: str,
    label: str,
    in_root: bool = False,
    size_spec: Optional[Any] = None,
) -> Tuple[Optional[Any], List[str], Optional[Any]]:
    """Stand-in for an authored decal that refuses a new image.

    Returns ``(stand_in, notes, leftover)``. ``leftover`` is a stand-in that failed
    verification and could not be removed (the original then stays hidden).

    The designer's placement is reused EXACTLY — same centre, axes, scale and
    chain flag — on the face found under the decal's centre; no auto-fit or
    grain alignment is applied. The original is hidden BEFORE the stand-in is
    created so one part can never carry two visible decals, and is brought back if
    the stand-in cannot be verified. Nothing is deleted and nothing is saved.

    Declined outright for a decal in the ROOT Decals folder (the batch restores
    root-decal visibility before every render, which would show the original on
    top of the stand-in) and for one with wrapping off (it covers a chosen set of
    faces that cannot be read back, so a stand-in could only cover one of them).

    ``size_spec`` (Render Instructions entry): the stand-in is created at that
    width x length instead of the designer's, centred on its part (the original
    cannot be redefined because its faces are unreadable).
    """
    notes: List[str] = []
    if in_root:
        return None, [
            "{}: no stand-in — it lives in the root Decals folder, whose visibility "
            "the batch restores before every render".format(label)
        ], None
    try:
        m = decal.transform
        origin, x_axis, y_axis, z_axis = m.getAsCoordinateSystem()
        mag_x = _vector_length(x_axis)
        mag_y = _vector_length(y_axis)
    except Exception as ex:
        return None, ["{}: cannot read the decal's placement ({})".format(label, ex)], None
    try:
        chain = bool(decal.isChainFaces)
    except Exception as ex:
        return None, ["{}: cannot read isChainFaces ({})".format(label, ex)], None
    notes.append(
        "{}: authored placement centre=({:.3f},{:.3f},{:.3f}) size={:.3f}x{:.3f} "
        "chain={}".format(label, origin.x, origin.y, origin.z, mag_x, mag_y, chain)
    )
    if not chain:
        # Wrapping off: the decal covers exactly the faces the designer picked.
        # A dead reference reads as [None] however many were picked (3-in-1's
        # covers ~8: the top and both rounded edges), so the list can be trusted
        # only when every entry resolves and there is exactly one.
        try:
            chosen = list(decal.faces)
        except Exception:
            chosen = []
        if not chosen or any(f is None for f in chosen):
            notes.append(
                "{}: no stand-in — wrapping is off and the faces it was applied to "
                "cannot be read back, so a stand-in could cover only part of them".format(
                    label
                )
            )
            return None, notes, None
        if len(chosen) != 1:
            notes.append(
                "{}: no stand-in — wrapping is off and it covers {} chosen faces; a "
                "stand-in is only made for a single face".format(label, len(chosen))
            )
            return None, notes, None

    face, face_notes = _find_face_at_point(comp, origin, z_axis)
    notes.extend("{}:   {}".format(label, n) for n in face_notes)
    if face is None:
        notes.append("{}: no unambiguous face under the decal — left as authored".format(label))
        return None, notes, None

    size_warnings: List[str] = []
    sized = False
    if size_spec is not None:
        try:
            plan = _json_size_plan(origin, x_axis, y_axis, z_axis, size_spec, [face], chain, face)
        except Exception as ex:
            plan = {
                "ok": False,
                "notes": [],
                "warnings": [],
                "reason": "unexpected error ({})".format(str(ex) or type(ex).__name__),
            }
        notes.extend("{}: {}".format(label, n) for n in plan["notes"])
        if plan["ok"]:
            m, origin = plan["matrix"], plan["origin"]
            mag_x, mag_y = plan["mag_x"], plan["mag_y"]
            size_warnings = list(plan["warnings"])
            sized = True
        else:
            size_warnings = ["size not applied — {}".format(plan["reason"])]
            notes.append("{}: Render Instructions size not applied ({})".format(label, plan["reason"]))

    try:
        decals = comp.decals
        count_before = decals.count
    except Exception as ex:
        notes.append("{}: decals collection unavailable ({})".format(label, ex))
        return None, notes, None

    # Hide first. If this fails nothing has been created yet.
    try:
        decal.isLightBulbOn = False
        if decal.isLightBulbOn:
            raise RuntimeError("light bulb stayed on")
    except Exception as ex:
        notes.append("{}: could not hide the original ({}) — left as authored".format(label, ex))
        return None, notes, None

    def _restore_original() -> None:
        try:
            decal.isLightBulbOn = True
        except Exception as ex2:
            notes.append("{}: WARNING could not re-show the original ({})".format(label, ex2))

    new_decal = None
    problem = ""
    try:
        inp = decals.createInput(target, [face], origin)
        if inp is None:
            raise RuntimeError("createInput returned None")
        if not _set_decal_input_chain_faces(inp, chain):
            raise RuntimeError("could not set isChainFaces on the input")
        try:
            inp.opacity = decal.opacity
        except Exception:
            pass
        err = _apply_entity_transform(inp, m)
        if err:
            raise RuntimeError(err)
        new_decal = decals.add(inp)
        if new_decal is None:
            raise RuntimeError("decals.add returned None")

        # Verify the stand-in really reproduces the authored placement.
        o2, x2, y2, _z2 = new_decal.transform.getAsCoordinateSystem()
        if origin.distanceTo(o2) > 1e-3:
            problem = "centre moved by {:.4f} cm".format(origin.distanceTo(o2))
        for want, got, axis in (
            (mag_x, _vector_length(x2), "X"),
            (mag_y, _vector_length(y2), "Y"),
        ):
            if not problem and abs(got - want) > 0.01 * max(want, 1e-9):
                problem = "{} size {:.3f} != authored {:.3f}".format(axis, got, want)
        if not problem and bool(new_decal.isChainFaces) != chain:
            problem = "chain flag differs"
        if not problem and decals.count != count_before + 1:
            problem = "decal count {} (expected {})".format(decals.count, count_before + 1)
        if not problem and decal.isLightBulbOn:
            problem = "original is visible again"
    except Exception as ex:
        problem = str(ex)

    if problem:
        notes.append("{}: stand-in rejected ({})".format(label, problem))
        removed = True
        if new_decal is not None:
            try:
                new_decal.deleteMe()
                removed = comp.decals.count == count_before
            except Exception as ex:
                removed = False
                notes.append("{}: could not delete the stand-in ({})".format(label, ex))
        if removed:
            _restore_original()
            notes.append("{}: rolled back — part keeps its authored texture".format(label))
            return None, notes, None
        # Better one unverified decal than two stacked ones.
        notes.append(
            "{}: WARNING stand-in could not be removed; original left hidden so "
            "the part does not carry two decals — check this render".format(label)
        )
        return None, notes, new_decal

    if sized:
        k = float(BATCH_DECAL_MAGNITUDE_CM_PER_UNIT) or 1.0
        notes.append(
            "{}: REPLACED with a stand-in on body '{}' at the designer's position, "
            "sized from the Render Instructions: {:.1f} x {:.1f} cm (original hidden, "
            "not deleted)".format(label, _safe_body_name(face), mag_x * k, mag_y * k)
        )
    else:
        notes.append(
            "{}: REPLACED with an identical-placement stand-in on body '{}' "
            "(original hidden, not deleted)".format(label, _safe_body_name(face))
        )
    for warning in size_warnings:
        _size_warning(label, warning, notes)
    return new_decal, notes, None


def _safe_body_name(face: Any) -> str:
    try:
        return face.body.name or "?"
    except Exception:
        return "?"


def _safe_attr(obj: Any, attr: str) -> str:
    try:
        return str(getattr(obj, attr) or "?")
    except Exception:
        return "?"


def _designer_inactive_reason(decal: Any, comp: Any) -> str:
    """Why this decal never shows in a render as saved, or "" when it does.

    Only states that are readably set count — anything unreadable is treated as
    active. Deliberately not ``isVisible``: a decal on a part hidden for one render
    configuration is still part of the look and must keep receiving images.
    """
    try:
        if decal.isLightBulbOn is False:
            return "switched off in the model"
    except Exception:
        pass
    try:
        if comp is not None and comp.isDecalFolderLightBulbOn is False:
            return "its Decals folder is switched off in the model"
    except Exception:
        pass
    try:
        timeline_obj = decal.timelineObject
    except Exception:
        timeline_obj = None
    if timeline_obj is not None:
        try:
            if timeline_obj.isSuppressed is True:
                return "suppressed in the timeline"
        except Exception:
            pass
        try:
            if timeline_obj.isRolledBack is True:
                return "after the timeline marker (rolled back)"
        except Exception:
            pass
    return ""


def _decal_folder_label(comp: Any, occ: Any) -> str:
    """Where a decal is kept, for log lines."""
    if occ is None:
        return "root Decals folder"
    return 'in "{}"'.format(_safe_attr(comp, "name"))


def _decal_part_label(decal: Any, comp: Any, occ: Any) -> str:
    """The part a decal textures, for log lines."""
    if occ is not None:
        return '"{}"'.format(_safe_attr(comp, "name"))
    try:
        face_occ = decal.faces[0].assemblyContext
    except Exception:
        face_occ = None
    if face_occ is None:
        return "a body of the root component"
    return '"{}"'.format(_safe_attr(face_occ, "name"))


def _register_stand_in(
    key: Tuple[str, int], comp: Any, slot: int, target: str
) -> Optional[Tuple[str, int]]:
    """Give the decal just added to ``comp`` the original's slot and image info."""
    try:
        new_key = (key[0], comp.decals.count - 1)
    except Exception:
        return None
    _AUTHORED_SLOT_BY_KEY[new_key] = slot
    # Later colour sets must be prepared exactly as the original was, or the
    # stand-in's texture would drift from the designer's.
    info = _AUTHORED_IMAGE_BY_KEY.get(key)
    if not info or info[0] is None:
        made = image_orient.image_size(Path(target))
        info = (made, image_orient.orientation_of(made), "stand-in image")
    _AUTHORED_IMAGE_BY_KEY[new_key] = info
    _PROBED_DECAL_KEYS.add(new_key)
    _DESIGNER_INACTIVE_BY_KEY[new_key] = ""
    return new_key


# How far (cm) a decal's centre may sit from its surface and still count as on it.
# A centre on a curved face can lie on the display chord ~0.012 cm inside.
_DECAL_CENTRE_ON_FACE_TOL: float = 0.1


def _decal_centre_on_face(decal: Any) -> Optional[bool]:
    """Whether the decal's centre lies on one of its own faces (None = cannot tell).

    Fusion positions a decal from its centre, which must lie on its first face.
    Every designer decal in the client's models passes. 3-in-1's re-made decal
    (rotated 90 degrees in the dialog) read back 6 cm off its face after the
    model was reopened, and the colour set then landed on the wrong strip.
    """
    try:
        origin = decal.transform.getAsCoordinateSystem()[0]
        listed = decal.faces
        # Index, don't iterate: on a dead reference Fusion returns None for
        # faces[0] but an empty wrapper when the list is iterated (Bullnose nose).
        faces = [listed[i] for i in range(len(listed))]
    except Exception:
        return None
    checked = False
    for face in faces:
        if face is None:
            continue
        try:
            on_face = face.isPointOnFace(origin, _DECAL_CENTRE_ON_FACE_TOL)
        except Exception:
            continue  # an unreadable face says nothing either way
        checked = True
        if on_face:
            return True
    return False if checked else None


def _decal_probe_line(decal: Any, comp: Any, occ: Any, index: int) -> str:
    """One-line state dump of an authored decal (logged once per model)."""
    bits: List[str] = []
    for label, getter in (
        ("name", lambda: decal.name),
        ("comp", lambda: comp.name),
        ("occ", lambda: occ.name if occ is not None else "(root)"),
        ("visible", lambda: decal.isVisible),
        ("bulb", lambda: decal.isLightBulbOn),
        ("folderBulb", lambda: comp.isDecalFolderLightBulbOn),
        ("suppressed", lambda: decal.timelineObject.isSuppressed),
        ("rolledBack", lambda: decal.timelineObject.isRolledBack),
        ("chain", lambda: decal.isChainFaces),
        ("nFaces", lambda: len(decal.faces)),
        ("face0", lambda: "None" if decal.faces[0] is None else "ok"),
        ("body", lambda: decal.faces[0].body.name),
        ("derived", lambda: decal.isDerived),
        ("timelineIdx", lambda: decal.timelineObject.index),
        ("health", lambda: decal.timelineObject.healthState),
        (
            "size",
            lambda: "{:.3f}x{:.3f}".format(
                _vector_length(decal.transform.getAsCoordinateSystem()[1]),
                _vector_length(decal.transform.getAsCoordinateSystem()[2]),
            ),
        ),
        (
            "centre",
            lambda: "({0.x:.2f},{0.y:.2f},{0.z:.2f})".format(
                decal.transform.getAsCoordinateSystem()[0]
            ),
        ),
        ("centreOnFace", lambda: _decal_centre_on_face(decal)),
    ):
        try:
            bits.append("{}={}".format(label, getter()))
        except Exception as ex:
            bits.append("{}=ERR({})".format(label, str(ex)[:48]))
    return "  Decal probe [{}#{}]: {}".format(
        bits[1].split("=", 1)[-1] if len(bits) > 1 else "?", index, ", ".join(bits)
    )


def _closest_point_on_face(face: Any, point: Any) -> Optional[Tuple[Any, float]]:
    """The point of ``face`` nearest ``point`` and its distance in cm, or None.

    Decal.redefine takes a centre only when it lies on the primary face within
    Fusion's own tight tolerance ("Input point is not located on primary face").
    Designer centres can read back a hair off their face, and a centre moved along
    a decal's (slightly tilted) length axis leaves the surface, so the sizing pass
    puts every centre exactly on its face first.
    """
    try:
        ev = face.evaluator
        ok, param = ev.getParameterAtPoint(point)
        if not ok or param is None:
            return None
        ok2, on_face = ev.getPointAtParameter(param)
        if not ok2 or on_face is None:
            return None
        # The surface's nearest point can lie outside the face's boundary.
        if not face.isPointOnFace(on_face):
            return None
        return on_face, float(point.distanceTo(on_face))
    except Exception:
        return None


def _decal_anchor(
    faces: List[Any], chain: bool, point: Any
) -> Optional[Tuple[int, Any, float]]:
    """``(index, point_on_face, distance_cm)`` for the listed face nearest ``point``.

    With wrapping on only the first face counts (Fusion uses just that one). Ties
    go to the earlier face, so the designer's primary face is kept when it fits.
    """
    best: Optional[Tuple[int, Any, float]] = None
    for i, face in enumerate(faces[:1] if chain else faces):
        if face is None:
            continue
        hit = _closest_point_on_face(face, point)
        if hit is not None and (best is None or hit[1] < best[2] - 1e-6):
            best = (i, hit[0], hit[1])
    return best


def _primary_first(faces: List[Any], primary: int) -> List[Any]:
    """``faces`` with ``faces[primary]`` moved to the front (Fusion's primary face)."""
    faces = list(faces)
    if 0 < primary < len(faces):
        faces.insert(0, faces.pop(primary))
    return faces


def _primary_index(
    faces: List[Any], chain: bool, origin: Any, wanted: Any, fallback: int
) -> int:
    """Index in ``faces`` of the face to pass first: the one under ``origin``.

    Fusion lists a decal's faces primary-first, so after a resize that changed
    the primary face, faces fetched again come back in a different order from the
    caller's list (an undo would then lead with the wrong face and be refused).
    The face is found by where the centre is, then by identity, then by position.
    """
    if origin is not None:
        anchor = _decal_anchor(faces, chain, origin)
        if anchor is not None and anchor[2] <= _DECAL_CENTRE_ON_FACE_TOL:
            return anchor[0]
    if wanted is not None:
        for i, face in enumerate(faces):
            try:
                if face == wanted:
                    return i
            except Exception:
                continue
    return fallback if 0 <= fallback < len(faces) else 0


def _matrix_onto_face(matrix: Any, face: Any) -> Tuple[Optional[Any], str]:
    """``matrix`` with its origin moved onto ``face``, axes kept; ``(None, why)`` if not.

    Used with the timeline rolled back, where a face can sit slightly apart from
    where the finished model shows it. Moves of more than DECAL_SIZE_END_GAP_CM
    are refused (the read-back check would undo them anyway).
    """
    try:
        origin, x_axis, y_axis, z_axis = matrix.getAsCoordinateSystem()
    except Exception as ex:
        return None, "its placement cannot be read ({})".format(ex)
    hit = _closest_point_on_face(face, origin)
    if hit is None:
        return None, "its centre cannot be placed on that face"
    on_face, off = hit
    if off > float(DECAL_SIZE_END_GAP_CM):
        return None, "at the decal's step in the timeline its centre is {:.2f} cm from that face".format(off)
    if off <= 1e-9:
        return None, "its centre is on that face"
    m, err = _matrix_from_axes(on_face, x_axis, y_axis, z_axis)
    return m, err or ""


def _faces_near_diag(faces: List[Any], centre: Any) -> str:
    """How the listed faces relate to ``centre``, for a refusal message."""
    near = projected = 0
    nearest: Optional[float] = None
    for face in faces:
        try:
            if face.isPointOnFace(centre, _DECAL_CENTRE_ON_FACE_TOL):
                near += 1
        except Exception:
            pass
        hit = _closest_point_on_face(face, centre)
        if hit is not None:
            projected += 1
            nearest = hit[1] if nearest is None else min(nearest, hit[1])
    return "{} of {} face(s) within {} cm of the centre, {} measurable{}".format(
        near,
        len(faces),
        _DECAL_CENTRE_ON_FACE_TOL,
        projected,
        "" if nearest is None else ", nearest {:.3f} cm away".format(nearest),
    )


def _squared_designer_matrix(
    origin: Any, x_axis: Any, y_axis: Any, z_axis: Any, faces: List[Any], chain: bool
) -> Tuple[Optional[Any], int]:
    """The designer's own size and position on a frame Decal.redefine accepts.

    Some designer frames read back slightly skewed (Quarter Round: "X and Z axes
    ... must be perpendicular"), so they cannot be written back as they are.
    Returns ``(matrix or None, primary face index)``.
    """
    primary, o = 0, origin
    anchor = _decal_anchor(faces, chain, origin)
    if anchor is not None and anchor[2] <= _DECAL_CENTRE_ON_FACE_TOL:
        primary, o = anchor[0], anchor[1]
    frame = _square_decal_frame(o, x_axis, y_axis, z_axis, faces[primary] if faces else None)
    if frame is None:
        return None, primary
    mx, my = _vector_length(x_axis), _vector_length(y_axis)
    x_unit, y_unit, z_unit = frame
    m, _err = _matrix_from_axes(
        o,
        adsk.core.Vector3D.create(*(c * mx for c in x_unit)),
        adsk.core.Vector3D.create(*(c * my for c in y_unit)),
        adsk.core.Vector3D.create(*z_unit),
    )
    return m, primary


def _part_extent_along(
    faces: List[Any], chain: bool, origin: Any, unit: Tuple[float, float, float]
) -> Optional[Tuple[float, float]]:
    """``(start, end)`` of the textured part along ``unit``, in cm from ``origin``.

    With wrapping off the decal covers exactly its listed faces; with wrapping on
    it covers its first face's whole body. Measured from bounding boxes, which is
    exact along a straight trim (all of the client's parts are straight).
    """
    boxes: List[Any] = []
    for face in faces:
        if face is None:
            continue
        try:
            boxes.append(face.body.boundingBox if chain else face.boundingBox)
        except Exception:
            continue
        if chain:
            break
    lo, hi = math.inf, -math.inf
    for box in boxes:
        try:
            mn, mx = box.minPoint, box.maxPoint
            xs, ys, zs = (mn.x, mx.x), (mn.y, mx.y), (mn.z, mx.z)
        except Exception:
            continue
        for cx in xs:
            for cy in ys:
                for cz in zs:
                    t = (
                        (cx - origin.x) * unit[0]
                        + (cy - origin.y) * unit[1]
                        + (cz - origin.z) * unit[2]
                    )
                    lo, hi = min(lo, t), max(hi, t)
    return (lo, hi) if lo <= hi else None


def _square_decal_frame(
    origin: Any, x_axis: Any, y_axis: Any, z_axis: Any, face: Any, keep: str = "auto"
) -> Optional[Tuple[Tuple[float, ...], Tuple[float, ...], Tuple[float, ...]]]:
    """Unit X, Y, Z for a decal at ``origin``, exactly square to the surface.

    Decal.redefine rejects a frame whose axes are not perpendicular ("Y and Z axes
    defined by the matrix, must be perpendicular") — and a decal Fusion has moved
    on reopening (3-in-1) reads back a frame that is slightly off. Z is the face
    normal at the origin (on the decal's side). The axis named by ``keep`` ("x",
    "y", or "auto" = the longer one, which carries the grain) keeps the designer's
    direction within the surface, and the other follows with the designer's
    handedness, so a flipped decal stays flipped.
    """
    def unit(v: Tuple[float, ...]) -> Optional[Tuple[float, ...]]:
        n = math.sqrt(sum(c * c for c in v))
        return tuple(c / n for c in v) if n > 1e-12 else None

    def cross(a: Tuple[float, ...], b: Tuple[float, ...]) -> Tuple[float, ...]:
        return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])

    def dot(a: Tuple[float, ...], b: Tuple[float, ...]) -> float:
        return sum(p * q for p, q in zip(a, b))

    xo = (x_axis.x, x_axis.y, x_axis.z)
    yo = (y_axis.x, y_axis.y, y_axis.z)
    zo = unit((z_axis.x, z_axis.y, z_axis.z))
    if zo is None:
        return None
    z = zo
    try:
        ok, normal = face.evaluator.getNormalAtPoint(origin)
        n = unit((normal.x, normal.y, normal.z)) if ok else None
        if n is not None:
            z = n if dot(n, zo) >= 0 else tuple(-c for c in n)
    except Exception:
        pass
    right_handed = dot(cross(xo, yo), zo) >= 0
    if keep == "auto":
        keep = "x" if dot(xo, xo) >= dot(yo, yo) else "y"
    if keep == "y":
        y = unit(tuple(yc - dot(yo, z) * zc for yc, zc in zip(yo, z)))
        if y is None:
            return None
        x = cross(y, z) if right_handed else cross(z, y)
        return x, y, z
    x = unit(tuple(xc - dot(xo, z) * zc for xc, zc in zip(xo, z)))
    if x is None:
        return None
    y = cross(z, x) if right_handed else cross(x, z)
    return x, y, z


def _json_size_plan(
    origin: Any,
    x_axis: Any,
    y_axis: Any,
    z_axis: Any,
    spec: Any,
    faces: List[Any],
    chain: bool,
    first_face: Any,
    recentre: bool = True,
) -> Dict[str, Any]:
    """New placement for a decal: the JSON width x length, centred on its part.

    Keeps the designer's axis directions; the longer axis gets the JSON length.
    Moves the centre along that axis only when the decal would otherwise miss an
    end of the part (3-in-1: Fusion reopens its decal centred on the trim's first
    end), the moved centre is still on one of the decal's faces, and ``recentre``
    is on. Returns ``ok``, ``matrix``, ``origin``, ``mag_x``/``mag_y`` (new),
    ``was`` (old), ``notes``, ``warnings``, ``moved`` (the shift in cm, or None),
    ``snapped`` and, when not ok, ``reason``.
    """
    plan: Dict[str, Any] = {
        "ok": False, "notes": [], "warnings": [], "reason": "", "moved": None, "snapped": False,
    }
    k = float(BATCH_DECAL_MAGNITUDE_CM_PER_UNIT) or 1.0
    mag_x, mag_y = _vector_length(x_axis), _vector_length(y_axis)
    plan["was"] = (mag_x * k, mag_y * k)
    if mag_x <= 1e-9 or mag_y <= 1e-9:
        plan["reason"] = "its size cannot be read"
        return plan
    if max(mag_x, mag_y) / min(mag_x, mag_y) < DECAL_SIZE_MIN_ELONGATION:
        plan["reason"] = (
            "its sides are nearly equal ({:.1f} x {:.1f} cm), so which one is the "
            "length is unclear".format(mag_x * k, mag_y * k)
        )
        return plan
    length_cm = float(getattr(spec, "length_cm", 0) or 0)
    width_cm = float(getattr(spec, "width_cm", 0) or 0)
    if length_cm <= 0 or width_cm <= 0:
        plan["reason"] = "the Render Instructions size is missing or zero"
        return plan

    long_is_x = mag_x >= mag_y
    long_axis = x_axis if long_is_x else y_axis
    long_len = _vector_length(long_axis)
    unit = (long_axis.x / long_len, long_axis.y / long_len, long_axis.z / long_len)
    # Measure and move along the surface: a designer axis can tilt off it
    # (3-in-1's re-made decal), and a centre slid along a tilted axis lifts away.
    square = _square_decal_frame(origin, x_axis, y_axis, z_axis, first_face)
    if square is not None:
        unit = tuple(square[0] if long_is_x else square[1])
    designer_width_cm = (mag_y if long_is_x else mag_x) * k

    # A wrapping decal paints its whole body. On a full solid (one with cut-end
    # faces, unlike a show-only "Skin") anything longer than the part runs onto the
    # cut ends — the client's original complaint — so its length is held to the part.
    solid_body = False
    if chain and faces and faces[0] is not None:
        try:
            show_only, _why = audit_decal_body_is_show_only(faces[0].body)
            solid_body = not show_only
        except Exception:
            solid_body = False

    new_origin = origin
    half = length_cm / 2.0
    gap = float(DECAL_SIZE_END_GAP_CM)
    extent = _part_extent_along(faces, chain, origin, unit)
    if extent is not None and solid_body and length_cm > (extent[1] - extent[0]) + gap:
        part_len = extent[1] - extent[0]
        plan["notes"].append(
            "length held to the part's {:.1f} cm (Render Instructions: {:.1f} cm): this "
            "decal wraps a solid body, and more length would run onto its cut ends".format(
                part_len, length_cm
            )
        )
        length_cm = part_len
        half = length_cm / 2.0
    if extent is None:
        designer_length_cm = max(mag_x, mag_y) * k
        if length_cm < designer_length_cm - gap:
            plan["warnings"].append(
                "the part's length could not be measured and the decal gets shorter "
                "({:.1f} -> {:.1f} cm), so its ends may be bare".format(designer_length_cm, length_cm)
            )
        else:
            plan["notes"].append("part length could not be measured, so its ends were not checked")
    else:
        start, end = extent
        part_len = end - start
        if part_len > length_cm + gap:
            plan["warnings"].append(
                "the part is {:.1f} cm long but the Render Instructions length is "
                "{:.1f} cm, so about {:.1f} cm at its ends stays bare".format(
                    part_len, length_cm, part_len - length_cm
                )
            )
        if start < -half - gap or end > half + gap:
            shift = (start + end) / 2.0
            moved = adsk.core.Point3D.create(
                origin.x + unit[0] * shift,
                origin.y + unit[1] * shift,
                origin.z + unit[2] * shift,
            )
            on_face = False
            if recentre:
                # Any listed face will do (a part whose faces are split along its
                # length); the one it lands on becomes the primary face below.
                moved_anchor = _decal_anchor(faces, chain, moved)
                if moved_anchor is not None:
                    on_face = moved_anchor[2] <= _DECAL_CENTRE_ON_FACE_TOL
                else:
                    try:
                        on_face = bool(first_face.isPointOnFace(moved, _DECAL_CENTRE_ON_FACE_TOL))
                    except Exception:
                        on_face = False
            if on_face:
                new_origin = moved
                plan["moved"] = shift
                plan["notes"].append(
                    "centred on the part (moved {:+.1f} cm along its length)".format(shift)
                )
            else:
                # Bare length that comes from NOT centring (on top of any the JSON
                # length leaves anyway): the one missed end when the decal is longer
                # than the part, the off-centre amount when it is shorter.
                left, right = max(0.0, -half - start), max(0.0, end - half)
                plan["bare_end_cm"] = max(left, right) - max(0.0, part_len - length_cm) / 2.0
                if part_len <= length_cm + gap:
                    plan["warnings"].append(
                        "it does not reach one end of the part and could not be centred "
                        "on it, so about {:.1f} cm at that end stays bare".format(max(left, right))
                    )
                elif plan["bare_end_cm"] > gap:
                    plan["warnings"].append(
                        "it could not be centred on the part, so the bare ends are uneven: "
                        "about {:.1f} cm at one end and {:.1f} cm at the other".format(
                            max(left, right), min(left, right)
                        )
                    )
    if solid_body and width_cm > designer_width_cm * (1.0 + float(DECAL_SIZE_NARROWER_WARN)):
        plan["warnings"].append(
            "the Render Instructions width ({:.1f} cm) is wider than the designer's "
            "decal ({:.1f} cm) on a decal that wraps a solid body, so the texture may "
            "run onto the part's sides".format(width_cm, designer_width_cm)
        )
    if width_cm < designer_width_cm * (1.0 - float(DECAL_SIZE_NARROWER_WARN)):
        plan["warnings"].append(
            "the Render Instructions width ({:.1f} cm) is narrower than the "
            "designer's decal ({:.1f} cm), so the part's edges may show bare".format(
                width_cm, designer_width_cm
            )
        )

    # Fusion takes the centre only exactly on the primary face (2026-10-15: the
    # Bullnose and Square Nose planks, 3-in-1 and T-Molding were all refused).
    primary = 0
    anchor = _decal_anchor(faces, chain, new_origin)
    if anchor is not None:
        idx, on_face_point, off = anchor
        if off <= _DECAL_CENTRE_ON_FACE_TOL:
            primary, new_origin = idx, on_face_point
            plan["snapped"] = True
            if off > 1e-3:
                plan["notes"].append("centre put exactly on its face (it read {:.3f} cm off)".format(off))
        else:
            plan["notes"].append("its centre is {:.2f} cm from its nearest face".format(off))
    primary_face = faces[primary] if faces and faces[primary] is not None else first_face

    new_long, new_short = length_cm / k, width_cm / k
    new_mag_x, new_mag_y = (new_long, new_short) if long_is_x else (new_short, new_long)
    frame = _square_decal_frame(new_origin, x_axis, y_axis, z_axis, primary_face)
    if frame is None:
        plan["reason"] = "its axes cannot be squared to the surface"
        return plan
    x_unit, y_unit, z_unit = frame
    new_x = adsk.core.Vector3D.create(*(c * new_mag_x for c in x_unit))
    new_y = adsk.core.Vector3D.create(*(c * new_mag_y for c in y_unit))
    new_z = adsk.core.Vector3D.create(*z_unit)
    matrix, err = _matrix_from_axes(new_origin, new_x, new_y, new_z)
    if matrix is None:
        plan["reason"] = err or "the new placement was rejected"
        return plan
    plan.update(
        ok=True,
        matrix=matrix,
        origin=new_origin,
        mag_x=new_mag_x,
        mag_y=new_mag_y,
        length_axis="X" if long_is_x else "Y",
        primary=primary,
    )
    return plan


def _redefine_decal(
    design: Any, decal: Any, matrix: Any, faces: List[Any], chain: bool, primary: int = 0
) -> Tuple[str, bool]:
    """Apply ``matrix`` to an existing decal. Returns ``(problem or "", touched)``.

    ``touched`` is True once the timeline marker may have moved (decal and face
    objects fetched earlier should then be fetched again). ``faces[primary]`` is
    passed first: Fusion requires the centre on the first face.

    The API requires the marker immediately before the decal. Afterwards it goes
    back to EXACTLY where the model had it — not to the end: 3-in-1 is saved with
    its marker part-way along, and an old stair scene after it (planks, a nose,
    more decals) must stay off. If the marker cannot be put back, sizing stops for
    the model and the problem is reported with the decal failures.
    """
    try:
        timeline = design.timeline
        saved_marker = int(timeline.markerPosition)
        timeline_obj = decal.timelineObject
    except Exception as ex:
        return "the timeline cannot be read ({})".format(ex), False
    if timeline_obj is None:
        return "it has no timeline entry", False
    err = ""
    touched = True
    try:
        if timeline_obj.rollTo(True) is False:
            err = "the timeline marker could not be moved to it"
        else:
            # Faces as they are with the marker rolled back. Later timeline steps
            # can re-split a part's faces (T-Molding: 42 in the finished model, its
            # decal at step 5), so the count may differ — and the finished model's
            # face objects do not exist at the decal's step (Fusion then refused
            # even the designer's own centre, 2026-10-15).
            pool = list(faces)
            pool_note = "faces at the decal's step could not be read"
            try:
                listed = decal.faces
                fresh = [listed[i] for i in range(len(listed))]
                if fresh and all(f is not None for f in fresh):
                    pool = fresh
                    pool_note = "{} face(s) at the decal's step, {} in the finished model".format(
                        len(fresh), len(faces)
                    )
            except Exception:
                pass
            try:
                centre = matrix.getAsCoordinateSystem()[0]
            except Exception:
                centre = None
            wanted = faces[primary] if 0 <= primary < len(faces) else None
            use_faces = _primary_first(pool, _primary_index(pool, chain, centre, wanted, primary))
            if chain:
                use_faces = use_faces[:1]
            try:
                ok = decal.redefine(matrix, use_faces, chain)
            except Exception as ex:
                if "primary face" not in str(ex) or not pool or centre is None:
                    raise
                # Rolled back, the face under the centre can sit a hair apart from
                # where the finished model shows it: find it among the faces as
                # they are now and put the centre exactly on it.
                diag = "{}: {}".format(pool_note, _faces_near_diag(pool, centre))
                anchor = _decal_anchor(pool, chain, centre)
                if anchor is None:
                    raise RuntimeError("{} (no listed face found under its centre; {})".format(ex, diag))
                idx = anchor[0]
                retry, why = _matrix_onto_face(matrix, pool[idx])
                if retry is None and anchor[2] <= 1e-9 and pool[idx] is not use_faces[0]:
                    retry = matrix  # already on that face: only which face leads changes
                if retry is None:
                    raise RuntimeError("{} ({}; {})".format(ex, why, diag))
                use_faces = _primary_first(pool, idx)
                if chain:
                    use_faces = use_faces[:1]
                try:
                    ok = decal.redefine(retry, use_faces, chain)
                except Exception as ex2:
                    raise RuntimeError("{} ({})".format(str(ex2) or type(ex2).__name__, diag))
            if ok is False:
                err = "Fusion refused the new size"
    except Exception as ex:
        err = str(ex) or type(ex).__name__
    finally:
        try:
            if int(timeline.markerPosition) != saved_marker:
                timeline.markerPosition = saved_marker
            if int(timeline.markerPosition) != saved_marker:
                raise RuntimeError(
                    "marker at {} instead of {}".format(timeline.markerPosition, saved_marker)
                )
        except Exception as ex2:
            _SIZING_ABORTED[0] = True
            problem = (
                "the timeline marker could not be put back ({}); this model's renders "
                "may show the wrong parts".format(ex2)
            )
            _DECAL_SWAP_FAILURES.append("Decal sizing: " + problem)
            err = (err + "; " if err else "") + problem.upper()
    return err, touched


def _size_warning(label: str, text: str, lines: List[str]) -> None:
    lines.append("{}: SIZE WARNING — {}".format(label, text))
    _DECAL_SIZE_WARNINGS.append("{}: {}".format(label, text))


def apply_json_decal_sizes(design: Any) -> List[str]:
    """Give each authored decal on a listed part its Render Instructions size.

    Call right after the model is opened — before the geometry settle and the
    visibility snapshot — because resizing a decal rolls the timeline back and
    recomputes the model. Runs once per model (``apply_decal_color_set`` calls it
    as a fallback). Returns log lines; problems go to ``pop_decal_size_warnings``.
    """
    lines: List[str] = []
    if not (DECAL_APPLY_JSON_SIZE and _DECAL_TARGETS_BY_PART) or _JSON_SIZES_DONE[0]:
        return lines
    _JSON_SIZES_DONE[0] = True
    try:
        pending = bool(design.snapshots.hasPendingSnapshot)
    except Exception:
        pending = False
    if pending:
        _size_warning(
            "This model",
            "sizes not applied — it has parts moved without a captured position, and "
            "rolling its timeline would move them back",
            lines,
        )
        return lines
    _apply_json_sizes(design, lines)
    return lines


def _apply_json_sizes(design: Any, lines: List[str]) -> None:
    """The sizing pass: one decal at a time, fetched again after every roll.

    A decal that cannot be sized keeps the designer's size and is reported; the
    colour set is applied either way.
    """
    seen: Set[Tuple[str, int]] = set()
    for _round in range(256):
        restart = False
        for d, comp, occ, j in _collect_decals_with_owner(design):
            try:
                key = (str(comp.id), j)
            except Exception:
                key = ("id{}".format(id(comp)), j)
            if key in seen:
                continue
            seen.add(key)
            try:
                restart = _apply_json_size_one(design, d, comp, occ, key, lines)
            except Exception as ex:
                _size_warning(
                    'Decal "{}"'.format(_safe_attr(d, "name")),
                    "size not applied — unexpected error ({})".format(str(ex) or type(ex).__name__),
                    lines,
                )
            if _SIZING_ABORTED[0]:
                return
            if restart:
                break  # objects fetched before the roll are not reused
        if not restart:
            return


def _apply_json_size_one(
    design: Any, d: Any, comp: Any, occ: Any, key: Tuple[str, int], lines: List[str]
) -> bool:
    """One decal of the sizing pass. Returns True if the timeline was rolled."""
    if key in _RETIRED_AUTHORED_KEYS or key in _SIZED_KEYS:
        return False
    if key not in _DESIGNER_INACTIVE_BY_KEY:
        _DESIGNER_INACTIVE_BY_KEY[key] = _designer_inactive_reason(d, comp)
    if _DESIGNER_INACTIVE_BY_KEY[key]:
        return False
    spec = _json_spec_for_decal(d, comp, occ)
    if spec is None:
        return False
    _SIZED_KEYS.add(key)
    label = 'Decal "{}" on "{}"'.format(_safe_attr(d, "name"), _safe_attr(spec, "part_number"))
    try:
        derived = d.isDerived is True
    except Exception:
        derived = False
    if derived:
        # The API: "You should not attempt to make any edits to the derived decal."
        _size_warning(label, "size not applied — it is derived from another design", lines)
        return False
    try:
        listed = d.faces
        faces = [listed[i] for i in range(len(listed))]
    except Exception as ex:
        _size_warning(label, "size not applied — its faces cannot be read ({})".format(ex), lines)
        return False
    if not faces or any(f is None for f in faces):
        # Broken face link: redefine needs the faces. If a stand-in replaces this
        # decal, the stand-in is created at the JSON size instead (checked after the
        # first colour set, see _report_unapplied_stand_in_sizes).
        _STANDIN_SIZE_BY_KEY[key] = (spec, label)
        lines.append(
            "{}: its face link is broken, so the Render Instructions size "
            "({:.1f} x {:.1f} cm) goes to its stand-in".format(
                label, spec.length_cm, spec.width_cm
            )
        )
        return False
    try:
        designer = d.transform
        designer_matrix = designer.copy() if hasattr(designer, "copy") else designer
        origin, x_axis, y_axis, z_axis = designer.getAsCoordinateSystem()
        chain = bool(d.isChainFaces)
    except Exception as ex:
        _size_warning(label, "size not applied — its placement cannot be read ({})".format(ex), lines)
        return False
    plan = _json_size_plan(origin, x_axis, y_axis, z_axis, spec, faces, chain, faces[0])
    lines.extend("{}: {}".format(label, n) for n in plan["notes"])
    if not plan["ok"]:
        _size_warning(label, "size not applied — {}".format(plan["reason"]), lines)
        return False
    try:
        opacity = d.opacity
    except Exception:
        opacity = None
    err, touched = _redefine_decal(design, d, plan["matrix"], faces, chain, plan.get("primary", 0))
    if err and plan.get("moved") is not None and "primary face" in err and not _SIZING_ABORTED[0]:
        # Last resort when Fusion refuses the centre moved to the middle of the
        # part: size the decal at the centre Fusion placed itself — only when that
        # leaves at most a sliver more bare. (T-Molding, 2026-10-15, was refused at
        # both centres: they lie on the same full-length face, so the cause there
        # was the rolled-back faces, handled in _redefine_decal.)
        refused = "Fusion refused the centre moved to the middle of the part ({}; placed exactly on a listed face: {})".format(
            err, "yes" if plan.get("snapped") else "no"
        )
        try:
            listed = d.faces
            refetched = [listed[i] for i in range(len(listed))]
            if refetched and all(f is not None for f in refetched):
                faces = refetched
        except Exception:
            pass
        plan = _json_size_plan(origin, x_axis, y_axis, z_axis, spec, faces, chain, faces[0], recentre=False)
        bare = float(plan.get("bare_end_cm") or 0.0)
        if not plan["ok"] or bare > float(DECAL_SIZE_FALLBACK_MAX_BARE_CM):
            why = plan["reason"] if not plan["ok"] else (
                "at the designer's centre about {:.1f} cm more of the part would stay bare".format(bare)
            )
            _size_warning(label, "size not applied — {}, and {}".format(refused, why), lines)
            return True
        err, _t = _redefine_decal(design, d, plan["matrix"], faces, chain, plan.get("primary", 0))
        if err:
            _size_warning(
                label,
                "size not applied — {}, and at the designer's centre: {}".format(refused, err),
                lines,
            )
            return True
        lines.append("{}: {}, so it is sized at the designer's centre instead".format(label, refused))
        lines.extend("{}: {}".format(label, n) for n in plan["notes"])
    if err:
        _size_warning(label, "size not applied — {}".format(err), lines)
        return touched
    try:
        if opacity is not None and abs(float(d.opacity) - float(opacity)) > 1e-9:
            d.opacity = opacity  # redefine covers placement only; keep the designer's
    except Exception:
        pass

    # Read back what Fusion made of it. A clear miss is undone (designer size kept).
    stored_as: List[str] = []
    try:
        o2, x2, y2, _z2 = d.transform.getAsCoordinateSystem()
        got = (_vector_length(x2), _vector_length(y2))
        want = (plan["mag_x"], plan["mag_y"])
        moved = plan["origin"].distanceTo(o2)
        _o, rx, ry, rz = plan["matrix"].getAsCoordinateSystem()

        def _cos(a: Any, b: Any) -> float:
            la, lb = _vector_length(a), _vector_length(b)
            if la <= 1e-12 or lb <= 1e-12:
                return 0.0
            return (a.x * b.x + a.y * b.y + a.z * b.z) / (la * lb)

        # Only a turn ON the surface moves the grain. Fusion keeps frames it is
        # given a little off square (L-Cap, Reducer and Quarter Round read back
        # "turned" at exactly the asked size and centre and were undone for it,
        # 2026-10-15), and a reversed axis only mirrors the wood.
        long_got = x2 if plan["length_axis"] == "X" else y2
        long_req = rx if plan["length_axis"] == "X" else ry
        nz = _vector_length(rz)
        tilt_deg = 0.0
        if nz > 1e-12:
            along_n = (long_got.x * rz.x + long_got.y * rz.y + long_got.z * rz.z) / (nz * nz)
            full = _vector_length(long_got)
            if full > 1e-12:
                tilt_deg = math.degrees(math.asin(min(1.0, abs(along_n) * nz / full)))
            long_got = adsk.core.Vector3D.create(
                long_got.x - along_n * rz.x, long_got.y - along_n * rz.y, long_got.z - along_n * rz.z
            )
        turn_deg = math.degrees(math.acos(min(1.0, abs(_cos(long_got, long_req)))))
        turned = turn_deg > float(DECAL_SIZE_MAX_TURN_DEG)
        if turn_deg >= 0.5:
            stored_as.append("its length turned {:.1f} deg on the surface".format(turn_deg))
        if tilt_deg >= 0.5:
            stored_as.append("its length tilted {:.1f} deg off the surface".format(tilt_deg))
        if _cos(x2, rx) < 0:
            stored_as.append("its X axis reversed")
        if _cos(y2, ry) < 0:
            stored_as.append("its Y axis reversed")

        # Along the part a shift can leave an end bare; across it, Fusion shifts
        # wrapping decals sideways (see DECAL_SIZE_MAX_ACROSS_SHIFT_CM).
        po = plan["origin"]
        shift = (o2.x - po.x, o2.y - po.y, o2.z - po.z)

        def _component(axis: Any) -> float:
            n = _vector_length(axis)
            if n <= 1e-12:
                return moved
            return abs(shift[0] * axis.x + shift[1] * axis.y + shift[2] * axis.z) / n

        along_off = _component(long_req)
        across_off = _component(ry if plan["length_axis"] == "X" else rx)
        shifted = along_off > float(DECAL_SIZE_END_GAP_CM) or across_off > float(
            DECAL_SIZE_MAX_ACROSS_SHIFT_CM
        )
        size_off = any(abs(g - w) > 0.01 * max(w, 1e-9) for g, w in zip(got, want))
        if size_off or turned or shifted:
            what = (
                "Fusion placed it at {:.2f} x {:.2f} (asked {:.2f} x {:.2f}; centre {:.2f} cm "
                "away: {:.2f} along the part, {:.2f} across{})".format(
                    got[0],
                    got[1],
                    want[0],
                    want[1],
                    moved,
                    along_off,
                    across_off,
                    "; length turned {:.1f} deg on the surface".format(turn_deg) if turned else "",
                )
            )
            undo_err, _t = _redefine_decal(design, d, designer_matrix, faces, chain)
            if undo_err and not _SIZING_ABORTED[0]:
                # The designer's frame itself can be too far off square for redefine.
                squared, sq_primary = _squared_designer_matrix(origin, x_axis, y_axis, z_axis, faces, chain)
                if squared is not None:
                    err2, _t = _redefine_decal(design, d, squared, faces, chain, sq_primary)
                    undo_err = "" if not err2 else "{}; squared: {}".format(undo_err, err2)
            if undo_err:
                _size_warning(
                    label,
                    "{} and putting the designer's size back failed ({}) — check this render".format(
                        what, undo_err
                    ),
                    lines,
                )
            else:
                _size_warning(label, "size not applied — {}; the designer's size was kept".format(what), lines)
            return True
        if moved > 0.01:
            # Within the limits it cannot show (Bullnose plank, 2026-10-15: 0.136 cm
            # across a 17.8 cm wide decal), so it is logged, not warned.
            stored_as.append(
                "its centre {:.2f} cm from where it was asked ({:.2f} along the part, "
                "{:.2f} across)".format(moved, along_off, across_off)
            )
    except Exception as ex:
        _size_warning(label, "the new size could not be read back ({})".format(ex), lines)
    try:
        health = d.timelineObject.healthState
        if int(health) != 0:  # FeatureHealthStates.HealthyFeatureHealthState
            _size_warning(
                label,
                "after resizing, Fusion reports the decal as not healthy (state {}) — "
                "check this render".format(health),
                lines,
            )
    except Exception:
        pass

    k = float(BATCH_DECAL_MAGNITUDE_CM_PER_UNIT) or 1.0
    lines.append(
        "{}: size set from the Render Instructions: {:.1f} x {:.1f} cm (designer's "
        "{:.1f} x {:.1f} cm at ({:.2f}, {:.2f}, {:.2f}); length along its {} axis{})".format(
            label,
            plan["mag_x"] * k,
            plan["mag_y"] * k,
            plan["was"][0],
            plan["was"][1],
            origin.x,
            origin.y,
            origin.z,
            plan["length_axis"],
            "; Fusion stored it with " + ", ".join(stored_as) if stored_as else "",
        )
    )
    for warning in plan["warnings"]:
        _size_warning(label, warning, lines)
    return True


def _report_unapplied_stand_in_sizes() -> List[str]:
    """Sizes kept for a stand-in that was never made: report them, once."""
    lines: List[str] = []
    for key in list(_STANDIN_SIZE_BY_KEY):
        _spec, label = _STANDIN_SIZE_BY_KEY.pop(key)
        if key not in _RETIRED_AUTHORED_KEYS:
            _size_warning(
                label,
                "size not applied — its face link is broken and no stand-in was made",
                lines,
            )
    return lines


def pop_decal_size_warnings() -> List[str]:
    """Render Instructions sizes that may not cover their part, since the last call."""
    out = list(_DECAL_SIZE_WARNINGS)
    del _DECAL_SIZE_WARNINGS[:]
    return out


def pop_decal_swap_failures() -> List[str]:
    """Decals that still show their authored texture since the last call."""
    out = list(_DECAL_SWAP_FAILURES)
    del _DECAL_SWAP_FAILURES[:]
    return out


def apply_decal_color_set(
    design: adsk.fusion.Design,
    slot1: Optional[Path],
    slot2: Optional[Path],
) -> Tuple[int, List[str]]:
    """Put this colour set's rasters on the decals the designer authored.

    Every component's Decals folder is walked. Each decal keeps the placement it
    has in the model; only its image changes. Which raster it takes (``_1`` or
    ``_2``) comes from, in order: the slot it was given on an earlier colour set,
    the Render Instructions entry for the component that owns it, a trailing
    ``1``/``2`` in its name, then position. When Render Instructions are loaded,
    only decals on the parts they list are touched (name and position are never
    consulted), and decals the designer switched off are left alone.

    The raster is first turned to run along the decal's footprint (see
    ``_prepare_decal_image``), because a landscape raster dropped into a decal laid
    out lengthwise lands with its long side across the part.

    Returns ``(decals_now_showing_this_colour_set, log_lines)``. A decal that could
    not be updated is logged, recorded for ``pop_decal_swap_failures`` and NOT
    counted.
    """
    lines: List[str] = []
    total = 0

    # Sizes first (once per model, on the first colour set): every image written
    # afterwards is then prepared for, and stretched over, the final footprint.
    # (The controller normally runs this straight after opening the model.)
    lines.extend(apply_json_decal_sizes(design))

    # (decal, component, occurrence, index in that component's Decals folder).
    # The (component id, index) pair identifies a decal for the whole session:
    # Fusion renames a decal after every image it is given, and entity tokens
    # must not be compared or re-resolved (doing so broke a working decal).
    entries: List[Tuple[Any, Any, Any, Tuple[str, int]]] = []
    for d, comp, occ, j in _collect_decals_with_owner(design):
        try:
            cid = str(comp.id)
        except Exception:
            cid = "id{}".format(id(comp))
        key = (cid, j)
        if key in _RETIRED_AUTHORED_KEYS:
            continue  # hidden original that a stand-in has taken over from
        entries.append((d, comp, occ, key))

    if DECAL_PROBE_LOG:
        for d, comp, occ, key in entries:
            if key not in _PROBED_DECAL_KEYS:
                _PROBED_DECAL_KEYS.add(key)
                lines.append(_decal_probe_line(d, comp, occ, key[1]))

    # With Render Instructions loaded they alone say which parts are textured. A
    # decal on any other part (track, floor, light ...) keeps the designer's image:
    # the name and position guesses below exist only for runs without them, and
    # would hand e.g. "Wentworth Verdon Hickory-2" the _2 raster just for its name.
    json_scoped = bool(_DECAL_TARGETS_BY_PART)
    slot_of: Dict[int, int] = {}
    source_of: Dict[int, str] = {}
    for idx, (d, comp, occ, key) in enumerate(entries):
        if key not in _DESIGNER_INACTIVE_BY_KEY:
            _DESIGNER_INACTIVE_BY_KEY[key] = _designer_inactive_reason(d, comp)
        inactive = _DESIGNER_INACTIVE_BY_KEY[key]
        if inactive:
            # Never shows in a render as the designer saved it. Left alone — in
            # particular no stand-in is made for it, which WOULD show.
            if key not in _SKIP_REPORTED_KEYS:
                _SKIP_REPORTED_KEYS.add(key)
                lines.append(
                    'Decal "{}" ({}): {} — left as is'.format(
                        _safe_attr(d, "name"), _decal_folder_label(comp, occ), inactive
                    )
                )
            continue
        if key in _AUTHORED_SLOT_BY_KEY:
            slot_of[idx] = _AUTHORED_SLOT_BY_KEY[key]
            source_of[idx] = "slot"
            continue
        if occ is None:
            # ROOT Decals folder: only the decal's face can say which part it is on.
            jslot, face_readable = _json_slot_for_decal_face(d)
            source = "render-instructions slot (via its face)"
        else:
            jslot, face_readable = _json_slot_for_component(comp, occ), True
            source = "render-instructions slot"
        if jslot in (1, 2):
            slot_of[idx] = jslot
            source_of[idx] = source
            continue
        if json_scoped:
            if not face_readable:
                # It may well sit on a listed part, and it keeps its old image in
                # every render, so this is a failure — not a deliberate skip.
                label = 'Decal "{}" (root Decals folder)'.format(_safe_attr(d, "name"))
                lines.append(
                    "{}: FAILED — cannot tell which part it textures, its face reference "
                    "cannot be read; image NOT changed. Fix it in the model: in Fusion, "
                    "edit this decal and re-select ALL the faces it should cover (or "
                    "re-create it inside the part's own component), then save the "
                    "model.".format(label)
                )
                _DECAL_SWAP_FAILURES.append(label)
            elif key not in _SKIP_REPORTED_KEYS:
                _SKIP_REPORTED_KEYS.add(key)
                lines.append(
                    'Decal "{}" ({}): kept as authored — {} is not listed in the render '
                    "instructions".format(
                        _safe_attr(d, "name"),
                        _decal_folder_label(comp, occ),
                        _decal_part_label(d, comp, occ),
                    )
                )
            continue
        try:
            dslot = decal_name_slot(d.name or "")
        except Exception:
            dslot = None
        if dslot in (1, 2):
            slot_of[idx] = dslot
            source_of[idx] = "name slot"

    if DECAL_POSITIONAL_FALLBACK and entries and not json_scoped:
        assigned = set(slot_of.values())
        if slot1 and 1 not in assigned:
            for idx in range(len(entries)):
                if idx not in slot_of:
                    slot_of[idx] = 1
                    source_of[idx] = "positional slot"
                    assigned.add(1)
                    break
        if slot2 and 2 not in assigned:
            if len(entries) >= 2:
                for idx in range(len(entries)):
                    if idx not in slot_of:
                        slot_of[idx] = 2
                        source_of[idx] = "positional slot"
                        assigned.add(2)
                        break
            elif len(entries) == 1 and 1 not in assigned:
                slot_of[0] = 2
                source_of[0] = "positional slot, sole decal"
                assigned.add(2)

    # Pass 1 — the plain write, for every decal. Fallbacks wait until all of
    # these are done so nothing they do can disturb a decal that writes fine.
    failed: List[Tuple[int, str, str, str, str]] = []
    for idx, (d, comp, occ, key) in enumerate(entries):
        slot = slot_of.get(idx)
        if slot == 1 and slot1:
            slot_path: Optional[Path] = slot1
        elif slot == 2 and slot2:
            slot_path = slot2
        else:
            continue
        base = _win_path_for_decal_image(slot_path)
        if not base:
            continue
        which = "{} {}".format(source_of.get(idx, "slot"), slot)
        try:
            name_before = d.name or "?"
        except Exception:
            name_before = "?"

        if _decal_centre_on_face(d) is False:
            # Still written (better the colour set somewhere than not at all), but
            # the render will show it on the wrong strip of the part.
            lines.append(
                'Decal "{}" ({}): WARNING its centre is not on its own surface, so the '
                "colour set will land in the wrong place. Re-make this decal in Fusion "
                "without rotating it (Z Angle 0).".format(name_before, which)
            )
            _DECAL_SWAP_FAILURES.append(
                'Decal "{}" ({}): placed off its surface — check this render'.format(
                    name_before, which
                )
            )

        target, prep_note = _prepare_decal_image(base, d, key)
        ok, err = _set_decal_image(d, target)
        if not ok and target != base:
            # Never let the adjusted copy cost a write that would otherwise work.
            ok_base, _err_base = _set_decal_image(d, base)
            if ok_base:
                ok = True
                prep_note = "adjusted copy rejected ({}); wrote the original image — {}".format(
                    err, prep_note
                )
                target = base

        if not ok:
            failed.append((idx, target, which, err, prep_note))
            lines.append(
                'Decal "{}" ({}): direct write refused ({})'.format(name_before, which, err)
            )
            continue

        total += 1
        _AUTHORED_SLOT_BY_KEY[key] = slot
        try:
            name_after = d.name or "?"
        except Exception:
            name_after = "?"
        renamed = name_after == Path(target).stem
        lines.append(
            'Decal "{}" ({}): image -> {}{}'.format(
                name_after, which, target, "" if renamed else " [not confirmed by rename]"
            )
        )
        lines.append("    {}".format(prep_note))
        if key in _SUSPECT_STAND_IN_KEYS:
            lines.append(
                'Decal "{}" ({}): WARNING this is a stand-in that failed verification '
                "in an earlier colour set — check this render".format(name_after, which)
            )
            _DECAL_SWAP_FAILURES.append(
                'Decal "{}" ({}): unverified stand-in — check this render'.format(
                    name_after, which
                )
            )

    # Pass 2 — only decals whose write was refused.
    for idx, target, which, err, prep_note in failed:
        d, comp, occ, key = entries[idx]
        slot = slot_of.get(idx, 1)
        try:
            label = 'Decal "{}"'.format(d.name or "?")
        except Exception:
            label = "Decal"
        done = False

        # The same decal reached through its timeline entry occasionally resolves
        # where the collection's wrapper does not. Costs nothing to try.
        try:
            alt = adsk.fusion.Decal.cast(d.timelineObject.entity)
            if alt is not None and alt.faces and alt.faces[0] is not None:
                ok_alt, err_alt = _set_decal_image(alt, target)
                if ok_alt:
                    done = True
                    lines.append("{} ({}): image -> {} [via timeline entry]".format(label, which, target))
                    lines.append("    {}".format(prep_note))
                else:
                    lines.append("{}: timeline-entry write refused ({})".format(label, err_alt))
        except Exception as ex:
            lines.append("{}: timeline entry unavailable ({})".format(label, str(ex)[:80]))

        leftover = None
        if not done and DECAL_REPLACE_UNWRITABLE_AUTHORED:
            new_decal, notes, leftover = _replace_unwritable_decal(
                d,
                comp,
                target,
                label,
                in_root=occ is None,
                size_spec=(_STANDIN_SIZE_BY_KEY.get(key) or (None, None))[0]
                if DECAL_APPLY_JSON_SIZE
                else None,
            )
            lines.extend(notes)
            if new_decal is not None:
                done = True
                lines.append("    {}".format(prep_note))
                _RETIRED_AUTHORED_KEYS.add(key)
                _register_stand_in(key, comp, slot, target)
            elif leftover is not None:
                # The unverified stand-in now carries the part. The hidden original
                # must not be retried: a retry would stack a second stand-in.
                _RETIRED_AUTHORED_KEYS.add(key)
                stand_in_key = _register_stand_in(key, comp, slot, target)
                if stand_in_key is not None:
                    _SUSPECT_STAND_IN_KEYS.add(stand_in_key)

        if done:
            total += 1
            _AUTHORED_SLOT_BY_KEY.setdefault(key, slot)
        elif leftover is not None:
            lines.append(
                "{} ({}): FAILED ({}) — an unverified stand-in now carries this part; "
                "check this render.".format(label, which, err)
            )
            _DECAL_SWAP_FAILURES.append("{} ({})".format(label, which))
        else:
            face_hint = ""
            try:
                if d.faces and d.faces[0] is None:
                    face_hint = (
                        " Its face reference is broken in the .f3d: in Fusion, edit this "
                        "decal, re-select its face, and save the model."
                    )
            except Exception:
                pass
            lines.append(
                "{} ({}): FAILED ({}) — image NOT changed, this part keeps its "
                "authored texture.{}".format(label, which, err, face_hint)
            )
            _DECAL_SWAP_FAILURES.append("{} ({})".format(label, which))

    lines.extend(_report_unapplied_stand_in_sizes())
    return total, lines


def apply_color_set_for_open_design(
    mode: str,
    slot1: Optional[Path],
    slot2: Optional[Path],
) -> Tuple[int, List[str]]:
    """Apply textures using the requested pipeline against the active design.

    mode:
      - 'appearance' : update document appearances by name.
      - 'decal'      : swap the image on the designer's authored decals
                       (named match + positional fallback), then run the
                       appearance pass above.
    """
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return 0, ["No active design."]

    total = 0
    lines: List[str] = []

    if mode == "decal":
        d_total, d_lines = apply_decal_color_set(design, slot1, slot2)
        total += d_total
        lines += d_lines

    a_total, a_lines = apply_appearance_color_set(design, slot1, slot2)
    total += a_total
    lines += a_lines

    return total, lines


def list_appearance_and_decal_names(design: adsk.fusion.Design) -> Tuple[List[str], List[str]]:
    """Helper for UI: show names present in the document so users can edit SLOT*_ constants."""
    anames = [design.appearances.item(i).name for i in range(design.appearances.count)]
    dnames = [d.name for d in _collect_all_decals(design)]
    return anames, dnames


def design_has_slot2_target(design: adsk.fusion.Design) -> bool:
    """True if the document has at least one appearance matching slot-2 names."""
    apps = design.appearances
    for i in range(apps.count):
        if appearance_name_slot(apps.item(i).name) == 2:
            return True
    return False


def root_has_slot2_decal_target(design: adsk.fusion.Design) -> bool:
    decals = _collect_all_decals(design)
    for d in decals:
        if decal_name_slot(d.name) == 2:
            return True
    if DECAL_POSITIONAL_FALLBACK and len(decals) >= 2:
        return True
    return False
