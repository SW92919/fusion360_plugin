from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_log_path(texture_root: Path) -> Path:
    return texture_root / "_LifeproofBatchRender_plugin.log"


def append_log(log_path: Path, message: str, exc: Optional[BaseException] = None) -> None:
    line = "[{}] {}\n".format(_ts(), message)
    if exc is not None:
        line += traceback.format_exc() + "\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
    except Exception:
        pass
