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


def _collect_bodies(root, notes):
    """Every solid body, as OCCURRENCE PROXIES where possible.

    ``occurrence.bRepBodies`` yields proxies that carry their placement in the
    assembly; ``occurrence.component.bRepBodies`` yields definition bodies with no
    occurrence context, and some properties (``isVisible`` among them) can raise on
    those. Proxies first, definitions only as a fallback.

    Every failure is appended to ``notes`` — swallowing them is what made the first
    two runs of this probe report "nothing found" with no way to tell why.
    """
    bodies = []
    try:
        n_root = root.bRepBodies.count
        notes.append("root.bRepBodies.count = {}".format(n_root))
        for i in range(n_root):
            bodies.append(root.bRepBodies.item(i))
    except Exception as ex:
        notes.append("root.bRepBodies FAILED: {}".format(ex))

    try:
        n_direct = root.occurrences.count
        notes.append("root.occurrences.count = {}".format(n_direct))
    except Exception as ex:
        notes.append("root.occurrences FAILED: {}".format(ex))

    try:
        occs = root.allOccurrences
        n_occ = occs.count
        notes.append("root.allOccurrences.count = {}".format(n_occ))
        n_proxy = 0
        n_defn = 0
        for i in range(n_occ):
            try:
                occ = occs.item(i)
            except Exception as ex:
                notes.append("allOccurrences.item({}) FAILED: {}".format(i, ex))
                continue
            added = False
            try:
                for j in range(occ.bRepBodies.count):
                    bodies.append(occ.bRepBodies.item(j))
                    added = True
                    n_proxy += 1
            except Exception as ex:
                if i == 0:
                    notes.append("occ.bRepBodies FAILED: {}".format(ex))
                added = False
            if not added:
                try:
                    comp = occ.component
                    for j in range(comp.bRepBodies.count):
                        bodies.append(comp.bRepBodies.item(j))
                        n_defn += 1
                except Exception as ex:
                    if i == 0:
                        notes.append("occ.component.bRepBodies FAILED: {}".format(ex))
                    continue
        notes.append("bodies via proxy = {}, via definition = {}".format(n_proxy, n_defn))
    except Exception as ex:
        notes.append("root.allOccurrences FAILED: {}".format(ex))

    return bodies


def _largest_planar_face(root, notes):
    """Biggest flat face in the design — the cleanest measurement surface.

    Returns ``(face, area, report)``. Visibility is preferred but NOT required: a
    hidden body's geometry measures the same, and demanding visibility used to make
    this silently find nothing. ``report`` says what was actually examined so a
    failure is diagnosable instead of a dead end.
    """
    best = None
    best_area = 0.0
    n_bodies = 0
    n_faces = 0
    n_planar = 0
    kinds = {}

    try:
        plane_type = adsk.core.SurfaceTypes.PlaneSurfaceType
    except Exception:
        plane_type = 0

    for body in _collect_bodies(root, notes):
        n_bodies += 1
        try:
            faces = body.faces
            count = faces.count
        except Exception:
            continue
        for k in range(count):
            try:
                face = faces.item(k)
            except Exception:
                continue
            n_faces += 1
            try:
                stype = face.geometry.surfaceType
            except Exception:
                continue
            kinds[stype] = kinds.get(stype, 0) + 1
            if stype != plane_type:
                continue
            n_planar += 1
            try:
                area = face.area
            except Exception:
                continue
            if area > best_area:
                best_area, best = area, face

    report = "examined {} body/bodies, {} face(s), {} planar; surfaceType counts: {}".format(
        n_bodies, n_faces, n_planar, kinds or "(none)"
    )
    return best, best_area, report


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
        notes = []
        doc = None
        try:
            doc = app.activeDocument
            notes.append("activeDocument = {!r}".format(doc.name))
        except Exception as ex:
            notes.append("activeDocument FAILED: {}".format(ex))

        # activeProduct follows the active WORKSPACE, so in Render/CAM it need not be
        # the Design. Ask the document for its Design product as well, and use
        # whichever actually yields a root component.
        design = None
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            notes.append(
                "activeProduct = {} -> Design.cast {}".format(
                    type(app.activeProduct).__name__, "OK" if design else "None"
                )
            )
        except Exception as ex:
            notes.append("activeProduct FAILED: {}".format(ex))
        if design is None and doc is not None:
            try:
                prods = doc.products
                names = []
                for i in range(prods.count):
                    names.append(prods.item(i).productType)
                notes.append("document products = {}".format(names))
                design = adsk.fusion.Design.cast(
                    prods.itemByProductType("DesignProductType")
                )
                notes.append("itemByProductType -> {}".format("OK" if design else "None"))
            except Exception as ex:
                notes.append("document.products FAILED: {}".format(ex))

        if design is None:
            ui.messageBox(
                "Could not get a Design from this document.\n\n{}\n\n"
                "Open one of the product models (Bullnose.f3d etc.) and re-run.".format(
                    "\n".join(notes)
                )
            )
            return

        root = design.rootComponent
        try:
            notes.append("rootComponent = {!r}".format(root.name))
        except Exception as ex:
            notes.append("rootComponent.name FAILED: {}".format(ex))

        face, area, report = _largest_planar_face(root, notes)
        if face is None:
            ui.messageBox(
                "No planar face found.\n\n{}\n\nDIAGNOSTICS\n{}\n\n"
                "Send this text back — it says exactly where the scan stopped.".format(
                    report, "\n".join(notes)
                )
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

        point = face.pointOnFace
        decal_input = None
        create_errs = []
        # A proxy face's parentComponent is the DEFINITION, which is not always the
        # right context for createInput — try it, then the owning occurrence's
        # component, then root.
        candidates = []
        try:
            candidates.append(face.body.parentComponent)
        except Exception as ex:
            create_errs.append("face.body.parentComponent: {}".format(ex))
        try:
            ctx = face.assemblyContext
            if ctx is not None:
                candidates.append(ctx.component)
        except Exception:
            pass
        candidates.append(root)

        for comp in candidates:
            if comp is None:
                continue
            try:
                decal_input = comp.decals.createInput(str(image), [face], point)
                if decal_input is not None:
                    break
            except Exception as ex:
                create_errs.append("{}: {}".format(getattr(comp, "name", "?"), ex))
        if decal_input is None:
            ui.messageBox(
                "decals.createInput returned None on every candidate component.\n\n"
                "{}\n\nImage used: {}".format("\n".join(create_errs) or "(no errors)", image)
            )
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
            "Geometry scanned    : {}".format(report),
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
