# Apply / restore visibility from Component.description and BRepBody.description (if present).

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import FrozenSet, List, Optional, Set, Tuple

import adsk.fusion

from visibility_rules import visibility_for_description

# Optional exact Fusion Browser names (normalized lower-case / stripped — use
# ``my body`` or ``my-body`` as stored after trim+lower). Use when helpers use
# generic appearances so substring rules never match.
EXACT_BATCH_HIDE_BODY_NAMES: FrozenSet[str] = frozenset()
EXACT_BATCH_HIDE_OCCURRENCE_NAMES: FrozenSet[str] = frozenset()
EXACT_BATCH_HIDE_DECAL_NAMES: FrozenSet[str] = frozenset()

# Bodies/occurrences matching these are turned off during each viewport capture
# (after baseline restore). Use Fusion descriptions ``hide:batch`` / ``hide:render``
# on helper surfaces, or name them with one of the substrings below.
_BATCH_HIDE_DESCRIPTION_MARKERS: Tuple[str, ...] = ("hide:batch", "hide:render")
# Helper geometry to hide during viewport capture. NOTE: scene lights
# (``Main Light``, key/fill/rim, etc.) are intentionally NOT hidden — turning a
# light's bulb off disables its illumination and makes renders too dark (client
# feedback). Only UV / reference / explicit batch-hide helpers are listed here.
_BATCH_HIDE_OCCURRENCE_NAME_SUBSTRINGS: Tuple[str, ...] = (
    "uv plane",
    "uv_plane",
    "uv reference",
    "render hide",
    "batch hide",
    "batch_render_hide",
)
_BATCH_HIDE_BODY_NAME_SUBSTRINGS: Tuple[str, ...] = (
    "uv plane",
    "uv_plane",
    "uv map",
    "uvmap",
    "uvgrid",
    "uv grid",
    "checker plane",
    "checkerplane",
    "mapping plane",
    "texture plane",
    "image plane",
    "decal plane",
    "projection plane",
    "mapping sheet",
    "texture canvas",
    "carrier plane",
    "substrate",
    "batch_render_hide",
)
_BATCH_HIDE_DECAL_NAME_SUBSTRINGS: Tuple[str, ...] = (
    "uv plane",
    "uv_plane",
    "uv map",
    "uvmap",
    "uv grid",
    "checker",
    "mapping plane",
    "texture plane",
    "orientation",
    "reference grid",
    "batch_render_hide",
)
_BATCH_HIDE_APPEARANCE_NAME_SUBSTRINGS: Tuple[str, ...] = (
    "uv map",
    "uvmap",
    "uv checker",
    "checker grid",
    "pattern grid",
    "orientation grid",
)

def _norm_exact(name: str) -> str:
    return (name or "").strip().lower()


@dataclass
class _OccSnap:
    occ: adsk.fusion.Occurrence
    was_on: bool


@dataclass
class _BodySnap:
    body: adsk.fusion.BRepBody
    was_on: bool


@dataclass
class _MeshSnap:
    mesh: object
    was_on: bool


@dataclass
class _DecalSnap:
    decal: object
    was_on: bool


def _iter_occurrences_recursive(comp: adsk.fusion.Component) -> List[adsk.fusion.Occurrence]:
    out: List[adsk.fusion.Occurrence] = []
    for i in range(comp.occurrences.count):
        occ = comp.occurrences.item(i)
        out.append(occ)
        out.extend(_iter_occurrences_recursive(occ.component))
    return out


def _iter_occurrences_for_visibility(root: adsk.fusion.Component) -> List[adsk.fusion.Occurrence]:
    """Every occurrence in the active design (same universe as ``allOccurrences``).

    Recursive descent from root can miss nodes that only appear in the flat
    ``allOccurrences`` collection; Render lights such as ``Main Light`` must match here.
    """
    try:
        design = root.parentDesign
        ao = design.rootComponent.allOccurrences
        return [ao.item(i) for i in range(ao.count)]
    except Exception:
        return _iter_occurrences_recursive(root)


def _iter_occurrences_union(root: adsk.fusion.Component) -> List[adsk.fusion.Occurrence]:
    """Tree ∪ ``allOccurrences``, deduped by ``entityToken``.

    Some Render / proxy rows appear in only one traversal on certain builds; unioning
    avoids missing ``Main Light`` and keeps snapshots aligned with hide passes.
    """
    seen: Set[str] = set()
    out: List[adsk.fusion.Occurrence] = []

    def _push(occ: adsk.fusion.Occurrence) -> None:
        try:
            tok = _entity_token(occ)
            key = tok if tok else "id:{}".format(id(occ))
            if key in seen:
                return
            seen.add(key)
            out.append(occ)
        except Exception:
            pass

    for occ in _iter_occurrences_recursive(root):
        _push(occ)
    for occ in _iter_occurrences_for_visibility(root):
        _push(occ)
    return out


