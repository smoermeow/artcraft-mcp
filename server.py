#!/usr/bin/env python3
"""MCP server for ArtCraft desktop/app subscription workflows.

This is intentionally NOT a BYO-provider wrapper. It targets the ArtCraft
subscription/Storyteller path first, while exposing safe read-only local app
inspection and task/result access.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("artcraft")

DEFAULT_ROOTS = [Path.home() / "ArtCraft", Path.home() / "Artcraft"]
TASK_DB_NAME = "tasks_v7.sqlite"
ARTCRAFT_API = os.environ.get("ARTCRAFT_API_BASE", "https://api.storyteller.ai")
USER_AGENT = "ArtCraft-MCP/0.1 (local automation)"
STORYTELLER_API_HOST = "api.storyteller.ai"

# Curated Seedance 2.0 video model registry (live identifiers from
# GET /v1/omni_gen/models/video, model_creator="bytedance"). The `bp` (Plus)
# and `bpu` (Plus Ultra) tiers are ByteDance's real models and are less
# censored than the base Seedance 2.0 on ArtCraft. `fast`/`mini` variants are
# lighter/cheaper siblings of the same tier. `seedance_2p0_ultra` does NOT
# exist on the API; the real top tier is `seedance_2p0_bpu` (Plus Ultra).
SEEDANCE_2P0_MODELS: dict[str, dict[str, Any]] = {
    "seedance_2p0": {
        "model": "seedance_2p0",
        "full_name": "Seedance 2.0",
        "tier": "base",
        "is_bytedance_real_model": False,
        "resolution_ceiling": "four_k",
    },
    "seedance_2p0_fast": {
        "model": "seedance_2p0_fast",
        "full_name": "Seedance 2.0 Fast",
        "tier": "base",
        "is_bytedance_real_model": False,
        "resolution_ceiling": "seven_twenty_p",
    },
    "seedance_2p0_bp": {
        "model": "seedance_2p0_bp",
        "full_name": "Seedance 2.0 Plus",
        "tier": "plus",
        "is_bytedance_real_model": True,
        "resolution_ceiling": "four_k",
    },
    "seedance_2p0_bp_fast": {
        "model": "seedance_2p0_bp_fast",
        "full_name": "Seedance 2.0 Plus Fast",
        "tier": "plus",
        "is_bytedance_real_model": True,
        "resolution_ceiling": "seven_twenty_p",
    },
    "seedance_2p0_bpu": {
        "model": "seedance_2p0_bpu",
        "full_name": "Seedance 2.0 Plus Ultra",
        "tier": "plus_ultra",
        "is_bytedance_real_model": True,
        "resolution_ceiling": "four_k",
    },
    "seedance_2p0_bpu_fast": {
        "model": "seedance_2p0_bpu_fast",
        "full_name": "Seedance 2.0 Plus Ultra Fast",
        "tier": "plus_ultra",
        "is_bytedance_real_model": True,
        "resolution_ceiling": "seven_twenty_p",
    },
    "seedance_2p0_mini": {
        "model": "seedance_2p0_mini",
        "full_name": "Seedance 2.0 Mini",
        "tier": "base",
        "is_bytedance_real_model": False,
        "resolution_ceiling": "seven_twenty_p",
    },
    "seedance_2p0_bp_mini": {
        "model": "seedance_2p0_bp_mini",
        "full_name": "Seedance 2.0 Plus Mini",
        "tier": "plus",
        "is_bytedance_real_model": True,
        "resolution_ceiling": "seven_twenty_p",
    },
    "seedance_2p0_bpu_mini": {
        "model": "seedance_2p0_bpu_mini",
        "full_name": "Seedance 2.0 Plus Ultra Mini",
        "tier": "plus_ultra",
        "is_bytedance_real_model": True,
        "resolution_ceiling": "seven_twenty_p",
    },
}


def _validate_seedance_model(model: str) -> Optional[str]:
    """Accept a known Seedance 2.0 variant or any other (non-Seedance) video model.

    Returns the model string when acceptable, or None when the caller passed a
    Seedance-2.0-shaped id that is not in the registry (e.g. a typo like
    ``seedance_2p0_ultra``), so the caller can reject it with a helpful hint.
    """
    if not model:
        return None
    if model.startswith("seedance_2p0") and model not in SEEDANCE_2P0_MODELS:
        return None
    return model


class ArtCraftHTTPError(RuntimeError):
    def __init__(self, code: int):
        super().__init__(f"ArtCraft API HTTP {code}")
        self.code = code


def _cookie_domain_matches(
    domain: Any,
    host: str = STORYTELLER_API_HOST,
) -> bool:
    if not isinstance(domain, dict):
        return False
    normalized_host = host.lower()
    host_only = domain.get("HostOnly")
    if isinstance(host_only, str):
        return host_only.lower() == normalized_host
    suffix = domain.get("Suffix")
    if isinstance(suffix, str):
        normalized_suffix = suffix.lower().lstrip(".")
        return normalized_host == normalized_suffix or normalized_host.endswith(f".{normalized_suffix}")
    return False


def _parse_cookie_name_value(raw_cookie: Any) -> Optional[tuple[str, str]]:
    if not isinstance(raw_cookie, str):
        return None
    first_part = raw_cookie.split(";", 1)[0]
    if "=" not in first_part:
        return None
    name, value = first_part.split("=", 1)
    if name not in ("visitor", "session") or not value:
        return None
    return name, value


# Friendly resolution alias -> API serde variant.
RESOLUTION_ALIASES = {
    "1k": "one_k",
    "2k": "two_k",
    "3k": "three_k",
    "4k": "four_k",
    "480p": "four_eighty_p",
    "720p": "seven_twenty_p",
    "1080p": "ten_eighty_p",
}
DEFAULT_IMAGE_POLL_INTERVAL_SECONDS = 5
DEFAULT_IMAGE_WAIT_TIMEOUT_SECONDS = 240
DEFAULT_VIDEO_POLL_INTERVAL_SECONDS = 20
DEFAULT_VIDEO_WAIT_TIMEOUT_SECONDS = 3600
ASPECT_RATIO_ALIASES = {
    "1:1": "square",
    "3:2": "wide_three_by_two",
    "4:3": "wide_four_by_three",
    "5:4": "wide_five_by_four",
    "16:9": "wide_sixteen_by_nine",
    "21:9": "wide_twenty_one_by_nine",
    "2:3": "tall_two_by_three",
    "3:4": "tall_three_by_four",
    "9:16": "tall_nine_by_sixteen",
    "auto": "auto",
}


def _resolve_aspect_ratio(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return ASPECT_RATIO_ALIASES.get(value, value)


def _tool_error(
    error_code: str,
    message: str,
    remediation: str,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "remediation": remediation,
    }
    if details is not None:
        response["details"] = details
    return response


def _safe_api_error(error: Exception) -> dict[str, Any]:
    status = getattr(error, "code", None)
    if status in (401, 403):
        return _tool_error(
            "session_expired",
            "ArtCraft rejected the saved subscription session.",
            "Open ArtCraft, sign in again, then retry.",
            {"http_status": status},
        )
    return _tool_error(
        "backend_unavailable",
        "The ArtCraft backend request failed.",
        "Retry shortly. If it persists, check whether ArtCraft is also failing.",
        {"http_status": status} if status is not None else None,
    )


def _root(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("ARTCRAFT_DATA_DIR")
    if env:
        return Path(env).expanduser()
    for p in DEFAULT_ROOTS:
        if p.exists():
            return p
    return DEFAULT_ROOTS[0]


def _db_path(root: Optional[str] = None) -> Path:
    return _root(root) / "state" / TASK_DB_NAME


def _credentials_dir(root: Optional[str] = None) -> Path:
    return _root(root) / "credentials"


def _tauri_cookie_store() -> Path:
    # ArtCraft 0.x stores Storyteller/ArtCraft session cookies in the Tauri HTTP
    # plugin cookie store, NOT in ~/ArtCraft/credentials/artcraft_*.txt.
    env = os.environ.get("ARTCRAFT_COOKIE_STORE")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Caches" / "ai.artcraft.app" / ".cookies"


def _storyteller_cookie_header(root: Optional[str] = None) -> Optional[str]:
    # Matches ArtCraft's StorytellerCredentialSet::maybe_as_cookie_header().
    # Source: cookie names are "visitor" and "session" at api.storyteller.ai.
    # Do not expose these values in tool results.
    cstore = _tauri_cookie_store()
    pairs_by_name: dict[str, str] = {}
    if cstore.exists():
        try:
            data = json.loads(cstore.read_text())
            for entry in data:
                if not isinstance(entry, dict) or not _cookie_domain_matches(entry.get("domain")):
                    continue
                parsed = _parse_cookie_name_value(entry.get("raw_cookie"))
                if parsed:
                    name, value = parsed
                    if name in pairs_by_name:
                        continue
                    pairs_by_name[name] = f"{name}={value}"
        except Exception:
            pass
    pairs = [pairs_by_name[name] for name in ("visitor", "session") if name in pairs_by_name]
    if pairs:
        return "; ".join(pairs)
    # Fallback: legacy txt files if present.
    cdir = _credentials_dir(root)
    for name in ("artcraft_avt.txt", "artcraft_session.txt"):
        f = cdir / name
        if f.exists() and f.stat().st_size > 0:
            pairs.append(f.read_text().strip())
    return "; ".join(pairs) if pairs else None


def _safe_task_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    # These are not secrets, but keep output focused.
    return d


def _connect_tasks(root: Optional[str] = None) -> sqlite3.Connection:
    path = _db_path(root)
    if not path.exists():
        raise FileNotFoundError(f"ArtCraft task DB not found at {path}")
    con = sqlite3.connect(f"file:{path}?mode=rw", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _http_json(method: str, path: str, body: Optional[dict[str, Any]] = None, cookie: Optional[str] = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ARTCRAFT_API + path,
        data=data,
        method=method,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            **({"Cookie": cookie} if cookie else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        # Preserve only the status code. Response bodies can contain sensitive
        # backend details and must not flow into MCP errors or logs.
        raise ArtCraftHTTPError(error.code) from None


def _list_subscription_jobs(
    root: Optional[str] = None,
    include_states: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List subscription jobs; this is not the BYO-provider Omni API."""
    cookie = _storyteller_cookie_header(root)
    if not cookie:
        raise RuntimeError("Missing ArtCraft subscription cookies")
    path = "/v1/jobs/session"
    if include_states:
        path += "?" + urllib.parse.urlencode({"include_states": include_states})
    response = _http_json("GET", path, cookie=cookie)
    if not response.get("success"):
        raise RuntimeError(f"ArtCraft subscription jobs request failed: {response}")
    jobs = response.get("jobs")
    return jobs if isinstance(jobs, list) else []


