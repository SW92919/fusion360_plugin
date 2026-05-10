"""Infer appearance vs decal workflow from model filename (matches your .f3d naming)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

TextureMode = Literal["appearance", "decal"]


def infer_texture_mode(model_path: Path) -> TextureMode:
    # Keep logic aligned with fusion_addin/LifeproofBatchRender/support_paths.infer_texture_mode
    stem = model_path.stem.lower()
    if "decal" in stem:
        return "decal"
    if "appearance" in stem or "appearances" in stem:
        return "appearance"
    return "appearance"
