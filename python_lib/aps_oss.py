"""Pure APS/OSS helpers — keep in sync with fusion_addin/LifeproofBatchRender/aps_oss.py."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote


APS_API_ROOT = "https://developer.api.autodesk.com"
TOKEN_URL = APS_API_ROOT + "/authentication/v2/token"
DEFAULT_SCOPES = "data:read data:write bucket:create bucket:read"

VALID_BUCKET_POLICIES = frozenset({"transient", "temporary", "persistent"})
VALID_RENDER_MODES = frozenset(
    {"fusion_raytrace", "fusion_viewport", "automation"}
)


@dataclass
class APSConfig:
    client_id: str
    client_secret: str
    scopes: str = DEFAULT_SCOPES
    bucket_key: str = ""
    bucket_policy: str = "transient"
    auto_create_bucket: bool = True
    render_mode: str = "fusion_viewport"
    render_quality: int = 60
    max_retries: int = 2
    workflow_notes: str = ""


def sanitize_bucket_key(value: str, *, fallback_seed: str = "lifeproof") -> str:
    """APS bucket keys must be lowercase alphanumeric + hyphen, globally unique."""
    raw = (value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9-]", "-", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    if raw:
        return raw[:128]
    digest = hashlib.sha1(fallback_seed.encode("utf-8")).hexdigest()[:10]
    return "lbr-{}-batch".format(digest)


def encode_object_key(object_key: str) -> str:
    # Slashes must be percent-encoded — APS treats {objectKey} as one URL segment.
    # Literal "/" makes the router split the path and return 404 on upload.
    return quote(object_key.replace("\\", "/"), safe="-_.~")


def build_job_prefix(model_stem: str, color_name: str, view_name: str) -> str:
    safe = lambda s: re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "item").strip())[:80]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    uid = uuid.uuid4().hex[:8]
    return "jobs/{stamp}/{uid}/{model}/{color}/{view}".format(
        stamp=stamp,
        uid=uid,
        model=safe(model_stem),
        color=safe(color_name),
        view=safe(view_name),
    )


def build_job_manifest(
    *,
    model_stem: str,
    model_path: Path,
    color_folder: Path,
    color_name: str,
    view_name: str,
    width: int,
    height: int,
    render_mode: str,
    slot_paths: List[Optional[Path]],
    job_prefix: str,
) -> Dict[str, Any]:
    textures = []
    for idx, p in enumerate(slot_paths, start=1):
        if p is None:
            continue
        textures.append(
            {
                "slot": idx,
                "filename": p.name,
                "oss_key": "{}/inputs/{}".format(job_prefix, p.name),
            }
        )
    return {
        "version": 1,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": {
            "stem": model_stem,
            "source_path": str(model_path),
        },
        "color_set": {
            "name": color_name,
            "folder": str(color_folder),
        },
        "view": view_name,
        "render": {
            "mode": render_mode,
            "width": int(width),
            "height": int(height),
        },
        "textures": textures,
        "output_key": "{}/outputs/render.png".format(job_prefix),
    }


def manifest_json(manifest: Dict[str, Any]) -> bytes:
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