def _resolve_provider_job_id(
    task_or_job_id: str,
    root: Optional[str] = None,
) -> tuple[str, Optional[dict[str, Any]]]:
    """Accept either a local task_ id or a provider jinf_ job token."""
    if task_or_job_id.startswith("jinf_"):
        return task_or_job_id, None
    with _connect_tasks(root) as con:
        row = con.execute("select * from tasks where id = ?", (task_or_job_id,)).fetchone()
    if not row:
        raise KeyError(task_or_job_id)
    local = dict(row)
    provider_job_id = local.get("provider_job_id")
    if not provider_job_id:
        raise RuntimeError(f"Local task {task_or_job_id} has no provider_job_id")
    return str(provider_job_id), local


def _subscription_job_result(
    job: dict[str, Any],
    local_task_id: Optional[str] = None,
) -> dict[str, Any]:
    status_info = job.get("status") or {}
    status = status_info.get("status")
    result = job.get("maybe_result") or {}
    links = result.get("media_links") or {}
    request = job.get("request") or {}
    ready = status == "complete_success" and bool(result)
    response: dict[str, Any] = {
        "found": True,
        "ready": ready,
        "source": "subscription_api",
        "local_task_id": local_task_id,
        "provider_job_id": job.get("job_token"),
        "task_status": status,
        "model_type": request.get("maybe_model_type"),
        "inference_category": request.get("inference_category"),
    }
    if ready:
        inference_category = request.get("inference_category") or ""
        media_class = (
            "video" if inference_category == "video_generation"
            else "image" if inference_category == "image_generation"
            else result.get("entity_type")
        )
        response.update({
            "cdn_url": links.get("cdn_url"),
            "media_token": result.get("entity_token"),
            "media_class": media_class,
            "media_entity_type": result.get("entity_type"),
            "batch_token": result.get("maybe_batch_token"),
            "completed_at": result.get("maybe_successfully_completed_at"),
            "video_previews": links.get("maybe_video_previews"),
        })
    else:
        response.update({
            "progress_percentage": status_info.get("progress_percentage"),
            "on_failure_type": status_info.get("maybe_failure_category"),
            "on_failure_message": status_info.get("maybe_failure_message"),
        })
    return response


