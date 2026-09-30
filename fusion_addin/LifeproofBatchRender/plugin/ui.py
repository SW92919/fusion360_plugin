from __future__ import annotations

import adsk.core

COMMAND_ID = "LifeproofBatchRenderCmd"

INPUT_INSTRUCTIONS = "lbr_renderInstructions"
INPUT_TEXTURE_ROOT = "lbr_textureRoot"
INPUT_MODEL_PATHS = "lbr_modelPaths"

# "Browse..." buttons, each directly under the field it fills. Fusion cannot put a
# button inside a text input, so they sit on their own row.
INPUT_BROWSE_INSTRUCTIONS = "lbr_browseInstructions"
INPUT_BROWSE_TEXTURE_ROOT = "lbr_browseTextureRoot"
INPUT_BROWSE_MODEL_PATHS = "lbr_browseModelPaths"

BROWSE_INPUT_IDS = (
    INPUT_BROWSE_INSTRUCTIONS,
    INPUT_BROWSE_TEXTURE_ROOT,
    INPUT_BROWSE_MODEL_PATHS,
)
INPUT_IMG_W = "lbr_imgW"
INPUT_IMG_H = "lbr_imgH"
INPUT_QUALITY = "lbr_renderQuality"
INPUT_FORMAT = "lbr_format"
INPUT_PIPELINE = "lbr_pipeline"
INPUT_PROGRESS = "lbr_progress"
INPUT_STATUS = "lbr_status"

DEFAULT_IMG_W = "2000"
DEFAULT_IMG_H = "1130"

# Label → Fusion renderQuality (0-100). "Final (75)" is the client's default.
RENDER_QUALITY_CHOICES = (
    ("Draft (25)", 25),
    ("Standard (50)", 50),
    ("Final (75)", 75),
    ("Excellent (100)", 100),
)
DEFAULT_RENDER_QUALITY = 75


def _dropdown_selected_name(ins: adsk.core.CommandInputs, iid: str, default: str) -> str:
    inp = ins.itemById(iid)
    if inp is None:
        return default
    try:
        si = inp.selectedItem
        if si is None:
            return default
        return si.name or default
    except Exception:
        return default


def read_format(ins: adsk.core.CommandInputs) -> str:
    return _dropdown_selected_name(ins, INPUT_FORMAT, "PNG")


def read_pipeline(ins: adsk.core.CommandInputs) -> str:
    return _dropdown_selected_name(ins, INPUT_PIPELINE, "Auto (from render instructions)")


def read_render_quality(ins: adsk.core.CommandInputs, default: int = DEFAULT_RENDER_QUALITY) -> int:
    """Fusion render quality (0-100) from the dropdown label."""
    label = _dropdown_selected_name(ins, INPUT_QUALITY, "")
    for name, value in RENDER_QUALITY_CHOICES:
        if name == label:
            return value
    return default


def read_instructions_path(ins: adsk.core.CommandInputs) -> str:
    """Path to the client's Render Instructions JSON (validated by the controller)."""
    inp = ins.itemById(INPUT_INSTRUCTIONS)
    if inp is None:
        return ""
    try:
        from batch_config import read_textbox_or_string

        raw = read_textbox_or_string(inp).strip()
        if raw:
            return raw.strip('"').strip()
    except Exception:
        pass
    try:
        return str(inp.value or "").strip().strip('"').strip()
    except Exception:
        return ""


def _add_browse_button(inputs: adsk.core.CommandInputs, iid: str, label: str) -> None:
    """Clickable button row. A BoolValueInput with isCheckBox=False renders as one."""
    try:
        inputs.addBoolValueInput(iid, label, False, "", False)
    except Exception:
        pass


