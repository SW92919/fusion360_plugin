from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import adsk.core
import adsk.fusion

import image_orient
import render_instructions
import support_paths
import texture_pipeline
import visibility_apply
import viewport_render

from plugin import image_mapper
from plugin import logger as logutil
from plugin import model_handler


def _log_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _code_build_stamp() -> str:
    """Report the RUNNING build, and shout if it is older than the files on disk.

    Fusion keeps one interpreter per session, so editing a .py does nothing until
    the add-in is stopped and started again — re-running the command re-executes
    STALE code. A stamp built only from file mtimes says "fresh" while old code
    runs, which has already cost two debugging rounds. Comparing the mtime
    captured when texture_pipeline was imported against its mtime now is what
    actually detects that.
    """
    try:
        tp_file = Path(texture_pipeline.__file__).resolve()
        loaded = float(getattr(texture_pipeline, "MODULE_IMPORTED_MTIME", 0.0) or 0.0)
        on_disk = tp_file.stat().st_mtime

        newest = 0.0
        newest_name = "?"
        for py in tp_file.parent.rglob("*.py"):
            try:
                mtime = py.stat().st_mtime
            except OSError:
                continue
            if mtime > newest:
                newest, newest_name = mtime, py.name

        def _fmt(ts: float) -> str:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"

        # One second of slack for filesystem timestamp granularity.
        stale = loaded and (on_disk - loaded) > 1.0
        note = ""
        if stale:
            note = (
                "  *** STALE: running code imported from a {} build, but "
                "texture_pipeline.py on disk is {}. Fusion will not reload an edited "
                "file until the add-in is STOPPED and STARTED again (re-running the "
                "command is not enough). Any fix made since then is NOT active. ***"
            ).format(_fmt(loaded), _fmt(on_disk))
        return "running build {} | newest source on disk {} ({}){}".format(
            _fmt(loaded), _fmt(newest), newest_name, note
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
    reporter: Any,
    texture_root: Path,
    models: List[Path],
    render_width: int,
    render_height: int,
    output_ext: str,
    instructions_path: Path,
    render_quality: int,
) -> None:
    """Run one batch. ``reporter`` shows progress on the Batch Render panel.

    It needs ``set_status(text)``, ``set_progress(value, maximum)``,
    ``finish(summary)`` (every render made) and ``fail(text)`` (could not run, or
    some renders / decal updates failed).
    """
    log_path = logutil.default_log_path(texture_root)
    logutil.append_log(log_path, "execute_batch start")

    _execute_batch_inner(
        reporter,
        texture_root,
        models,
        render_width,
        render_height,
        output_ext,
        instructions_path,
        render_quality,
        log_path,
    )