def _list_batch_outputs(
    batch_token: str,
    root: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Resolve every media file in an ArtCraft batch, regardless of media class."""
    cookie = _storyteller_cookie_header(root)
    if not cookie:
        raise RuntimeError("Missing ArtCraft subscription cookies")
    response = _http_json(
        "GET",
        f"/v1/media_files/batch_gen_redux/{urllib.parse.quote(batch_token, safe='')}",
        cookie=cookie,
    )
    if not response.get("success"):
        raise RuntimeError(f"ArtCraft batch media request failed: {response}")
    media_files = response.get("media_files")
    if not isinstance(media_files, list):
        return []
    outputs: list[dict[str, Any]] = []
    for media_file in media_files:
        links = media_file.get("media_links") or {}
        outputs.append({
            "media_token": media_file.get("token"),
            "media_class": media_file.get("media_class"),
            "media_type": media_file.get("media_type"),
            "cdn_url": links.get("cdn_url"),
            "thumbnail_url_template": links.get("maybe_thumbnail_template"),
            "video_previews": links.get("maybe_video_previews"),
            "created_at": media_file.get("created_at"),
        })
    return outputs


def _expand_subscription_outputs(
    response: dict[str, Any],
    root: Optional[str] = None,
) -> dict[str, Any]:
    """Attach a uniform outputs array, expanding a batch token when present."""
    if not response.get("ready"):
        return response
    primary = {
        "media_token": response.get("media_token"),
        "media_class": response.get("media_class"),
        "media_type": None,
        "cdn_url": response.get("cdn_url"),
        "thumbnail_url_template": None,
        "video_previews": response.get("video_previews"),
        "created_at": response.get("completed_at"),
    }
    outputs = [primary]
    batch_token = response.get("batch_token")
    if batch_token:
        try:
            batch_outputs = _list_batch_outputs(str(batch_token), root)
            if batch_outputs:
                outputs = batch_outputs
        except Exception as error:
            response["batch_expand_error"] = str(error)
    response["outputs"] = outputs
    response["output_count"] = len(outputs)
    return response


def _get_subscription_job_result(
    task_or_job_id: str,
    root: Optional[str] = None,
) -> dict[str, Any]:
    try:
        provider_job_id, local = _resolve_provider_job_id(task_or_job_id, root)
    except KeyError:
        return {"found": False, "task_id": task_or_job_id}
    for job in _list_subscription_jobs(root):
        if job.get("job_token") == provider_job_id:
            response = _subscription_job_result(job, local.get("id") if local else None)
            return _expand_subscription_outputs(response, root)
    return {
        "found": False,
        "task_id": task_or_job_id,
        "provider_job_id": provider_job_id,
        "source": "subscription_api",
    }


def _insert_enqueued_task(
    *,
    root: Optional[str],
    task_type: str,
    model_type: Optional[str],
    provider_job_id: str,
    frontend_caller: str = "mcp",
) -> str:
    task_id = "task_" + uuid.uuid4().hex
    now = int(time.time())
    with _connect_tasks(root) as con:
        con.execute(
            """
            insert into tasks (
                id, task_status, task_type, model_type, provider, provider_job_id,
                frontend_caller, created_at, updated_at
            ) values (?, 'pending', ?, ?, 'artcraft', ?, ?, ?, ?)
            """,
            (task_id, task_type, model_type, provider_job_id, frontend_caller, now, now),
        )
        con.commit()
    return task_id


def _is_artcraft_installed() -> bool:
    return Path("/Applications/ArtCraft.app").exists() or _tauri_cookie_store().parent.exists()


@mcp.tool()
def artcraft_check_connection(root: Optional[str] = None) -> dict[str, Any]:
    """Check ArtCraft installation, sign-in state, and subscription session validity."""
    installed = _is_artcraft_installed()
    cookie = _storyteller_cookie_header(root)
    if not installed:
        return {
            "status": "not_installed",
            "desktop_app_installed": False,
            "signed_in": False,
            "session_valid": None,
            "error_code": "artcraft_not_installed",
            "remediation": "Install and open ArtCraft, then sign in.",
        }
    if not cookie:
        return {
            "status": "not_logged_in",
            "desktop_app_installed": True,
            "signed_in": False,
            "session_valid": None,
            "error_code": "not_logged_in",
            "remediation": "Open ArtCraft, sign in, then retry.",
        }
    try:
        credits = _http_json("GET", "/v1/credits/namespace/artcraft", cookie=cookie)
    except Exception as error:
        failure = _safe_api_error(error)
        invalid = failure["error_code"] == "session_expired"
        return {
            "status": "session_invalid" if invalid else "error",
            "desktop_app_installed": True,
            "signed_in": True,
            "session_valid": False if invalid else None,
            "error_code": failure["error_code"],
            "remediation": failure["remediation"],
        }
    if not credits.get("success"):
        return {
            "status": "error",
            "desktop_app_installed": True,
            "signed_in": True,
            "session_valid": None,
            "error_code": "backend_unavailable",
            "remediation": "Retry shortly. If it persists, check ArtCraft itself.",
        }
    return {
        "status": "ok",
        "desktop_app_installed": True,
        "signed_in": True,
        "session_valid": True,
        "error_code": None,
        "remediation": None,
    }


@mcp.tool()
def artcraft_detect_installation(root: Optional[str] = None) -> dict[str, Any]:
    """Detect local ArtCraft app data, task DB, app bundle, and running process."""
    r = _root(root)
    db = _db_path(str(r))
    cdir = _credentials_dir(str(r))
    app = Path("/Applications/ArtCraft.app")
    return {
        "data_root": str(r),
        "data_root_exists": r.exists(),
        "task_db": str(db),
        "task_db_exists": db.exists(),
        "credentials_dir": str(cdir),
        "credentials_dir_exists": cdir.exists(),
        "app_bundle": str(app),
        "app_bundle_exists": app.exists(),
        "api_base": ARTCRAFT_API,
    }


@mcp.tool()
def artcraft_credential_status(root: Optional[str] = None) -> dict[str, Any]:
    """Report which ArtCraft credential files exist without revealing secret values."""
    cdir = _credentials_dir(root)
    cstore = _tauri_cookie_store()
    cookie_header = _storyteller_cookie_header(root)
    files = [
        "artcraft_avt.txt",
        "artcraft_session.txt",
        "fal_api_key.txt",
        "grok_state.json",
        "grok_cookies.txt",
        "sora_cookies.txt",
        "sora_bearer_token.txt",
        "sora_sentinel.txt",
        "worldlabs_bearer.txt",
        "worldlabs_refresh.txt",
    ]
    return {
        "credentials_dir": str(cdir),
        "tauri_cookie_store": str(cstore),
        "tauri_cookie_store_exists": cstore.exists(),
        "credentials": {
            name: {"exists": (cdir / name).exists(), "size": (cdir / name).stat().st_size if (cdir / name).exists() else 0}
            for name in files
        },
        "has_artcraft_subscription_cookies": cookie_header is not None,
    }


@mcp.tool()
def artcraft_get_user_status(root: Optional[str] = None) -> dict[str, Any]:
    """Return the signed-in user's ArtCraft credits and subscription summary."""
    cookie = _storyteller_cookie_header(root)
    if not cookie:
        return _tool_error(
            "not_logged_in",
            "No ArtCraft subscription session was found.",
            "Open ArtCraft, sign in, then retry.",
        )
    try:
        credits = _http_json("GET", "/v1/credits/namespace/artcraft", cookie=cookie)
        subscription_response = _http_json(
            "GET",
            "/v1/subscriptions/namespace/artcraft",
            cookie=cookie,
        )
    except Exception as error:
        return _safe_api_error(error)
    if not credits.get("success") or not subscription_response.get("success"):
        return _tool_error(
            "backend_unavailable",
            "ArtCraft did not return a valid user status response.",
            "Retry shortly. If it persists, check ArtCraft itself.",
        )
    active = subscription_response.get("active_subscription")
    subscription = None
    if isinstance(active, dict):
        subscription = {
            "product_slug": active.get("product_slug"),
            "next_bill_at": active.get("next_bill_at"),
            "subscription_end_at": active.get("subscription_end_at"),
        }
    return {
        "ok": True,
        "credits": {
            "free": credits.get("free_credits"),
            "monthly": credits.get("monthly_credits"),
            "banked": credits.get("banked_credits"),
            "total": credits.get("sum_total_credits"),
        },
        "subscription": subscription,
    }


@mcp.tool()
def artcraft_list_tasks(limit: int = 20, status: Optional[str] = None, root: Optional[str] = None) -> dict[str, Any]:
    """List local ArtCraft task queue rows from tasks_v7.sqlite."""
    limit = max(1, min(int(limit), 100))
    sql = "select * from tasks"
    params: list[Any] = []
    if status:
        sql += " where task_status = ?"
        params.append(status)
    sql += " order by created_at desc limit ?"
    params.append(limit)
    with _connect_tasks(root) as con:
        rows = [_safe_task_row(r) for r in con.execute(sql, params).fetchall()]
    return {"task_db": str(_db_path(root)), "tasks": rows}


@mcp.tool()
def artcraft_get_task(task_id: str, root: Optional[str] = None) -> dict[str, Any]:
    """Get one local ArtCraft task by task id."""
    with _connect_tasks(root) as con:
        row = con.execute("select * from tasks where id = ?", (task_id,)).fetchone()
    if not row:
        return {"found": False, "task_id": task_id}
    return {"found": True, "task": _safe_task_row(row)}


@mcp.tool()
def artcraft_list_subscription_jobs(
    limit: int = 20,
    include_states: Optional[str] = None,
    root: Optional[str] = None,
) -> dict[str, Any]:
    """List ArtCraft subscription jobs from `/v1/jobs/session`.

    This is the cookie-authenticated subscription surface, not BYO Omni API.
    """
    limit = max(1, min(int(limit), 100))
    jobs = _list_subscription_jobs(root, include_states)
    return {
        "source": "subscription_api",
        "jobs": [
            _subscription_job_result(job)
            for job in jobs[:limit]
        ],
    }


@mcp.tool()
def artcraft_list_image_models(provider: Optional[str] = None) -> dict[str, Any]:
    """List image models from ArtCraft's public Omni model endpoint when available."""
    # The ArtCraft app calls this with maybe_creds=None. Provider filtering may or may not be accepted by backend.
    suffix = "" if not provider else f"?provider={provider}"
    try:
        return _http_json("GET", f"/v1/omni_gen/models/image{suffix}")
    except Exception as e:
        return {"error": str(e), "note": "Endpoint shape may have changed; local task tools still work."}


@mcp.tool()
def artcraft_list_video_models(root: Optional[str] = None) -> dict[str, Any]:
    """List ArtCraft video models, enriching the Seedance 2.0 family with tier and capability flags.

    Returns every video model from `GET /v1/omni_gen/models/video` plus a
    `seedance_2p0` registry block so Hermes can offer the full Seedance 2.0
    line (base / Plus / Plus Ultra and their fast / mini variants). The Plus
    and Plus Ultra tiers (`bp`, `bpu`) are ByteDance's real models and are less
    censored on ArtCraft than the base Seedance 2.0.

    Each model entry carries capability booleans (text prompt, starting/ending
    keyframe, image references, audio references), reference limits, defaults,
    and `is_seedance_2p0` / `is_bytedance_real_model` flags.
    """
    cookie = _storyteller_cookie_header(root)
    if not cookie:
        return {
            "ok": False,
            "error_code": "not_logged_in",
            "message": "No ArtCraft subscription session was found.",
            "remediation": "Open ArtCraft, sign in, then retry.",
        }
    try:
        payload = _http_json("GET", "/v1/omni_gen/models/video", cookie=cookie)
    except ArtCraftHTTPError as error:
        return {
            "ok": False,
            "error_code": "backend_unavailable",
            "details": {"http_status": error.code},
            "remediation": "Retry shortly; the ArtCraft model endpoint may be down.",
        }
    except Exception as error:
        return _tool_error(
            "backend_unavailable",
            "ArtCraft did not return the video model list.",
            "Retry shortly.",
            {"error": str(error)},
        )
    models = payload.get("models", []) if isinstance(payload, dict) else []
    enriched = []
    for entry in models:
        model_id = entry.get("model")
        seedance = SEEDANCE_2P0_MODELS.get(model_id)
        enriched.append({
            **entry,
            "model": model_id,
            "full_name": entry.get("full_name", model_id),
            "model_creator": entry.get("model_creator"),
            "is_seedance_2p0": model_id in SEEDANCE_2P0_MODELS,
            "tier": seedance.get("tier") if seedance else None,
            "is_bytedance_real_model": seedance.get("is_bytedance_real_model", False) if seedance else False,
            "supports_text_prompt": bool(entry.get("text_prompt_supported") or entry.get("text_to_video_supported")),
            "supports_starting_keyframe": bool(entry.get("starting_keyframe_supported")),
            "supports_ending_keyframe": bool(entry.get("ending_keyframe_supported")),
            "supports_image_references": bool(entry.get("image_references_supported")),
            "image_references_max": entry.get("image_references_max"),
            "supports_audio_references": bool(entry.get("audio_references_supported")),
            "aspect_ratio_default": entry.get("aspect_ratio_default"),
            "resolution_default": entry.get("resolution_default"),
            "duration_seconds_default": entry.get("duration_seconds_default"),
            "duration_seconds_max": entry.get("duration_seconds_max"),
            "batch_size_default": entry.get("batch_size_default"),
        })
    return {
        "ok": True,
        "models": enriched,
        "seedance_2p0_registry": SEEDANCE_2P0_MODELS,
    }


@mcp.tool()
def artcraft_estimate_image_cost(
    model: str,
    aspect_ratio: Optional[str] = None,
    resolution: Optional[str] = None,
    quality: Optional[str] = None,
    batch_size: int = 1,
    reference_image_count: int = 0,
    root: Optional[str] = None,
) -> dict[str, Any]:
    """Spend-free ArtCraft subscription image cost estimate."""
    cookie = _storyteller_cookie_header(root)
    if not cookie:
        return {
            "ok": False,
            "blocked": "missing_artcraft_subscription_credentials",
            "cookie_store": str(_tauri_cookie_store()),
        }
    ref_count = max(0, int(reference_image_count))
    generation_mode: dict[str, Any] = (
        {"type": "image_edit", "count": ref_count}
        if ref_count
        else {"type": "text_to_image"}
    )
    body = {
        "model": model,
        "provider": "artcraft",
        "generation_mode": generation_mode,
        "aspect_ratio": _resolve_aspect_ratio(aspect_ratio),
        "resolution": RESOLUTION_ALIASES.get(resolution, resolution),
        "quality": quality,
        "image_batch_count": max(1, min(int(batch_size), 4)),
    }
    return _http_json(
        "POST",
        "/v1/generate/cost_estimate/image",
        body,
        cookie=cookie,
    )


@mcp.tool()
def artcraft_estimate_video_cost(
    model: str,
    duration_seconds: int = 5,
    aspect_ratio: Optional[str] = "square",
    resolution: Optional[str] = None,
    video_batch_count: int = 1,
    generate_audio: bool = False,
    root: Optional[str] = None,
) -> dict[str, Any]:
    """Spend-free ArtCraft subscription video cost estimate."""
    cookie = _storyteller_cookie_header(root)
    if not cookie:
        return {
            "ok": False,
            "blocked": "missing_artcraft_subscription_credentials",
            "cookie_store": str(_tauri_cookie_store()),
        }
    validated_model = _validate_seedance_model(model)
    if validated_model is None:
        valid = ", ".join(SEEDANCE_2P0_MODELS)
        return _tool_error(
            "invalid_params",
            f"Unknown Seedance 2.0 model id: {model!r}.",
            f"Use one of: {valid}",
        )
    body: dict[str, Any] = {
        "model": validated_model,
        "provider": "artcraft",
        "generation_mode": {"type": "text_to_video"},
        "aspect_ratio": _resolve_aspect_ratio(aspect_ratio),
        "duration_seconds": max(1, min(int(duration_seconds), 60)),
        "video_batch_count": max(1, min(int(video_batch_count), 4)),
        "generate_audio": bool(generate_audio),
    }
    if resolution is not None:
        body["resolution"] = RESOLUTION_ALIASES.get(resolution, resolution)
    return _http_json(
        "POST",
        "/v1/generate/cost_estimate/video",
        body,
        cookie=cookie,
    )


def _reference_mime_type(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def _load_reference_bytes(source: str, max_bytes: int) -> tuple[bytes, str, str]:
    parsed = urllib.parse.urlparse(source)
    if len(parsed.scheme) > 1:  # single letter = Windows drive letter
        if parsed.scheme.lower() != "https":
            raise ValueError("Reference image URLs must use HTTPS.")
        request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(max_bytes + 1)
        filename = Path(urllib.parse.unquote(parsed.path)).name or "reference-image"
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValueError("Reference image file does not exist.")
        data = path.read_bytes()
        filename = path.name
    if len(data) > max_bytes:
        raise ValueError("Reference image exceeds max_bytes.")
    mime_type = _reference_mime_type(data)
    if mime_type is None:
        raise ValueError("Reference image must be PNG, JPEG, or GIF.")
    return data, mime_type, filename


def _http_multipart_image_upload(
    image_bytes: bytes,
    mime_type: str,
    filename: str,
    cookie: str,
    is_intermediate_system_file: bool,
) -> dict[str, Any]:
    boundary = f"----ArtCraftMCP{uuid.uuid4().hex}"
    safe_filename = filename.replace('"', "") or "reference-image"
    token = str(uuid.uuid4())
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"uuid_idempotency_token\"\r\n\r\n{token}\r\n".encode(),
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{safe_filename}\"\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode(),
        image_bytes,
        b"\r\n",
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"is_intermediate_system_file\"\r\n\r\n"
            f"{'true' if is_intermediate_system_file else 'false'}\r\n"
        ).encode(),
        f"--{boundary}--\r\n".encode(),
    ]
    request = urllib.request.Request(
        f"{ARTCRAFT_API}/v1/media_files/upload/image",
        data=b"".join(chunks),
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Cookie": cookie,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raise ArtCraftHTTPError(error.code) from None


def _reference_media_mime_type(data: bytes, filename: str, media_kind: str) -> Optional[str]:
    """Identify the narrow audio/video formats accepted by ArtCraft uploads."""
    suffix = Path(filename).suffix.lower()
    if media_kind == "video":
        if len(data) >= 12 and data[4:8] == b"ftyp":
            return "video/quicktime" if suffix in {".mov", ".qt"} else "video/mp4"
        return None
    if media_kind == "audio":
        if len(data) >= 2 and data[0] == 0xff and data[1] in (0xf1, 0xf9):
            return "audio/aac"
        if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xff and data[1] & 0xe0 == 0xe0):
            return "audio/mpeg"
        if data.startswith(b"fLaC"):
            return "audio/x-flac"
        if data.startswith(b"OggS"):
            return "audio/ogg"
        if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
            return "audio/x-wav"
        if len(data) >= 12 and data[4:8] == b"ftyp":
            return "audio/m4a" if suffix in {".m4a", ".m4b"} else "audio/mp4"
    return None