def build_command_inputs(inputs: adsk.core.CommandInputs) -> None:
    inputs.addStringValueInput(
        INPUT_INSTRUCTIONS,
        "Render Instructions Location",
        "",
    )
    _add_browse_button(
        inputs, INPUT_BROWSE_INSTRUCTIONS, "   ... browse for instructions file"
    )

    inputs.addStringValueInput(
        INPUT_TEXTURE_ROOT,
        "Texture root folder",
        "",
    )
    _add_browse_button(
        inputs, INPUT_BROWSE_TEXTURE_ROOT, "   ... browse for texture folder"
    )

    try:
        inputs.addTextBoxCommandInput(
            INPUT_MODEL_PATHS,
            "Model files (.f3d) — one path per line, or separate with |",
            "",
            5,
            False,
        )
    except Exception:
        inputs.addStringValueInput(
            INPUT_MODEL_PATHS,
            "Model paths (.f3d), | separated",
            "",
        )
    _add_browse_button(
        inputs, INPUT_BROWSE_MODEL_PATHS, "   ... browse for model files (.f3d)"
    )

    inputs.addStringValueInput(INPUT_IMG_W, "Image width (px)", DEFAULT_IMG_W)
    inputs.addStringValueInput(INPUT_IMG_H, "Image height (px)", DEFAULT_IMG_H)

    quality = inputs.addDropDownCommandInput(
        INPUT_QUALITY,
        "Render Quality",
        adsk.core.DropDownStyles.TextListDropDownStyle,
    )
    for name, value in RENDER_QUALITY_CHOICES:
        quality.listItems.add(name, value == DEFAULT_RENDER_QUALITY)

    fmt = inputs.addDropDownCommandInput(
        INPUT_FORMAT,
        "Image format",
        adsk.core.DropDownStyles.TextListDropDownStyle,
    )
    fmt.listItems.add("PNG", True)
    fmt.listItems.add("JPG", False)

    pipe = inputs.addDropDownCommandInput(
        INPUT_PIPELINE,
        "Texture pipeline",
        adsk.core.DropDownStyles.TextListDropDownStyle,
    )
    # "Auto" now reads the mode from the Render Instructions entry (decals[]
    # present → decal), not from the .f3d filename — the new model files no
    # longer carry a "Decal"/"Appearance" suffix to sniff.
    pipe.listItems.add("Auto (from render instructions)", True)
    pipe.listItems.add("Force Appearance", False)
    pipe.listItems.add("Force Decal", False)

    # ProgressBarCommandInput signature is (id, name, formatString) on most builds —
    # it does not accept min/max args. Use a try/except so older/newer builds both work.
    try:
        inputs.addProgressBarCommandInput(INPUT_PROGRESS, "Progress", "%v / %m")
    except Exception:
        pass

    try:
        inputs.addTextBoxCommandInput(INPUT_STATUS, "Status", "", 3, True)
    except Exception:
        try:
            inputs.addStringValueInput(INPUT_STATUS, "Status", "")
        except Exception:
            pass


def set_text_input(ins: adsk.core.CommandInputs, iid: str, text: str) -> bool:
    """Write into a string or textbox input, whichever this build created."""
    inp = ins.itemById(iid)
    if inp is None:
        return False
    for attr in ("text", "formattedText", "value"):
        if hasattr(inp, attr):
            try:
                setattr(inp, attr, text)
                return True
            except Exception:
                continue
    return False


def reset_browse_button(ins: adsk.core.CommandInputs, iid: str) -> None:
    """Clear a button's pressed state so it can be clicked again."""
    inp = ins.itemById(iid)
    if inp is None:
        return
    try:
        inp.value = False
    except Exception:
        pass


def set_status(ins: adsk.core.CommandInputs, text: str) -> None:
    inp = ins.itemById(INPUT_STATUS)
    if inp is None:
        return
    for attr in ("text", "formattedText", "value"):
        if hasattr(inp, attr):
            try:
                setattr(inp, attr, text)
                return
            except Exception:
                pass


def set_progress(ins: adsk.core.CommandInputs, value: int, maximum: int = 100) -> None:
    inp = ins.itemById(INPUT_PROGRESS)
    if inp is None:
        return
    mx = max(1, int(maximum))
    v = max(0, min(mx, int(value)))
    for fn in (
        lambda: inp.setProgress(v, mx),
        lambda: setattr(inp, "progressValue", v),
    ):
        try:
            fn()
            return
        except Exception:
            pass
