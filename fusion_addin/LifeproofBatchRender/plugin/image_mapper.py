from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    import adsk.fusion

import texture_pipeline


def effective_texture_slots(
    design: "adsk.fusion.Design",
    mode: str,
    slot1: Optional[Path],
    slot2: Optional[Path],
) -> Tuple[Optional[Path], Optional[Path]]:
    """
    If the active document has no second-slot targets, ignore *_2 images (never crash).
    """
    if mode == "appearance":
        if not texture_pipeline.design_has_slot2_target(design):
            return slot1, None
        return slot1, slot2
    if mode == "decal":
        if not texture_pipeline.root_has_slot2_decal_target(design):
            return slot1, None
        return slot1, slot2
    return slot1, slot2
