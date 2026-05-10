# LifeproofBatchRender — Fusion 360 Python add-in entry point.
# Implementation lives in ./plugin (modular UI, controller, APS client skeleton).

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime

_ADDIN_DIR = os.path.dirname(os.path.abspath(__file__))
_STARTUP_LOG = os.path.join(_ADDIN_DIR, "_LifeproofBatchRender_startup.log")


def _log(message: str) -> None:
    """Best-effort startup log — runs even before Fusion APIs are available."""
    try:
        with open(_STARTUP_LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write("[{}] {}\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def _ensure_sys_path() -> None:
    if _ADDIN_DIR not in sys.path:
        sys.path.insert(0, _ADDIN_DIR)


def run(context):
    _ensure_sys_path()
    _log("run() called from {}".format(_ADDIN_DIR))
    try:
        from plugin import bootstrap

        bootstrap.run(context)
        _log("run() completed OK")
    except Exception:
        tb = traceback.format_exc()
        _log("run() FAILED:\n" + tb)
        try:
            import adsk.core

            app = adsk.core.Application.get()
            if app and app.userInterface:
                app.userInterface.messageBox(
                    "Lifeproof Batch Render failed to start.\n\n"
                    "See:\n{}\n\n{}".format(_STARTUP_LOG, tb)
                )
        except Exception:
            pass


def stop(context):
    _ensure_sys_path()
    _log("stop() called")
    try:
        from plugin import bootstrap

        bootstrap.stop(context)
        _log("stop() completed OK")
    except Exception:
        tb = traceback.format_exc()
        _log("stop() FAILED:\n" + tb)
