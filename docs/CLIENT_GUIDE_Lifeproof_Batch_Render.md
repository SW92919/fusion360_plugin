# Lifeproof Batch Render — Client guide

This document describes the **Lifeproof Batch Render** Fusion 360 add-in: what it does, how to use the command dialog, and what to expect in outputs and logs.

---

## What the add-in does

For each **Fusion model** (`.f3d`) you select and each **color folder** under your texture root, the add-in:

1. Opens the model and applies the color-set images (textures via **appearances** and/or **decals**, depending on the model and settings).
2. Saves one **rendered image** per **named camera view** defined in that model (unless you limit views — see below).
3. Writes files into the **same color folder** that supplied the images, and records a **CSV log** under the texture root.

Rendering uses the **local Fusion viewport** unless you enable the optional **APS** (cloud) path and have it configured.

---

## Installing the add-in

1. Use the folder **`LifeproofBatchRender`** from this repository (under `fusion_addin/LifeproofBatchRender/`) so it contains the `.py` files and **`LifeproofBatchRender.manifest`**.
2. In Fusion: **Utilities → ADD-INS → Scripts and Add-Ins** → **+** (Add) → select that folder → enable the add-in.
3. Open the **SOLID** workspace → **MODIFY** panel → **Lifeproof Batch Render**.

The command opens a **palette dialog** on the right (standard Fusion command UI).

**Distribution:** Before wide release, assign a **new unique GUID** to the `id` field in `LifeproofBatchRender.manifest` if this add-in might coexist with others using the same id.

---

## Delivered capabilities

| Capability | Included |
|------------|----------|
| Select **texture root** (subfolders = color / finish variations) | Yes |
| Select **one or more** `.f3d` models | Yes |
| Map **`_1` / `_2`** images to **appearance** or **decal** slots (pipeline from model filename or Force mode) | Yes |
| Process **all** color subfolders under the root | Yes |
| **Named views** as cameras for each export (optional cap in the dialog) | Yes |
| **Visibility** from **description** on components/bodies (`hide:…`, `show:…`, or single view name) | Yes |
| **Local export** per color folder: `{model} – {image stem} – {view}` + extension | Yes |
| Auto **versioning** when a file exists (`(v2)`, `(v3)`, …) | Yes |
| **CSV log** at texture root: `_LifeproofBatchRender_log.csv` | Yes |

---

## Known limitations

- **APS / cloud** — Optional in the UI; a working cloud path requires customer-side configuration (`aps_config.json`, Autodesk workflow, etc.).
- **Photoreal Render workspace** — Exports use **viewport capture**, not the full Fusion Render engine (unless APS is fully implemented for your environment).
- **Templates** — Appearance/decal names in each `.f3d` must match the add-in’s name lists (or you change `texture_pipeline.py`). See **`docs/APPEARANCE_AND_DECAL.md`**.

---

## Prerequisites

1. **Autodesk Fusion 360** (current channel recommended) with **Named Views** available on the design.
2. **Models** aligned with the appearance or decal naming rules (`docs/APPEARANCE_AND_DECAL.md`).
3. **Folder layout** per **`docs/FOLDER_LAYOUT.md`** (texture root → one subfolder per color → `_1` / `_2` rasters).
4. **Named views** in each design for every camera angle you want exported (or use **Max named views** to limit).

---

## How to run a batch

1. In Fusion: **SOLID** workspace → **MODIFY** → **Lifeproof Batch Render**.
2. Set the options below (or use **browse** to pick folder and models).
3. Click **OK** and wait until Fusion finishes. A summary appears when the batch completes.

---

## Command dialog — field reference

Fusion’s side panel may **shorten labels** with “…”. The full meaning of each control is below.