def _load_reference_media_bytes(source: str, max_bytes: int, media_kind: str) -> tuple[bytes, str, str]:
    parsed = urllib.parse.urlparse(source)
    if len(parsed.scheme) > 1:  # single letter = Windows drive letter
        if parsed.scheme.lower() != "https":
            raise ValueError(f"Reference {media_kind} URLs must use HTTPS.")
        request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(max_bytes + 1)
        filename = Path(urllib.parse.unquote(parsed.path)).name or f"reference-{media_kind}"
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValueError(f"Reference {media_kind} file does not exist.")
        if path.stat().st_size > max_bytes:
            raise ValueError(f"Reference {media_kind} exceeds max_bytes.")
        data = path.read_bytes()
        filename = path.name
    if len(data) > max_bytes:
        raise ValueError(f"Reference {media_kind} exceeds max_bytes.")
    mime_type = _reference_media_mime_type(data, filename, media_kind)
    if mime_type is None:
        allowed = "MP4 or QuickTime" if media_kind == "video" else "AAC, M4A/MP4, MP3, Ogg/Opus, FLAC, or WAV"
        raise ValueError(f"Reference {media_kind} must be {allowed}.")
    return data, mime_type, filename


def _http_multipart_media_upload(
    endpoint: str,
    media_bytes: bytes,
    mime_type: str,
    filename: str,
    cookie: str,
    is_intermediate_system_file: Optional[bool],
) -> dict[str, Any]:
    boundary = f"----ArtCraftMCP{uuid.uuid4().hex}"
    safe_filename = filename.replace('"', "") or "reference-media"
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"uuid_idempotency_token\"\r\n\r\n{uuid.uuid4()}\r\n".encode(),
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{safe_filename}\"\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode(),
        media_bytes,
        b"\r\n",
    ]
    if is_intermediate_system_file is not None:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"is_intermediate_system_file\"\r\n\r\n"
                f"{'true' if is_intermediate_system_file else 'false'}\r\n"
            ).encode()
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"{ARTCRAFT_API}{endpoint}",
        data=b"".join(chunks),
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Cookie": cookie,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raise ArtCraftHTTPError(error.code) from None


