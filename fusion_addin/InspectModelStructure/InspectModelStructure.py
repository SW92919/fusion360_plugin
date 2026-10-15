# Report the component/body structure of the OPEN model and say, per body,
# whether it is a show-only "skin" or a full solid.
#
# WHY THIS EXISTS
#   A decal with wrapping enabled is applied to a BODY, and Fusion wraps it onto
#   every face of that body — Autodesk's own wording in Decal.redefine:
#     "If true, only one face is needed and the decal wraps onto all the faces of
#      the body."
#   So the body decides what gets textured. If the body still contains the flat
#   cut-end faces, the texture lands on the cut ends and there is nothing the
#   plugin can do about it.
#
#   Bullnose renders correctly because its Stair Nose carries a separate body,
#   "Nose Skin", holding ONLY the visible outer surface. This script tells you
#   which bodies in the open model already look like that and which do not.
#
# HOW TO RUN
#   1. Open one model in Fusion (Bullnose first, to see what "good" looks like).
#   2. Utilities -> ADD-INS -> Scripts and Add-Ins -> green + -> pick this folder -> Run.
#   3. Read the dialog. A report is also written next to this script.
#   Repeat for each model. Nothing is modified — this only reads.

import traceback
from pathlib import Path

import adsk.core
import adsk.fusion

# A face whose normal runs along the part's long axis is a cut end.
# Matches BATCH_DECAL_END_CAP_NORMAL_DOT in texture_pipeline.py.
END_CAP_NORMAL_DOT = 0.82


def _unit_length_axis(body):
    """Unit vector along the body's longest bounding-box dimension."""
    try:
        bb = body.boundingBox
        dx = bb.maxPoint.x - bb.minPoint.x
        dy = bb.maxPoint.y - bb.minPoint.y
        dz = bb.maxPoint.z - bb.minPoint.z
    except Exception:
        return (1.0, 0.0, 0.0), 0.0
    dims = ((dx, (1.0, 0.0, 0.0)), (dy, (0.0, 1.0, 0.0)), (dz, (0.0, 0.0, 1.0)))
    longest, axis = max(dims, key=lambda t: t[0])
    return axis, longest


def _face_normal(face):
    try:
        ev = face.evaluator
        ok, pt = ev.getPointAtParameter(
            adsk.core.Point2D.create(
                (face.evaluator.parametricRange().minPoint.x
                 + face.evaluator.parametricRange().maxPoint.x) / 2.0,
                (face.evaluator.parametricRange().minPoint.y
                 + face.evaluator.parametricRange().maxPoint.y) / 2.0,
            )
        )
        if not ok:
            return None
        ok, nrm = ev.getNormalAtPoint(pt)
        if not ok:
            return None
        return (nrm.x, nrm.y, nrm.z)
    except Exception:
        return None


def _classify_body(body):
    """Return (n_faces, total_area, n_end_cap, end_cap_area, is_skin)."""
    axis, _length = _unit_length_axis(body)
    n_faces = 0
    total_area = 0.0
    n_end_cap = 0
    end_cap_area = 0.0
    try:
        faces = body.faces
        count = faces.count
    except Exception:
        return 0, 0.0, 0, 0.0, False
    for i in range(count):
        try:
            f = faces.item(i)
        except Exception:
            continue
        n_faces += 1
        try:
            area = f.area
        except Exception:
            area = 0.0
        total_area += area
        nrm = _face_normal(f)
        if nrm is None:
            continue
        dot = abs(nrm[0] * axis[0] + nrm[1] * axis[1] + nrm[2] * axis[2])
        if dot >= END_CAP_NORMAL_DOT:
            n_end_cap += 1
            end_cap_area += area
    return n_faces, total_area, n_end_cap, end_cap_area, n_end_cap == 0


def _walk(comp, lines, depth=0, seen=None):
    seen = seen if seen is not None else set()
    try:
        cid = comp.id
    except Exception:
        cid = None
    if cid is not None:
        if cid in seen:
            return
        seen.add(cid)

    pad = "    " * depth
    try:
        bodies = comp.bRepBodies
        n_bodies = bodies.count
    except Exception:
        n_bodies = 0

    if n_bodies:
        for i in range(n_bodies):
            try:
                b = bodies.item(i)
                name = b.name or "?"
            except Exception:
                continue
            n_f, area, n_ec, ec_area, is_skin = _classify_body(b)
            verdict = (
                "SKIN  (show-only — safe to texture)"
                if is_skin
                else "SOLID ({} cut-end face(s), {:.0f} cm2) — texture WILL hit the ends".format(
                    n_ec, ec_area
                )
            )
            lines.append(
                "{}    body {:<24} {:>3} faces {:>8.0f} cm2   {}".format(
                    pad, repr(name), n_f, area, verdict
                )
            )

    try:
        occs = comp.occurrences
        for i in range(occs.count):
            occ = occs.item(i)
            try:
                oname = occ.name or "?"
            except Exception:
                oname = "?"
            lines.append("{}  + {}".format(pad, oname))
            _walk(occ.component, lines, depth + 1, seen)
    except Exception:
        pass


def run(context):
    _ = context
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            doc = app.activeDocument
            prods = doc.products
            design = adsk.fusion.Design.cast(
                prods.itemByProductType("DesignProductType")
            )
        if design is None:
            ui.messageBox("Open a Fusion design first (one of the trim models).")
            return

        try:
            doc_name = app.activeDocument.name
        except Exception:
            doc_name = "(unknown)"

        lines = [
            "MODEL STRUCTURE — {}".format(doc_name),
            "",
            "A decal is applied to a BODY and wraps onto EVERY face of that body,",
            "so a body that still has cut-end faces will show texture on the ends.",
            "Bullnose is correct because 'Nose Skin' holds only the visible surface.",
            "",
        ]
        _walk(design.rootComponent, lines)

        skins = [l for l in lines if "SKIN  (" in l]
        solids = [l for l in lines if "SOLID (" in l]
        lines.append("")
        lines.append("SUMMARY: {} show-only skin body/bodies, {} full solid(s).".format(
            len(skins), len(solids)))
        if solids:
            lines.append("")
            lines.append("The product body that should carry the texture needs a")
            lines.append("show-only skin, the same as Bullnose's 'Nose Skin'.")

        text = "\n".join(lines)
        try:
            out = Path(__file__).resolve().parent / "model_structure_report.txt"
            with open(str(out), "a", encoding="utf-8") as fh:
                fh.write(text + "\n\n" + ("=" * 70) + "\n\n")
            text += "\n\nAppended to: {}".format(out)
        except Exception:
            pass
        ui.messageBox(text[:9000], "Model Structure")

    except Exception:
        try:
            ui.messageBox("Inspect failed:\n{}".format(traceback.format_exc()))
        except Exception:
            pass


def stop(context):
    _ = context
