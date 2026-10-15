from __future__ import annotations

import struct
import tempfile
import zlib
from pathlib import Path
from typing import List, Optional, Tuple

import adsk.core

COMMAND_ID = "LifeproofBatchRenderCmd"

INPUT_INSTRUCTIONS = "lbr_renderInstructions"
INPUT_TEXTURE_ROOT = "lbr_textureRoot"
INPUT_MODEL_PATHS = "lbr_modelPaths"

# "..." buttons, each beside the field it fills.
INPUT_BROWSE_INSTRUCTIONS = "lbr_browseInstructions"
INPUT_BROWSE_TEXTURE_ROOT = "lbr_browseTextureRoot"
INPUT_BROWSE_MODEL_PATHS = "lbr_browseModelPaths"

BROWSE_INPUT_IDS = (
    INPUT_BROWSE_INSTRUCTIONS,
    INPUT_BROWSE_TEXTURE_ROOT,
    INPUT_BROWSE_MODEL_PATHS,
)

# Fusion gives every input a row of its own and has no "field + browse button"
# input, so the settings sit in one table: label | input | "..." button. That is
# how Autodesk's own add-ins lay out labelled rows (a read-only text box as the
# label cell), and one shared table keeps every label and box aligned.
INPUT_SETTINGS_TABLE = "lbr_settings"
SETTINGS_COLUMN_RATIO = "6:9:1"  # label : input : "..." button
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


