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


def _dialog_instructions_file(fui: adsk.core.UserInterface) -> Optional[Path]:
    dlg = fui.createFileDialog()
    dlg.title = "Select the Render Instructions file"
    dlg.filter = "Render instructions (*.json;*.txt)|*.json;*.txt|All files (*.*)|*.*"
    dlg.filterIndex = 0
    if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
        return None
    return Path(dlg.filename) if dlg.filename else None


def _dialog_texture_root(fui: adsk.core.UserInterface) -> Optional[Path]:
    fd = fui.createFolderDialog()
    fd.title = "Select texture root (subfolders = color sets)"
    if fd.showDialog() != adsk.core.DialogResults.DialogOK:
        return None
    return Path(fd.folder) if fd.folder else None


def _dialog_model_paths(fui: adsk.core.UserInterface) -> List[Path]:
    dlg = fui.createFileDialog()
    dlg.isMultiSelectEnabled = True
    dlg.title = "Select one or more Fusion designs (.f3d)"
    dlg.filter = "Fusion designs (*.f3d)|*.f3d|All files (*.*)|*.*"
    dlg.filterIndex = 0
    if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
        return []
    paths: List[Path] = []
    try:
        names = dlg.filenames
        for i in range(names.count):
            paths.append(Path(names.item(i)))
    except Exception:
        if dlg.filename:
            paths.append(Path(dlg.filename))
    return paths


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

                    instructions_raw = uip.read_instructions_path(ins)
                    if not instructions_raw:
                        fui.messageBox(
                            "Enter the Render Instructions Location "
                            "(full path to the render instructions JSON file)."
                        )
                        return
                    instructions_path = Path(instructions_raw)
                    if not instructions_path.is_file():
                        # A relative path resolves against Fusion's working directory,
                        # not the repo, so show where it actually looked.
                        try:
                            resolved = instructions_path.resolve()
                        except Exception:
                            resolved = instructions_path
                        hint = ""
                        if not instructions_path.is_absolute():
                            hint = (
                                "\n\nThat is a RELATIVE path. Fusion resolves it against its "
                                "own working folder, not your project. Use the browse button, "
                                "or paste the full path starting with a drive letter."
                            )
                        fui.messageBox(
                            "Render Instructions file not found.\n\n"
                            "Entered : {}\nLooked in: {}{}".format(
                                instructions_raw, resolved, hint
                            )
                        )
                        return

                    tr_inp = ins.itemById(uip.INPUT_TEXTURE_ROOT)
                    if tr_inp is None:
                        fui.messageBox("Internal error: missing texture path input.")
                        return
                    texture_raw = (tr_inp.value or "").strip()
                    if not texture_raw:
                        fui.messageBox("Enter the texture root folder path.")
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
                    render_w = batch_config.parse_positive_int(
                        w_in.value, int(uip.DEFAULT_IMG_W), 64, 8192
                    )
                    render_h = batch_config.parse_positive_int(
                        h_in.value, int(uip.DEFAULT_IMG_H), 64, 8192
                    )
                    ext = ".png" if (fmt_sel or "").upper().startswith("P") else ".jpg"
                    render_quality = uip.read_render_quality(ins)

                    controller.execute_batch(
                        fui,
                        ins,
                        texture_root,
                        models,
                        render_w,
                        render_h,
                        ext,
                        pipeline_sel,
                        instructions_path,
                        render_quality,
                    )
                except Exception:
                    try:
                        fui.messageBox("Command failed:\n{}".format(traceback.format_exc()))
                    except Exception:
                        pass

        class OnInputChanged(adsk.core.InputChangedEventHandler):
            """Fills a path field from a file/folder dialog when its button is clicked."""

            def __init__(self):
                super().__init__()

            def notify(self, args):
                try:
                    changed = args.input
                    ins = args.inputs
                    iid = changed.id
                    if iid not in uip.BROWSE_INPUT_IDS:
                        return
                    # A button input latches on; clear it so it can be used again.
                    uip.reset_browse_button(ins, iid)

                    if iid == uip.INPUT_BROWSE_INSTRUCTIONS:
                        picked = _dialog_instructions_file(fui)
                        if picked:
                            uip.set_text_input(ins, uip.INPUT_INSTRUCTIONS, str(picked))
                    elif iid == uip.INPUT_BROWSE_TEXTURE_ROOT:
                        picked = _dialog_texture_root(fui)
                        if picked:
                            uip.set_text_input(ins, uip.INPUT_TEXTURE_ROOT, str(picked))
                    elif iid == uip.INPUT_BROWSE_MODEL_PATHS:
                        picked = _dialog_model_paths(fui)
                        if picked:
                            uip.set_text_input(
                                ins,
                                uip.INPUT_MODEL_PATHS,
                                "\n".join(str(p) for p in picked),
                            )
                except Exception:
                    _log("OnInputChanged failed:\n" + traceback.format_exc())

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
                    on_changed = OnInputChanged()
                    cmd.inputChanged.add(on_changed)
                    _handlers.append(on_changed)
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