def _occurrence_should_hide_batch(occ: adsk.fusion.Occurrence) -> bool:
    """True if this occurrence or any assembly ancestor matches batch-hide rules."""
    cur: Optional[adsk.fusion.Occurrence] = occ
    for _ in range(128):
        if cur is None:
            break
        try:
            if _occurrence_hide_for_batch(cur):
                return True
        except Exception:
            pass
        try:
            cur = cur.assemblyContext
        except Exception:
            break
    return False


def _iter_bodies_recursive(comp: adsk.fusion.Component) -> List[adsk.fusion.BRepBody]:
    out: List[adsk.fusion.BRepBody] = []
    for i in range(comp.bRepBodies.count):
        out.append(comp.bRepBodies.item(i))
    for i in range(comp.occurrences.count):
        out.extend(_iter_bodies_recursive(comp.occurrences.item(i).component))
    return out


def _iter_mesh_bodies_recursive(comp: adsk.fusion.Component) -> List[object]:
    out: List[object] = []
    try:
        meshes = comp.meshBodies
        for i in range(meshes.count):
            out.append(meshes.item(i))
    except Exception:
        pass
    for i in range(comp.occurrences.count):
        out.extend(_iter_mesh_bodies_recursive(comp.occurrences.item(i).component))
    return out


def _iter_root_decals(root: adsk.fusion.Component) -> List[object]:
    try:
        decals = root.decals
        return [decals.item(i) for i in range(decals.count)]
    except Exception:
        return []


def _desc_has_batch_hide_marker(description: str) -> bool:
    d = (description or "").lower()
    return any(m in d for m in _BATCH_HIDE_DESCRIPTION_MARKERS)


def _entity_token(ent: object) -> Optional[str]:
    try:
        tok = ent.entityToken
        return str(tok) if tok else None
    except Exception:
        return None


def _occurrence_hide_for_batch(occ: adsk.fusion.Occurrence) -> bool:
    try:
        if _desc_has_batch_hide_marker(occ.component.description or ""):
            return True
    except Exception:
        pass
    occ_name = occ.name or ""
    try:
        comp_name = occ.component.name or ""
    except Exception:
        comp_name = ""
    fp = ""
    try:
        fp = getattr(occ, "fullPathName", "") or ""
    except Exception:
        fp = ""
    # Match browser label, internal component name, and browser path. Scene
    # lights are intentionally not matched here — see the comment on
    # ``_BATCH_HIDE_OCCURRENCE_NAME_SUBSTRINGS`` (keep lights on for brightness).
    blob = re.sub(r"\s+", " ", "{} {} {}".format(occ_name, comp_name, fp)).strip().lower()
    if EXACT_BATCH_HIDE_OCCURRENCE_NAMES:
        if _norm_exact(occ_name) in EXACT_BATCH_HIDE_OCCURRENCE_NAMES:
            return True
        if comp_name and _norm_exact(comp_name) in EXACT_BATCH_HIDE_OCCURRENCE_NAMES:
            return True
    return any(sub in blob for sub in _BATCH_HIDE_OCCURRENCE_NAME_SUBSTRINGS)


def _body_hide_for_batch(body: adsk.fusion.BRepBody, *, include_face_uv_pins: bool = True) -> bool:
    try:
        if _desc_has_batch_hide_marker(getattr(body, "description", "") or ""):
            return True
    except Exception:
        pass
    name = (body.name or "").lower()
    if EXACT_BATCH_HIDE_BODY_NAMES and _norm_exact(body.name or "") in EXACT_BATCH_HIDE_BODY_NAMES:
        return True
    if any(sub in name for sub in _BATCH_HIDE_BODY_NAME_SUBSTRINGS):
        return True
    try:
        ap = body.appearance
        apn = (ap.name if ap else "").lower()
    except Exception:
        apn = ""
    if any(sub in apn for sub in _BATCH_HIDE_APPEARANCE_NAME_SUBSTRINGS):
        return True
    if include_face_uv_pins:
        return _body_has_uv_like_face_appearance(body)
    return False


def _body_has_uv_like_face_appearance(body: adsk.fusion.BRepBody) -> bool:
    """True when any face pins a UV / checker-ish appearance (common on mapping sheets)."""
    try:
        faces = body.faces
        n = faces.count
    except Exception:
        return False
    for fi in range(n):
        try:
            face = faces.item(fi)
            ap = face.appearance
            if not ap:
                continue
            apn = (ap.name or "").lower()
            if any(sub in apn for sub in _BATCH_HIDE_APPEARANCE_NAME_SUBSTRINGS):
                return True
        except Exception:
            continue
    return False


