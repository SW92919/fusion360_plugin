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


def _is_batch_output_stem(stem: str) -> bool:
    """True for PNG/JPG renders this add-in writes into color folders."""
    s = (stem or "").strip()
    if re.search(r" \(v\d+\)$", s):
        return True
    return s.count(" - ") >= 2


def _is_slot1_stem(stem_lower: str) -> bool:
    return stem_lower.endswith("_1") or stem_lower.endswith("-1")


def _is_slot2_stem(stem_lower: str) -> bool:
    return stem_lower.endswith("_2") or stem_lower.endswith("-2")


def find_slot_images(folder: Path) -> tuple[Path | None, Path | None]:
    """
    Find slot-1 / slot-2 rasters (``*_1``, ``*-1``, ``*_2``, ``*-2`` stems).
    Ignores batch-render outputs and flipped sidecars. Fallback: first / last
    raster if slots missing.
    """
    rasters = sorted(
        [
            p
            for p in folder.iterdir()
            if _is_raster(p)
            and not _ignored_texture_stem(p.stem)
            and not _is_batch_output_stem(p.stem)
        ],
        key=lambda x: x.name.lower(),
    )
    slot1 = slot2 = None
    for p in rasters:
        stem_lower = p.stem.lower()
        if _is_slot1_stem(stem_lower):
            slot1 = p
        elif _is_slot2_stem(stem_lower):
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
