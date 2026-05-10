# Lifeproof Batch Render — troubleshooting

For documentation navigation (including the **client guide**), see **`docs/README.md`**.

## “Nothing happens” after clicking OK

**Cause A — folder dialog cancelled**  
If **Browse** is on and you cancel the folder picker, the add-in used to exit **silently**. It now shows a message. Pick a valid **texture root** (the folder that **contains** color subfolders, not a single color folder).

**Cause B — missing Python files**  
The add-in folder must contain **all** of these in the **same** directory:

- `LifeproofBatchRender.py`
- `LifeproofBatchRender.manifest`
- `batch_config.py`
- `support_paths.py`
- `texture_pipeline.py`
- `visibility_apply.py`
- `visibility_rules.py`
- `viewport_render.py`
- `plugin/` (package: `__init__.py`, `bootstrap.py`, `controller.py`, `ui.py`, `logger.py`, `renderer_aps.py`, `task_manager.py`, `image_mapper.py`, `model_handler.py`, `visibility_handler.py`)

If any file is missing, Fusion may fail at startup or on run. Re-copy the whole `LifeproofBatchRender` folder from the repo.

## Run toggle stays OFF after install

1. Turn **Run** ON manually for the current session (Fusion does not always auto-enable add-ins).
2. If it immediately turns OFF again, Fusion hit an exception in `run()`. Check **`LifeproofBatchRender/_LifeproofBatchRender_startup.log`** next to the add-in folder for a traceback.
3. The add-in now also registers a **custom toolbar panel** named **Lifeproof Batch** on your **active workspace** (more reliable than only targeting **SolidModifyPanel**). If you still do not see the command, use the Fusion **command search** for **Lifeproof Batch Render**.

**Cause C — script error**  
If Fusion shows **“Command failed:”** with a traceback, copy the full text (or screenshot) and share it — that pinpoints the line (e.g. API not available on your Fusion version).

---

## Add-in does not appear under SOLID → MODIFY

1. **Scripts and Add-Ins** → add-in **ON** (and **Run** if shown).  
2. **Open a design** (`.f3d`) — the command is not on the Home/Samples screen.  
3. Use the **SOLID** workspace (not only Render / Animation).  
4. Restart Fusion after the first install.

---

## “No color subfolders found”

The **texture root** must be the **parent** of the color folders, e.g.:

`...\Lifeproof\`  
`...\Lifeproof\Antler Trail Oak\` ← color folder  
`...\Lifeproof\OtherColor\`

Selecting `Antler Trail Oak` as root is **wrong** — select `Lifeproof`.

---

## “No models were selected”

With **Browse** on, you must pick at least one **`.f3d`** in the file dialog. With **Browse** off, paste full paths (one per line or `|` separated).

---

## Renders fail (`success = 0` in CSV) or blank images

- Try a **smaller** width/height (e.g. 800×600).  
- Ensure the output folder is **writable** (not under protected system dirs without permission).  
- Path length: avoid extremely long paths on Windows.

---

## Textures never change (`Textures 0` in summary)

Appearance/decal **names in the Fusion document** must match **`SLOT*_…_NAMES`** in `texture_pipeline.py`, or change those sets to your names. See **`docs/APPEARANCE_AND_DECAL.md`**.

---

## Viewport only / one shot named “Viewport”

Your Fusion/API may not expose **`design.namedViews`**. Create **Named Views** in the design and update Fusion. Until then, the script saves one capture per color using the current camera.

---

## Text box does not pass model paths

The add-in reads **Text** / **formattedText** / **value** depending on Fusion version. If paths still fail, use **Browse** for models, or use **`|`-separated** paths in the single-line field fallback.
