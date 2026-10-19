# Lifeproof Batch Render (Fusion 360 add-in)

A Fusion 360 Python add-in that batch-renders flooring-trim models for every color set in a texture folder. A **Render Instructions** JSON file says which models to render, the size of each part's decal, which named views to use, and which parts to hide in each image. Images are made with Fusion's local renderer.

**User guide** (install, panel fields, JSON format, outputs, troubleshooting, acceptance checklist): [`docs/CLIENT_GUIDE_Lifeproof_Batch_Render.md`](docs/CLIENT_GUIDE_Lifeproof_Batch_Render.md).

## Repository layout

| Path | Purpose |
|------|---------|
| `fusion_addin/BatchRender/` | The add-in. Install this folder in Fusion; it is listed by its folder name, **BatchRender**. |
| `Resources/Models/` | The client's 8 models (`.f3d`), `Rendering Instructions.json`, and the change request (`.docx` / `.pdf`). |
| `Resources/Models_old/` | Earlier versions of the models, from before the 2026-09 rename. |
| `docs/CLIENT_GUIDE_Lifeproof_Batch_Render.md` | User guide for the client. |
| `python_lib/` | Unit tests. They run outside Fusion. |
| `scripts/run_tests.ps1` | Runs the unit tests. |

## Add-in modules

| File | Role |
|------|------|
| `BatchRender.py`, `BatchRender.manifest` | Entry point and manifest. Their names must match the folder's. Reloads the add-in's modules on every **Run**. |
| `plugin/bootstrap.py` | Registers the **Batch Render** command (SOLID → MODIFY, UTILITIES → ADD-INS). |
| `plugin/panel.py`, `plugin/html/batch_render.html` | The Batch Render palette: fields, status text, progress bar. |
| `plugin/ui.py` | Command and palette IDs, and the panel's default values and choices (size, quality, format). |
| `plugin/panel_settings.py` | Fills the panel with those defaults and checks its settings before a run. |
| `plugin/controller.py` | The batch: models × color sets × render configurations, CSV log and summary. |
| `plugin/model_handler.py` | Opens each `.f3d` in a new document. |
| `plugin/image_mapper.py` | Drops the `_2` image when a model has no second decal. |
| `plugin/logger.py` | Writes `_LifeproofBatchRender_plugin.log` in the texture root. |
| `render_instructions.py` | Loads and validates the Render Instructions JSON. No Fusion API, so it is unit-tested. |
| `texture_pipeline.py` | Swaps the color images onto the designer's decals and sizes them from the JSON. |
| `image_orient.py` | Rotates and fits color images to each decal (temporary copies). |
| `visibility_apply.py` | Hides `hideBodies` parts and helper geometry, and restores visibility after each image. |
| `viewport_render.py` | Fusion local render through the named view's camera. |
| `support_paths.py` | Color-folder scanning and output file names. |
| `batch_config.py` | Parses the panel's model-path and number fields. |

## Install (developers)

1. In Fusion: **UTILITIES → ADD-INS → Scripts and Add-Ins** → **+** → select `fusion_addin/BatchRender` (the folder with the `.py` files and the `.manifest`).
2. Select **BatchRender** and click **Run**, then open **SOLID → MODIFY → Batch Render**.
3. After editing code: **Stop**, then **Run** again. The `Code loaded:` line in `_LifeproofBatchRender_summary.txt` (texture root) shows which build ran. `STALE` there, or a `running build` time older than `newest source on disk`, means the old code is still loaded; the `STALE` check itself only looks at `texture_pipeline.py`.

## Running the tests

```powershell
powershell -File scripts/run_tests.ps1
```

or, from the repository root:

```bash
python -m unittest discover -s python_lib -p "test_*.py"
```

The tests use your own Python 3 installation (Fusion ships its own Python for the add-in). Some of them read the models and JSON in `Resources/Models/`.
