# Lifeproof Batch Render — Client guide

This guide explains how to install the **Batch Render** add-in for Fusion 360, prepare your files, run a batch, and read the results.

---

## What the add-in does

You give it three things: a **Render Instructions** file (JSON), your **models** (`.f3d`), and a **texture root folder** with one subfolder per color set. For every model in the Render Instructions, the add-in:

1. Opens the model once, as a new, unsaved Fusion document. Your `.f3d` file on disk is never changed.
2. Resizes the designer's decals to the sizes given in the Render Instructions.

Then, for every color folder:

3. Puts the color set's `-1` and `-2` images on those decals.
4. For each render configuration in the Render Instructions: hides the parts listed for it, uses the model's saved **named view** as the camera, and renders the image with Fusion's **local renderer** on this computer.
5. Saves each image into that color folder.

A CSV log and a summary are written into the texture root folder. No cloud rendering is used.

---

## Installing the add-in

1. Copy the whole folder **`fusion_addin/BatchRender`** from this repository, with all its files: `BatchRender.py`, `BatchRender.manifest`, the other `.py` files and the `plugin` folder. Keep the folder name `BatchRender`: Fusion lists the add-in by its folder name.
2. In Fusion: **UTILITIES → ADD-INS → Scripts and Add-Ins** → **+** (Add) → select that folder.
3. Select **BatchRender** in the list and click **Run**. Tick **Run on Startup** if you want it loaded every time Fusion starts.
4. The command is called **Batch Render**. It appears at the bottom of the **SOLID → MODIFY** menu, and also in the **UTILITIES → ADD-INS** menu. It has no icon, only the text entry. If you cannot find it, use Fusion's command search and type `Batch Render`.

Clicking **Batch Render** opens the **Batch Render panel**. The panel stays open while a batch runs.

---

## Before you run

### 1. The models (`.f3d`)

- Each model needs a saved **named view** for every `namedView` used in the Render Instructions.
- The parts that receive the color images must already have **decals placed by the designer**, switched on. The add-in changes the image and size of those decals; it does not create new ones. A decal that is hidden, in a hidden Decals folder, suppressed, or after the timeline marker is left as it is, and only the summary mentions it.
- To use **Model files (empty = all)**, keep the `.f3d` files directly in the **same folder** as the Render Instructions file (not in a subfolder). A listed model whose `.f3d` is not found there is left out **without a warning**; the `Models:` line at the top of the summary shows which models ran.
- Helper geometry is hidden automatically in every render. To hide a helper part, body or decal yourself, put `batch_render_hide` in its name, or `hide:batch` / `hide:render` in its **description**. Bodies with helper names (containing for example `uv plane`, `uv map`, `texture plane`, `decal plane` or `substrate`) are hidden too. Scene lights are not hidden by these rules.

### 2. The Render Instructions file (JSON)

The file can be saved as `.json` or `.txt`, as long as its content is JSON. It is a list with one entry per model. Example (the Bullnose entry, shortened to two render configurations):

```json
[
  {
    "fileName": "Bullnose",
    "decals": [
      { "componentPartNumber": "Stair Nose",
        "textureSize": { "width": 9, "length": 50, "unit": "in" } },
      { "componentPartNumber": "Supplemental Plank",
        "textureSize": { "width": 7, "length": 50, "unit": "in" } }
    ],
    "renderConfigurations": [
      { "namedView": "Full Front", "hideBodies": [] },
      { "namedView": "Full Front", "hideBodies": ["Supplemental Plank"] }
    ]
  }
]
```

| Key | Meaning |
|-----|---------|
| `fileName` | The model's file name **without** `.f3d`. Upper/lower case, spaces, hyphens and underscores do not matter (`Square Nose` matches `square-nose.f3d`). Do not write `.f3d` here, or the model will not be found. |
| `decals` | The parts whose decals get a color image and a size. The **first** entry gets the color set's `-1` image, the **second** gets the `-2` image. A third or later entry reuses `-1` (with a warning). |
| `componentPartNumber` | The part that carries the decal: its component name, its name in the browser, or its Part Number. `L-Cap` matches `L-Cap (1):1`. |
| `textureSize` | `width` and `length` of the decal, as numbers greater than 0. `length` goes on the **long** side of the designer's decal, `width` across it. `unit` is optional: `in` (default), `cm` or `mm`. |
| `renderConfigurations` | One image per entry, per color set. |
| `namedView` | The name of a named view saved in the model (case, spaces and hyphens do not matter). |
| `hideBodies` | Optional list of parts to hide for this image: component names, browser names, Part Numbers or body names. |

Rules to know:

