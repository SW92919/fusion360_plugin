# Local batch render (current add-in behavior)

**Client summary:** For a non-technical overview of the dialog and how many images to expect, read **`docs/CLIENT_GUIDE_Lifeproof_Batch_Render.md`**. This file is the deeper technical reference.

## What runs when you click **Lifeproof Batch Render**

1. **Folder dialog** — texture root (subfolders = color sets, e.g. `Lifeproof`).
2. **File dialog** — one or more `.f3d` models.
3. For **each model** (opened in order):
   - Infer **appearance** vs **decal** pipeline from the filename (`docs/APPEARANCE_AND_DECAL.md`).
   - Capture **baseline visibility** (`isLightBulbOn` on all occurrences and bodies under the root assembly).
   - Enumerate **`design.namedViews`** (Fusion API). If none are returned (older builds without this API), the add-in saves **one** image per color set labeled **`Viewport`** using the current camera.
   - **Output count:** each named view produces **one** image file per color folder (e.g. three views ⇒ three PNGs). To cap that, set **Max named views per color set** in the batch UI (e.g. `2` keeps only the **first two** views in Fusion’s named-view order—reorder views in the design if needed).
4. For **each color subfolder** that has a slot‑1 image:
   - Apply **`_1` / `_2`** textures (appearances or decals per pipeline).
   - For **each named view** (after the optional cap):
     - Restore baseline visibility.
     - Apply rules from **`Component.description`** (each occurrence’s component) and **`BRepBody.description`** when the API exposes it — same grammar as `python_lib/visibility_rules.py` (`hide:…`, `show:…`, or a single view name to hide in).
     - Assign the named view’s **camera** to the active viewport and **refresh**.
     - Save **`{model} – {image stem} – {view}.png`** into that color folder, with **`(v2)`** style versioning if the file exists.
     - Restore baseline visibility again.
5. Append a row to **`{texture_root}/_LifeproofBatchRender_log.csv`** for every save attempt.

## Tunables

- **Batch command UI:** resolution, format, **Max named views per color set** (`0` = export every named view; `2` = first two only), pipeline, APS options.
- Legacy **`LifeproofBatchRender.py`** (if used): `RENDER_WIDTH`, `RENDER_HEIGHT`, `OUTPUT_EXT` (`.png` or `.jpg`).

## Requirements / caveats

- **`Design.namedViews`** is required to iterate custom cameras like “Nose Front”. It exists on current Fusion versions; if your build returns no views, use **Viewport** fallback or update Fusion.
- Export uses **`Viewport.saveAsImageFile`** (or `saveAsImage` fallback) — this is a **viewport capture**, not the full **Render** workspace photoreal engine. Swapping to in-canvas or cloud render is a later step (APS).
- Documents are **not** auto-closed or saved after the batch; close tabs manually if you do not want to keep texture swaps.

## Next milestones

- UI for resolution / format / quality.
- Optional **visibility suffix** in filenames when you add combination tasks.
- **APS** backend sharing the same task ordering.
