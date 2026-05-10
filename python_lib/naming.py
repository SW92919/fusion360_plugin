"""Build output filenames from model, texture stem, view, and optional visibility label."""

from __future__ import annotations

import re
from pathlib import Path


def sanitize_filename_segment(segment: str) -> str:
    """Remove characters that are risky on Windows paths."""
    s = segment.strip()
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    s = re.sub(r"\s+", " ", s)
    return s


def build_output_basename(
    model_stem: str,
    image_stem: str,
    named_view: str,
    visibility_label: str | None = None,
    sep: str = " - ",
) -> str:
    """
    Default pattern:
      {model}{sep}{image}{sep}{view}
    Optional:
      {model}{sep}{image}{sep}{view}{sep}{visibility}
    """
    parts = [
        sanitize_filename_segment(model_stem),
        sanitize_filename_segment(image_stem),
        sanitize_filename_segment(named_view),
    ]
    if visibility_label:
        parts.append(sanitize_filename_segment(visibility_label))
    return sep.join(parts)


def versioned_path(path: Path) -> Path:
    """
    If `path` exists, return path with ' (vN)' before suffix, N >= 2.
    """
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem} (v{n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1