def _png_bytes(size: int, pixel) -> bytes:
    """An RGBA PNG of ``size`` x ``size``; ``pixel(x, y)`` gives each (r, g, b, a)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = b"".join(
        b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(size)) for y in range(size)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def _browse_button_pixel(size: int):
    """A small push button with "..." on it: light face, grey frame, dark dots."""
    k = size / 16.0
    dot = max(2, int(round(2 * k)))
    dots_x = [int(round(x * k)) for x in (3.5, 7, 10.5)]
    dot_y = int(round(7 * k))
    corner = max(1, int(round(k)))

    def pixel(x: int, y: int) -> Tuple[int, int, int, int]:
        top, bottom = int(round(1 * k)), size - 1 - int(round(1 * k))
        if y < top or y > bottom:
            return (0, 0, 0, 0)
        dx = min(x, size - 1 - x)
        dy = min(y - top, bottom - y)
        if dx < corner and dy < corner and dx + dy < corner:
            return (0, 0, 0, 0)  # rounded corner
        if dx == 0 or dy == 0:
            return (138, 138, 138, 255)  # frame
        if any(dx0 <= x < dx0 + dot for dx0 in dots_x) and dot_y <= y < dot_y + dot:
            return (45, 45, 45, 255)  # "..."
        return (236, 236, 236, 255) if y < (top + bottom) / 2 else (224, 224, 224, 255)

    return pixel


def browse_button_icon_folder() -> str:
    """Folder holding the "..." button picture, drawn on first use; "" if it can't be.

    Inside a table Fusion draws a text-only button flat, without a frame, so the
    "..." gets a picture of a button. It is drawn here rather than shipped as a
    file (the repository ignores *.png).
    """
    folder = Path(tempfile.gettempdir()) / "LifeproofBatchRender" / "browse_button_v1"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for size in (16, 32):
            data = _png_bytes(size, _browse_button_pixel(size))
            for name in ("{0}x{0}.png".format(size), "{0}x{0}-dark.png".format(size)):
                target = folder / name
                if not target.is_file() or target.read_bytes() != data:
                    target.write_bytes(data)
        return str(folder)
    except Exception:
        return ""


def _add_browse_button(
    inputs: adsk.core.CommandInputs, iid: str, in_table: bool, tooltip: str
) -> Optional[adsk.core.CommandInput]:
    """Clickable "..." button (a BoolValueInput with isCheckBox=False)."""
    icon = browse_button_icon_folder() if in_table else ""
    name = "..." if in_table else "   ... browse"
    try:
        button = inputs.addBoolValueInput(iid, name, False, icon, False)
    except Exception:
        if not icon:
            return None
        icon = ""
        try:
            button = inputs.addBoolValueInput(iid, name, False, "", False)
        except Exception:
            return None
    settings = [("tooltip", tooltip)]
    if not icon:
        settings.append(("text", name))
    for attr, value in settings:
        try:
            setattr(button, attr, value)
        except Exception:
            pass
    return button


def _add_settings_table(inputs: adsk.core.CommandInputs, rows: int) -> Optional[adsk.core.CommandInput]:
    try:
        table = inputs.addTableCommandInput(INPUT_SETTINGS_TABLE, "Settings", 3, SETTINGS_COLUMN_RATIO)
    except Exception:
        return None
    for attr, get_value in (
        (
            "tablePresentationStyle",
            lambda: adsk.core.TablePresentationStyles.transparentBackgroundTablePresentationStyle,
        ),
        ("hasGrid", lambda: False),
        ("minimumVisibleRows", lambda: rows),
        ("maximumVisibleRows", lambda: rows),
        ("columnSpacing", lambda: 4),
        ("rowSpacing", lambda: 4),
        ("isFullWidth", lambda: True),
    ):
        try:
            setattr(table, attr, get_value())
        except Exception:
            pass
    return table


def build_command_inputs(inputs: adsk.core.CommandInputs) -> None:
    # (label, tooltip, input id, browse button id or None, browse tooltip)
    path_rows = (
        (
            "Render Instructions Location",
            "Full path of the Render Instructions JSON file.",
            INPUT_INSTRUCTIONS,
            INPUT_BROWSE_INSTRUCTIONS,
            "Browse for the Render Instructions file",
        ),
        (
            "Texture root folder",
            "Folder that holds the colour-set folders.",
            INPUT_TEXTURE_ROOT,
            INPUT_BROWSE_TEXTURE_ROOT,
            "Browse for the texture folder",
        ),
        (
            # A table cell clips a long label; the full explanation is the tooltip.
            "Model files (empty = all)",
            "Model files (.f3d). Leave EMPTY to render every model listed in the Render "
            "Instructions file, or pick "
            "one or more .f3d files with the ... button (several paths are separated by |).",
            INPUT_MODEL_PATHS,
            INPUT_BROWSE_MODEL_PATHS,
            "Browse for one or more model files (.f3d)",
        ),
    )
    table = _add_settings_table(inputs, len(path_rows) + 5)
    placed: List[Tuple[str, str, adsk.core.CommandInput, Optional[adsk.core.CommandInput]]] = []

    for label, tooltip, field_id, button_id, button_tooltip in path_rows:
        field = inputs.addStringValueInput(field_id, label, "")
        try:
            field.tooltip = tooltip
        except Exception:
            pass
        button = _add_browse_button(inputs, button_id, table is not None, button_tooltip)
        placed.append((label, tooltip, field, button))

    width = inputs.addStringValueInput(INPUT_IMG_W, "Image width (px)", DEFAULT_IMG_W)
    height = inputs.addStringValueInput(INPUT_IMG_H, "Image height (px)", DEFAULT_IMG_H)

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

    for inp in (width, height, quality, fmt, pipe):
        placed.append((inp.name, "", inp, None))

    if table is None:
        return  # every input keeps a row of its own, buttons under their fields
    for row, (label, tooltip, inp, button) in enumerate(placed):
        try:
            cell = inputs.addTextBoxCommandInput("lbr_label{}".format(row), label, label, 1, True)
            if tooltip:
                cell.tooltip = tooltip
            table.addCommandInput(cell, row, 0)
            table.addCommandInput(inp, row, 1)
            if button is not None:
                table.addCommandInput(button, row, 2)
        except Exception:
            continue  # the input stays on a row of its own

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
