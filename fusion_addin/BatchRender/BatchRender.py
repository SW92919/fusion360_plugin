# BatchRender — Fusion 360 Python add-in entry point. Fusion lists an add-in by its
# folder name, which must match this file's and the .manifest's names.
# Implementation lives in ./plugin (modular UI, controller, local render export).

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


def _purge_addin_modules() -> int:
    """Drop this add-in's modules from ``sys.modules`` so edits take effect.

    Fusion keeps the Python interpreter alive for the whole session, so
    ``import texture_pipeline`` returns the copy loaded the FIRST time the add-in
    ran. Stopping and restarting the add-in re-runs ``run()`` but does NOT
    re-import — so edited code silently does not take effect and you end up
    testing a stale build while reading a fresh log.

    Only this add-in's own modules are dropped (anything whose ``__file__`` lives
    under the add-in folder); adsk and the standard library are left alone.
    """
    base = os.path.normcase(os.path.abspath(_ADDIN_DIR))
    dropped = 0
    for name in list(sys.modules):
        if name == __name__:
            continue
        mod = sys.modules.get(name)
        path = getattr(mod, "__file__", None)
        if not path:
            continue
        try:
            if os.path.normcase(os.path.abspath(path)).startswith(base):
                del sys.modules[name]
                dropped += 1
        except Exception:
            continue
    return dropped


def run(context):
    _ensure_sys_path()
    _log("run() called from {}".format(_ADDIN_DIR))
    try:
        # Before importing anything of ours — otherwise a reload of the add-in
        # silently keeps running the code loaded when Fusion started.
        dropped = _purge_addin_modules()
        _log("purged {} cached add-in module(s) so edits take effect".format(dropped))

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
                    "Batch Render failed to start.\n\n"
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
