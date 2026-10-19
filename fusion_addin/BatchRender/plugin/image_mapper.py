from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    import adsk.fusion

import texture_pipeline


def effective_texture_slots(
    design: "adsk.fusion.Design",
    slot1: Optional[Path],
    slot2: Optional[Path],
) -> Tuple[Optional[Path], Optional[Path]]:
    """
    If the model has no second decal target, ignore the *_2 image (never crash).
    """
    if not texture_pipeline.root_has_slot2_decal_target(design):
        return slot1, None
    return slot1, slot2