| # | Control | Purpose |
|---|---------|--------|
| 1 | **Pick texture folder and models with file dialogs** | When **on**, after **OK** you choose the **texture root folder** and **`.f3d` files** in Windows dialogs; the typed path fields are **not** used for those choices. When **off**, type the texture root and model paths manually. |
| 2 | **Texture root folder** | Full path to the folder that **contains** your color subfolders (e.g. `Color Set 01`, `Color Set 02`). Each subfolder should include at least one “slot 1” image (see *Folder layout* below). |
| 3 | **Model files (.f3d)** | One full path per line, or multiple paths separated by **`|`** or **`;`**. Used when browse is **off**. |
| 4 | **Image width (px)** / **Image height (px)** | Output resolution for each saved image (for example **1920** × **1080**). Invalid values are clamped to a safe range. |
| 5 | **Max named views per color set** | Limits how many **camera angles** are exported **per color folder** for each model. **`0`** = export **every** named view in the `.f3d`. **`2`** = only the **first two** named views (in Fusion’s order — reorder views in the design if you need specific shots first). This does **not** control how many texture images (`_1` / `_2`) exist; it only caps **views**. |
| 6 | **Image format** | **PNG** or **JPG** for saved renders. |
| 7 | **Texture pipeline** | **Auto** — chooses appearance vs decal workflow from the **`.f3d` file name** (e.g. names containing “Decal” vs “Appearance”). **Force Appearance** / **Force Decal** — overrides Auto for testing or special files. |
| 8 | **Render backend** | **Local (viewport)** — saves what the viewport shows (typical). **APS** — optional cloud rendering; requires separate Autodesk setup (`aps_config.json` and a working workflow). |
| 9 | **APS network concurrency (threads)** | Used **only when APS** is selected: how many parallel network requests to use (often **3**). This is **not** the number of output images per model. |
| 10 | **If APS fails, fall back to local viewport capture** | When on, a failed APS attempt can still produce an image using **local** viewport capture. |
| 11 | **Progress** / **Status** | Shows batch progress and short status text while running. |
| 12 | **OK** / **Cancel** | **OK** starts the batch. **Cancel** closes without running. |

---

## Folder layout (texture root)

Under the **texture root**, each **subfolder** is one **color set**:

- **`color-set-name-1.jpg`** (or `.png`) — **slot 1** (primary / main image). Required for that color to run.
- **`color-set-name-2.jpg`** (optional) — **slot 2** (secondary image, e.g. pattern or label), if your workflow uses two images.

Exact naming rules follow your project’s **Lifeproof** folder convention; the add-in scans subfolders and finds slot images automatically.

---

## How many images are generated?

For each **model** and each **color folder** (that has a valid slot‑1 image):

**Number of images ≈ (number of named views used) × (that color folder).**

- If the `.f3d` defines **three** named views (e.g. Close Up, Full Front, Full Back) and **Max named views** is **0**, you get **three** files per color folder for that model — **this is expected**, not a defect.
- If another model has **two** named views, you get **two** files per color folder.
- If **Max named views** is set to **2**, you never get more than **two** views per color folder, even if the design has more bookmarks (only the **first two** in Fusion’s list are used).

**Important:** Output filenames are built from the **model name**, the **color-set image stem** (usually from slot 1), and the **view name** — not “one file per `_1` and one per `_2`”. Both textures can be applied to the scene before a single view is captured.

---

## Where files appear

| Output | Location |
|--------|----------|
| Rendered images | Inside each **color subfolder** under the texture root (with optional `(v2)` style versioning if a file already exists). |
| CSV log | **`_LifeproofBatchRender_log.csv`** in the **texture root** folder. |
| Text summary | **`_LifeproofBatchRender_summary.txt`** in the **texture root** folder (when generated by your workflow). |

---

## Acceptance checklist (optional sign-off)

Use this when validating the add-in on your hardware:

- [ ] Add-in loads without script errors.
- [ ] Command appears under **SOLID → MODIFY → Lifeproof Batch Render**.
- [ ] With a test texture root and test models, exports are created for **each color × each named view** used (or **Viewport** fallback if the design has no named views).
- [ ] Output filenames follow **`{model} – {image stem} – {view}`** plus extension.
- [ ] Re-running produces **`(v2)`** (or higher) when names collide.
- [ ] Log file records each attempt with timestamp and success flag.
- [ ] Where description rules exist, visibility matches the **Named View** names in those rules (spot-check 2–3 views).

---
