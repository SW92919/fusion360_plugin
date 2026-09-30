from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import adsk.core
import adsk.fusion

import batch_config
import render_instructions
import support_paths
import texture_pipeline
import visibility_apply
import viewport_render

from plugin import image_mapper
from plugin import logger as logutil
from plugin import model_handler
from plugin import ui as uip


def _log_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _code_build_stamp() -> str:
    """Newest source timestamp across the add-in — identifies the running build.

    Fusion caches modules for the life of the session, so an edited file and a
    stale in-memory copy are otherwise indistinguishable from the log.
    """
    try:
        addin_dir = Path(texture_pipeline.__file__).resolve().parent
        newest = 0.0
        newest_name = "?"
        for py in addin_dir.rglob("*.py"):
            try:
                mtime = py.stat().st_mtime
            except OSError:
                continue
            if mtime > newest:
                newest, newest_name = mtime, py.name
        if not newest:
            return "(unknown)"
        return "{} (newest source: {})".format(
            datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M:%S"), newest_name
        )
    except Exception as ex:
        return "(unavailable: {})".format(ex)


def _append_log(log_path: Path, row: List[str]) -> None:
    new_file = not log_path.exists()
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "model", "color_set", "view", "output", "success", "notes"])
        w.writerow(row)


def _save_local_image(
    design: adsk.fusion.Design,
    app: adsk.core.Application,
    out_path: Path,
    render_width: int,
    render_height: int,
    camera=None,
    render_quality: int = 75,
) -> bool:
    """Ray-traced local export via ``Rendering.startLocalRender``."""
    path = str(out_path.resolve())
    return viewport_render.save_fusion_local_render(
        design,
        app,
        path,
        render_width,
        render_height,
        camera=camera,
        render_quality=render_quality,
    )


def _resolve_render_tasks(
    design: adsk.fusion.Design,
    entry: "render_instructions.ModelInstructions",
) -> Tuple[List[Tuple[Any, str, Tuple[str, ...], str]], List[str]]:
    """Map the JSON's renderConfigurations onto the design's saved named views.

    Returns ``(tasks, log_lines)`` where each task is
    ``(named_view, view_name, hide_bodies, visibility_label)``. A configuration
    naming a view the model does not have is dropped and reported — rendering it
    from whatever camera happened to be active would ship a wrong image quietly.
    """
    available: Dict[str, Any] = {}
    order: List[str] = []
    for nv in viewport_render.list_named_views(design):
        try:
            name = nv.name or ""
        except Exception:
            continue
        key = render_instructions.normalize_name_key(name)
        if key and key not in available:
            available[key] = nv
            order.append(name)

    lines: List[str] = [
        "Named views in model ({}): {}".format(
            len(order), ", ".join(order) if order else "(none)"
        )
    ]
    tasks: List[Tuple[Any, str, Tuple[str, ...], str]] = []
    missing: List[str] = []
    for cfg in entry.render_configurations:
        nv = available.get(render_instructions.normalize_name_key(cfg.named_view))
        if nv is None:
            missing.append(cfg.named_view)
            continue
        tasks.append((nv, cfg.named_view, cfg.hide_bodies, cfg.label))

    for nv, view_name, hides, label in tasks:
        lines.append(
            "Render config: '{}'{}{}".format(
                view_name,
                " | hide: {}".format(", ".join(hides)) if hides else "",
                " | label: '{}'".format(label) if label else "",
            )
        )
    for name in missing:
        lines.append(
            "NO MATCH for namedView '{}' — not saved in this model; that image is skipped".format(
                name
            )
        )
    return tasks, lines


def execute_batch(
    fui: adsk.core.UserInterface,
    ins: adsk.core.CommandInputs,
    texture_root: Path,
    models: List[Path],
    render_width: int,
    render_height: int,
    output_ext: str,
    pipeline_selection: str,
    instructions_path: Path,
    render_quality: int,
) -> None:
    log_path = logutil.default_log_path(texture_root)
    logutil.append_log(log_path, "execute_batch start")

    _execute_batch_inner(
        fui,
        ins,
        texture_root,
        models,
        render_width,
        render_height,
        output_ext,
        pipeline_selection,
        instructions_path,
        render_quality,
        log_path,
    )


