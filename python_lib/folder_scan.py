"""Discover color-set folders and _1 / _2 texture mapping."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


@dataclass(frozen=True)
class ColorSetTextures:
    folder: Path
    slot1: Path | None
    slot2: Path | None
    """Stem used for output naming (prefer slot1 stem)."""
    output_image_stem: str


def _is_raster(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in RASTER_SUFFIXES


def _ignored_texture_stem(stem: str) -> bool:
    sl = (stem or "").lower().strip()
    if "unflipped" in sl:
        return False
    if "_flipped" in sl:
        return True
    return bool(re.search(r"(?<!un)flipped(?![a-z])", sl))


def find_slot_images(folder: Path) -> tuple[Path | None, Path | None]:
    """
    Find *_1.* and *_2.* (case-insensitive stem ending with _1 or _2).
    Fallback: first / last raster if slots missing.
    """
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


def scan_texture_root(root: Path) -> list[ColorSetTextures]:
    """Each immediate subfolder of root is one color set."""
    if not root.is_dir():
        raise NotADirectoryError(root)
    sets: list[ColorSetTextures] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        s1, s2 = find_slot_images(child)
        stem = s1.stem if s1 else child.name
        sets.append(ColorSetTextures(folder=child, slot1=s1, slot2=s2, output_image_stem=stem))
    return sets
