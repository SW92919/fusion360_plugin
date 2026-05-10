# Mirror of python_lib/visibility_rules.py — keep logic in sync.

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import FrozenSet, Optional


@dataclass(frozen=True)
class VisibilityDirective:
    hide_in: FrozenSet[str]
    only_show: Optional[FrozenSet[str]]


_VIEW_LIST = re.compile(r"^(show|hide)\s*:\s*(.+)$", re.IGNORECASE)


def parse_description(description: str) -> Optional[VisibilityDirective]:
    """
    Structured rules:
      - "hide:Front,Rear"
      - "show:Front"
      - Single token without spaces -> hide in that exact named view only.

    Free-form multi-word descriptions (no hide:/show:) are handled by visibility_for_description()
    using substring matching: hidden in view V if V appears inside the description text.
    """
    raw = (description or "").strip()
    if not raw:
        return None
    m = _VIEW_LIST.match(raw)
    if m:
        kind, rest = m.group(1).lower(), m.group(2)
        names = frozenset(x.strip() for x in rest.split(",") if x.strip())
        if not names:
            return None
        if kind == "hide":
            return VisibilityDirective(hide_in=names, only_show=None)
        return VisibilityDirective(hide_in=frozenset(), only_show=names)
    if "," in raw or ":" in raw:
        return None
    if " " in raw:
        return None
    return VisibilityDirective(hide_in=frozenset({raw}), only_show=None)


def is_visible_for_view(directive: Optional[VisibilityDirective], named_view: str) -> bool:
    if directive is None:
        return True
    if directive.only_show is not None:
        return named_view in directive.only_show
    return named_view not in directive.hide_in


def visibility_for_description(description: str, named_view: str) -> bool:
    """
    Full rule set for one component/body description and one named view.
    """
    raw = (description or "").strip()
    if not raw:
        return True
    d = parse_description(description)
    if d is not None:
        return is_visible_for_view(d, named_view)
    if named_view and named_view in raw:
        return False
    return True