def _upload_reference_media(
    source: str,
    media_kind: str,
    endpoint: str,
    is_intermediate_system_file: Optional[bool],
    max_bytes: int,
    root: Optional[str],
) -> dict[str, Any]:
    source = source.strip()
    if re.fullmatch(r"m_[A-Za-z0-9_-]+", source):
        response: dict[str, Any] = {
            "ok": True,
            "media_token": source,
            "uploaded": False,
            "source_type": "existing_media_token",
            "media_kind": media_kind,
        }
        if is_intermediate_system_file is not None:
            response["is_intermediate_system_file"] = is_intermediate_system_file
        return response
    try:
        media_bytes, mime_type, filename = _load_reference_media_bytes(source, int(max_bytes), media_kind)
    except (OSError, ValueError, urllib.error.URLError) as error:
        return _tool_error(
            f"reference_{media_kind}_failed",
            str(error),
            f"Provide an existing ArtCraft token, a readable local {media_kind} file, or a public HTTPS {media_kind} URL.",
        )
    cookie = _storyteller_cookie_header(root)
    if not cookie:
        return _tool_error("not_logged_in", "No ArtCraft subscription session was found.", "Open ArtCraft, sign in, then retry.")
    try:
        api_response = _http_multipart_media_upload(
            endpoint, media_bytes, mime_type, filename, cookie, is_intermediate_system_file,
        )
    except Exception as error:
        failure = _safe_api_error(error)
        if failure["error_code"] == "session_expired":
            return failure
        return _tool_error(
            f"reference_{media_kind}_failed",
            f"ArtCraft did not accept the reference {media_kind} upload.",
            "Retry shortly, or reuse an existing ArtCraft m_... media token.",
            failure.get("details"),
        )
    media_token = api_response.get("media_file_token")
    if not api_response.get("success") or not isinstance(media_token, str):
        return _tool_error(
            f"reference_{media_kind}_failed",
            f"ArtCraft did not return a media token for the reference {media_kind}.",
            "Retry the upload or reuse an existing ArtCraft m_... media token.",
        )
    response = {
        "ok": True,
        "media_token": media_token,
        "uploaded": True,
        "source_type": "https_url" if len(urllib.parse.urlparse(source).scheme) > 1 else "local_file",
        "media_kind": media_kind,
    }
    if is_intermediate_system_file is not None:
        response["is_intermediate_system_file"] = is_intermediate_system_file
    return response


