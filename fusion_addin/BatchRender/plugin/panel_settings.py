"""Checks the Batch Render panel's settings before a run (no Fusion needed)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import batch_config
import render_instructions

from plugin import ui as uip


@dataclass
class BatchRequest:
    """Everything the controller needs for one batch, already checked."""

    instructions_path: Path
    texture_root: Path
    models: List[Path]
    width: int
    height: int
    ext: str
    render_quality: int


def panel_defaults() -> Dict[str, Any]:
    """The choices and starting values the panel's page is filled with."""
    quality = next(
        name for name, value in uip.RENDER_QUALITY_CHOICES if value == uip.DEFAULT_RENDER_QUALITY
    )
    return {
        "qualities": [name for name, _value in uip.RENDER_QUALITY_CHOICES],
        "formats": list(uip.IMAGE_FORMATS),
        "values": {
            "instructions": "",
            "textureRoot": "",
            "models": "",
            "width": uip.DEFAULT_IMG_W,
            "height": uip.DEFAULT_IMG_H,
            "quality": quality,
            "format": uip.IMAGE_FORMATS[0],
        },
    }


def _clean_path(value: Any) -> str:
    return str(value or "").strip().strip('"').strip()


def validate_settings(raw: Dict[str, Any]) -> Tuple[Optional[BatchRequest], str]:
    """``(request, "")`` when the batch can start, else ``(None, what to fix)``."""
    instructions_raw = _clean_path(raw.get("instructions"))
    if not instructions_raw:
        return None, (
            "Enter the Render Instructions Location "
            "(full path to the render instructions .json or .txt file)."
        )
    instructions_path = Path(instructions_raw)
    if not instructions_path.is_file():
        # A relative path resolves against Fusion's working directory, not the
        # project, so show where it actually looked.
        try:
            resolved = instructions_path.resolve()
        except Exception:
            resolved = instructions_path
        hint = ""
        if not instructions_path.is_absolute():
            hint = (
                "\n\nThat is a RELATIVE path. Fusion resolves it against its own working "
                "folder, not your project. Use the ... button, or paste the full path "
                "starting with a drive letter."
            )
        return None, "Render Instructions file not found.\n\nEntered : {}\nLooked in: {}{}".format(
            instructions_raw, resolved, hint
        )

    texture_raw = _clean_path(raw.get("textureRoot"))
    if not texture_raw:
        return None, "Enter the texture root folder path."
    texture_root = Path(texture_raw)
    if not texture_root.is_dir():
        return None, "Texture root is not a valid folder:\n{}".format(texture_root)

    models = batch_config.parse_model_paths_blob(str(raw.get("models") or ""))
    if not models:
        # Left blank = render every model the instructions list, taken from the
        # folder the instructions file itself came from. The JSON names the models
        # but carries no paths; fill the field in to render a subset instead.
        loaded = render_instructions.load_instructions(instructions_path)
        models, find_lines = render_instructions.discover_model_files(loaded)
        if not models:
            return None, (
                "No model files given, and none could be found automatically.\n\n{}\n\n"
                "Use the ... button to pick the .f3d files, or put them next to the "
                "instructions file.".format("\n".join(find_lines))
            )
    for path in models:
        if not path.exists():
            return None, "Model file not found:\n{}".format(path)

    width = batch_config.parse_positive_int(raw.get("width"), int(uip.DEFAULT_IMG_W), 64, 8192)
    height = batch_config.parse_positive_int(raw.get("height"), int(uip.DEFAULT_IMG_H), 64, 8192)
    fmt = str(raw.get("format") or uip.IMAGE_FORMATS[0])
    ext = ".png" if fmt.upper().startswith("P") else ".jpg"
    quality_label = str(raw.get("quality") or "")
    quality = next(
        (value for name, value in uip.RENDER_QUALITY_CHOICES if name == quality_label),
        uip.DEFAULT_RENDER_QUALITY,
    )

    return (
        BatchRequest(
            instructions_path=instructions_path,
            texture_root=texture_root,
            models=list(models),
            width=width,
            height=height,
            ext=ext,
            render_quality=quality,
        ),
        "",
    )
