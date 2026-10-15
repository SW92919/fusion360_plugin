"""Names, defaults and choices shared by the Batch Render panel (no Fusion needed)."""

from __future__ import annotations

COMMAND_ID = "LifeproofBatchRenderCmd"
PALETTE_ID = "LifeproofBatchRenderPanel"

DEFAULT_IMG_W = "2000"
DEFAULT_IMG_H = "1130"

# Label → Fusion renderQuality (0-100). "Final (75)" is the client's default.
RENDER_QUALITY_CHOICES = (
    ("Draft (25)", 25),
    ("Standard (50)", 50),
    ("Final (75)", 75),
    ("Excellent (100)", 100),
)
DEFAULT_RENDER_QUALITY = 75

IMAGE_FORMATS = ("PNG", "JPG")

# "Auto" reads the mode from the Render Instructions entry (decals[] present →
# decal), not from the .f3d filename — the model files no longer carry a
# "Decal"/"Appearance" suffix to sniff.
PIPELINE_CHOICES = (
    "Auto (from render instructions)",
    "Force Appearance",
    "Force Decal",
)