@mcp.tool()
def artcraft_upload_reference_image(
    source: str,
    is_intermediate_system_file: bool = True,
    max_bytes: int = 20 * 1024 * 1024,
    root: Optional[str] = None,
) -> dict[str, Any]:
    """Reuse an ArtCraft media token or upload a local/HTTPS reference image."""
    source = source.strip()
    if re.fullmatch(r"m_[A-Za-z0-9_-]+", source):
        return {
            "ok": True,
            "media_token": source,
            "uploaded": False,
            "source_type": "existing_media_token",
            "is_intermediate_system_file": is_intermediate_system_file,
        }
    try:
        image_bytes, mime_type, filename = _load_reference_bytes(source, int(max_bytes))
    except (OSError, ValueError, urllib.error.URLError) as error:
        return _tool_error(
            "reference_image_failed",
            str(error),
            "Provide an existing ArtCraft token, a readable local PNG/JPEG/GIF, or a public HTTPS image URL.",
        )
    cookie = _storyteller_cookie_header(root)
    if not cookie:
        return _tool_error(
            "not_logged_in",
            "No ArtCraft subscription session was found.",
            "Open ArtCraft, sign in, then retry.",
        )
    try:
        response = _http_multipart_image_upload(
            image_bytes,
            mime_type,
            filename,
            cookie,
            is_intermediate_system_file,
        )
    except Exception as error:
        failure = _safe_api_error(error)
        if failure["error_code"] == "session_expired":
            return failure
        return _tool_error(
            "reference_image_failed",
            "ArtCraft did not accept the reference image upload.",
            "Retry shortly, or reuse an existing ArtCraft m_... media token.",
            failure.get("details"),
        )
    media_token = response.get("media_file_token")
    if not response.get("success") or not isinstance(media_token, str):
        return _tool_error(
            "reference_image_failed",
            "ArtCraft did not return a media token for the reference image.",
            "Retry the upload or reuse an existing ArtCraft m_... media token.",
        )
    source_type = "https_url" if len(urllib.parse.urlparse(source).scheme) > 1 else "local_file"
    return {
        "ok": True,
        "media_token": media_token,
        "uploaded": True,
        "source_type": source_type,
        "is_intermediate_system_file": is_intermediate_system_file,
    }


