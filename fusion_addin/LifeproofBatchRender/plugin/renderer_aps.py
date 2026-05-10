from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


TOKEN_URL = "https://developer.api.autodesk.com/authentication/v2/token"


@dataclass
class APSConfig:
    client_id: str
    client_secret: str
    scopes: str = "data:read data:write bucket:create bucket:read"
    # Optional workflow hooks (customer-specific). When empty, APS render path is not available.
    bucket_key: str = ""
    workflow_notes: str = ""


@dataclass
class TokenResult:
    ok: bool
    access_token: str = ""
    expires_in: int = 0
    error: str = ""


def fetch_two_legged_token(cfg: APSConfig) -> TokenResult:
    """
    OAuth two-legged (client credentials). Uses stdlib only (no requests dependency in Fusion).
    """
    if not (cfg.client_id and cfg.client_secret):
        return TokenResult(False, error="Missing APS client_id / client_secret.")
    data = urllib.parse.urlencode(
        {
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "grant_type": "client_credentials",
            "scope": cfg.scopes,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        payload: Dict[str, Any] = json.loads(body)
        token = str(payload.get("access_token") or "")
        if not token:
            return TokenResult(False, error="Token response missing access_token.")
        exp = int(payload.get("expires_in") or 0)
        return TokenResult(True, access_token=token, expires_in=exp)
    except urllib.error.HTTPError as ex:
        try:
            detail = ex.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(ex)
        return TokenResult(False, error="HTTP {}: {}".format(ex.code, detail[:800]))
    except Exception as ex:
        return TokenResult(False, error=str(ex))


@dataclass
class APSRenderOutcome:
    ok: bool
    message: str
    output_bytes: Optional[bytes] = None


def render_placeholder(
    cfg: APSConfig,
    token: str,
    *,
    model_path: Path,
    color_folder: Path,
    view_name: str,
    width: int,
    height: int,
) -> APSRenderOutcome:
    """
    Placeholder for a customer-specific APS render workflow.

    Photoreal batch rendering of arbitrary Fusion designs via APS is not a single public
    "submit PNG" endpoint; teams typically combine OSS + Model Derivative + Design Automation
    or Fusion Industry Cloud workflows. This function validates credentials and returns a
    clear message so the UI can fall back to local viewport capture when unconfigured.
    """
    _ = (token, model_path, color_folder, view_name, width, height)
    if not cfg.bucket_key.strip():
        return APSRenderOutcome(
            False,
            "APS render workflow is not configured (set bucket_key / workflow in aps_config.json). "
            "Use Local (Fusion render) mode, or wire OSS + your render job endpoint here.",
        )
    return APSRenderOutcome(
        False,
        "APS render job submission is not implemented in this build. "
        "Implement OSS upload + your job API in plugin/renderer_aps.py::render_placeholder.",
    )


def load_aps_config(addin_dir: Path) -> APSConfig:
    """
    Reads optional JSON next to the add-in:
      {
        "client_id": "...",
        "client_secret": "...",
        "scopes": "data:read data:write bucket:create bucket:read",
        "bucket_key": "",
        "workflow_notes": ""
      }
    """
    p = addin_dir / "aps_config.json"
    if not p.is_file():
        return APSConfig("", "")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return APSConfig("", "")
    default_scopes = "data:read data:write bucket:create bucket:read"
    return APSConfig(
        client_id=str(raw.get("client_id") or ""),
        client_secret=str(raw.get("client_secret") or ""),
        scopes=str(raw.get("scopes") or default_scopes),
        bucket_key=str(raw.get("bucket_key") or ""),
        workflow_notes=str(raw.get("workflow_notes") or ""),
    )
