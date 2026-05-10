# Shipped inside the add-in bundle so Fusion can load without repo PYTHONPATH.
# Keep in sync with ../../python_lib/folder_scan.py and naming.py when those change.

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import shutil

RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

# Delete ``*flipped*`` sidecar rasters in each color folder before swatches (not written by this add-in).
DELETE_FLIPPED_TEXTURE_SIDECARS: bool = True


def infer_texture_mode(model_path: Path) -> str:
    """Return 'decal' or 'appearance' from .f3d filename (see docs/APPEARANCE_AND_DECAL.md)."""
    stem = model_path.stem.lower()
    if "decal" in stem:
        return "decal"
    if "appearance" in stem or "appearances" in stem:
        return "appearance"
    return "appearance"


def _is_raster(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in RASTER_SUFFIXES


def _ignored_texture_stem(stem: str) -> bool:
    """Sidecar / mirror duplicates must never be chosen as slot1/slot2 or fallbacks."""
    sl = (stem or "").lower().strip()
    if "unflipped" in sl:
        return False
    if "_flipped" in sl:
        return True
    return bool(re.search(r"(?<!un)flipped(?![a-z])", sl))


def purge_ignored_texture_sidecars(folder: Path) -> List[str]:
    if not DELETE_FLIPPED_TEXTURE_SIDECARS or not folder.is_dir():
        return []
    lines: List[str] = []
    for p in list(folder.iterdir()):
        if not _is_raster(p) or not _ignored_texture_stem(p.stem):
            continue
        try:
            p.unlink()
            lines.append("Removed sidecar {}".format(p.name))
        except OSError as ex:
            lines.append("Remove {} FAILED: {}".format(p.name, ex))
    return lines


def find_slot_images(folder: Path) -> Tuple[Optional[Path], Optional[Path]]:
    rasters = sorted(
        [p for p in folder.iterdir() if _is_raster(p) and not _ignored_texture_stem(p.stem)],
        key=lambda x: x.name.lower(),
    )
    slot1 = slot2 = None
    for p in rasters:
        stem_lower = p.stem.lower()
        if stem_lower.endswith("_1"):
            slot1 = p
        elif stem_lower.endswith("_2"):
            slot2 = p
    if not slot1 and rasters:
        slot1 = rasters[0]
    if not slot2 and len(rasters) >= 2:
        slot2 = rasters[-1]
    elif not slot2 and len(rasters) == 1:
        slot2 = None
    return slot1, slot2


@dataclass(frozen=True)
class ColorSetTextures:
    folder: Path
    slot1: Optional[Path]
    slot2: Optional[Path]
    output_image_stem: str


def scan_texture_root(root: Path) -> List[ColorSetTextures]:
    if not root.is_dir():
        raise NotADirectoryError(root)
    sets: List[ColorSetTextures] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        s1, s2 = find_slot_images(child)
        stem = s1.stem if s1 else child.name
        sets.append(ColorSetTextures(folder=child, slot1=s1, slot2=s2, output_image_stem=stem))
    return sets


def sanitize_filename_segment(segment: str) -> str:
    s = segment.strip()
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    s = re.sub(r"\s+", " ", s)
    return s


def write_color_set_delivery_swatches(cs: ColorSetTextures) -> Tuple[int, List[str]]:
    """Copy slot rasters to predictable names next to batch renders.

    Clients often expect ``color-set-01-1.jpg`` / ``color-set-02-2.png`` alongside the
    viewport PNGs (same folder as source textures). Uses the **color subfolder name**
    as the stem prefix; preserves the source file extension.
    """
    lines: List[str] = []
    n_ok = 0
    base = sanitize_filename_segment(cs.folder.name)

    def _copy_slot(src: Optional[Path], slot_suffix: str) -> None:
        nonlocal n_ok
        label = "{}{}".format(base, slot_suffix)
        if not src or not src.is_file():
            lines.append("{}: skipped (no raster)".format(label))
            return
        dst = cs.folder / "{}{}{}".format(base, slot_suffix, src.suffix.lower())
        try:
            if dst.resolve() == src.resolve():
                lines.append("{}".format(dst.name))
                n_ok += 1
                return
            shutil.copy2(src, dst)
            n_ok += 1
            lines.append("{}".format(dst.name))
        except Exception as ex:
            lines.append("{} FAILED: {}".format(dst.name, ex))

    _copy_slot(cs.slot1, "-1")
    _copy_slot(cs.slot2, "-2")
    return n_ok, lines


def build_output_basename(
    model_stem: str,
    image_stem: str,
    named_view: str,
    visibility_label: Optional[str] = None,
    sep: str = " - ",
) -> str:
    parts = [
        sanitize_filename_segment(model_stem),
        sanitize_filename_segment(image_stem),
        sanitize_filename_segment(named_view),
    ]
    if visibility_label:
        parts.append(sanitize_filename_segment(visibility_label))
    return sep.join(parts)


def versioned_path(path: Path) -> Path:
    """If path exists, return path with ' (vN)' before suffix, N >= 2."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name("{} (v{}){}".format(stem, n, suffix))
        if not candidate.exists():
            return candidate
        n += 1
