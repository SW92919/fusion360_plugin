# Fusion 360: apply color-set images via document appearances OR root decals.
# Edit the name sets below to match your template .f3d (Appearance / Decal names in Browser).

from __future__ import annotations

import base64
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, FrozenSet, List, Optional, Set, Tuple

import adsk.core
import adsk.fusion

from visibility_apply import (
    _body_hide_for_batch,
    _mesh_hide_for_batch,
    _occurrence_should_hide_batch,
)

# --- User template: exact Fusion names for each slot (Appearance mode) ---
SLOT1_APPEARANCE_NAMES: FrozenSet[str] = frozenset(
    {
        "custom_appearance_1",
        "Batch_Slot_1",
        "Wood_1",
        "Texture_1",
        # Common species names used as the primary wood appearance:
        "Pine",
        "Oak",
        "Maple",
        "Walnut",
        "Ash",
        "Cherry",
        "Birch",
        "Hickory",
        "Wood",
        "Wood-1",
        "Wood_1",
    }
)
SLOT2_APPEARANCE_NAMES: FrozenSet[str] = frozenset(
    {
        "custom_appearance_2",
        "Batch_Slot_2",
        "Wood_2",
        "Texture_2",
        "Wood-2",
    }
)

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

# When True, slot 1 is also pushed into EVERY appearance in the design that
# already has a texture slot, in addition to (or instead of) explicit name
# matching. This is what gives "100% coverage" on templates whose
# appearance names you haven't pre-configured. Set to False if you want
# strict per-name control (e.g. when a model has wood + steel + glass
# textures and you only want to swap the wood).
APPEARANCE_BROADCAST: bool = True

# When True, every body in the model is force-assigned to a single "carrier"
# appearance whose texture is swapped per color set. This gives a real
# 100% coverage even when the template's appearances are procedural /
# read-only / refuse changeTextureImage. The original body->appearance
# mapping is restored at the end of the batch. Set to False if you want
# the original mixed materials preserved (some bodies will not change).
FORCE_BODY_COVERAGE: bool = True

# When True, every body gets a freshly-created decal of the user's color-set
# image projected onto its largest planar face. Disabled by default because
# the Fusion 360 builds we tested on silently reject every decal-sizing API
# (width/height on input, post-add, scaleX/Y, scaleFactor, transform matrix
# scaling) — decals are created at Fusion's default ~5cm patch size and
# can't be made to cover the face. With this flag False, the plugin runs
# the appearance pipeline against ``SLOT1_APPEARANCE_NAMES`` (the prepared-
# template path described in docs/CLIENT_GUIDE_Lifeproof_Batch_Render.md).
BODY_COVERAGE_VIA_DECALS: bool = False

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

# Multiplier applied to the face's longest bounding-box side when sizing each
# decal. Fusion clips decals at the face boundary, so over-sizing the decal
# guarantees full-face coverage even when the build auto-fits to the image
# aspect ratio. Set this aggressively — even when Fusion ignores it, the
# tiling fallback below picks up the slack.
DECAL_OVERSIZE_FACTOR: float = 20.0

# When True, decals are TILED across the face's longest direction in a grid
# of step-sized cells, instead of one decal centered on the face. This is
# the workaround for Fusion builds that ignore every decal-sizing API:
# place many small default-sized decals next to each other and let them
# collectively cover the face.
BATCH_DECAL_TILE: bool = True

# Grid step in cm for tiling. Each decal is a ~5 cm patch; with image
# slicing OFF every tile shows the full swatch. 5 cm makes cells the same
# size as the patch → contiguous coverage with no bare slivers (6+ cm left
# the plank only partially covered, reading as a strip). Smaller = denser.
BATCH_DECAL_TILE_STEP_CM: float = 5.0

# Hard cap on tile decals per single face. A large plank/tread show face
# needs a full grid (a ~120 × 15 cm face is ~24 × 3 = ~72 patches at 5 cm)
# or it only partially covers. Set high enough for full single-face cover.
BATCH_DECAL_MAX_TILES_PER_FACE: int = 80

# Hard cap on total batch decals across the WHOLE model — the anti-freeze
# safety valve (each decal = 1 create + N color-set swaps + 1 teardown, all
# synchronous re-projections). This is THE speed↔coverage knob:
#   * lower (e.g. 80)  → faster, but long parts may show bare gaps;
#   * higher (e.g. 160)→ fuller coverage, slower (can freeze on weak PCs).
# 320 + GLOBAL largest-face-first + visible-only filtering: enough to FULLY
# cover the visible show faces of a multi-body assembly at the 5 cm step
# (each big plank/tread/nose face wants ~70-80 patches). Ray tracing is OFF
# (instant viewport capture) so decals are the only load; ~320 stays
# workable (UI is pumped) — it's slower than before but gives full coverage.
# Lower if decal create/teardown bogs the UI; raise if gaps remain.
BATCH_DECAL_MAX_TOTAL: int = 320

