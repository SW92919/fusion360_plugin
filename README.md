# Lifeproof / Fusion 360 batch render pipeline

This repository is a **project scaffold** for a Fusion 360 add-in that batch-renders stair tread/nose models across **color folders** (texture pairs), **named views**, and optional **per-view visibility** rules.

**Final client documentation** (install, UI, scope, acceptance checklist): [`docs/CLIENT_GUIDE_Lifeproof_Batch_Render.md`](docs/CLIENT_GUIDE_Lifeproof_Batch_Render.md) · **All docs index:** [`docs/README.md`](docs/README.md).

**Add-in not working in Fusion?** See [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

It is based on your screenshots and requirements (no `.f3d` or texture binaries are committed here).

## What the screenshots imply

| Item | Meaning |
|------|--------|
| Root folder `Lifeproof` | Contains one subfolder per **color set** (e.g. `Antler Trail Oak`). |
| Files like `antler-trail-oak-1.jpg`, `antler-trail-oak-2.jpg` | Map to **appearance slot 1** and **slot 2** (`_1` / `_2` suffix convention). |
| Large swatch image | Optional reference; pipeline can ignore or use for QA. |
| `Treads Plus Bullnose - Appearance.f3d` | Model that expects **appearances** for wood grain. |
| `Treads Plus Square Nose - Decal.f3d` | Model that expects **decals** for the same look. |
| Existing PNGs (`Antler Trail Oak - Nose Front`, etc.) | Target **look**: same naming spirit as outputs; your locked rule is below. |

## Output naming (locked)

```text
{model name} – {image name without extension} – {named view}
```

Examples (model = Fusion file name without `.f3d`, image = texture stem you map, view = Fusion named view):

```text
Treads Plus Bullnose - Appearance – antler-trail-oak-1 – Nose Front.png
```

If you add the earlier **visibility** dimension, extend to:

```text
{model} – {image} – {view} – {visibility label}
```

Auto-versioning (`v2`, `v3`) when a file already exists should be applied in the **same folder** as the color set inputs (per your spec).

## Repository layout

| Path | Purpose |
|------|--------|
| `docs/README.md` | **Index** of all documentation (client review order + support). |
| `docs/CLIENT_GUIDE_Lifeproof_Batch_Render.md` | **Client guide:** dialog fields, outputs, folder layout. |
| `docs/FOLDER_LAYOUT.md` | How to arrange folders and textures. |
| `fusion_addin/LifeproofBatchRender/` | Fusion Python add-in (install this folder into Fusion **Scripts and Add-Ins**). |
| `docs/LOCAL_BATCH.md` | Detailed batch behavior (colors × views, logging, limits). |
| `python_lib/` | Pure Python: scan folders, build filenames, parse visibility tokens (unit-testable without Fusion). |

## Appearance + decal (both)

The add-in includes **`texture_pipeline.py`**: it can drive **document appearances** (texture swap via `AppearanceTexture.changeTextureImage`) or **root decals** (`Decal.imageFilename`), chosen from the model filename (`…Appearance…` vs `…Decal…`). Rename targets in Fusion to match the `SLOT*_…_NAMES` frozensets, or edit those sets — see **`docs/APPEARANCE_AND_DECAL.md`**.

## Local batch (all colors × views)

The **Lifeproof Batch Render** command runs a **local viewport batch**: every color subfolder × every **named view** in the `.f3d` (each view is one output file—**not** one file per `_1` / `_2` texture). Use **Max named views per color set** in the dialog to cap how many views export (e.g. `2` when the design defines three bookmarks but you only want the first two). **Visibility** comes from **description** fields; PNG/JPG export, versioning, and **`_LifeproofBatchRender_log.csv`** — see **`docs/LOCAL_BATCH.md`**.

## Fusion API work (remaining)

- **Photoreal** Fusion Render workspace or **APS** cloud rendering instead of viewport capture.
- Richer **visibility combination** matrix if you need explicit Cartesian product of body states.

## Quick install (developers)

1. Clone or copy this repo.
2. In Fusion: **Utilities → ADD-INS → Scripts and Add-Ins** → **green plus** → select `fusion_addin/LifeproofBatchRender` (folder containing `.py` and `.manifest`).
3. Run the add-in once; use **SOLID → MODIFY → Lifeproof Batch Render**. A **command dialog** opens for paths, resolution, format, pipeline, and more (see **`docs/CLIENT_GUIDE_Lifeproof_Batch_Render.md`**).

**Launch Fusion** on your PC to use the add-in. From this repo you can only run automated checks:

`powershell -File scripts/run_tests.ps1`

## Running unit tests (naming / folders only)

```bash
cd lifeproof-fusion-batch-render
python -m unittest discover -s python_lib -p "test_*.py"
```

Requires Python 3.10+ on your machine for tests; Fusion ships its own embedded Python for the add-in runtime.
