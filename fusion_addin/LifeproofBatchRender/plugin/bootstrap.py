from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import adsk.core
import adsk.fusion

import batch_config

from plugin import controller
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
            panel = ws.toolbarPanels.itemById(panel_id)
            if not panel:
                continue
            ctrls = panel.controls
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
            panel = ws.toolbarPanels.itemById(panel_id)
            if not panel:
                continue
            ctrl = panel.controls.itemById(uip.COMMAND_ID)
            _destroy(fui, ctrl)
        except Exception:
            pass


def _file_dialog_model_paths(fui: adsk.core.UserInterface) -> List[Path]:
    dlg = fui.createFileDialog()
    dlg.isMultiSelectEnabled = True
    dlg.title = "Select one or more Fusion 360 designs (.f3d)"
    dlg.filter = "Fusion designs (*.f3d)|*.f3d|All files (*.*)|*.*"
    dlg.filterIndex = 0
    if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
        return []
    paths: List[Path] = []
    names = dlg.filenames
    try:
        for i in range(names.count):
            paths.append(Path(names.item(i)))
    except Exception:
        if dlg.filename:
            paths.append(Path(dlg.filename))
    return paths


def _folder_dialog_texture_root(fui: adsk.core.UserInterface) -> Optional[Path]:
    fd = fui.createFolderDialog()
    fd.title = "Select texture root (subfolders = color sets)"
    if fd.showDialog() != adsk.core.DialogResults.DialogOK:
        return None
    folder = fd.folder
    if not folder:
        return None
    return Path(folder)


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

        class OnExecute(adsk.core.CommandEventHandler):
            def __init__(self):
                super().__init__()

            def notify(self, args):
                try:
                    cmd = args.command
                    ins = cmd.commandInputs
                    use_browse = uip.input_bool(ins, uip.INPUT_USE_BROWSE, False)

                    if use_browse:
                        texture_root = _folder_dialog_texture_root(fui)
                        if not texture_root:
                            fui.messageBox(
                                "Texture folder was not selected (dialog cancelled or empty).\n"
                                "Try again, or turn off browse and paste the full folder path."
                            )
                            return
                        models = _file_dialog_model_paths(fui)
                        if not models:
                            fui.messageBox("No models were selected.")
                            return
                        pipeline_sel = uip.read_pipeline(ins)
                        fmt_sel = uip.read_format(ins)
                    else:
                        tr_inp = ins.itemById(uip.INPUT_TEXTURE_ROOT)
                        if tr_inp is None:
                            fui.messageBox("Internal error: missing texture path input.")
                            return
                        texture_raw = (tr_inp.value or "").strip()
                        if not texture_raw:
                            fui.messageBox("Enter the texture root folder path, or enable browse.")
                            return
                        texture_root = Path(texture_raw)
                        if not texture_root.is_dir():
                            fui.messageBox("Texture root is not a valid folder:\n{}".format(texture_root))
                            return
                        mp_inp = ins.itemById(uip.INPUT_MODEL_PATHS)
                        if mp_inp is None:
                            fui.messageBox("Internal error: missing model paths input.")
                            return
                        blob = batch_config.read_textbox_or_string(mp_inp)
                        models = batch_config.parse_model_paths_blob(blob)
                        if not models:
                            fui.messageBox("Enter at least one .f3d path (one per line or | separated).")
                            return
                        for p in models:
                            if not p.exists():
                                fui.messageBox("Model file not found:\n{}".format(p))
                                return
                        pipeline_sel = uip.read_pipeline(ins)
                        fmt_sel = uip.read_format(ins)

                    w_in = ins.itemById(uip.INPUT_IMG_W)
                    h_in = ins.itemById(uip.INPUT_IMG_H)
                    if w_in is None or h_in is None:
                        fui.messageBox("Internal error: missing width/height inputs.")
                        return
                    render_w = batch_config.parse_positive_int(w_in.value, 1920, 64, 8192)
                    render_h = batch_config.parse_positive_int(h_in.value, 1080, 64, 8192)
                    ext = ".png" if (fmt_sel or "").upper().startswith("P") else ".jpg"

                    render_backend = uip.read_render_backend(ins)
                    conc = uip.read_concurrency(ins, 3)
                    aps_fb = uip.read_aps_fallback(ins)
                    max_named_views = uip.read_max_named_views(ins, 0)
                    decal_scale_xy = uip.read_decal_scale_plane_xy(ins, 2.5)

                    controller.execute_batch(
                        fui,
                        ins,
                        texture_root,
                        models,
                        render_w,
                        render_h,
                        ext,
                        pipeline_sel,
                        render_backend,
                        conc,
                        aps_fb,
                        max_named_views,
                        decal_scale_xy,
                        _ADDIN_DIR,
                    )
                except Exception:
                    try:
                        fui.messageBox("Command failed:\n{}".format(traceback.format_exc()))
                    except Exception:
                        pass

        class OnCreated(adsk.core.CommandCreatedEventHandler):
            def __init__(self):
                super().__init__()

            def notify(self, args):
                try:
                    cmd = args.command
                    uip.build_command_inputs(cmd.commandInputs)
                    on_ex = OnExecute()
                    cmd.execute.add(on_ex)
                    _handlers.append(on_ex)
                except Exception:
                    _log("OnCreated failed:\n" + traceback.format_exc())

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
            "Lifeproof Batch Render",
            "Batch textures, named views, visibility, APS/local export.",
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
                "Use Fusion's command search (Shift+S) for 'Lifeproof Batch Render'."
            )
    except Exception:
        tb = traceback.format_exc()
        _log("bootstrap.run() FAILED:\n" + tb)
        try:
            if fui:
                fui.messageBox("Lifeproof Batch Render failed to start:\n{}".format(tb))
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
        _detach_from_ribbon(fui)
        cmd_def = fui.commandDefinitions.itemById(uip.COMMAND_ID)
        _destroy(fui, cmd_def)
        _handlers.clear()
        _log("stop() OK")
    except Exception:
        _log("stop() FAILED:\n" + traceback.format_exc())
        try:
            if fui:
                fui.messageBox("Lifeproof Batch Render stop failed:\n{}".format(traceback.format_exc()))
        except Exception:
            pass
