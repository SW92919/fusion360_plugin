# Load / validate the client's "Rendering Instructions" JSON and match entries to .f3d files.
# Pure stdlib (no adsk imports) so it can be unit-tested outside Fusion.
#
# Shape (array of entries):
#   [{ "fileName": "Bullnose",
#      "decals": [{"componentPartNumber": "Stair Nose",
#                  "textureSize": {"width": 9, "length": 50, "unit": "in"}}],
#      "renderConfigurations": [{"namedView": "Full Front", "hideBodies": []},
#                               {"namedView": "Full Front", "hideBodies": ["Supplemental Plank"]}] }]
#
# The same namedView may repeat with different hideBodies — each repeat is its own
# render, so every configuration carries a collision-free ``label`` for the 4th
# segment of support_paths.build_output_basename.

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

INCH_TO_CM: float = 2.54
DEFAULT_TEXTURE_UNIT: str = "in"

# textureSize units → centimetres. The client ships "in"; mm/cm are tolerated so a
# future unit change converts instead of silently mis-sizing a decal by 25.4x.
_UNIT_TO_CM: Dict[str, float] = {
    "in": 2.54,
    "inch": 2.54,
    "inches": 2.54,
    '"': 2.54,
    "mm": 0.1,
    "cm": 1.0,
}

# Sanity ceiling for a texture edge, checked AFTER unit conversion (so 1270 mm,
# which is the client's own 50 in, is accepted rather than rejected as absurd).
MAX_TEXTURE_DIMENSION_IN: float = 400.0

PROBLEM_ERROR: str = "error"
PROBLEM_WARN: str = "warn"

# Names travel through Word/PDF, where "-" can become an en/em dash.
_DASH_CHARS: Tuple[str, ...] = ("‐", "‑", "‒", "–", "—", "―", "−")

# Pre-rename stems: ``End Cap - Decal`` → ``End Cap``; ``... - Appearance2`` → ``...``.
_LEGACY_SUFFIX_RE = re.compile(r"\s*-\s*(Decals?|Appearances?)\s*\d*\s*$", re.IGNORECASE)

# Fusion decorates browser names: ``:N`` per occurrence instance, ``(N)`` per copy.
# The client writes the bare part name ("L-Cap") while the browser shows
# ``L-Cap (1):1``, so both suffixes are stripped before matching.
_INSTANCE_SUFFIX_RE = re.compile(r"\s*:\s*\d+\s*$")
_COPY_SUFFIX_RE = re.compile(r"\s*\(\s*\d+\s*\)\s*$")


@dataclass(frozen=True)
class Problem:
    """One validation complaint. Collected, never raised — the batch still runs."""

    level: str  # PROBLEM_ERROR | PROBLEM_WARN
    where: str  # "entry 1 'Bullnose' decals[0]"
    message: str


@dataclass(frozen=True)
class DecalSizeSpec:
    """Absolute decal size for one component, as authored by the client."""

    part_number: str  # verbatim componentPartNumber, e.g. "Stair Nose"
    part_key: str  # normalize_part_key(part_number) → "stair-nose"
    width_in: float  # across the part (client: 9in)
    length_in: float  # along the long axis (client: 50in)
    width_cm: float  # Fusion internal units
    length_cm: float
    unit: str
    # Which colour raster this part takes: decals[0] → _1, decals[1] → _2.
    # Confirmed against Bullnose.f3d, where Stair Nose carries "dusk-cherry - 1"
    # and Supplemental Plank carries "dusk-cherry-2", matching the JSON order.
    slot: int = 1


@dataclass(frozen=True)
class RenderConfiguration:
    """One deliverable image: a named view plus the components hidden for it."""

    index: int  # position in the JSON's renderConfigurations array
    named_view: str
    hide_bodies: Tuple[str, ...]
    label: str  # "" for the un-hidden render; else "no Supplemental Plank"


@dataclass(frozen=True)
class ModelInstructions:
    file_name: str  # verbatim fileName
    match_key: str  # normalize_name_key(file_name)
    decals: Tuple[DecalSizeSpec, ...]
    render_configurations: Tuple[RenderConfiguration, ...]

    @property
    def texture_mode(self) -> str:
        """Non-empty decals[] means this model is driven by the decal pipeline."""
        return "decal" if self.decals else "appearance"


