# Appearance + decal pipelines (both supported)

## How the add-in chooses a pipeline

From the **`.f3d` file name** (same idea as your two files):

| Filename contains | Pipeline |
|-------------------|----------|
| `decal` (case-insensitive) | **Decal** — updates `rootComponent.decals` by name |
| `appearance` / `appearances` | **Appearance** — updates document `design.appearances` by name |
| neither | Defaults to **appearance** (rename decal models to include `decal` if needed) |

Logic lives in `support_paths.infer_texture_mode` and `python_lib/model_kind.py` (keep in sync).

## What you must set in the template

The add-in does **not** guess which body gets which texture. It matches **stable names** in the Fusion browser.

Edit **`fusion_addin/LifeproofBatchRender/texture_pipeline.py`**:

- **`SLOT1_APPEARANCE_NAMES` / `SLOT2_APPEARANCE_NAMES`** — exact names of **document appearances** (often copies you placed under the design’s Appearances folder).
- **`SLOT1_DECAL_NAMES` / `SLOT2_DECAL_NAMES`** — exact names of **decals** on the root component.

Defaults are generic placeholders (`Batch_Slot_1`, `Batch_Decal_1`, …). Rename your assets in Fusion to match, or change the frozensets to match your existing names.

### Appearance mode

- For each matching appearance, the script walks **`appearance.appearanceProperties`** and calls **`AppearanceTexture.changeTextureImage(fullPath)`** on every texture property. That updates raster-driven looks (albedo, etc.) on custom appearances.
- If nothing updates, use **Utilities → ADD-INS → Scripts and Add-Ins → Lifeproof Batch Render** then run **Apply test textures** — the message lists **all appearance and decal names** in the active document so you can copy them into the frozensets.

### Decal mode

- Sets **`Decal.imageFilename`** to the `_1` / `_2` image path.
- If Fusion refuses to update (rare), roll the timeline to the decal feature and **Redefine** once in the UI, or switch that template to **appearance**-based texturing.

## Quick test in Fusion

1. In each `.f3d`, name one appearance or decal per slot to match the frozensets (or edit frozensets to your names).
2. Create **Named Views** in the design (cameras you want per shot).
3. Run **Lifeproof Batch Render** → texture root → models. The add-in applies **every** color subfolder’s `_1` / `_2` images and exports **every named view** per color (see **`docs/LOCAL_BATCH.md`**).
