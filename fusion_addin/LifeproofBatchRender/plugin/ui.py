from __future__ import annotations

import adsk.core

COMMAND_ID = "LifeproofBatchRenderCmd"

# Dropdown labels — keep in sync with ``controller._save_local_image`` routing.
RENDER_BACKEND_LOCAL_FUSION = "Local (Fusion render)"
RENDER_BACKEND_LOCAL_VIEWPORT = "Local (viewport)"

INPUT_USE_BROWSE = "lbr_useBrowse"
INPUT_TEXTURE_ROOT = "lbr_textureRoot"
INPUT_MODEL_PATHS = "lbr_modelPaths"
INPUT_IMG_W = "lbr_imgW"
INPUT_IMG_H = "lbr_imgH"
INPUT_FORMAT = "lbr_format"
INPUT_PIPELINE = "lbr_pipeline"
INPUT_RENDER_BACKEND = "lbr_renderBackend"
INPUT_CONCURRENCY = "lbr_concurrency"
INPUT_APS_FALLBACK = "lbr_apsFallbackLocal"
INPUT_MAX_NAMED_VIEWS = "lbr_maxNamedViews"
INPUT_DECAL_SCALE_XY = "lbr_decalScaleXY"
INPUT_PROGRESS = "lbr_progress"
INPUT_STATUS = "lbr_status"


def input_bool(ins: adsk.core.CommandInputs, iid: str, default: bool = False) -> bool:
    inp = ins.itemById(iid)
    if inp is None:
        return default
    try:
        return bool(inp.value)
    except Exception:
        try:
            return bool(inp.boolValue)
        except Exception:
            return default


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


def read_render_backend(ins: adsk.core.CommandInputs) -> str:
    return _dropdown_selected_name(ins, INPUT_RENDER_BACKEND, RENDER_BACKEND_LOCAL_FUSION)


def read_format(ins: adsk.core.CommandInputs) -> str:
    return _dropdown_selected_name(ins, INPUT_FORMAT, "PNG")


def read_pipeline(ins: adsk.core.CommandInputs) -> str:
    return _dropdown_selected_name(ins, INPUT_PIPELINE, "Auto (from .f3d filename)")


def read_concurrency(ins: adsk.core.CommandInputs, default: int = 3) -> int:
    inp = ins.itemById(INPUT_CONCURRENCY)
    if inp is None:
        return default
    try:
        from batch_config import parse_bounded_int

        return parse_bounded_int(inp.value, default, 1, 16)
    except Exception:
        try:
            return max(1, min(16, int(str(inp.value).strip())))
        except Exception:
            return default


def read_aps_fallback(ins: adsk.core.CommandInputs) -> bool:
    inp = ins.itemById(INPUT_APS_FALLBACK)
    if inp is None:
        return True
    try:
        return bool(inp.value)
    except Exception:
        try:
            return bool(inp.boolValue)
        except Exception:
            return True


def read_max_named_views(ins: adsk.core.CommandInputs, default: int = 0) -> int:
    """0 = render every named view; N > 0 = only first N views (design browser order)."""
    inp = ins.itemById(INPUT_MAX_NAMED_VIEWS)
    if inp is None:
        return default
    try:
        from batch_config import parse_positive_int, read_textbox_or_string

        raw = read_textbox_or_string(inp).strip()
        if not raw:
            return default
        return parse_positive_int(raw, default, 0, 50)
    except Exception:
        return default


def read_decal_scale_plane_xy(ins: adsk.core.CommandInputs, default: float = 1.0) -> float:
    """Fusion Decal Scale Plane XY multiplier (after auto-fit)."""
    inp = ins.itemById(INPUT_DECAL_SCALE_XY)
    if inp is None:
        return default
    try:
        from batch_config import parse_bounded_float, read_textbox_or_string

        raw = read_textbox_or_string(inp).strip()
        if not raw:
            try:
                raw = str(inp.value or "").strip()
            except Exception:
                raw = ""
        if not raw:
            return default
        return parse_bounded_float(raw, default, 0.1, 20.0)
    except Exception:
        return default


def build_command_inputs(inputs: adsk.core.CommandInputs) -> None:
    inputs.addBoolValueInput(
        INPUT_USE_BROWSE,
        "Pick texture folder and models with file dialogs (overrides path fields below)",
        True,
        "",
        False,
    )

    inputs.addStringValueInput(
        INPUT_TEXTURE_ROOT,
        "Texture root folder (full path, when browse is off)",
        "",
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

    inputs.addStringValueInput(INPUT_IMG_W, "Image width (px)", "1920")
    inputs.addStringValueInput(INPUT_IMG_H, "Image height (px)", "1080")

    inputs.addStringValueInput(
        INPUT_MAX_NAMED_VIEWS,
        "Max named views per color set (0 = all; e.g. 2 = first two views only)",
        "0",
    )

    inputs.addStringValueInput(
        INPUT_DECAL_SCALE_XY,
        "Decal Scale Plane XY (multiplier after auto-fit; 1.0 = auto-fit only)",
        "1.0",
    )

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
    pipe.listItems.add("Auto (from .f3d filename)", True)
    pipe.listItems.add("Force Appearance", False)
    pipe.listItems.add("Force Decal", False)

    rb = inputs.addDropDownCommandInput(
        INPUT_RENDER_BACKEND,
        "Render backend",
        adsk.core.DropDownStyles.TextListDropDownStyle,
    )
    rb.listItems.add(RENDER_BACKEND_LOCAL_FUSION, True)
    rb.listItems.add(RENDER_BACKEND_LOCAL_VIEWPORT, False)
    rb.listItems.add("APS (requires aps_config.json + workflow)", False)

    inputs.addStringValueInput(INPUT_CONCURRENCY, "APS network concurrency (threads)", "3")

    inputs.addBoolValueInput(
        INPUT_APS_FALLBACK,
        "If APS fails, fall back to local Fusion render (then viewport if needed)",
        True,
        "",
        True,
    )

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
