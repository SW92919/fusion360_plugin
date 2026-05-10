"""Build a flat list of render tasks (model × color × view [× visibility])."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from folder_scan import ColorSetTextures, scan_texture_root


@dataclass(frozen=True)
class RenderTask:
    model_path: Path
    color_set: ColorSetTextures
    named_view: str
    visibility_tag: str | None  # None = default / no extra suffix in filename


def plan_tasks(
    model_paths: list[Path],
    texture_root: Path,
    named_views: list[str],
    visibility_tags: list[str | None] | None = None,
) -> list[RenderTask]:
    """
    `visibility_tags`: e.g. [None, "Body1-off"] for filename suffix; Fusion side applies real visibility.
    """
    color_sets = scan_texture_root(texture_root)
    tags = visibility_tags if visibility_tags is not None else [None]
    tasks: list[RenderTask] = []
    for m in model_paths:
        for cs in color_sets:
            for v in named_views:
                for t in tags:
                    tasks.append(RenderTask(model_path=m, color_set=cs, named_view=v, visibility_tag=t))
    return tasks