# Faces smaller than this (cm² of bounding-box area) are skipped entirely —
# they are slivers/fillets that are not meaningfully visible and only burn
# the decal budget.
BATCH_DECAL_MIN_FACE_AREA_CM2: float = 1.5

# Faces whose longest side is <= this (cm) get exactly ONE decal instead of
# a grid: a single ~5 cm patch already covers them (end caps, short returns),
# so gridding them just wastes the budget and freezes the UI.
BATCH_DECAL_SINGLE_DECAL_MAX_CM: float = 7.0

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
        from PIL import Image
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
        if ap.name in SLOT1_APPEARANCE_NAMES and slot1:
            target_path = _win_path(slot1)
        elif ap.name in SLOT2_APPEARANCE_NAMES and slot2:
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
        from PIL import Image
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


def _create_decal_on_face(
    decals_collection: Any,
    image_win_path: str,
    face: adsk.fusion.BRepFace,
    decal_name: str,
    center_override: Optional[adsk.core.Point3D] = None,
) -> Tuple[Optional[adsk.fusion.Decal], str]:
    """Create one decal on ``face`` using the known-working call shape:
    ``createInput(filename, [face], point_on_face)``. Keep API calls to the
    minimum — every extra ``getattr``/``setattr`` on a Fusion API object can
    trigger a compute/redraw and 100+ decals × extra calls freezes the UI.

    When ``center_override`` is provided, the decal is placed at that point
    (used by tiling to spread decals across the face). Otherwise we pick the
    face's natural centre point.
    """
    center = center_override if center_override is not None else _on_face_point(face)
    if center is None:
        return None, "no on-face point"

    # MINIMAL fast path: createInput + add + name. Nothing else.
    #
    # Earlier iterations tried width/height/scaleX/Y/scaleFactor/xDirection
    # setattr on both DecalCreateInput AND Decal, plus a transform-matrix
    # get+set as a "last resort". On this Fusion build NONE of those took
    # effect (verified by the user — visible decal size never changed) but
    # `decal.transform = new_m` and the readbacks each triggered a full
    # Fusion re-projection per decal (~100-300ms × hundreds of tile decals =
    # the UI freeze the user kept hitting). Coverage now comes from the
    # tiling grid (BATCH_DECAL_TILE), not from per-decal sizing.
    try:
        decal_input = decals_collection.createInput(image_win_path, [face], center)
    except Exception as ex:
        return None, "createInput: {}".format(ex)
    if decal_input is None:
        return None, "createInput returned None"

    try:
        decal = decals_collection.add(decal_input)
    except Exception as ex:
        return None, "decals.add: {}".format(ex)
    if decal is None:
        return None, "decals.add returned None"

    try:
        decal.name = decal_name
    except Exception:
        pass
    return decal, ""


