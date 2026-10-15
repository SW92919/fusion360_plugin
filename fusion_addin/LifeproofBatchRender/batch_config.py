# Parse the Batch Render panel's settings (pure Python, no Fusion API).

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, List


def parse_model_paths_blob(text: str) -> List[Path]:
    """Model paths from the panel's model-files box: ``|`` / ``;`` separated, or one per line.

    Also accepts HTML line breaks and two paths run together without a separator
    (a pasted list; only one of two selected models was rendered on 2026-10-15).
    """
    if not text or not text.strip():
        return []
    raw = re.sub(r"(?i)<br\s*/?>|</?p[^>]*>|</?div[^>]*>", "\n", text)
    raw = raw.replace("\r", "").replace("|", "\n").replace(";", "\n")
    out: List[Path] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = re.findall(r".+?\.f3d(?=\s*(?:[A-Za-z]:[\\/]|\\\\|$))", line, flags=re.I)
        out.extend(Path(p.strip()) for p in (parts or [line]))
    return out


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


def parse_positive_int(s: str, default: int, min_v: int, max_v: int) -> int:
    try:
        v = int(str(s).strip())
    except Exception:
        v = default
    return max(min_v, min(max_v, v))


