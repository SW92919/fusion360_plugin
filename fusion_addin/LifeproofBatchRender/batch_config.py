# Read/validate batch settings from command inputs (keeps LifeproofBatchRender.py smaller).

from __future__ import annotations

from pathlib import Path
from typing import Callable, List

import adsk.core


def parse_model_paths_blob(text: str) -> List[Path]:
    if not text or not text.strip():
        return []
    raw = text.replace("\r", "").replace("|", "\n").replace(";", "\n")
    return [Path(line.strip()) for line in raw.split("\n") if line.strip()]


def resolve_pipeline_mode(
    setting: str,
    model_path: Path,
    infer_fn: Callable[[Path], str],
) -> str:
    s = (setting or "").strip().lower()
    if "force appearance" in s or s == "appearance":
        return "appearance"
    if "force decal" in s or s == "decal":
        return "decal"
    return infer_fn(model_path)


def read_textbox_or_string(inp: adsk.core.CommandInput) -> str:
    if inp is None:
        return ""
    for attr in ("text", "formattedText"):
        if hasattr(inp, attr):
            try:
                val = getattr(inp, attr)
                if val is not None:
                    return str(val)
            except Exception:
                pass
    try:
        return str(inp.value or "")
    except Exception:
        return ""


def parse_positive_int(s: str, default: int, min_v: int, max_v: int) -> int:
    try:
        v = int(str(s).strip())
    except Exception:
        v = default
    return max(min_v, min(max_v, v))


def parse_bounded_int(s: str, default: int, min_v: int, max_v: int) -> int:
    """Alias for bounded integer parsing (concurrency, etc.)."""
    return parse_positive_int(s, default, min_v, max_v)