def _execute_batch_inner(
    reporter: Any,
    texture_root: Path,
    models: List[Path],
    render_width: int,
    render_height: int,
    output_ext: str,
    instructions_path: Path,
    render_quality: int,
    log_path: Path,
) -> None:

    try:
        color_sets = support_paths.scan_texture_root(texture_root)
    except Exception as ex:
        reporter.fail("Invalid texture folder:\n{}".format(ex))
        logutil.append_log(log_path, "scan_texture_root failed", ex)
        return
    if not color_sets:
        reporter.fail("No color subfolders found under:\n{}".format(texture_root))
        return
    if not models:
        reporter.fail("No model files selected.")
        return

    # The render instructions now drive views, hides and decal sizes, so a bad file
    # means nothing can render correctly — fail here rather than part-way through.
    instructions = render_instructions.load_instructions(instructions_path)
    if not instructions.ok:
        detail = "\n".join(render_instructions.describe_problems(instructions, limit=12))
        reporter.fail(
            "Render Instructions could not be used:\n\n{}\n\n"
            "Fix the file and run again.".format(detail)
        )
        logutil.append_log(log_path, "render instructions rejected: {}".format(detail))
        return

    # Prepared (rotated / fitted) copies of the colour-set rasters live in a temp
    # cache; start clean so a raster edited since the last run is re-prepared.
    image_orient.clear_cache()

    app = adsk.core.Application.get()
    csv_log = texture_root / "_LifeproofBatchRender_log.csv"
    summary_lines: List[str] = [
        "Texture root: {}".format(texture_root),
        "Color sets: {}".format(len(color_sets)),
        "Models: {} ({})".format(len(models), ", ".join(Path(m).name for m in models)),
        "Output: {} × {} {}".format(render_width, render_height, output_ext.upper()),
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
    # Decals that kept the model's original texture. Renders still succeed in
    # that case, so without this the totals read 'Failed: 0' over a wrong image.
    decal_failures: List[str] = []
    # Render Instructions decal sizes that may not cover their part (bare ends or
    # edges). Renders are still made; these say which ones to look at.
    size_warnings: List[str] = []

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
        try:
            reporter.set_status(msg)
            reporter.set_progress(min(done_steps, total_steps), total_steps)
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

        summary_lines.append("=== {} ===".format(mp.name))

        # Install this model's decal targets. These drive BOTH which components get
        # a decal (replacing the filename-derived guess, which the model rename
        # broke) and the exact physical size each one is given.
        texture_pipeline.set_decal_targets(
            render_instructions.decal_size_map(entry), model_path=mp
        )
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
        else:
            summary_lines.append(
                "  No decals listed in the Render Instructions: the model's own "
                "decals are matched to _1 / _2 by name, else by order"
            )

        doc, err = model_handler.open_model(app, mp)
        if not doc:
            summary_lines.append(err or "Open failed.")
            continue

        design = model_handler.active_design(app)
        if not design:
            summary_lines.append("No active design.")
            continue

        # Render Instructions decal sizes (deliverable 2). Resizing a decal rolls the
        # timeline back and recomputes the model, so it is done first: the geometry
        # settle and the visibility snapshot below then see the finished model.
        for size_line in texture_pipeline.apply_json_decal_sizes(design):
            summary_lines.append("  {}".format(size_line))
        for size_warning in texture_pipeline.pop_decal_size_warnings():
            size_warnings.append("{} | {}".format(mp.name, size_warning))

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

        dn = texture_pipeline.list_decal_names(design)
        summary_lines.append(
            "Template decals ({}): {}".format(len(dn), ", ".join(dn) if dn else "(none)")
        )

        occ_snap, body_snap, mesh_snap, decal_snap = visibility_apply.capture_visibility(root)
        batch_hide_tokens = visibility_apply.collect_batch_hide_persistent_tokens(root)

        zero_texture_color_sets: List[str] = []
        # Hide results are identical for every colour set, so report each
        # configuration once rather than repeating it per colour.
        logged_hides: set = set()

        for cs in color_sets:
            if not cs.slot1:
                summary_lines.append("Skip color '{}': no _1 / first image.".format(cs.folder.name))
                continue

            bump_progress("Textures: {} / {}".format(model_stem, cs.folder.name))

            s1, s2 = image_mapper.effective_texture_slots(design, cs.slot1, cs.slot2)
            n_tex, tex_lines = texture_pipeline.apply_decal_color_set(design, s1, s2)
            for failed_decal in texture_pipeline.pop_decal_swap_failures():
                decal_failures.append(
                    "{} | {} | {}".format(mp.name, cs.folder.name, failed_decal)
                )
            for size_warning in texture_pipeline.pop_decal_size_warnings():
                size_warnings.append("{} | {}".format(mp.name, size_warning))
            if n_tex == 0:
                zero_texture_color_sets.append(cs.folder.name)
            # Emit the texture log lines even when 0 decals were updated — they
            # say which decal was skipped or refused, and why.
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
                hide_lines = _apply_hides()
                hide_key = (view_name, vis_label)
                if hide_lines and hide_key not in logged_hides:
                    logged_hides.add(hide_key)
                    for line in hide_lines:
                        summary_lines.append("  {}".format(line))

                viewport_render.activate_named_view(app, nv)
                viewport_render.apply_named_view_framing(app)
                viewport_render.pump_ui()
                # Hide again once the camera is set: camera updates can bring back
                # hidden Render-proxy meshes.
                _apply_hides()
                viewport_render.pump_ui()
                # Re-apply the designer's exact saved camera right before capture so
                # the visibility toggling above can't nudge the composition.
                viewport_render.activate_named_view(app, nv)
                viewport_render.apply_named_view_framing(app)
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

                bump_progress(
                    "Rendering {} of {}: {}".format(done_steps + 1, total_steps, out_path.name)
                )
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
                bump_progress(
                    "{}: {}".format("Saved" if ok else "Render FAILED", out_path.name)
                )

                visibility_apply.restore_visibility(
                    occ_snap, body_snap, mesh_snap, decal_snap
                )
                viewport_render.pump_ui()

        visibility_apply.restore_visibility(occ_snap, body_snap, mesh_snap, decal_snap)

        if zero_texture_color_sets:
            summary_lines.append(
                "Textures 0 for {} color set(s): {}".format(
                    len(zero_texture_color_sets),
                    ", ".join(zero_texture_color_sets),
                )
            )
            summary_lines.append(
                "  Hint: check the Render Instructions part numbers and the model's "
                "decals (names in SLOT*_DECAL_NAMES in texture_pipeline.py)."
            )

    summary_lines.append("")
    _post_purge: List[str] = []
    for cs in color_sets:
        _post_purge.extend(support_paths.purge_ignored_texture_sidecars(cs.folder))
    if _post_purge:
        summary_lines.append("Removed *flipped* / mirror sidecar rasters after batch:")
        summary_lines.extend(["  {}".format(x) for x in _post_purge])

    summary_lines.append("")
    if size_warnings:
        summary_lines.append(
            "*** {} decal size warning(s) - the Render Instructions size may not "
            "cover these parts fully; check their renders: ***".format(len(size_warnings))
        )
        summary_lines.extend("  {}".format(x) for x in size_warnings)
        summary_lines.append("")
    if decal_failures:
        summary_lines.append(
            "*** {} decal problem(s) - these parts do not show the colour set "
            "correctly (see each line): ***".format(len(decal_failures))
        )
        summary_lines.extend("  {}".format(x) for x in decal_failures)
        summary_lines.append("")
    summary_lines.append(
        "Renders OK: {}  Failed: {}  Models skipped: {}  Decal updates failed: {}  "
        "Size warnings: {}".format(
            renders_ok, renders_fail, models_skipped, len(decal_failures), len(size_warnings)
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

    # The result comes first: the panel's Status box shows the top of this text.
    short_lines: List[str] = [
        "Done. Renders OK: {}  Failed: {}".format(renders_ok, renders_fail),
        *(
            ["Models skipped (no render-instructions entry): {}".format(models_skipped)]
            if models_skipped
            else []
        ),
        *(
            [
                "",
                "WARNING: {} decal problem(s) - those parts do not show the colour "
                "set correctly. See the summary.".format(len(decal_failures)),
            ]
            if decal_failures
            else []
        ),
        *(
            [
                "",
                "NOTE: {} decal size warning(s) - the Render Instructions size may not "
                "cover some parts fully. See the summary.".format(len(size_warnings)),
            ]
            if size_warnings
            else []
        ),
        "",
        "Texture root: {}".format(texture_root),
        "Color sets: {} | Models: {}".format(len(color_sets), len(models)),
        "Output: {} x {} {}".format(render_width, render_height, output_ext.upper()),
        "",
        "Full summary: {}".format(summary_path),
        "CSV log:      {}".format(csv_log),
        "Plugin log:   {}".format(log_path),
    ]
    short_text = "\n".join(short_lines)

    try:
        # The images actually made (views missing from a model are planned but
        # never rendered), and red text when anything failed.
        reporter.set_progress(done_steps, max(1, done_steps))
        if renders_fail or decal_failures or not renders_ok:
            reporter.fail(short_text)
        else:
            reporter.finish(short_text)
    except Exception:
        pass
    logutil.append_log(
        log_path,
        "execute_batch done ok={} fail={} decal_failures={}".format(
            renders_ok, renders_fail, len(decal_failures)
        ),
    )
    # Only now: Fusion may still read a prepared image until the last render.
    image_orient.clear_cache()