def _mesh_hide_for_batch(mbody: object) -> bool:
    try:
        if _desc_has_batch_hide_marker(getattr(mbody, "description", "") or ""):
            return True
    except Exception:
        pass
    name = (getattr(mbody, "name", None) or "").lower()
    if EXACT_BATCH_HIDE_BODY_NAMES and _norm_exact(getattr(mbody, "name", None) or "") in EXACT_BATCH_HIDE_BODY_NAMES:
        return True
    if any(sub in name for sub in _BATCH_HIDE_BODY_NAME_SUBSTRINGS):
        return True
    try:
        ap = mbody.appearance
        apn = (ap.name if ap else "").lower()
    except Exception:
        apn = ""
    return any(sub in apn for sub in _BATCH_HIDE_APPEARANCE_NAME_SUBSTRINGS)


def _decal_hide_for_batch(decal: object) -> bool:
    try:
        if _desc_has_batch_hide_marker(getattr(decal, "description", "") or ""):
            return True
    except Exception:
        pass
    name = (getattr(decal, "name", None) or "").lower()
    if EXACT_BATCH_HIDE_DECAL_NAMES and _norm_exact(getattr(decal, "name", None) or "") in EXACT_BATCH_HIDE_DECAL_NAMES:
        return True
    return any(sub in name for sub in _BATCH_HIDE_DECAL_NAME_SUBSTRINGS)


def collect_batch_hide_persistent_tokens(root: adsk.fusion.Component) -> frozenset[str]:
    """Entity tokens for helpers matched **before** carrier paint.

    After ``LifeproofBatchCarrier`` replaces UV-ish appearances, substring checks
    on appearance names stop working — we still hide using these stable tokens.

    Also captures bodies whose **faces** pin UV/checker appearances (common on
    mapping sheets): carrier wipes those pins, but tokens keep hiding correct.
    """
    tokens: Set[str] = set()
    for occ in _iter_occurrences_union(root):
        if _occurrence_should_hide_batch(occ):
            t = _entity_token(occ)
            if t:
                tokens.add(t)
    for body in _iter_bodies_recursive(root):
        if _body_hide_for_batch(body):
            t = _entity_token(body)
            if t:
                tokens.add(t)
    for mesh in _iter_mesh_bodies_recursive(root):
        if _mesh_hide_for_batch(mesh):
            t = _entity_token(mesh)
            if t:
                tokens.add(t)
    for decal in _iter_root_decals(root):
        if _decal_hide_for_batch(decal):
            t = _entity_token(decal)
            if t:
                tokens.add(t)
    return frozenset(tokens)


def capture_visibility(
    root: adsk.fusion.Component,
) -> Tuple[List[_OccSnap], List[_BodySnap], List[_MeshSnap], List[_DecalSnap]]:
    occ_snaps: List[_OccSnap] = []
    body_snaps: List[_BodySnap] = []
    for occ in _iter_occurrences_union(root):
        try:
            occ_snaps.append(_OccSnap(occ, occ.isLightBulbOn))
        except Exception:
            pass
    for body in _iter_bodies_recursive(root):
        try:
            body_snaps.append(_BodySnap(body, body.isLightBulbOn))
        except Exception:
            pass
    mesh_snaps: List[_MeshSnap] = []
    for mesh in _iter_mesh_bodies_recursive(root):
        try:
            mesh_snaps.append(_MeshSnap(mesh, mesh.isLightBulbOn))
        except Exception:
            pass
    decal_snaps: List[_DecalSnap] = []
    for decal in _iter_root_decals(root):
        try:
            decal_snaps.append(_DecalSnap(decal, decal.isLightBulbOn))
        except Exception:
            pass
    return occ_snaps, body_snaps, mesh_snaps, decal_snaps


def restore_visibility(
    occ_snaps: List[_OccSnap],
    body_snaps: List[_BodySnap],
    mesh_snaps: List[_MeshSnap],
    decal_snaps: List[_DecalSnap],
) -> None:
    for snap in occ_snaps:
        try:
            if snap.occ.isValid:
                snap.occ.isLightBulbOn = snap.was_on
        except Exception:
            pass
    for snap in body_snaps:
        try:
            if snap.body.isValid:
                snap.body.isLightBulbOn = snap.was_on
        except Exception:
            pass
    for snap in mesh_snaps:
        try:
            if getattr(snap.mesh, "isValid", True):
                snap.mesh.isLightBulbOn = snap.was_on
        except Exception:
            pass
    for snap in decal_snaps:
        try:
            if getattr(snap.decal, "isValid", True):
                snap.decal.isLightBulbOn = snap.was_on
        except Exception:
            pass


