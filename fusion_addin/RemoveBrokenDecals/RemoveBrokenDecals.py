# Remove decals whose link to their surfaces is broken, in the OPEN model.
#
# WHY THIS EXISTS
#   3-in-1.f3d's trim decal "Grand Palais Oak-1" was applied before the trim was
#   turned into its own component. Fusion moved the decal but lost its surfaces,
#   and it no longer appears in the Browser, so it cannot be selected and deleted
#   by hand. The API still lists it — that is how the batch finds it — so this
#   script removes it from the open model.
#
# WHAT IT DOES
#   Lists every decal (top level and every component) whose surface reference
#   cannot be read, shows them, and only after you click Yes deletes them. If
#   Fusion refuses a delete, that decal is switched off instead (the batch leaves
#   switched-off decals alone). Healthy decals are never touched. Nothing is saved:
#   export the model yourself afterwards.
#
# HOW TO RUN
#   Make the model's tab active in Fusion, then UTILITIES -> ADD-INS -> Scripts and
#   Add-Ins -> Scripts tab -> green + -> pick this folder -> Run.

import traceback

import adsk.core
import adsk.fusion


def _is_broken(decal) -> bool:
    """True when the decal's surfaces cannot be read (an empty or [None] list)."""
    try:
        faces = list(decal.faces)
    except Exception:
        return True
    return len(faces) == 0 or any(f is None for f in faces)


def _components(design):
    """Root plus every component used in the design, each once."""
    out, seen = [], set()
    root = design.rootComponent
    for comp in [root] + [root.allOccurrences.item(i).component for i in range(root.allOccurrences.count)]:
        try:
            key = comp.id
        except Exception:
            key = id(comp)
        if key in seen:
            continue
        seen.add(key)
        out.append(comp)
    return out


def run(context):
    _ = context
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            ui.messageBox("Open the model and switch to the DESIGN workspace first.")
            return
        try:
            doc_name = app.activeDocument.name
        except Exception:
            doc_name = "(this model)"

        root_id = design.rootComponent.id
        broken = []
        healthy = 0
        for comp in _components(design):
            where = "top level" if comp.id == root_id else 'component "{}"'.format(comp.name)
            for i in range(comp.decals.count):
                decal = comp.decals.item(i)
                if _is_broken(decal):
                    broken.append((decal, decal.name, where))
                else:
                    healthy += 1

        if not broken:
            ui.messageBox(
                "Model: {}\n\nNo broken decals found ({} healthy decal(s)). "
                "Nothing was changed.".format(doc_name, healthy),
                "Remove broken decals",
            )
            return

        listing = "\n".join("  - {}  ({})".format(name, where) for _d, name, where in broken)
        answer = ui.messageBox(
            "Model: {}\n\nBroken decal(s) found:\n{}\n\nHealthy decals that will NOT be "
            "touched: {}\n\nDelete the broken decal(s)?".format(doc_name, listing, healthy),
            "Remove broken decals",
            adsk.core.MessageBoxButtonTypes.YesNoButtonType,
            adsk.core.MessageBoxIconTypes.QuestionIconType,
        )
        if answer != adsk.core.DialogResults.DialogYes:
            ui.messageBox("Nothing was changed.", "Remove broken decals")
            return

        results = []
        for decal, name, where in broken:
            deleted = False
            try:
                deleted = bool(decal.deleteMe())
            except Exception:
                deleted = False
            if deleted:
                results.append("  - {}: DELETED".format(name))
                continue
            try:
                decal.isLightBulbOn = False
                results.append("  - {}: could not be deleted, so it was SWITCHED OFF".format(name))
            except Exception as ex:
                results.append("  - {}: could NOT be deleted or switched off ({})".format(name, ex))

        ui.messageBox(
            "Model: {}\n\n{}\n\nNext: add the new decal inside the 3-In-1 part, then "
            "export the model.".format(doc_name, "\n".join(results)),
            "Remove broken decals",
        )
    except Exception:
        ui.messageBox("Script failed:\n{}".format(traceback.format_exc()), "Remove broken decals")


def stop(context):
    _ = context