def _execute_batch_inner(
    fui: adsk.core.UserInterface,
    ins: adsk.core.CommandInputs,
    texture_root: Path,
    models: List[Path],
    render_width: int,
    render_height: int,
    output_ext: str,
    pipeline_selection: str,
    instructions_path: Path,
    render_quality: int,
    log_path: Path,
) -> None:

    try:
        color_sets = support_paths.scan_texture_root(texture_root)
    except Exception as ex:
        fui.messageBox("Invalid texture folder:\n{}".format(ex))
        logutil.append_log(log_path, "scan_texture_root failed", ex)
        return
    if not color_sets:
        fui.messageBox("No color subfolders found under:\n{}".format(texture_root))
        return
    if not models:
        fui.messageBox("No model files selected.")
        return

    # The render instructions now drive views, hides and decal sizes, so a bad file
    # means nothing can render correctly — fail here rather than part-way through.
    instructions = render_instructions.load_instructions(instructions_path)
    if not instructions.ok:
        detail = "\n".join(render_instructions.describe_problems(instructions, limit=12))
        fui.messageBox(
            "Render Instructions could not be used:\n\n{}\n\n"
            "Fix the file and run again.".format(detail)
        )
        logutil.append_log(log_path, "render instructions rejected: {}".format(detail))
        return

    texture_pipeline.clear_decal_shift_temp_files()

    app = adsk.core.Application.get()
    csv_log = texture_root / "_LifeproofBatchRender_log.csv"
    summary_lines: List[str] = [
        "Texture root: {}".format(texture_root),
        "Color sets: {}".format(len(color_sets)),
        "Models: {}".format(len(models)),
        "Output: {} × {} {}".format(render_width, render_height, output_ext.upper()),
        "Pipeline mode: {}".format(pipeline_selection),
        # Which build actually ran. Fusion caches modules for the whole session,
        # so without this a stale build and a fresh log look identical.
        "Code loaded: {}".format(_code_build_stamp()),
        "Render instructions: {} ({} model entries)".format(
            instructions.source, len(instructions.models)
        ),
        "Render quality: {}".format(render_quality),
        "",
        "Delivery swatches (texture copies per color folder):",
    ]
    if instructions.problems:
        summary_lines.append("Render instruction warnings:")
        summary_lines.extend(render_instructions.describe_problems(instructions))
        summary_lines.append("")
    for cs in color_sets:
        for purge_line in support_paths.purge_ignored_texture_sidecars(cs.folder):
            summary_lines.append("  {}".format(purge_line))
        _ns, sw_lines = support_paths.write_color_set_delivery_swatches(cs)
        summary_lines.extend(["  {}".format(x) for x in sw_lines])
    summary_lines.append("")

    renders_ok = 0
    renders_fail = 0
    models_skipped = 0

    # Estimate from the JSON rather than the first model — configuration counts
    # differ per model now, so the old "first model × everything" guess would skew
    # the progress bar. Over-counts only when a named view is missing from a model.
    planned = 0
    for _mp in models:
        _entry = render_instructions.find_for_model(instructions, _mp)
        if _entry is not None:
            planned += len(_entry.render_configurations)
    total_steps: Optional[int] = max(1, planned * max(1, len(color_sets)))
    done_steps = 0

    def bump_progress(msg: str) -> None:
        uip.set_status(ins, msg)
        try:
            pct = 0 if not total_steps else int(min(100, (100 * done_steps) // max(1, total_steps)))
            uip.set_progress(ins, pct, 100)
        except Exception:
            pass
        viewport_render.pump_ui()

    for mp in models:
        model_stem = mp.stem

        entry = render_instructions.find_for_model(instructions, mp)
        if entry is None:
            reason = render_instructions.unmatched_model_message(mp, instructions)
            summary_lines.append("=== {} → SKIPPED ===".format(mp.name))
            summary_lines.append("  {}".format(reason))
            logutil.append_log(log_path, "skip {}: {}".format(mp.name, reason))
            try:
                _append_log(
                    csv_log, [_log_timestamp(), model_stem, "", "", "", "skipped", reason]
                )
            except Exception:
                pass
            models_skipped += 1
            continue

        # The .f3d filenames no longer carry "Decal"/"Appearance", so the old
        # filename sniff (support_paths.infer_texture_mode) would call every model
        # "appearance" via its fallback and texture nothing. The JSON is the
        # authority now: a non-empty decals[] means decal mode.
        mode = batch_config.resolve_pipeline_mode(
            pipeline_selection, mp, lambda _p, _entry=entry: _entry.texture_mode
        )
        summary_lines.append("=== {} → {} ===".format(mp.name, mode))

        # Install this model's decal targets. These drive BOTH which components get
        # a decal (replacing the filename-derived guess, which the model rename
        # broke) and the exact physical size each one is given.
        texture_pipeline.set_decal_targets(render_instructions.decal_size_map(entry))
        if entry.decals:
            summary_lines.append(
                "  Decal targets from instructions: {}".format(
                    "; ".join(
                        "{} = {:.1f}x{:.1f} in -> _{}".format(
                            d.part_number, d.width_in, d.length_in, d.slot
                        )
                        for d in entry.decals
                    )
                )
            )

        doc, err = model_handler.open_model(app, mp)
        if not doc:
            summary_lines.append(err or "Open failed.")
            continue

        design = model_handler.active_design(app)
        if not design:
            summary_lines.append("No active design.")
            continue

        viewport_render.prepare_clean_render_view(app)

        # Let Fusion finish computing / refining the display mesh before the first
        # render. importToNewDocument returns as soon as the doc exists, so
        # rendering immediately can ray-trace a still-coarse tessellation —
        # smooth arcs then come out as flat facets ("hexagonal").
        _settled = viewport_render.settle_geometry(
            app, viewport_render.GEOMETRY_SETTLE_AFTER_OPEN_SEC, design=design
        )
        summary_lines.append(
            "  Geometry settle after open: {:.1f}s (avoids faceted curves)".format(_settled)
        )

        root = design.rootComponent
        summary_lines.append(
            "  Named-view visibility rules: {}".format(
                "ON (hide:/show: in descriptions)" if texture_pipeline.APPLY_NAMED_VIEW_VISIBILITY else "OFF (use file visibility as opened)"
            )
        )
        # The JSON's renderConfigurations are now the complete deliverable list:
        # an ordered set of (named view, components to hide) pairs, where the same
        # view may legitimately repeat with a different hide-list.
        view_tasks, view_lines = _resolve_render_tasks(design, entry)
        summary_lines.extend("  {}".format(x) for x in view_lines)
        if not view_tasks:
            summary_lines.append(
                "  Nothing to render for this model — no configuration matched a saved view."
            )
            try:
                _append_log(
                    csv_log,
                    [
                        _log_timestamp(),
                        model_stem,
                        "",
                        "",
                        "",
                        "skipped",
                        "no renderConfigurations matched a named view in the model",
                    ],
                )
            except Exception:
                pass
            models_skipped += 1
            continue

        an, dn = texture_pipeline.list_appearance_and_decal_names(design)
        summary_lines.append(
            "Template targets - Appearances ({}): {} | Decals ({}): {}".format(
                len(an),
                ", ".join(an) if an else "(none)",
                len(dn),
                ", ".join(dn) if dn else "(none)",
            )
        )

        occ_snap, body_snap, mesh_snap, decal_snap = visibility_apply.capture_visibility(root)
        batch_hide_tokens = visibility_apply.collect_batch_hide_persistent_tokens(root)

        carrier: Optional[adsk.fusion.Appearance] = None
        body_appearance_snap: list = []
        batch_decals: List[Any] = []
        # Force decal/carrier coverage is MODE-GATED:
        #  * appearance mode → OFF: the .f3d already has Vinyl Skin-1/-2 on the
        #    right faces; we only swap the appearance image. Projecting decals
        #    here would bleed onto the cut edge (client rejected that).
        #  * decal mode → ON: the authored decals can't be recreated on this
        #    Fusion build (no decal.faces), so the proven batch projection path
        #    (auto-size + grain-align) is what gives correct coverage/direction.
        use_decal_coverage = (
            mode == "decal"
            and texture_pipeline.FORCE_BODY_COVERAGE
            and texture_pipeline.BODY_COVERAGE_VIA_DECALS
        )

        if use_decal_coverage:
            first_image = None
            for cs in color_sets:
                if cs.slot1:
                    first_image = cs.slot1
                    break
            if first_image is None:
                summary_lines.append(
                    "  Decal projection skipped: no color-set image available"
                )
            else:
                _neutralize = getattr(
                    texture_pipeline, "neutralize_dark_body_appearances", None
                )
                body_appearance_snap = texture_pipeline.capture_body_appearances(
                    design
                )
                if callable(_neutralize):
                    _, neut_lines = _neutralize(design)
                    for line in neut_lines:
                        summary_lines.append("  " + line)
                batch_decals, decal_lines = texture_pipeline.create_batch_decals_for_all_bodies(
                    design, first_image, appearance_snap=body_appearance_snap, model_path=mp
                )
                for line in decal_lines:
                    summary_lines.append("  " + line)
        elif mode == "decal" and texture_pipeline.FORCE_BODY_COVERAGE:
            summary_lines.append("  Pre-batch appearance audit:")
            for line in texture_pipeline.audit_design_appearances(design):
                summary_lines.append("    " + line)

            test_image = None
            for cs in color_sets:
                if cs.slot1:
                    test_image = cs.slot1
                    break
            carrier, carrier_lines = texture_pipeline.get_or_create_carrier(
                design, test_image=test_image
            )
            for line in carrier_lines:
                summary_lines.append("  Carrier setup: {}".format(line))
            if carrier is not None:
                body_appearance_snap = texture_pipeline.capture_body_appearances(design)
                n_ok, n_fail, body_lines = texture_pipeline.apply_carrier_to_all_bodies(
                    design, carrier
                )
                summary_lines.append(
                    "  Carrier '{}' force-assigned: bodies {} OK / {} failed".format(
                        carrier.name, n_ok, n_fail
                    )
                )
                for line in body_lines:
                    summary_lines.append("    {}".format(line))

                summary_lines.append("  Post-carrier appearance audit:")
                for line in texture_pipeline.audit_design_appearances(design):
                    summary_lines.append("    " + line)
            else:
                # No carrier means this Fusion build exposes no usable
                # appearance texture slots (every addByCopy probe failed), so
                # the per-name appearance match can't texture solid materials
                # like the end-cap "Paint - Metallic (Black)". Decals are the
                # only mechanism that works here — auto-fall back to projecting
                # batch tile decals onto every face so coverage is complete.
                summary_lines.append(
                    "  Carrier setup: NONE found - auto-falling back to "
                    "batch-decal coverage (build has no texture slots)"
                )
                fb_image = test_image
                if fb_image is None:
                    summary_lines.append(
                        "  Decal fallback skipped: no color-set image available"
                    )
                else:
                    # Neutralize dark/reflective body appearances BEFORE decals
                    # project, so any decal gap shows matte grey instead of a
                    # black-metallic or shiny-steel stripe. Snapshot is
                    # restored at end-of-batch via body_appearance_snap.
                    # getattr-guarded so a texture_pipeline version that lacks
                    # this helper degrades gracefully instead of hard-crashing.
                    _neutralize = getattr(
                        texture_pipeline, "neutralize_dark_body_appearances", None
                    )
                    body_appearance_snap = texture_pipeline.capture_body_appearances(
                        design
                    )
                    if callable(_neutralize):
                        _, neut_lines = _neutralize(design)
                        for line in neut_lines:
                            summary_lines.append("  " + line)
                    batch_decals, decal_lines = (
                        texture_pipeline.create_batch_decals_for_all_bodies(
                            design, fb_image, appearance_snap=body_appearance_snap, model_path=mp
                        )
                    )
                    for line in decal_lines:
                        summary_lines.append("  " + line)

        zero_texture_color_sets: List[str] = []
        # Hide results are identical for every colour set, so report each
        # configuration once rather than repeating it per colour.
        logged_hides: set = set()

        for cs in color_sets:
            if not cs.slot1:
                summary_lines.append("Skip color '{}': no _1 / first image.".format(cs.folder.name))
                continue

            bump_progress("Textures: {} / {}".format(model_stem, cs.folder.name))

            s1, s2 = image_mapper.effective_texture_slots(design, mode, cs.slot1, cs.slot2)
            n_tex = 0
            tex_lines: List[str] = []
            if batch_decals:
                decal_image = s1 or cs.slot1
                n_expected = len(batch_decals)
                # slot-2 bodies (Vinyl Skin-2) receive the _2 raster; the rest _1.
                n_tex, dec_lines, batch_decals = texture_pipeline.update_batch_decal_images(
                    batch_decals, decal_image, s2
                )
                tex_lines.extend(dec_lines)
                # n_tex is now a real success count, so a partial failure is
                # visible instead of being averaged away by the list length.
                if n_tex < n_expected:
                    tex_lines.append(
                        "DECAL UPDATE FAILURES: {} of {} decal(s) did not recreate — "
                        "any absolute size may not be applied for this color set".format(
                            n_expected - n_tex, n_expected
                        )
                    )
                if mode == "appearance":
                    n_app, app_lines = texture_pipeline.apply_hybrid_appearance_for_batch(
                        design, s1, s2
                    )
                    n_tex += n_app
                    tex_lines.extend(app_lines)
                # Optionally also swap imageFilename on user-authored decals
                # in the .f3d. Disabled by default — every extra decal write
                # triggers a Fusion re-projection that grows the freeze
                # window, and with the batch tile coverage we usually don't
                # need them. Toggle UPDATE_USER_AUTHORED_DECALS in
                # texture_pipeline.py to re-enable.
                if texture_pipeline.UPDATE_USER_AUTHORED_DECALS:
                    try:
                        batch_ids = set(id(d) for d in batch_decals)
                    except Exception:
                        batch_ids = set()
                    n_user, user_lines = texture_pipeline.update_user_authored_decals(
                        design, decal_image, batch_ids
                    )
                    n_tex += n_user
                    tex_lines.extend(user_lines)
            else:
                n_tex, tex_lines = texture_pipeline.apply_color_set_for_open_design(
                    mode, s1, s2, carrier=carrier
                )
            if n_tex == 0:
                zero_texture_color_sets.append(cs.folder.name)
            # Emit the texture log lines even when 0 updates — they carry the
            # appearance property-tree diagnostic (DIAG_DUMP_APPEARANCE_TREE)
            # that tells us why changeTextureImage found no image node.
            for line in tex_lines:
                summary_lines.append("  {} | {}".format(cs.folder.name, line))

            viewport_render.pump_ui()
            try:
                app.activeViewport.refresh()
            except Exception:
                pass

            for nv, view_name, hide_bodies, vis_label in view_tasks:

                def _apply_hides(_hides=hide_bodies) -> List[str]:
                    """Helper hides, then the configuration's own — explicit wins."""
                    visibility_apply.apply_batch_render_geometry_hides(
                        root, batch_hide_tokens
                    )
                    if not _hides:
                        return []
                    _n, _lines = visibility_apply.hide_components_by_part_number(
                        root, _hides
                    )
                    return _lines

                visibility_apply.restore_visibility(
                    occ_snap, body_snap, mesh_snap, decal_snap
                )
                if texture_pipeline.APPLY_NAMED_VIEW_VISIBILITY:
                    visibility_apply.apply_visibility_for_named_view(root, view_name)
                hide_lines = _apply_hides()
                hide_key = (view_name, vis_label)
                if hide_lines and hide_key not in logged_hides:
                    logged_hides.add(hide_key)
                    for line in hide_lines:
                        summary_lines.append("  {}".format(line))

                viewport_render.activate_named_view(app, nv)
                viewport_render.apply_named_view_framing(
                    app, view_name, preserve_orientation=True, design=design
                )
                viewport_render.pump_ui()
                # Reframe once helpers are off — otherwise ViewFit bounds still
                # include proxy solids hidden after the first fit (wrong crop vs manual hide).
                _apply_hides()
                viewport_render.pump_ui()
                # Re-apply the designer's exact saved camera right before capture so
                # the visibility toggling above can't nudge the composition
                # (PRESERVE_NAMED_VIEW_COMPOSITION trusts it 1:1).
                viewport_render.activate_named_view(app, nv)
                viewport_render.apply_named_view_framing(
                    app, view_name, preserve_orientation=True, design=design
                )
                viewport_render.pump_ui()

                # Last-chance hide after ``apply_named_view_framing`` (camera updates can
                # resurrect Render-proxy meshes until bulbs are toggled again).
                _apply_hides()
                viewport_render.pump_ui()

                base = support_paths.build_output_basename(
                    model_stem,
                    cs.output_image_stem,
                    view_name,
                    vis_label,
                )
                out_path = support_paths.versioned_path(cs.folder / (base + output_ext))

                # Top-up settle so the tessellation is fully refined for THIS
                # capture (cheap next to a ray-trace).
                viewport_render.settle_geometry(
                    app, viewport_render.GEOMETRY_SETTLE_BEFORE_CAPTURE_SEC
                )

                # The viewport camera won't stick on this build, so hand the
                # named view's OWN camera straight to the local renderer.
                render_cam = None
                if nv is not None:
                    try:
                        render_cam = nv.camera
                        render_cam.isSmoothTransition = False
                    except Exception:
                        render_cam = None

                ok = _save_local_image(
                    design,
                    app,
                    out_path,
                    render_width,
                    render_height,
                    camera=render_cam,
                    render_quality=render_quality,
                )

                ts = _log_timestamp()
                try:
                    _append_log(
                        csv_log,
                        [
                            ts,
                            model_stem,
                            cs.folder.name,
                            view_name,
                            str(out_path),
                            "1" if ok else "0",
                            vis_label,
                        ],
                    )
                except Exception:
                    pass
                if ok:
                    renders_ok += 1
                else:
                    renders_fail += 1

                done_steps += 1
                bump_progress("Saved: {}".format(out_path.name))

                visibility_apply.restore_visibility(
                    occ_snap, body_snap, mesh_snap, decal_snap
                )
                viewport_render.pump_ui()

        visibility_apply.restore_visibility(occ_snap, body_snap, mesh_snap, decal_snap)

        if batch_decals:
            if texture_pipeline.SKIP_BATCH_DECAL_CLEANUP:
                summary_lines.append(
                    "  Decal teardown: skipped (SKIP_BATCH_DECAL_CLEANUP=True — inspect in Fusion)"
                )
            else:
                n_removed = texture_pipeline.cleanup_batch_decals(batch_decals)
                summary_lines.append(
                    "  Decal teardown: {} batch decal(s) removed".format(n_removed)
                )

        if body_appearance_snap and texture_pipeline.RESTORE_BODY_APPEARANCES_ON_FINISH:
            n_restored = texture_pipeline.restore_body_appearances(body_appearance_snap)
            summary_lines.append(
                "  Carrier teardown: original appearance restored on {} body/bodies".format(
                    n_restored
                )
            )
        elif body_appearance_snap:
            summary_lines.append(
                "  Carrier teardown: skipped (RESTORE_BODY_APPEARANCES_ON_FINISH=False)"
            )

        if zero_texture_color_sets:
            summary_lines.append(
                "Textures 0 for {} color set(s): {}".format(
                    len(zero_texture_color_sets),
                    ", ".join(zero_texture_color_sets),
                )
            )
            summary_lines.append(
                "  Hint: rename appearances/decals to match SLOT*_NAMES in texture_pipeline.py,"
            )
            summary_lines.append(
                "  or rely on the positional fallback (decal mode only)."
            )

    summary_lines.append("")
    _post_purge: List[str] = []
    for cs in color_sets:
        _post_purge.extend(support_paths.purge_ignored_texture_sidecars(cs.folder))
    if _post_purge:
        summary_lines.append("Removed *flipped* / mirror sidecar rasters after batch:")
        summary_lines.extend(["  {}".format(x) for x in _post_purge])

    summary_lines.append("")
    summary_lines.append(
        "Renders OK: {}  Failed: {}  Models skipped: {}".format(
            renders_ok, renders_fail, models_skipped
        )
    )
    summary_lines.append("CSV log: {}".format(csv_log))
    summary_lines.append("Plugin log: {}".format(log_path))

    summary_path = texture_root / "_LifeproofBatchRender_summary.txt"
    full_text = "\n".join(summary_lines)
    try:
        summary_path.write_text(full_text, encoding="utf-8")
    except Exception as ex:
        logutil.append_log(log_path, "summary write failed: {}".format(ex))

    short_lines: List[str] = [
        "Texture root: {}".format(texture_root),
        "Color sets: {} | Models: {}".format(len(color_sets), len(models)),
        "Output: {} x {} {}".format(render_width, render_height, output_ext.upper()),
        "Pipeline: {}".format(pipeline_selection),
        "",
        "Renders OK: {}  Failed: {}".format(renders_ok, renders_fail),
        *(
            ["Models skipped (no render-instructions entry): {}".format(models_skipped)]
            if models_skipped
            else []
        ),
        "",
        "Full summary: {}".format(summary_path),
        "CSV log:      {}".format(csv_log),
        "Plugin log:   {}".format(log_path),
    ]
    short_text = "\n".join(short_lines)

    try:
        uip.set_progress(ins, 100, 100)
    except Exception:
        pass
    fui.messageBox(short_text, "Batch Render")
    logutil.append_log(log_path, "execute_batch done ok={} fail={}".format(renders_ok, renders_fail))
    texture_pipeline.clear_decal_shift_temp_files()