@mcp.tool()
def artcraft_upload_reference_audio(
    source: str,
    max_bytes: int = 512 * 1024 * 1024,
    root: Optional[str] = None,
) -> dict[str, Any]:
    """Reuse an ArtCraft token or upload an audio reference for video generation.

    ArtCraft's audio endpoint does not accept a hidden/intermediate-file flag,
    so new audio uploads are account-visible. This tool writes to ArtCraft but
    does not spend generation credits.
    """
    return _upload_reference_media(
        source, "audio", "/v1/media_files/upload/audio", None, max_bytes, root,
    )


@mcp.tool()
def artcraft_upload_reference_video(
    source: str,
    is_intermediate_system_file: bool = True,
    max_bytes: int = 512 * 1024 * 1024,
    root: Optional[str] = None,
) -> dict[str, Any]:
    """Reuse an ArtCraft token or upload an MP4/QuickTime video reference."""
    return _upload_reference_media(
        source,
        "video",
        "/v1/media_files/upload/new_video",
        is_intermediate_system_file,
        max_bytes,
        root,
    )


@mcp.tool()
def artcraft_generate_image_subscription(
    prompt: str,
    model: str,
    aspect_ratio: Optional[str] = None,
    resolution: Optional[str] = None,
    quality: Optional[str] = None,
    batch_size: int = 1,
    reference_image_media_tokens: Optional[str] = None,
    root: Optional[str] = None,
    insert_into_artcraft_task_db: bool = True,
) -> dict[str, Any]:
    """Generate image through the ArtCraft subscription/Storyteller Omni API.

    This intentionally uses ArtCraft subscription cookies, not BYO provider keys.
    If credentials are absent, it returns a precise blocker instead of falling back
    to FAL/Grok/Midjourney/etc.
    """
    cookie = _storyteller_cookie_header(root)
    if not cookie:
        return {
            "ok": False,
            "blocked": "missing_artcraft_subscription_credentials",
            "message": "No artcraft_avt.txt/artcraft_session.txt found in ArtCraft credentials dir. Log into ArtCraft subscription in the app, or add an upstream local automation bridge so MCP can call the running app's in-memory StorytellerCredentialManager.",
            "credentials_dir": str(_credentials_dir(root)),
        }
    ref_images = None
    if reference_image_media_tokens:
        ref_images = [
            token.strip()
            for token in reference_image_media_tokens.split(",")
            if token.strip()
        ]
    body = {
        "idempotency_token": uuid.uuid4().hex,
        "model": model,
        "prompt": prompt,
        "image_media_tokens": ref_images,
        "resolution": RESOLUTION_ALIASES.get(resolution, resolution),
        "aspect_ratio": _resolve_aspect_ratio(aspect_ratio),
        "quality": quality,
        "image_batch_count": max(1, min(int(batch_size), 8)),
        "adjust_horizontal_angle": None,
        "adjust_vertical_angle": None,
        "adjust_zoom": None,
    }
    response = _http_json("POST", "/v1/omni_gen/generate/image", body, cookie=cookie)
    job_token = response.get("inference_job_token") or response.get("job_token") or response.get("token")
    task_id = None
    if job_token and insert_into_artcraft_task_db:
        task_id = _insert_enqueued_task(root=root, task_type="image_generation", model_type=model, provider_job_id=str(job_token))
    return {"ok": True, "api_response": response, "provider_job_id": job_token, "local_task_id": task_id}


