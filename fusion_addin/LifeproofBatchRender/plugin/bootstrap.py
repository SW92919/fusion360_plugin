from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import adsk.core

from plugin import panel
from plugin import ui as uip

_handlers: list = []

_ADDIN_DIR = Path(__file__).resolve().parent.parent
_STARTUP_LOG = _ADDIN_DIR / "_LifeproofBatchRender_startup.log"

# Ordered list of (workspaceId, panelId) targets we will TRY to attach to.
# We never raise if a target is missing; the command stays available via Fusion's
# command-search (Ctrl+S) even if no ribbon target works on this build.
_PANEL_TARGETS = (
    ("FusionSolidEnvironment", "SolidScriptsAddinsPanel"),
    ("FusionSolidEnvironment", "SolidModifyPanel"),
    ("FusionDesignEnvironment", "SolidScriptsAddinsPanel"),
    ("FusionDesignEnvironment", "SolidModifyPanel"),
)


def _log(msg: str) -> None:
    try:
        with open(_STARTUP_LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write("[{}] {}\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _destroy(fui: adsk.core.UserInterface, obj: Optional[adsk.core.Base]) -> None:
    try:
        if obj and obj.isValid:
            obj.deleteMe()
    except Exception:
        pass


def _create_command_definition(
    cmd_defs: adsk.core.CommandDefinitions,
    cmd_id: str,
    name: str,
    tooltip: str,
):
    """
    Different Fusion builds expose different APIs:
      - addButtonDefinition(id, name, tooltip, resourceFolder='')   (current builds)
      - addCommandDefinition(id, name, tooltip, resourceFolder='')  (older builds)
    Try both, with and without the resource-folder argument.
    """
    last_err: Optional[Exception] = None
    for attr in ("addButtonDefinition", "addCommandDefinition"):
        fn = getattr(cmd_defs, attr, None)
        if fn is None:
            continue
        for args in ((cmd_id, name, tooltip, ""), (cmd_id, name, tooltip)):
            try:
                return fn(*args)
            except Exception as ex:
                last_err = ex
    if last_err is not None:
        _log("create_command_definition: last error = {}".format(last_err))
    return None


def _attach_to_ribbon(fui: adsk.core.UserInterface, cmd_def: adsk.core.CommandDefinition) -> List[str]:
    """Try to attach the command to known ribbon panels. Returns list of panels we hit."""
    attached: List[str] = []
    for ws_id, panel_id in _PANEL_TARGETS:
        try:
            ws = fui.workspaces.itemById(ws_id)
            if not ws:
                continue
            ribbon_panel = ws.toolbarPanels.itemById(panel_id)
            if not ribbon_panel:
                continue
            ctrls = ribbon_panel.controls
            if ctrls.itemById(uip.COMMAND_ID) is None:
                ctrls.addCommand(cmd_def, "", False)
            attached.append("{}/{}".format(ws_id, panel_id))
        except Exception as ex:
            _log("attach skip {}/{}: {}".format(ws_id, panel_id, ex))
    return attached


def _detach_from_ribbon(fui: adsk.core.UserInterface) -> None:
    for ws_id, panel_id in _PANEL_TARGETS:
        try:
            ws = fui.workspaces.itemById(ws_id)
            if not ws:
                continue
            ribbon_panel = ws.toolbarPanels.itemById(panel_id)
            if not ribbon_panel:
                continue
            ctrl = ribbon_panel.controls.itemById(uip.COMMAND_ID)
            _destroy(fui, ctrl)
        except Exception:
            pass


def run(context):
    _ = context
    fui: Optional[adsk.core.UserInterface] = None
    try:
        app = adsk.core.Application.get()
        fui = app.userInterface
    except Exception:
        _log("Application.get()/userInterface failed:\n" + traceback.format_exc())
        return

    try:
        panel.start(app, fui, _log)

        class OnCreated(adsk.core.CommandCreatedEventHandler):
            """The toolbar button opens the Batch Render panel.

            The command creates no inputs, so Fusion shows no dialog of its own.
            """

            def __init__(self):
                super().__init__()

            def notify(self, args):
                try:
                    panel.show(app, fui)
                except Exception:
                    tb = traceback.format_exc()
                    _log("opening the panel failed:\n" + tb)
                    try:
                        fui.messageBox("Batch Render could not open its panel:\n{}".format(tb))
                    except Exception:
                        pass

        cmd_defs = fui.commandDefinitions
        try:
            existing = cmd_defs.itemById(uip.COMMAND_ID)
            if existing:
                existing.deleteMe()
        except Exception:
            pass

        cmd_def = _create_command_definition(
            cmd_defs,
            uip.COMMAND_ID,
            "Batch Render",
            "Batch textures, named views, visibility, local render export.",
        )
        if cmd_def is None:
            raise RuntimeError(
                "Could not create command definition (neither addButtonDefinition nor addCommandDefinition available)."
            )
        on_created = OnCreated()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        attached = _attach_to_ribbon(fui, cmd_def)
        if attached:
            _log("Command attached to: " + ", ".join(attached))
        else:
            _log(
                "Command registered but no ribbon panel was available. "
                "Use Fusion's command search (Shift+S) for 'Batch Render'."
            )
    except Exception:
        tb = traceback.format_exc()
        _log("bootstrap.run() FAILED:\n" + tb)
        try:
            if fui:
                fui.messageBox("Batch Render failed to start:\n{}".format(tb))
        except Exception:
            pass


def stop(context):
    _ = context
    fui = None
    try:
        app = adsk.core.Application.get()
        fui = app.userInterface
    except Exception:
        _log("stop(): Application.get() failed")
        return
    try:
        panel.stop(app, fui)
        _detach_from_ribbon(fui)
        cmd_def = fui.commandDefinitions.itemById(uip.COMMAND_ID)
        _destroy(fui, cmd_def)
        _handlers.clear()
        _log("stop() OK")
    except Exception:
        _log("stop() FAILED:\n" + traceback.format_exc())
        try:
            if fui:
                fui.messageBox("Batch Render stop failed:\n{}".format(traceback.format_exc()))
        except Exception:
            pass