def apply_visibility_for_named_view(root: adsk.fusion.Component, view_name: str) -> None:
    """Set isLightBulbOn from description rules for this view name (absolute, not delta)."""
    for occ in _iter_occurrences_union(root):
        try:
            desc = occ.component.description or ""
        except Exception:
            desc = ""
        vis = visibility_for_description(desc, view_name)
        try:
            occ.isLightBulbOn = vis
        except Exception:
            pass
    for body in _iter_bodies_recursive(root):
        try:
            desc = getattr(body, "description", "") or ""
        except Exception:
            desc = ""
        vis = visibility_for_description(desc, view_name)
        try:
            body.isLightBulbOn = vis
        except Exception:
            pass
    for mesh in _iter_mesh_bodies_recursive(root):
        try:
            desc = getattr(mesh, "description", "") or ""
        except Exception:
            desc = ""
        vis = visibility_for_description(desc, view_name)
        try:
            mesh.isLightBulbOn = vis
        except Exception:
            pass
    for decal in _iter_root_decals(root):
        try:
            desc = getattr(decal, "description", "") or ""
        except Exception:
            desc = ""
        vis = visibility_for_description(desc, view_name)
        try:
            decal.isLightBulbOn = vis
        except Exception:
            pass


def _hide_geometry_under_component(comp: adsk.fusion.Component) -> int:
    """Turn off solids/mesh/nested occurrences under one component (Render proxy fallback)."""
    n = 0
    try:
        breps = comp.bRepBodies
        for bi in range(breps.count):
            try:
                breps.item(bi).isLightBulbOn = False
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    try:
        meshes = comp.meshBodies
        for mi in range(meshes.count):
            try:
                meshes.item(mi).isLightBulbOn = False
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    try:
        occs = comp.occurrences
        for oi in range(occs.count):
            try:
                child = occs.item(oi)
                child.isLightBulbOn = False
                n += 1
                n += _hide_geometry_under_component(child.component)
            except Exception:
                pass
    except Exception:
        pass
    return n


def _silence_batch_hide_occurrence(occ: adsk.fusion.Occurrence) -> int:
    """Hide occurrence plus drill into bodies — occurrence bulb alone often misses viewport draws."""
    n = 0
    try:
        occ.isLightBulbOn = False
        n += 1
    except Exception:
        pass
    try:
        comp = occ.component
        n += _hide_geometry_under_component(comp)
    except Exception:
        pass
    return n


def apply_batch_render_geometry_hides(
    root: adsk.fusion.Component,
    persistent_hide_tokens: Optional[frozenset[str]] = None,
) -> int:
    """Hide UV/reference helper geometry during viewport capture.

    Call **after** ``restore_visibility`` each frame so helpers stay off even when
    the baseline snapshot had them visible.

    ``persistent_hide_tokens`` comes from ``collect_batch_hide_persistent_tokens``
    taken once before carrier assignment so helpers stay hidden after appearances
    are rewritten.

    Fusion-side setup (pick one):
    - Put ``hide:batch`` or ``hide:render`` in the component or body **description**, or
    - Name the helper occurrence/body with a substring from the ``_BATCH_HIDE_*`` tuples
      at the top of this file, or set ``EXACT_BATCH_HIDE_*_NAMES``.
    """
    toks = persistent_hide_tokens or frozenset()
    n = 0
    for occ in _iter_occurrences_union(root):
        try:
            if hasattr(occ, "isValid") and not occ.isValid:
                continue
        except Exception:
            continue
        tok = _entity_token(occ)
        if not (_occurrence_should_hide_batch(occ) or (tok is not None and tok in toks)):
            continue
        n += _silence_batch_hide_occurrence(occ)
    for body in _iter_bodies_recursive(root):
        tok = _entity_token(body)
        if not (_body_hide_for_batch(body) or (tok is not None and tok in toks)):
            continue
        try:
            body.isLightBulbOn = False
            n += 1
        except Exception:
            pass
    for mesh in _iter_mesh_bodies_recursive(root):
        tok = _entity_token(mesh)
        if not (_mesh_hide_for_batch(mesh) or (tok is not None and tok in toks)):
            continue
        try:
            mesh.isLightBulbOn = False
            n += 1
        except Exception:
            pass
    for decal in _iter_root_decals(root):
        tok = _entity_token(decal)
        if not (_decal_hide_for_batch(decal) or (tok is not None and tok in toks)):
            continue
        try:
            decal.isLightBulbOn = False
            n += 1
        except Exception:
            pass
    return n

