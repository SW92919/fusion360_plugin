from __future__ import annotations

import json
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aps_oss


TOKEN_URL = aps_oss.TOKEN_URL
APS_API_ROOT = aps_oss.APS_API_ROOT
APSConfig = aps_oss.APSConfig

# Ray-trace cap for APS when fusion_raytrace is selected and FORCE_VIEWPORT_CAPTURE
# is off — prevents multi-hour UI freezes on decal-heavy models.
APS_RAYTRACE_TIMEOUT_SEC: float = 120.0


@dataclass
class TokenResult:
    ok: bool
    access_token: str = ""
    expires_in: int = 0
    error: str = ""


@dataclass
class OSSResult:
    ok: bool
    message: str = ""
    data: Optional[bytes] = None


@dataclass
class APSRenderOutcome:
    ok: bool
    message: str
    output_bytes: Optional[bytes] = None
    job_prefix: str = ""
    bucket_key: str = ""


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _http_request(
    method: str,
    url: str,
    token: str = "",
    *,
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 120.0,
) -> Tuple[int, bytes, str]:
    hdrs = dict(headers or {})
    if token:
        hdrs.setdefault("Authorization", "Bearer {}".format(token))
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=timeout) as resp:
            body = resp.read()
            return int(resp.status), body, str(resp.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as ex:
        try:
            detail = ex.read()
        except Exception:
            detail = str(ex).encode("utf-8", errors="replace")
        return int(ex.code), detail, str(ex.headers.get("Content-Type") or "")


def _json_body(status: int, body: bytes) -> Dict[str, Any]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {"_http_status": status}
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            payload.setdefault("_http_status", status)
            return payload
    except Exception:
        pass
    return {"_http_status": status, "raw": text[:2000]}


def fetch_two_legged_token(cfg: APSConfig) -> TokenResult:
    """OAuth two-legged (client credentials). Uses stdlib only (no requests dependency in Fusion)."""
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
    status, body, _ = _http_request(
        "POST",
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=data,
        timeout=60.0,
    )
    if status >= 400:
        return TokenResult(False, error="HTTP {}: {}".format(status, body[:800].decode("utf-8", errors="replace")))
    payload = _json_body(status, body)
    token = str(payload.get("access_token") or "")
    if not token:
        return TokenResult(False, error="Token response missing access_token.")
    exp = int(payload.get("expires_in") or 0)
    return TokenResult(True, access_token=token, expires_in=exp)


def verify_aps_setup(cfg: APSConfig, token: str) -> Tuple[bool, str]:
    """Smoke-test credentials + bucket access before a long batch."""
    bucket_key = resolve_bucket_key(cfg)
    if not bucket_key:
        return False, "APS bucket_key is empty and auto_create_bucket is disabled."
    ensured = ensure_bucket(token, bucket_key, cfg.bucket_policy, cfg.auto_create_bucket)
    if not ensured.ok:
        return False, ensured.message
    return True, "APS ready (bucket='{}', render_mode='{}')".format(
        bucket_key, cfg.render_mode
    )


def resolve_bucket_key(cfg: APSConfig) -> str:
    key = sanitize_bucket_key(cfg.bucket_key, fallback_seed=cfg.client_id or "lifeproof")
    if key:
        return key
    if cfg.auto_create_bucket:
        return sanitize_bucket_key("", fallback_seed=cfg.client_id or "lifeproof")
    return ""


def sanitize_bucket_key(value: str, *, fallback_seed: str = "lifeproof") -> str:
    return aps_oss.sanitize_bucket_key(value, fallback_seed=fallback_seed)


def ensure_bucket(
    token: str,
    bucket_key: str,
    policy: str = "transient",
    auto_create: bool = True,
) -> OSSResult:
    policy_key = policy if policy in aps_oss.VALID_BUCKET_POLICIES else "transient"
    details_url = "{}/oss/v2/buckets/{}/details".format(APS_API_ROOT, bucket_key)
    status, body, _ = _http_request("GET", details_url, token, timeout=60.0)
    if status == 200:
        return OSSResult(True, "Bucket '{}' exists.".format(bucket_key))
    if status != 404:
        return OSSResult(
            False,
            "Bucket check failed (HTTP {}): {}".format(
                status, body[:500].decode("utf-8", errors="replace")
            ),
        )
    if not auto_create:
        return OSSResult(False, "Bucket '{}' not found (auto_create_bucket=false).".format(bucket_key))
    create_url = "{}/oss/v2/buckets".format(APS_API_ROOT)
    payload = json.dumps({"bucketKey": bucket_key, "policyKey": policy_key}).encode("utf-8")
    status, body, _ = _http_request(
        "POST",
        create_url,
        token,
        data=payload,
        headers={"Content-Type": "application/json"},
        timeout=60.0,
    )
    if status in (200, 201, 409):
        return OSSResult(True, "Bucket '{}' ready (policy={}).".format(bucket_key, policy_key))
    return OSSResult(
        False,
        "Create bucket failed (HTTP {}): {}".format(
            status, body[:500].decode("utf-8", errors="replace")
        ),
    )


def upload_bytes(
    token: str,
    bucket_key: str,
    object_key: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
    max_retries: int = 2,
) -> OSSResult:
    encoded = aps_oss.encode_object_key(object_key)
    last_err = ""
    for attempt in range(max(1, int(max_retries) + 1)):
        result = _upload_bytes_once(token, bucket_key, encoded, data, content_type=content_type)
        if result.ok:
            return result
        last_err = result.message
        time.sleep(min(2.0, 0.25 * attempt))
    return OSSResult(False, last_err or "Upload failed.")


def _upload_bytes_once(
    token: str,
    bucket_key: str,
    encoded_object_key: str,
    data: bytes,
    *,
    content_type: str,
) -> OSSResult:
    signed_url = "{}/oss/v2/buckets/{}/objects/{}/signeds3upload".format(
        APS_API_ROOT, bucket_key, encoded_object_key
    )
    status, body, _ = _http_request(
        "GET",
        signed_url,
        token,
        headers={"Accept": "application/json"},
        timeout=60.0,
    )
    if status >= 400:
        detail = body[:400].decode("utf-8", errors="replace")
        return OSSResult(
            False,
            "Signed upload init failed (HTTP {}): {}".format(status, detail),
        )
    payload = _json_body(status, body)
    upload_key = str(payload.get("uploadKey") or "")
    urls = list(payload.get("urls") or [])
    if not urls:
        single_url = payload.get("url")
        if single_url:
            urls = [str(single_url)]
    if not upload_key or not urls:
        return OSSResult(
            False,
            "Signed upload response missing uploadKey/url(s): {}".format(
                body[:300].decode("utf-8", errors="replace")
            ),
        )
    put_status, put_body, _ = _http_request(
        "PUT",
        str(urls[0]),
        data=data,
        headers={"Content-Type": content_type},
        timeout=300.0,
    )
    if put_status >= 400:
        return OSSResult(
            False,
            "S3 PUT failed (HTTP {}): {}".format(
                put_status, put_body[:300].decode("utf-8", errors="replace")
            ),
        )
    complete_url = signed_url
    complete_payload = json.dumps({"uploadKey": upload_key}).encode("utf-8")
    c_status, c_body, _ = _http_request(
        "POST",
        complete_url,
        token,
        data=complete_payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=60.0,
    )
    if c_status >= 400:
        return OSSResult(
            False,
            "Complete upload failed (HTTP {}): {}".format(
                c_status, c_body[:500].decode("utf-8", errors="replace")
            ),
        )
    return OSSResult(True, "Uploaded '{}' ({} bytes).".format(encoded_object_key, len(data)))


def download_bytes(
    token: str,
    bucket_key: str,
    object_key: str,
    *,
    max_retries: int = 2,
) -> OSSResult:
    encoded = aps_oss.encode_object_key(object_key)
    signed_url = "{}/oss/v2/buckets/{}/objects/{}/signeds3download".format(
        APS_API_ROOT, bucket_key, encoded
    )
    last_err = ""
    for attempt in range(max(1, int(max_retries) + 1)):
        status, body, _ = _http_request(
            "GET",
            signed_url,
            token,
            headers={"Accept": "application/json"},
            timeout=60.0,
        )
        if status >= 400:
            last_err = "Signed download failed (HTTP {}): {}".format(
                status, body[:300].decode("utf-8", errors="replace")
            )
            time.sleep(min(2.0, 0.25 * attempt))
            continue
        payload = _json_body(status, body)
        url = str(payload.get("url") or "")
        if not url:
            last_err = "Signed download response missing url."
            continue
        d_status, d_body, _ = _http_request("GET", url, timeout=300.0)
        if d_status >= 400:
            last_err = "Download GET failed (HTTP {}).".format(d_status)
            continue
        return OSSResult(True, "Downloaded '{}'.".format(object_key), data=d_body)
    return OSSResult(False, last_err or "Download failed.")


def upload_job_package(
    cfg: APSConfig,
    token: str,
    *,
    bucket_key: str,
    job_prefix: str,
    manifest: Dict[str, Any],
    slot_paths: List[Optional[Path]],
) -> OSSResult:
    manifest_key = "{}/job.json".format(job_prefix)
    up = upload_bytes(
        token,
        bucket_key,
        manifest_key,
        aps_oss.manifest_json(manifest),
        content_type="application/json",
        max_retries=cfg.max_retries,
    )
    if not up.ok:
        return up
    for p in slot_paths:
        if p is None or not p.is_file():
            continue
        try:
            blob = p.read_bytes()
        except OSError as ex:
            return OSSResult(False, "Read texture failed ({}): {}".format(p.name, ex))
        tex_key = "{}/inputs/{}".format(job_prefix, p.name)
        tex_up = upload_bytes(
            token,
            bucket_key,
            tex_key,
            blob,
            content_type="application/octet-stream",
            max_retries=cfg.max_retries,
        )
        if not tex_up.ok:
            return tex_up
    return OSSResult(True, "Job package uploaded to '{}'.".format(job_prefix))


def _render_with_fusion(
    design: Any,
    app: Any,
    out_path: Path,
    width: int,
    height: int,
    *,
    render_mode: str,
    render_quality: int,
) -> bool:
    import viewport_render

    path = str(out_path.resolve())
    force_viewport = bool(
        getattr(viewport_render, "FORCE_VIEWPORT_CAPTURE", False)
    )
    if render_mode == "fusion_viewport" or force_viewport:
        return bool(viewport_render.save_viewport_image(app, path, width, height))
    if viewport_render.save_fusion_local_render(
        design,
        app,
        path,
        width,
        height,
        render_quality=render_quality,
        timeout_sec=APS_RAYTRACE_TIMEOUT_SEC,
    ):
        return True
    return bool(viewport_render.save_viewport_image(app, path, width, height))


def submit_render_job(
    cfg: APSConfig,
    token: str,
    *,
    design: Any,
    app: Any,
    model_path: Path,
    color_folder: Path,
    color_name: str,
    view_name: str,
    width: int,
    height: int,
    model_stem: str,
    slot_paths: List[Optional[Path]],
) -> APSRenderOutcome:
    """
    APS batch render for one (model × color × view).

    Hybrid workflow (default ``fusion_raytrace``):
      1. Ensure OSS bucket exists
      2. Upload job manifest + source textures to APS OSS
      3. Ray-trace (or viewport-capture) locally in Fusion with textures already applied
      4. Upload finished PNG back to OSS and return bytes to the caller

    Full cloud-only rendering requires Fusion Automation API (``render_mode=automation``)
    and additional customer setup (nickname, activity id, Fusion Team hub).
    """
    if not (cfg.client_id and cfg.client_secret):
        return APSRenderOutcome(False, "Missing APS client_id / client_secret in aps_config.json.")
    if not token:
        return APSRenderOutcome(False, "APS access token is empty.")

    render_mode = (cfg.render_mode or "fusion_viewport").strip().lower()
    if render_mode not in aps_oss.VALID_RENDER_MODES:
        render_mode = "fusion_viewport"
    if render_mode == "automation":
        return APSRenderOutcome(
            False,
            "render_mode=automation requires Fusion Automation API setup "
            "(nickname, activity id, Fusion Team hub). Use fusion_raytrace for now.",
        )

    bucket_key = resolve_bucket_key(cfg)
    if not bucket_key:
        return APSRenderOutcome(
            False,
            "Set bucket_key in aps_config.json or enable auto_create_bucket.",
        )

    ensured = ensure_bucket(token, bucket_key, cfg.bucket_policy, cfg.auto_create_bucket)
    if not ensured.ok:
        return APSRenderOutcome(False, ensured.message, bucket_key=bucket_key)

    job_prefix = aps_oss.build_job_prefix(model_stem, color_name, view_name)
    manifest = aps_oss.build_job_manifest(
        model_stem=model_stem,
        model_path=model_path,
        color_folder=color_folder,
        color_name=color_name,
        view_name=view_name,
        width=width,
        height=height,
        render_mode=render_mode,
        slot_paths=slot_paths,
        job_prefix=job_prefix,
    )

    uploaded = upload_job_package(
        cfg,
        token,
        bucket_key=bucket_key,
        job_prefix=job_prefix,
        manifest=manifest,
        slot_paths=slot_paths,
    )
    if not uploaded.ok:
        return APSRenderOutcome(False, uploaded.message, job_prefix=job_prefix, bucket_key=bucket_key)

    suffix = ".png"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        rendered = _render_with_fusion(
            design,
            app,
            temp_path,
            width,
            height,
            render_mode=render_mode,
            render_quality=cfg.render_quality,
        )
        if not rendered or not temp_path.is_file() or temp_path.stat().st_size == 0:
            if render_mode == "fusion_raytrace":
                rendered = _render_with_fusion(
                    design,
                    app,
                    temp_path,
                    width,
                    height,
                    render_mode="fusion_viewport",
                    render_quality=cfg.render_quality,
                )
            if not rendered or not temp_path.is_file() or temp_path.stat().st_size == 0:
                return APSRenderOutcome(
                    False,
                    "Fusion render produced no image (mode={}).".format(render_mode),
                    job_prefix=job_prefix,
                    bucket_key=bucket_key,
                )
        output_bytes = temp_path.read_bytes()
    finally:
        try:
            temp_path.unlink()
        except (OSError, FileNotFoundError):
            pass

    # Per SOW, the rendered image is saved to the local color folder by the
    # controller (caller writes ``output_bytes``). The OSS bucket only holds the
    # job inputs (manifest + textures) — we do NOT upload the rendered output.
    return APSRenderOutcome(
        True,
        "APS job complete (inputs uploaded to oss://{}/{}; image saved locally).".format(
            bucket_key, job_prefix
        ),
        output_bytes=output_bytes,
        job_prefix=job_prefix,
        bucket_key=bucket_key,
    )


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
    """Backward-compatible entry point when design/app are not supplied."""
    _ = (model_path, color_folder, view_name, width, height)
    if not (cfg.client_id and cfg.client_secret):
        return APSRenderOutcome(False, "Missing APS client_id / client_secret.")
    if not token:
        return APSRenderOutcome(False, "APS token missing.")
    bucket_key = resolve_bucket_key(cfg)
    if not bucket_key:
        return APSRenderOutcome(
            False,
            "APS render workflow is not configured (set bucket_key in aps_config.json).",
        )
    return APSRenderOutcome(
        False,
        "APS render requires design context. Update the add-in — call submit_render_job instead.",
    )


def load_aps_config(addin_dir: Path) -> APSConfig:
    """
    Reads optional JSON next to the add-in:
      {
        "client_id": "...",
        "client_secret": "...",
        "scopes": "data:read data:write bucket:create bucket:read",
        "bucket_key": "my-unique-bucket",
        "bucket_policy": "transient",
        "auto_create_bucket": true,
        "render_mode": "fusion_viewport",
        "render_quality": 90,
        "max_retries": 2,
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
    policy = str(raw.get("bucket_policy") or "transient").strip().lower()
    if policy not in aps_oss.VALID_BUCKET_POLICIES:
        policy = "transient"
    render_mode = str(raw.get("render_mode") or "fusion_viewport").strip().lower()
    if render_mode not in aps_oss.VALID_RENDER_MODES:
        render_mode = "fusion_viewport"
    try:
        render_quality = int(raw.get("render_quality") or 90)
    except (TypeError, ValueError):
        render_quality = 90
    try:
        max_retries = int(raw.get("max_retries") or 2)
    except (TypeError, ValueError):
        max_retries = 2
    return APSConfig(
        client_id=str(raw.get("client_id") or ""),
        client_secret=str(raw.get("client_secret") or ""),
        scopes=str(raw.get("scopes") or aps_oss.DEFAULT_SCOPES),
        bucket_key=str(raw.get("bucket_key") or ""),
        bucket_policy=policy,
        auto_create_bucket=bool(raw.get("auto_create_bucket", True)),
        render_mode=render_mode,
        render_quality=max(25, min(100, render_quality)),
        max_retries=max(0, min(5, max_retries)),
        workflow_notes=str(raw.get("workflow_notes") or ""),
    )
