from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import adsk.core
import adsk.fusion

import viewport_render


def open_model(app: adsk.core.Application, model_path: Path) -> Tuple[Optional[adsk.core.Document], str]:
    """Open a local ``.f3d`` archive in a new document.

    Current Fusion API only accepts a ``DataFile`` for ``Documents.open``,
    so passing a Windows path string raises ``Wrong number or type of
    arguments``. For local archives we must go through the ``ImportManager``
    using ``createFusionArchiveImportOptions`` + ``importToNewDocument``,
    which is the documented path for local ``.f3d`` files.

    Falls back to the legacy string ``documents.open`` only if the import
    manager path is unavailable, so older Fusion builds keep working.
    """

    resolved = str(model_path.resolve())
    errors: List[str] = []

    try:
        import_mgr = app.importManager
        options = import_mgr.createFusionArchiveImportOptions(resolved)
        if options is None:
            errors.append("createFusionArchiveImportOptions returned None")
        else:
            doc = import_mgr.importToNewDocument(options)
            if doc:
                return doc, ""
            active_doc = app.activeDocument
            if active_doc:
                return active_doc, ""
            errors.append("importToNewDocument returned no document")
    except Exception as ex:
        errors.append("importManager: {}".format(ex))

    try:
        doc = app.documents.open(resolved)
        if doc:
            return doc, ""
        errors.append("documents.open returned None")
    except Exception as ex:
        errors.append("documents.open: {}".format(ex))

    return None, "Open failed for {} ({})".format(model_path.name, " | ".join(errors))


def active_design(app: adsk.core.Application) -> Optional[adsk.fusion.Design]:
    return adsk.fusion.Design.cast(app.activeProduct)


def named_view_tasks(design: adsk.fusion.Design) -> Tuple[List[Tuple[Optional[adsk.fusion.NamedView], str]], str]:
    named_views = viewport_render.list_named_views(design)
    if named_views:
        return [(nv, nv.name) for nv in named_views], ""
    return [(None, "Viewport")], "No named views — using single 'Viewport' capture per color set."