@dataclass(frozen=True)
class RenderInstructions:
    source: Optional[Path]
    models: Tuple[ModelInstructions, ...]
    problems: Tuple[Problem, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not any(p.level == PROBLEM_ERROR for p in self.problems)


def normalize_name_key(name: str) -> str:
    """Canonical compare key: ``Square Nose`` / ``square-nose`` / ``Square_Nose`` → ``square-nose``.

    Keep in sync with texture_pipeline._normalize_product_name_key so keys built
    here are directly comparable with keys built there. NBSP and Unicode dashes are
    folded first because these names are copied out of Word/PDF.
    """
    s = (name or "").strip().replace(" ", " ").lower()
    for ch in _DASH_CHARS:
        s = s.replace(ch, "-")
    return re.sub(r"[\s\-_]+", "-", s).strip("-")


def strip_legacy_mode_suffix(stem: str) -> str:
    """``End Cap - Decal`` → ``End Cap`` (pre-rename files in Resources/Models_old)."""
    return _LEGACY_SUFFIX_RE.sub("", (stem or "").strip()).strip()


def strip_occurrence_suffixes(name: str) -> str:
    """``L-Cap (1):1`` → ``L-Cap``.

    Fusion appends ``:N`` to an occurrence and ``(N)`` to a copied component, so
    the browser shows ``L-Cap (1):1`` where the client's JSON says ``L-Cap``.
    Applied repeatedly because both can stack.
    """
    s = (name or "").strip()
    previous = None
    while previous != s and s:
        previous = s
        s = _INSTANCE_SUFFIX_RE.sub("", s).strip()
        s = _COPY_SUFFIX_RE.sub("", s).strip()
    return s


def normalize_part_key(name: str) -> str:
    """Compare key for a component / occurrence / body name against a JSON part number.

    ``L-Cap (1):1``, ``L-Cap``, ``l_cap`` → ``l-cap``.
    """
    return normalize_name_key(strip_occurrence_suffixes(name))


def model_match_key(model_path: Path) -> str:
    """Match key for a selected .f3d — extension dropped, case/spacing-insensitive."""
    return normalize_name_key(model_path.stem)


def inches_to_cm(value: float) -> float:
    return float(value) * INCH_TO_CM


def _as_clean_str(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.replace(" ", " ")).strip()


def _as_number(value: object) -> Optional[float]:
    """Finite, positive number. Rejects bools (``True`` is an int in Python)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")) or v <= 0.0:
        return None
    return v


def _parse_decal(
    where: str, raw: object, slot: int = 1
) -> Tuple[Optional[DecalSizeSpec], List[Problem]]:
    problems: List[Problem] = []
    if not isinstance(raw, dict):
        return None, [Problem(PROBLEM_ERROR, where, "decal entry is not an object")]

    part = _as_clean_str(raw.get("componentPartNumber"))
    if not part:
        problems.append(Problem(PROBLEM_ERROR, where, "componentPartNumber is missing or empty"))

    size = raw.get("textureSize")
    if not isinstance(size, dict):
        problems.append(Problem(PROBLEM_ERROR, where, "textureSize is missing or not an object"))
        return None, problems

    unit = (_as_clean_str(size.get("unit")) or DEFAULT_TEXTURE_UNIT).lower()
    factor = _UNIT_TO_CM.get(unit)
    if factor is None:
        problems.append(
            Problem(
                PROBLEM_ERROR,
                where,
                "unknown textureSize unit {!r} (expected one of: {})".format(
                    unit, ", ".join(sorted(_UNIT_TO_CM))
                ),
            )
        )
        return None, problems

    width = _as_number(size.get("width"))
    length = _as_number(size.get("length"))
    if width is None:
        problems.append(Problem(PROBLEM_ERROR, where, "textureSize.width must be a positive number"))
    if length is None:
        problems.append(Problem(PROBLEM_ERROR, where, "textureSize.length must be a positive number"))
    if width is None or length is None or not part:
        return None, problems

    width_cm = width * factor
    length_cm = length * factor
    width_in = width_cm / INCH_TO_CM
    length_in = length_cm / INCH_TO_CM
    # Range-check in inches, AFTER conversion, so mm/cm values are judged fairly.
    for axis, inches in (("width", width_in), ("length", length_in)):
        if inches > MAX_TEXTURE_DIMENSION_IN:
            problems.append(
                Problem(
                    PROBLEM_ERROR,
                    where,
                    "textureSize.{} is {:.1f} in — above the {:.0f} in sanity limit".format(
                        axis, inches, MAX_TEXTURE_DIMENSION_IN
                    ),
                )
            )
            return None, problems

    if length_in < width_in:
        problems.append(
            Problem(
                PROBLEM_WARN,
                where,
                "length ({:.2f} in) is shorter than width ({:.2f} in) — 'length' should be "
                "the long axis of the part".format(length_in, width_in),
            )
        )

    return (
        DecalSizeSpec(
            part_number=part,
            part_key=normalize_part_key(part),
            width_in=width_in,
            length_in=length_in,
            width_cm=width_cm,
            length_cm=length_cm,
            unit=unit,
            slot=slot,
        ),
        problems,
    )


def _parse_render_configuration(
    where: str, index: int, raw: object
) -> Tuple[Optional[RenderConfiguration], List[Problem]]:
    if not isinstance(raw, dict):
        return None, [Problem(PROBLEM_ERROR, where, "renderConfiguration is not an object")]

    view = _as_clean_str(raw.get("namedView"))
    if not view:
        return None, [Problem(PROBLEM_ERROR, where, "namedView is missing or empty")]

    problems: List[Problem] = []
    hides: List[str] = []
    raw_hides = raw.get("hideBodies", [])
    if raw_hides is None:
        raw_hides = []
    if not isinstance(raw_hides, (list, tuple)):
        problems.append(Problem(PROBLEM_ERROR, where, "hideBodies must be an array"))
        raw_hides = []
    for item in raw_hides:
        name = _as_clean_str(item)
        if not name:
            problems.append(Problem(PROBLEM_WARN, where, "ignoring an empty hideBodies entry"))
            continue
        if name not in hides:
            hides.append(name)

    return (
        RenderConfiguration(
            index=index,
            named_view=view,
            hide_bodies=tuple(hides),
            label="",  # assigned once the whole list is known
        ),
        problems,
    )


def _label_for(hide_bodies: Sequence[str]) -> str:
    """Filename segment distinguishing renders of the same view. '' = nothing hidden."""
    if not hide_bodies:
        return ""
    return "no " + ", ".join(hide_bodies)


def _assign_config_labels(
    configs: Sequence[RenderConfiguration],
) -> Tuple[Tuple[RenderConfiguration, ...], List[Problem]]:
    """Give every configuration a label unique within its named view.

    Two configs sharing a view AND a hide-list are true duplicates; the second gets
    a ``#N`` suffix so its render does not land on the first one's filename.
    """
    problems: List[Problem] = []
    used: Dict[Tuple[str, str], int] = {}
    out: List[RenderConfiguration] = []
    for cfg in configs:
        base = _label_for(cfg.hide_bodies)
        key = (normalize_name_key(cfg.named_view), normalize_name_key(base))
        seen = used.get(key, 0)
        used[key] = seen + 1
        label = base
        if seen:
            label = "{} #{}".format(base, seen + 1) if base else "#{}".format(seen + 1)
            problems.append(
                Problem(
                    PROBLEM_WARN,
                    "renderConfigurations[{}]".format(cfg.index),
                    "duplicate of an earlier configuration for view {!r}; labelling it {!r} "
                    "so the renders do not overwrite each other".format(cfg.named_view, label),
                )
            )
        out.append(
            RenderConfiguration(
                index=cfg.index,
                named_view=cfg.named_view,
                hide_bodies=cfg.hide_bodies,
                label=label,
            )
        )
    return tuple(out), problems


def _parse_entry(index: int, raw: object) -> Tuple[Optional[ModelInstructions], List[Problem]]:
    where = "entry {}".format(index)
    if not isinstance(raw, dict):
        return None, [Problem(PROBLEM_ERROR, where, "entry is not an object")]

    file_name = _as_clean_str(raw.get("fileName"))
    if not file_name:
        return None, [Problem(PROBLEM_ERROR, where, "fileName is missing or empty")]
    where = "entry {} {!r}".format(index, file_name)

    problems: List[Problem] = []

    decals: List[DecalSizeSpec] = []
    raw_decals = raw.get("decals", []) or []
    if not isinstance(raw_decals, (list, tuple)):
        problems.append(Problem(PROBLEM_ERROR, where, "decals must be an array"))
        raw_decals = []
    seen_parts: Dict[str, str] = {}
    for di, rd in enumerate(raw_decals):
        # Only two colour rasters exist per set (_1 / _2); a third part reuses _1.
        slot = 2 if di == 1 else 1
        if di == 2:
            problems.append(
                Problem(
                    PROBLEM_WARN,
                    where,
                    "more than two decal entries — only _1 and _2 rasters exist, so entries "
                    "after the second reuse the _1 image",
                )
            )
        spec, probs = _parse_decal("{} decals[{}]".format(where, di), rd, slot=slot)
        problems.extend(probs)
        if spec is None:
            continue
        prev = seen_parts.get(spec.part_key)
        if prev is not None:
            problems.append(
                Problem(
                    PROBLEM_WARN,
                    "{} decals[{}]".format(where, di),
                    "componentPartNumber {!r} repeats {!r} — the later size wins".format(
                        spec.part_number, prev
                    ),
                )
            )
            decals = [d for d in decals if d.part_key != spec.part_key]
        seen_parts[spec.part_key] = spec.part_number
        decals.append(spec)

    configs: List[RenderConfiguration] = []
    raw_configs = raw.get("renderConfigurations", []) or []
    if not isinstance(raw_configs, (list, tuple)):
        problems.append(Problem(PROBLEM_ERROR, where, "renderConfigurations must be an array"))
        raw_configs = []
    for ci, rc in enumerate(raw_configs):
        cfg, probs = _parse_render_configuration(
            "{} renderConfigurations[{}]".format(where, ci), ci, rc
        )
        problems.extend(probs)
        if cfg is not None:
            configs.append(cfg)

    if not configs:
        problems.append(
            Problem(PROBLEM_ERROR, where, "no usable renderConfigurations — nothing to render")
        )

    labelled, label_problems = _assign_config_labels(configs)
    problems.extend(label_problems)

    return (
        ModelInstructions(
            file_name=file_name,
            match_key=normalize_name_key(file_name),
            decals=tuple(decals),
            render_configurations=labelled,
        ),
        problems,
    )


def parse_instructions(text: str, source: Optional[Path] = None) -> RenderInstructions:
    """Parse JSON text into validated instructions. Never raises on bad content."""
    where = str(source) if source else "render instructions"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as ex:
        return RenderInstructions(
            source=source,
            models=(),
            problems=(
                Problem(
                    PROBLEM_ERROR,
                    where,
                    "not valid JSON — {} (line {}, column {})".format(ex.msg, ex.lineno, ex.colno),
                ),
            ),
        )

    if isinstance(data, dict):
        # Tolerate a wrapper object, e.g. {"models": [...]}.
        for key in ("models", "entries", "renderInstructions"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return RenderInstructions(
            source=source,
            models=(),
            problems=(
                Problem(PROBLEM_ERROR, where, "expected a JSON array of model entries"),
            ),
        )

    problems: List[Problem] = []
    models: List[ModelInstructions] = []
    by_key: Dict[str, str] = {}
    for i, raw in enumerate(data):
        entry, probs = _parse_entry(i, raw)
        problems.extend(probs)
        if entry is None:
            continue
        prev = by_key.get(entry.match_key)
        if prev is not None:
            problems.append(
                Problem(
                    PROBLEM_ERROR,
                    "entry {} {!r}".format(i, entry.file_name),
                    "duplicate fileName — already defined as {!r}; the first one is used".format(prev),
                )
            )
            continue
        by_key[entry.match_key] = entry.file_name
        models.append(entry)

    if not models:
        problems.append(Problem(PROBLEM_ERROR, where, "no usable model entries found"))

    return RenderInstructions(source=source, models=tuple(models), problems=tuple(problems))


def load_instructions(path: Path) -> RenderInstructions:
    """Read and parse the instructions file. Tolerates a UTF-8 BOM and UTF-16."""
    if not path or not str(path).strip():
        return RenderInstructions(
            source=None,
            models=(),
            problems=(Problem(PROBLEM_ERROR, "render instructions", "no path given"),),
        )
    p = Path(path)
    if not p.is_file():
        return RenderInstructions(
            source=p,
            models=(),
            problems=(Problem(PROBLEM_ERROR, str(p), "file not found"),),
        )
    try:
        raw = p.read_bytes()
    except OSError as ex:
        return RenderInstructions(
            source=p,
            models=(),
            problems=(Problem(PROBLEM_ERROR, str(p), "could not read file — {}".format(ex)),),
        )

    text = None
    for encoding in ("utf-8-sig", "utf-16", "utf-8", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        return RenderInstructions(
            source=p,
            models=(),
            problems=(Problem(PROBLEM_ERROR, str(p), "could not decode file as text"),),
        )
    return parse_instructions(text, source=p)


def find_for_model(instructions: RenderInstructions, model_path: Path) -> Optional[ModelInstructions]:
    """Entry for this .f3d, or None when the model is not listed (skip + flag)."""
    if instructions is None or not instructions.models:
        return None
    key = model_match_key(model_path)
    for entry in instructions.models:
        if entry.match_key == key:
            return entry
    # Pre-rename files (``Bullnose - Decal.f3d``) still match the short fileName.
    legacy = normalize_name_key(strip_legacy_mode_suffix(model_path.stem))
    if legacy and legacy != key:
        for entry in instructions.models:
            if entry.match_key == legacy:
                return entry
    return None


def discover_model_files(
    instructions: RenderInstructions, search_dir: Optional[Path] = None
) -> Tuple[List[Path], List[str]]:
    """Find one .f3d per entry, in the folder the instructions file came from.

    The JSON names models (``"Bullnose"``) but carries no paths, and the client
    ships the .f3d files beside it — so the selection is derivable and does not
    need to be made by hand. Entries whose file is missing are reported, not
    guessed at.

    Returns ``(paths_in_JSON_order, log_lines)``.
    """
    if instructions is None or not instructions.models:
        return [], ["No render-instruction entries to find models for."]
    base = search_dir
    if base is None:
        base = instructions.source.parent if instructions.source else None
    if base is None or not Path(base).is_dir():
        return [], ["Cannot auto-find models: the instructions folder is unknown."]
    base = Path(base)

    by_key: Dict[str, Path] = {}
    for candidate in sorted(base.glob("*.f3d")):
        by_key.setdefault(model_match_key(candidate), candidate)
        legacy = normalize_name_key(strip_legacy_mode_suffix(candidate.stem))
        if legacy:
            by_key.setdefault(legacy, candidate)

    found: List[Path] = []
    missing: List[str] = []
    for entry in instructions.models:
        hit = by_key.get(entry.match_key)
        if hit is None:
            missing.append(entry.file_name)
            continue
        if hit not in found:
            found.append(hit)

    lines = [
        "Auto-selected {} model file(s) from {}".format(len(found), base),
    ]
    lines.extend("  {}".format(p.name) for p in found)
    if missing:
        lines.append(
            "  NOT FOUND next to the instructions file: {}".format(", ".join(missing))
        )
    return found, lines


def decal_size_for_part(entry: ModelInstructions, part_name: str) -> Optional[DecalSizeSpec]:
    """Size authored for this component, matched case/spacing-insensitively."""
    if entry is None or not entry.decals:
        return None
    key = normalize_part_key(part_name)
    if not key:
        return None
    for spec in entry.decals:
        if spec.part_key == key:
            return spec
    return None


def decal_size_map(entry: ModelInstructions) -> Dict[str, DecalSizeSpec]:
    """part_key → size, for callers that resolve many components in a loop."""
    if entry is None:
        return {}
    return {spec.part_key: spec for spec in entry.decals}


def unmatched_model_message(model_path: Path, instructions: RenderInstructions) -> str:
    """One-line reason for the summary + CSV when a model has no entry."""
    known = ", ".join(m.file_name for m in instructions.models) if instructions.models else "(none)"
    return "no '{}' entry in {} — skipped. Listed models: {}".format(
        model_path.stem,
        instructions.source.name if instructions.source else "the render instructions",
        known,
    )


def describe_problems(instructions: RenderInstructions, limit: int = 40) -> List[str]:
    """Summary lines for the batch log."""
    out: List[str] = []
    for p in instructions.problems[:limit]:
        out.append("  {}: {} — {}".format(p.level.upper(), p.where, p.message))
    remaining = len(instructions.problems) - limit
    if remaining > 0:
        out.append("  ... and {} more".format(remaining))
    return out