- Key names are **case-sensitive** (`namedView`, not `namedview`). Unknown keys are ignored without a warning, so a typo such as `hideBody` means nothing is hidden.
- Decals on parts that are **not** listed keep the designer's image.
- If a model's entry has **no** `decals`, nothing is resized, and the model's decals are matched to `-1` / `-2` by name (a name ending in 1 or 2), else by order (first decal `-1`, second `-2`).
- Any **error** in the file stops the whole batch before any model opens, even an error in an entry for a model you did not pick. The panel lists up to 12 problems. Entries are counted from 0, so `entry 0` is the first model and `decals[1]` is the second decal. A `line N, column M` in a JSON syntax error is counted from 1, as in a text editor.
- Exception: if **Model files** is empty and the file cannot be read at all (for example a JSON syntax error), the panel says `No model files given, and none could be found automatically` instead. Check the JSON first.
- **Warnings** (for example `length` shorter than `width`) do not stop the batch. They appear only in the summary file.

### 3. The texture root folder

```text
Texture root\
  Color Set 01\
    Color Set 01-1.jpg     <- the -1 image
    Color Set 01-2.jpg     <- the -2 image
  Color Set 02\
    ...
```

- **Every** subfolder of the texture root is treated as a color set, so use a folder that contains only color-set folders. Subfolders inside a color folder are not read.
- An image whose name ends in `-1` or `_1` is the **-1 image**; `-2` or `_2` is the **-2 image**. Keep **exactly one of each** in every folder, named `<folder name>-1.jpg` and `<folder name>-2.jpg` as above. If several images end in `-1` (or `-2`), the last one in alphabetical order is used, and that includes the copies the add-in writes (see below). When you replace a color image, delete the old one and the add-in's copies.
- Use **JPG or PNG**. Other formats (`.tif`, `.bmp`, `.webp`) are accepted but cannot be rotated or fitted to the decal.
- If there is no `-1` image, the first image in alphabetical order is used. If there is no `-2` image, the last image is used. With only one image in the folder, the parts that take `-2` keep their image on the first run, with no warning; on later runs the add-in's own copy counts as a second image. Always provide both images.
- The add-in **writes into** these folders: the rendered images, plus copies of the two images named `<folder name>-1` and `<folder name>-2` (nothing is copied when your images already have those names).
- The add-in **deletes** any image whose name contains `flipped` (but not `unflipped`) from the color folders. Do not keep such files there.
- Files whose names look like earlier renders (two or more ` - ` in the name, or ending in ` (v2)`, ` (v3)` and so on) are never used as color images.

The original images are never changed. When an image has to be rotated or fitted to a decal, the add-in uses a temporary copy in the Windows temp folder and deletes it after the batch.

---

## Running a batch

### The Batch Render panel

| Field | What to enter |
|-------|---------------|
| **Render Instructions Location** | Full path of the Render Instructions file. **...** opens a file dialog (`.json` / `.txt`). |
| **Texture root folder** | The folder that holds the color-set folders. **...** opens a folder dialog. |
| **Model files (empty = all)** | Leave **empty** to render every model listed in the Render Instructions, taken from the same folder as that file. Or click **...** to pick one or more `.f3d` files from one folder; a new pick replaces the list. Typed paths are separated by `|`. |
| **Image width (px)** / **Image height (px)** | Output size; default **2000 × 1130**. Fusion renders each side at least 108 and at most 4000 pixels. |
| **Render Quality** | **Draft (25)**, **Standard (50)**, **Final (75)** (default) or **Excellent (100)**. Higher quality takes longer. |
| **Image format** | **PNG** (default) or **JPG**. |

Click **Render**. The add-in first checks the paths; a problem is shown in red under **Status** and nothing starts.

### While it runs

- The fields and buttons are locked. **Status** shows each step (for example `Rendering 3 of 72: …`) and the green bar shows how many images are done.
- You may close the panel; the batch continues and the panel comes back when it finishes.
- There is **no Cancel button**. The only way to stop a batch early is to close Fusion.
- Do **not** switch document tabs, open or close documents, or edit a model while a batch runs. The add-in works on the active document.
- Do **not** keep the CSV log open in Excel during a batch. Rows that cannot be written are lost without a warning.
- Render time depends on the computer, the image size and the quality. On our test computer, one image at 2000 × 1130 and **Final (75)** took about 1 to 1.5 minutes.

When you close the panel and open it again, the three path fields start empty. Image size, quality and format keep the values of your last **Render** click until Fusion is closed or the add-in is stopped.

### When it finishes

The panel shows `Done. Renders OK: X  Failed: Y`, followed by any skipped models, decal problems and size warnings, and the paths of the summary and log files. The message is shown in **red** if any render failed, any decal could not be updated, or nothing was rendered.

Afterwards:

- Every model stays open as an extra Fusion document with the last color set applied. **Close these documents without saving.** Your `.f3d` files were not changed.
- The batch turns off the display of the ground reflection, origin, planes, axes and layout grid. Turn them back on in Fusion if you need them.

---

## What you get

### Images

Images are saved in each **color folder**, named:

```text
<model file name> - <-1 image name> - <named view>[ - no <hidden parts>].png
```

For example:

```text
Bullnose - Color Set 01-1 - Full Front.png
Bullnose - Color Set 01-1 - Full Front - no Supplemental Plank.png
```

- The model name is the `.f3d` file name; the view is written as in the Render Instructions.
- The ` - no …` part appears only when the configuration hides parts.
- An existing file is never overwritten. A new run adds ` (v2)`, ` (v3)` and so on.
- **Number of images** = render configurations × color folders, for each model. The current Render Instructions file gives **18 images per color folder** for the 8 models.

### Logs (in the texture root folder)

| File | Contents |
|------|----------|
| `_LifeproofBatchRender_log.csv` | One row per image, added to on every run. Columns: `timestamp`, `model`, `color_set`, `view`, `output`, `success`, `notes`. `success` is `1` (saved), `0` (render failed) or `skipped` (whole model skipped; `notes` says why). |
| `_LifeproofBatchRender_summary.txt` | Full details of the **last** run (replaced each time): models, decal sizes, named views found, hidden parts, warnings and totals. |
| `_LifeproofBatchRender_plugin.log` | Short technical log, added to on every run. |

---

## When something goes wrong

Check the **summary file** after every batch that finishes; some problems appear only there. If the panel shows `The batch stopped with an error:`, no new summary was written: send the panel text and `_LifeproofBatchRender_startup.log` (in the add-in folder) with your report.

| You see | Meaning and what to do |
|---------|------------------------|
| A red message before the batch starts | A path is empty or wrong; use the **...** buttons to pick the file or folder. With **Model files** empty, `No model files given, and none could be found automatically` can also mean that no `.f3d` next to the Render Instructions matches a `fileName`, or that the JSON cannot be read at all. |
| `Render Instructions could not be used: …` | The JSON has errors. Fix the listed problems and run again. Entries and `decals[…]` are counted from 0; a `line N, column M` is counted from 1. |
| `=== Model.f3d → SKIPPED ===`, `Nothing to render for this model`, or CSV `skipped` | The model has no entry in the Render Instructions (check `fileName`), or none of its `namedView` names exists in the model. |
| A model has no images and no `SKIPPED` line | With **Model files** empty, only `.f3d` files next to the Render Instructions whose name matches a `fileName` are rendered; the others are left out without a message. Compare the summary's `Models:` line with the Render Instructions. |
| `NO MATCH for namedView '…'` | That view is not saved in the model, so that image is skipped. |
| `NO MATCH for hideBodies '…'` | Nothing matched that name; the image was rendered with the part visible. |
| `kept as authored — "…" is not listed` | That decal's part is not in `decals`, so it keeps the designer's image. A misspelled `componentPartNumber` looks like this too. |
| `SIZE WARNING` / `NOTE: N decal size warning(s)` | The size was not applied (the decal kept the designer's size), or it may not cover the whole part. The image was still rendered; check it. |
| `WARNING: N decal problem(s)` in the panel; `… FAILED …` or `check this render` in the summary | A part did not get the color set correctly. With `image NOT changed`, it kept the designer's texture. The status is shown in red. |
| `Open failed for …` | The model could not be opened. It is listed only in the summary. |
| The add-in does not start | A message box says `Batch Render failed to start` (or `could not open its panel`). Send `_LifeproofBatchRender_startup.log` from the add-in folder with your report. |

**After updating the add-in files**, open **Scripts and Add-Ins**, select **BatchRender**, click **Stop**, then **Run** (or restart Fusion). Re-opening the panel is not enough. The `Code loaded:` line near the top of the summary file shows which build ran; `STALE` there means the old code is still running.

---

## Acceptance checklist

- [ ] The add-in runs without an error message, and **Batch Render** appears under **SOLID → MODIFY**.
- [ ] The panel opens and stays open during a batch; the green progress bar moves.
- [ ] A test batch with two color folders creates one image per render configuration in each color folder.
- [ ] File names follow `<model> - <-1 image> - <named view>[ - no <hidden parts>]`.
- [ ] Decals show the sizes from the Render Instructions, with `-1` and `-2` on the right parts (Bullnose: Stair Nose `-1`, Supplemental Plank `-2`).
- [ ] The parts in `hideBodies` are hidden in those images.
- [ ] Running again adds ` (v2)` files instead of overwriting (with the color images named `<folder name>-1` / `-2`).
- [ ] The CSV log has a row with `success` = `1` for each image, and the summary file is written.