@mcp.tool()
def artcraft_generate_video_subscription(
    prompt: str,
    model: str = "seedance_2p0",
    aspect_ratio: Optional[str] = None,
    resolution: Optional[str] = None,
    quality: Optional[str] = None,
    duration_seconds: int = 15,
    video_batch_count: int = 1,
    generate_audio: bool = False,
    reference_image_media_tokens: Optional[str] = None,
    reference_video_media_tokens: Optional[str] = None,
    reference_audio_media_tokens: Optional[str] = None,
    root: Optional[str] = None,
    insert_into_artcraft_task_db: bool = True,
) -> dict[str, Any]:
    """Generate video through the ArtCraft subscription/Storyteller Omni API.

    Same subscription auth as the image tool. `reference_image_media_tokens`
    accepts a comma-separated list of media tokens (e.g. from prior results).

    `model` accepts any ArtCraft video model id. The full Seedance 2.0 family
    is selectable (verified against the live endpoint):

      seedance_2p0          Seedance 2.0            (base)
      seedance_2p0_fast     Seedance 2.0 Fast       (base, lighter)
      seedance_2p0_bp       Seedance 2.0 Plus       (ByteDance real model, less censored)
      seedance_2p0_bp_fast  Seedance 2.0 Plus Fast  (ByteDance real model)
      seedance_2p0_bpu      Seedance 2.0 Plus Ultra      (ByteDance real model, top tier)
      seedance_2p0_bpu_fast Seedance 2.0 Plus Ultra Fast (ByteDance real model)
      seedance_2p0_mini     Seedance 2.0 Mini       (base, lighter)
      seedance_2p0_bp_mini  Seedance 2.0 Plus Mini  (ByteDance real model)
      seedance_2p0_bpu_mini Seedance 2.0 Plus Ultra Mini (ByteDance real model)

    Note: there is no `seedance_2p0_ultra`; the top tier is `seedance_2p0_bpu`
    (Plus Ultra). All Seedance 2.0 variants support text prompts, starting and
    ending keyframes, and up to 9 image references. They do NOT generate audio
    (the `generate_audio` flag is accepted but the backend ignores it for this
    family); audio *reference* input is supported via `reference_audio_media_tokens`.
    """
    cookie = _storyteller_cookie_header(root)
    if not cookie:
        return {
            "ok": False,
            "blocked": "missing_artcraft_subscription_credentials",
            "message": "No ArtCraft subscription cookies in the Tauri cookie store. Log into ArtCraft in the app first.",
            "cookie_store": str(_tauri_cookie_store()),
        }
    validated_model = _validate_seedance_model(model)
    if validated_model is None:
        valid = ", ".join(SEEDANCE_2P0_MODELS)
        return {
            "ok": False,
            "error_code": "invalid_params",
            "message": (
                f"Unknown Seedance 2.0 model id: {model!r}. "
                "The top ByteDance-real tier is 'seedance_2p0_bpu' (Plus Ultra); "
                "there is no 'seedance_2p0_ultra'."
            ),
            "remediation": f"Use one of: {valid}",
        }
    model = validated_model
    ref_images = None
    if reference_image_media_tokens:
        ref_images = [t.strip() for t in reference_image_media_tokens.split(",") if t.strip()]
    ref_videos = None
    if reference_video_media_tokens:
        ref_videos = [t.strip() for t in reference_video_media_tokens.split(",") if t.strip()]
    ref_audio = None
    if reference_audio_media_tokens:
        ref_audio = [t.strip() for t in reference_audio_media_tokens.split(",") if t.strip()]
    body = {
        "idempotency_token": uuid.uuid4().hex,
        "model": model,
        "prompt": prompt,
        "negative_prompt": None,
        "start_frame_image_media_token": None,
        "end_frame_image_media_token": None,
        "reference_image_media_tokens": ref_images,
        "reference_video_media_tokens": ref_videos,
        "reference_audio_media_tokens": ref_audio,
        "reference_character_tokens": None,
        "resolution": RESOLUTION_ALIASES.get(resolution, resolution),
        "aspect_ratio": _resolve_aspect_ratio(aspect_ratio),
        "bitrate": None,
        "quality": quality,
        "duration_seconds": max(1, min(int(duration_seconds), 60)),
        "video_batch_count": max(1, min(int(video_batch_count), 4)),
        "generate_audio": generate_audio,
    }
    response = _http_json("POST", "/v1/omni_gen/generate/video", body, cookie=cookie)
    job_token = response.get("inference_job_token") or response.get("job_token") or response.get("token")
    task_id = None
    if job_token and insert_into_artcraft_task_db:
        task_id = _insert_enqueued_task(root=root, task_type="video_generation", model_type=model, provider_job_id=str(job_token))
    return {"ok": True, "api_response": response, "provider_job_id": job_token, "local_task_id": task_id}


@mcp.tool()
def artcraft_get_task_result(
    task_id: str,
    root: Optional[str] = None,
) -> dict[str, Any]:
    """Get a subscription result by local `task_` id or provider `jinf_` token.

    Uses `/v1/jobs/session` as the source of truth because local sqlite rows may
    remain `pending` after a server-side job has completed.
    """
    try:
        return _get_subscription_job_result(task_id, root)
    except Exception as api_error:
        # Last-resort local fallback for offline/API failure cases.
        try:
            with _connect_tasks(root) as con:
                row = con.execute(
                    "select task_status, provider_job_id, on_complete_primary_media_file_cdn_url, "
                    "on_complete_primary_media_file_token, on_complete_primary_media_file_class, "
                    "on_failure_type, on_failure_message, completed_at from tasks where id = ?",
                    (task_id,),
                ).fetchone()
        except Exception:
            row = None
        if not row:
            return {"found": False, "task_id": task_id, "api_error": str(api_error)}
        return {
            "found": True,
            "ready": row["task_status"] == "complete_success",
            "source": "local_db_fallback",
            "local_task_id": task_id,
            "provider_job_id": row["provider_job_id"],
            "task_status": row["task_status"],
            "cdn_url": row["on_complete_primary_media_file_cdn_url"],
            "media_token": row["on_complete_primary_media_file_token"],
            "media_class": row["on_complete_primary_media_file_class"],
            "completed_at": row["completed_at"],
            "on_failure_type": row["on_failure_type"],
            "on_failure_message": row["on_failure_message"],
            "api_error": str(api_error),
        }


@mcp.tool()
def artcraft_wait_for_result(
    task_id: str,
    timeout_seconds: Optional[int] = None,
    poll_interval_seconds: Optional[int] = None,
    root: Optional[str] = None,
) -> dict[str, Any]:
    """Poll `/v1/jobs/session` silently until a subscription job completes or fails.

    Accepts either a local `task_` id or provider `jinf_` token. Image jobs use
    four-minute waits with five-second checks; video jobs use slower checks and
    remain resumable. A local timeout never means the generation failed.
    """
    started_at = time.time()
    last = _get_subscription_job_result(task_id, root)
    if not last.get("found"):
        return last
    status = last.get("task_status")
    if last.get("ready") or status in ("dead", "complete_failure", "failed"):
        return last

    is_video = last.get("inference_category") == "video_generation"
    default_timeout = (
        DEFAULT_VIDEO_WAIT_TIMEOUT_SECONDS if is_video
        else DEFAULT_IMAGE_WAIT_TIMEOUT_SECONDS
    )
    default_interval = (
        DEFAULT_VIDEO_POLL_INTERVAL_SECONDS if is_video
        else DEFAULT_IMAGE_POLL_INTERVAL_SECONDS
    )
    effective_timeout = default_timeout if timeout_seconds is None else int(timeout_seconds)
    effective_interval = default_interval if poll_interval_seconds is None else int(poll_interval_seconds)
    deadline = started_at + max(10, min(effective_timeout, 3600))

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(max(2, effective_interval), remaining))
        last = _get_subscription_job_result(task_id, root)
        if not last.get("found"):
            return last
        status = last.get("task_status")
        if last.get("ready") or status in ("dead", "complete_failure", "failed"):
            return last
    return {
        **last,
        "timed_out": True,
        "error_code": "generation_timeout",
        "message": "The local wait ended before ArtCraft reported a terminal result.",
        "remediation": (
            "The generation may still be running. Call artcraft_get_task_result "
            "with the preserved provider_job_id to resume checking."
        ),
    }


if __name__ == "__main__":
    mcp.run()