def create_batch_decals_for_all_bodies(
    design: adsk.fusion.Design, image_path: Path
) -> Tuple[List[adsk.fusion.Decal], List[str]]:
    """Project ``image_path`` onto every body as a decal on its largest face.

    Returns ``(decals_created, log_lines)``. The decals are added to whichever
    component the body belongs to (root or sub-occurrence). All created decals
    are named ``BATCH_DECAL_NAME_PREFIX<index>`` so ``cleanup_batch_decals``
    can find and remove them without touching pre-existing decals.
    """
    lines: List[str] = []
    created: List[adsk.fusion.Decal] = []
    if not image_path or not image_path.is_file():
        lines.append("Decal projection skipped: image not found ({})".format(image_path))
        return created, lines

    image_win = _win_path(image_path)

    # (area, face, decals_collection, body_label) gathered across the WHOLE
    # model in phase 1, then decaled biggest-first in phase 2. Collecting
    # globally (not per body) is what stops a small first-processed body
    # like "Foam Pad" eating the whole budget and starving the visible
    # tread / nose faces.
    candidates: List[Tuple[float, Any, Any, str]] = []

    def _handle_component(
        comp: adsk.fusion.Component, label: str
    ) -> None:
        try:
            decals_coll = comp.decals
        except Exception as ex:
            lines.append("  {}: decals collection error: {}".format(label, ex))
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
            if _body_hide_for_batch(body, include_face_uv_pins=False):
                continue
            # Skip bodies the designer hid in the .f3d — they are not in the
            # render, so spending the decal budget on them leaves the
            # visible geometry bare.
            if not _entity_is_visible(body, default=True):
                continue
            body_label = "{}/{}".format(label, body.name or "?")

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
                    decal, info = _create_decal_on_face(decals_coll, image_win, face, name)
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
                # Phase 1: just COLLECT this body's worthwhile faces. Decals
                # are created later (phase 2) biggest-face-first across the
                # WHOLE model, so the global budget always lands on the
                # visible show surfaces regardless of which body/occurrence
                # they belong to (a small first-processed body like the Foam
                # Pad must not eat the whole budget).
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
                    a = _face_area(f)
                    if a < BATCH_DECAL_MIN_FACE_AREA_CM2:
                        continue  # sliver / fillet — not worth a decal
                    candidates.append((a, f, decals_coll, body_label))

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

    # ---- Phase 2: decal the biggest faces in the WHOLE model first ----
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
                BATCH_DECAL_TILE
                and d_long > BATCH_DECAL_SINGLE_DECAL_MAX_CM
            )
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
            for tile_pt, iu, iv, nu, nv in tile_specs:
                name = "{}{}".format(BATCH_DECAL_NAME_PREFIX, len(created))
                slice_path = (
                    _slice_image_for_tile(image_path, iu, iv, nu, nv)
                    if BATCH_DECAL_TILE_SLICE_IMAGE and nu * nv > 1
                    else None
                )
                decal_image_str = (
                    _win_path(slice_path) if slice_path else image_win
                )
                decal, err = _create_decal_on_face(
                    decals_coll,
                    decal_image_str,
                    face,
                    name,
                    center_override=tile_pt,
                )
                if decal is None:
                    per_body_fail[body_label] = (
                        per_body_fail.get(body_label, 0) + 1
                    )
                    if not first_err:
                        first_err = err
                    continue
                created.append(decal)
                _TILE_METADATA[id(decal)] = (iu, iv, nu, nv)
                per_body_ok[body_label] = per_body_ok.get(body_label, 0) + 1
                used_face = True
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
                    "  {}: {} tile decal(s) added{}".format(bl, ok, extra)
                )
        if budget_hit:
            lines.append(
                "  Global decal budget {} reached — smaller faces skipped "
                "(anti-freeze cap)".format(BATCH_DECAL_MAX_TOTAL)
            )

    lines.insert(0, "Batch decal projection: {} decal(s) created".format(len(created)))
    return created, lines


def update_batch_decal_images(
    decals: List[adsk.fusion.Decal], image_path: Path
) -> Tuple[int, List[str]]:
    """Swap ``imageFilename`` on every tracked decal. When per-decal grid
    metadata exists in ``_TILE_METADATA``, each decal receives its own slice
    of ``image_path`` so the tiles assemble into one continuous image.
    """
    lines: List[str] = []
    if not image_path or not image_path.is_file():
        lines.append("Decal update skipped: image not found ({})".format(image_path))
        return 0, lines
    base_target = _win_path(image_path)
    n = 0
    pump_every = max(int(BATCH_DECAL_UI_PUMP_INTERVAL), 1)
    # Cache slice paths so we only run PIL once per (iu, iv, nu, nv) — many
    # decals share the same grid coords if a face yields the same cell across
    # passes (defensive; usually each decal has a unique tuple).
    slice_cache: dict = {}
    for idx, d in enumerate(decals):
        meta = _TILE_METADATA.get(id(d))
        if (
            BATCH_DECAL_TILE_SLICE_IMAGE
            and meta is not None
            and meta[2] * meta[3] > 1
        ):
            cache_key = meta
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
    lines.insert(0, "Batch decal update: {} / {} imageFilename swap(s)".format(n, len(decals)))
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
            import PIL  # noqa: F401
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


def broadcast_to_textured_appearances(
    design: adsk.fusion.Design,
    slot1: Optional[Path],
) -> Tuple[int, List[str]]:
    """Push slot 1 into every appearance that already exposes a texture slot.

    Skips appearances with no AppearanceTexture properties so plain colour
    appearances (and most metals/plastics) are left untouched. This is the
    "fill everything textureable" pass used to get 100% coverage when the
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


def list_appearance_and_decal_names(design: adsk.fusion.Design) -> Tuple[List[str], List[str]]:
    """Helper for UI: show names present in the document so users can edit SLOT*_ constants."""
    anames = [design.appearances.item(i).name for i in range(design.appearances.count)]
    dnames = [d.name for d in _collect_all_decals(design)]
    return anames, dnames


def design_has_slot2_target(design: adsk.fusion.Design) -> bool:
    """True if the document has at least one appearance matching slot-2 names."""
    apps = design.appearances
    for i in range(apps.count):
        if apps.item(i).name in SLOT2_APPEARANCE_NAMES:
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
