# Settle BATCH_DECAL_MAGNITUDE_CM_PER_UNIT ("k") in about a minute.
#
# WHY THIS EXISTS
#   Absolute decal sizing writes a physical size onto DecalInput.transform by setting
#   the X/Y axis vector magnitudes. One thing is not provable from the add-in source:
#   whether a magnitude of M means the decal spans M cm (k = 1.0) or 2*M cm (k = 2.0,
#   i.e. M is a half-extent). Both readings fit every number recorded in
#   texture_pipeline.py, and Fusion's own API uses magnitude-as-radius for ellipse
#   axes while calling the decal transform origin the decal's CENTRE.
#
#   Guessing wrong makes every decal exactly half or double the requested size — which
#   still renders, still looks like a texture, and gets blamed on the image.
#
# WHAT IT DOES
#   Creates a DecalInput on a large flat face with chain-faces OFF (so wrapping cannot
#   confound the reading), prints the DEFAULT axis magnitudes Fusion chose, and
#   converts them under both conventions. Fusion's default decal is a ~5 cm patch —
#   a fact this add-in already relies on in five places — so whichever conversion
#   lands near 5 cm is the right one.
#
# HOW TO RUN
#   1. Open any of the product models (Bullnose.f3d is fine).
#   2. Utilities -> ADD-INS -> Scripts and Add-Ins -> green + -> pick this folder -> Run.
#   3. Read the dialog. It states k outright.
#
#   Nothing is created permanently: the DecalInput is never added, so the document is
#   untouched. If the result says k = 2.0, change BATCH_DECAL_MAGNITUDE_CM_PER_UNIT in
#   texture_pipeline.py to 2.0 — that constant is the only place the convention lives.

import traceback
from pathlib import Path

import adsk.core
import adsk.fusion

# Fusion's documented default decal footprint, in cm.
FUSION_DEFAULT_PATCH_CM = 5.0


def _largest_planar_face(root):
    """Biggest flat face in the design — the cleanest possible measurement surface."""
    best = None
    best_area = 0.0
    bodies = []
    try:
        for i in range(root.bRepBodies.count):
            bodies.append(root.bRepBodies.item(i))
    except Exception:
        pass
    try:
        occs = root.allOccurrences
        for i in range(occs.count):
            comp = occs.item(i).component
            for j in range(comp.bRepBodies.count):
                bodies.append(comp.bRepBodies.item(j))
    except Exception:
        pass

    for body in bodies:
        try:
            if not body.isVisible:
                continue
            faces = body.faces
        except Exception:
            continue
        for k in range(faces.count):
            try:
                face = faces.item(k)
                if face.geometry.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType:
                    continue
                area = face.area
                if area > best_area:
                    best_area, best = area, face
            except Exception:
                continue
    return best, best_area


def _any_image(root_dir):
    for pattern in ("*.png", "*.jpg", "*.jpeg"):
        for p in Path(root_dir).rglob(pattern):
            try:
                if p.is_file() and p.stat().st_size > 0:
                    return p
            except Exception:
                continue
    return None


def run(context):
    _ = context
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            ui.messageBox("Open a Fusion design first.")
            return
        root = design.rootComponent

        face, area = _largest_planar_face(root)
        if face is None:
            ui.messageBox(
                "No planar face found. Open a model with at least one flat face "
                "(any of the trim products will do)."
            )
            return

        image = _any_image(Path(__file__).resolve().parent)
        if image is None:
            dlg = ui.createFileDialog()
            dlg.title = "Pick any PNG/JPG to probe with (it is never applied)"
            dlg.filter = "Images (*.png;*.jpg;*.jpeg)|*.png;*.jpg;*.jpeg"
            if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
                return
            image = Path(dlg.filename)

        comp = face.body.parentComponent
        point = face.pointOnFace
        decal_input = comp.decals.createInput(str(image), [face], point)
        if decal_input is None:
            ui.messageBox("decals.createInput returned None — cannot probe.")
            return

        # Chain faces OFF: a wrapped decal's developed size differs from its
        # projected size, which is exactly the confound that made this ambiguous.
        for attr in ("isChainFaces", "chainFaces"):
            try:
                setattr(decal_input, attr, False)
                break
            except Exception:
                continue

        trf = decal_input.transform
        if trf is None:
            ui.messageBox("DecalInput.transform is not available on this build.")
            return
        origin, x_axis, y_axis, _z = trf.getAsCoordinateSystem()
        mx = (x_axis.x ** 2 + x_axis.y ** 2 + x_axis.z ** 2) ** 0.5
        my = (y_axis.x ** 2 + y_axis.y ** 2 + y_axis.z ** 2) ** 0.5

        # Which conversion lands nearer Fusion's ~5 cm default patch?
        err_k1 = abs(mx - FUSION_DEFAULT_PATCH_CM)
        err_k2 = abs(mx * 2.0 - FUSION_DEFAULT_PATCH_CM)
        verdict = "1.0" if err_k1 <= err_k2 else "2.0"

        lines = [
            "DECAL MAGNITUDE PROBE",
            "",
            "Face area           : {:.2f} cm^2".format(area),
            "Chain faces         : OFF (no wrap confound)",
            "",
            "Default magnitudes  : |X| = {:.4f}   |Y| = {:.4f}".format(mx, my),
            "",
            "Reading as FULL extent (k = 1.0): {:.2f} x {:.2f} cm".format(mx, my),
            "Reading as HALF extent (k = 2.0): {:.2f} x {:.2f} cm".format(
                mx * 2.0, my * 2.0
            ),
            "",
            "Fusion's default patch is ~{:.0f} cm.".format(FUSION_DEFAULT_PATCH_CM),
            "  distance from ~5 cm, k=1.0 : {:.2f}".format(err_k1),
            "  distance from ~5 cm, k=2.0 : {:.2f}".format(err_k2),
            "",
            ">>> SET BATCH_DECAL_MAGNITUDE_CM_PER_UNIT = {} <<<".format(verdict),
            "",
            "(in fusion_addin/LifeproofBatchRender/texture_pipeline.py — it is the",
            " only place this convention is used.)",
            "",
            "If both distances are close, or the magnitudes look nothing like ~5 cm,",
            "do NOT guess: place one decal by hand at a known size and measure it.",
        ]
        text = "\n".join(lines)
        try:
            out = Path(__file__).resolve().parent / "decal_magnitude_probe.txt"
            out.write_text(text, encoding="utf-8")
            text += "\n\nSaved to: {}".format(out)
        except Exception:
            pass
        ui.messageBox(text, "Decal Magnitude Probe")

    except Exception:
        try:
            ui.messageBox("Probe failed:\n{}".format(traceback.format_exc()))
        except Exception:
            pass


def stop(context):
    _ = context
