# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small local Web UI for scanning uploaded SkillSpector inputs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, unquote, urlparse, urlsplit, urlunsplit

import typer

from skillspector import __version__
from skillspector.llm_limiter import DEFAULT_LLM_MAX_CONCURRENCY
from skillspector.logging_config import get_logger, set_level

logger = get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_UPLOAD_MB = 50
DEFAULT_UPLOAD_TICKET_TTL_SECONDS = 15 * 60
DEFAULT_UPLOAD_RETENTION_SECONDS = 60 * 60
AUTH_TOKEN_ENV = "SKILLSPECTOR_AUTH_TOKEN"
API_USERNAME_ENV = "SKILLSPECTOR_API_USERNAME"
API_PASSWORD_ENV = "SKILLSPECTOR_API_PASSWORD"
PUBLIC_URL_ENV = "SKILLSPECTOR_PUBLIC_URL"
GIT_COMMIT_ENV = "SKILLSPECTOR_GIT_COMMIT"
SCHEMA_VERSION_ENV = "SKILLSPECTOR_SCHEMA_VERSION"
RELEASE_VERSION_ENV = "SKILLSPECTOR_RELEASE_VERSION"
WEB_LOG_LEVEL_ENV = "SKILLSPECTOR_WEB_LOG_LEVEL"
CHUNK_SIZE = 1024 * 1024
MAX_HISTORY_ITEMS = 50
MODEL_SLOTS = (
    "default",
    "mcp_least_privilege",
    "mcp_rug_pull",
    "mcp_tool_poisoning",
    "semantic_developer_intent",
    "semantic_quality_policy",
    "semantic_security_discovery",
    "meta_analyzer",
)
SCAN_ENV_KEYS = (
    "SKILLSPECTOR_PROVIDER",
    "SKILLSPECTOR_MODEL",
    "SKILLSPECTOR_STRUCTURED_OUTPUT_METHOD",
    "SKILLSPECTOR_LLM_MAX_CONCURRENCY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "NVIDIA_INFERENCE_KEY",
)
STRUCTURED_OUTPUT_METHODS = {"json_schema", "json_mode", "function_calling", "text_json"}
_WEB_LOGGING_CONFIGURED = False


def _normalize_llm_max_concurrency(raw: str | None) -> str:
    if raw:
        try:
            configured = int(raw)
        except ValueError:
            configured = DEFAULT_LLM_MAX_CONCURRENCY
    else:
        configured = DEFAULT_LLM_MAX_CONCURRENCY
    return str(max(1, min(16, configured)))


def _health_payload() -> dict[str, str | bool]:
    return {
        "ok": True,
        "service": "skillspector",
        "version": __version__,
        "release_version": os.environ.get(RELEASE_VERSION_ENV, "").strip() or "dev",
        "git_commit": os.environ.get(GIT_COMMIT_ENV, "").strip() or "unknown",
        "schema_version": os.environ.get(SCHEMA_VERSION_ENV, "").strip() or "none",
    }


def _configure_web_logging() -> None:
    global _WEB_LOGGING_CONFIGURED
    if _WEB_LOGGING_CONFIGURED:
        return
    level = (
        os.environ.get(WEB_LOG_LEVEL_ENV)
        or os.environ.get("SKILLSPECTOR_LOG_LEVEL")
        or "INFO"
    )
    set_level(level)
    _WEB_LOGGING_CONFIGURED = True


@dataclass
class _UploadRecord:
    upload_id: str
    token_hash: str
    created_at: float
    expires_at: float
    max_bytes: int
    filename_hint: str
    upload_path: Path | None = None
    uploaded_at: float | None = None
    size_bytes: int = 0
    sha256: str = ""


_SCAN_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()
_HISTORY: list[dict[str, Any]] = []
_UPLOAD_LOCK = threading.Lock()
_UPLOADS: dict[str, _UploadRecord] = {}

app = typer.Typer(
    name="skillspector-web",
    help="Run the local SkillSpector Web upload scanner.",
    add_completion=False,
)


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SkillSpector Web</title>
  <style>
    :root{color-scheme:light;--bg:#f6f7f9;--panel:#fff;--ink:#15191f;--muted:#586271;--line:#d9dee6;--accent:#0f6f68;--warn:#a94700;--bad:#b42318;--good:#087443;--soft:#eef6f5}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    .shell{min-height:100vh;display:grid;grid-template-rows:auto 1fr}header{border-bottom:1px solid var(--line);background:#fff}.bar{max-width:980px;margin:auto;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}
    h1{font-size:18px;margin:0;font-weight:750}.status{color:var(--muted);font-size:13px}.main{max-width:980px;width:100%;margin:0 auto;padding:24px;display:grid;grid-template-columns:minmax(300px,360px) 1fr;gap:20px}
    section,.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px}.panel{padding:20px;display:flex;flex-direction:column;gap:16px}.title{font-size:15px;font-weight:700;margin:0 0 4px}.muted{color:var(--muted);margin:0}.file{border:1px dashed var(--line);border-radius:8px;padding:12px;background:#fafbfc}
    label{display:grid;gap:5px;color:#303846;font-weight:650}input,select{width:100%;border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--ink);padding:9px 10px;font:inherit}input[type=file]{padding:0;border:0}.check{display:flex;align-items:center;gap:8px;color:var(--muted);font-weight:500}.check input{width:auto}.row{display:flex;align-items:center;justify-content:space-between;gap:12px}
    button{border:0;border-radius:7px;background:var(--accent);color:#fff;font-weight:700;padding:10px 14px;cursor:pointer}button.ghost{background:#eef2f7;color:#26313d}button:disabled{opacity:.55;cursor:not-allowed}.result{min-height:360px;padding:20px}.score{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:14px 0 18px}
    .metric{border:1px solid var(--line);border-radius:8px;padding:12px}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{font-size:19px}.pill{display:inline-flex;border-radius:999px;padding:3px 8px;font-size:12px;font-weight:700;background:#eef2f7;color:#2f3a4a}.LOW{color:var(--good)}.MEDIUM{color:var(--warn)}.HIGH,.CRITICAL{color:var(--bad)}
    table{width:100%;border-collapse:collapse}th,td{text-align:left;border-top:1px solid var(--line);padding:9px;vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:700}.details{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:12px}.kv{border:1px solid var(--line);border-radius:8px;padding:10px;color:var(--muted)}.kv b{display:block;color:var(--ink)}.error{border-color:#f1b4ad;background:#fff6f5;color:#7a271a}
    @media(max-width:860px){.main{grid-template-columns:1fr;padding:16px}.bar{padding:14px 16px;align-items:flex-start}.score,.details{grid-template-columns:1fr}}
  </style>
</head>
<body>
<div class="shell">
  <header><div class="bar"><h1>SkillSpector Web</h1><div class="status" id="state">就绪</div></div></header>
  <main class="main">
    <form class="panel" id="form">
      <div><p class="title">上传检查对象</p><p class="muted">支持 zip、SKILL.md、脚本文件或单文件导出。</p></div>
      <div class="file"><input id="file" name="file" type="file" required></div>
      <button id="scan" type="submit">开始检查</button>
    </form>
    <section class="result" id="result"><p class="title">结果详情</p><p class="muted">等待上传。</p></section>
  </main>
</div>
<script>
const $=id=>document.getElementById(id),form=$("form"),file=$("file"),scan=$("scan"),state=$("state"),result=$("result");
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function headersFor(picked){return {"X-Filename":encodeURIComponent(picked.name),"Content-Type":"application/octet-stream"}}
async function apiFetch(url,options={}){return fetch(url,{credentials:"same-origin",...options})}
function findingRow(f){const loc=f.location||{};const rule=f.rule_id||f.id||"-",file=f.file||loc.file||"-",line=f.start_line||loc.start_line||"-",msg=f.message||f.explanation||f.finding||"-";return `<tr><td><span class="pill ${esc(f.severity)}">${esc(f.severity)}</span></td><td>${esc(rule)}</td><td>${esc(file)}:${esc(line)}</td><td>${esc(msg)}</td></tr>`}
function elapsedText(ms){if(!Number.isFinite(ms))return "-";return ms<1000?`${Math.round(ms)} ms`:`${(ms/1000).toFixed(1)} s`}
function renderReport(filename,report,id,history){const r=report||{},risk=r.risk_assessment||{},items=r.issues||r.findings||[],meta=r.metadata||{},components=r.components||[],elapsed=elapsedText(Number(history?.elapsed_ms));result.className="result";result.innerHTML=`<p class="title">${esc(filename||"扫描详情")}</p><div class="score"><div class="metric"><span>风险评分</span><strong class="${esc(risk.severity)}">${esc(risk.score??0)}/100</strong></div><div class="metric"><span>严重度</span><strong class="${esc(risk.severity)}">${esc(risk.severity??"LOW")}</strong></div><div class="metric"><span>建议</span><strong>${esc(risk.recommendation??"SAFE")}</strong></div><div class="metric"><span>问题</span><strong>${items.length}</strong></div></div><div class="details"><div class="kv"><b>${esc(r.skill?.name||"unknown")}</b>Skill</div><div class="kv"><b>${esc(meta.llm_requested?"LLM":"Static")}</b>模式</div><div class="kv"><b>${components.length}</b>组件</div><div class="kv"><b>${esc(elapsed)}</b>耗时</div><div class="kv"><b>${esc(id||"-")}</b>Run ID</div></div>${items.length?`<table><thead><tr><th>等级</th><th>规则</th><th>位置</th><th>说明</th></tr></thead><tbody>${items.map(findingRow).join("")}</tbody></table>`:"<p class=muted>未发现安全问题。</p>"}`;}
function renderError(message){result.className="result error";result.innerHTML=`<p class="title">检查失败</p><p>${esc(message)}</p>`}
form.addEventListener("submit",async e=>{e.preventDefault();const picked=file.files[0];if(!picked)return;scan.disabled=true;state.textContent="检查中";result.className="result";result.innerHTML="<p class=title>检查中</p><p class=muted>正在分析上传内容。</p>";try{const qs=new URLSearchParams({use_llm:"true"});const res=await apiFetch(`/api/scan?${qs}`,{method:"POST",headers:headersFor(picked),body:picked});const data=await res.json();if(!data.ok)renderError(data.error);else renderReport(data.filename,data.report,data.id,data.history_item)}catch(err){renderError(err.message)}finally{scan.disabled=false;state.textContent="就绪"}});
</script>
</body>
</html>"""


def _max_upload_bytes(max_upload_mb: int | None = None) -> int:
    if max_upload_mb is not None:
        return max(1, max_upload_mb) * 1024 * 1024
    raw = os.environ.get("SKILLSPECTOR_WEB_MAX_UPLOAD_MB", str(DEFAULT_MAX_UPLOAD_MB))
    try:
        mb = int(raw)
    except ValueError:
        mb = DEFAULT_MAX_UPLOAD_MB
    return max(1, mb) * 1024 * 1024


def _safe_upload_name(raw_name: str | None) -> str:
    name = Path(unquote(raw_name or "")).name
    name = "".join("_" if ord(char) < 32 or 127 <= ord(char) <= 159 else char for char in name)
    name = name.strip()
    return name or "upload.bin"


def _header_value(headers: Any, name: str) -> str:
    return (headers.get(name) or "").strip()


def _utc_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bearer_token(headers: Any) -> str | None:
    value = _header_value(headers, "Authorization")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def _basic_credentials(headers: Any) -> tuple[str, str] | None:
    value = _header_value(headers, "Authorization")
    scheme, _, encoded = value.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    username, sep, password = decoded.partition(":")
    if not sep:
        return None
    return username, password


def _auth_configured() -> bool:
    return bool(os.environ.get(AUTH_TOKEN_ENV)) or bool(
        os.environ.get(API_USERNAME_ENV) and os.environ.get(API_PASSWORD_ENV)
    )


def _request_is_authorized(headers: Any) -> bool:
    token = os.environ.get(AUTH_TOKEN_ENV)
    if token:
        supplied = _bearer_token(headers)
        if supplied is not None and secrets.compare_digest(supplied, token):
            return True

    username = os.environ.get(API_USERNAME_ENV)
    password = os.environ.get(API_PASSWORD_ENV)
    if username and password:
        credentials = _basic_credentials(headers)
        if credentials is not None:
            supplied_user, supplied_password = credentials
            return secrets.compare_digest(supplied_user, username) and secrets.compare_digest(
                supplied_password, password
            )

    return not _auth_configured()


def _request_origin_allowed(headers: Any) -> bool:
    sec_fetch_site = _header_value(headers, "Sec-Fetch-Site").lower()
    if sec_fetch_site == "cross-site":
        return False

    origin = _header_value(headers, "Origin")
    if not origin:
        return True
    try:
        parsed_origin = urlsplit(origin)
    except ValueError:
        return False
    if parsed_origin.scheme not in {"http", "https"} or not parsed_origin.netloc:
        return False

    allowed_hosts = set()
    host = _header_value(headers, "Host").lower()
    if host:
        allowed_hosts.add(host)

    configured = os.environ.get(PUBLIC_URL_ENV, "").strip()
    if configured:
        try:
            parsed_public_url = urlsplit(configured)
        except ValueError:
            parsed_public_url = None
        if parsed_public_url is not None and parsed_public_url.netloc:
            allowed_hosts.add(parsed_public_url.netloc.lower())

    return parsed_origin.netloc.lower() in allowed_hosts


def _is_loopback_host(host: str) -> bool:
    return host in {"", "127.0.0.1", "localhost", "::1"}


def _cleanup_uploads_locked(now: float | None = None) -> None:
    now = time.time() if now is None else now
    expired_ids: list[str] = []
    for upload_id, record in _UPLOADS.items():
        uploaded_expired = bool(
            record.uploaded_at is not None
            and record.uploaded_at + DEFAULT_UPLOAD_RETENTION_SECONDS <= now
        )
        ticket_expired = record.upload_path is None and record.expires_at <= now
        if uploaded_expired or ticket_expired:
            expired_ids.append(upload_id)
    for upload_id in expired_ids:
        record = _UPLOADS.pop(upload_id)
        if record.upload_path is not None:
            shutil.rmtree(record.upload_path.parent, ignore_errors=True)


def _discard_upload(upload_id: str) -> None:
    upload_root: Path | None = None
    with _UPLOAD_LOCK:
        record = _UPLOADS.pop(upload_id, None)
        if record is not None and record.upload_path is not None:
            upload_root = record.upload_path.parent
    if upload_root is not None:
        shutil.rmtree(upload_root, ignore_errors=True)


def create_upload_ticket(
    base_url: str,
    filename: str | None = None,
    max_bytes: int | None = None,
    ttl_seconds: int = DEFAULT_UPLOAD_TICKET_TTL_SECONDS,
) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    upload_id = uuid.uuid4().hex
    now = time.time()
    ttl = max(1, min(int(ttl_seconds), DEFAULT_UPLOAD_TICKET_TTL_SECONDS))
    default_limit = _max_upload_bytes()
    byte_limit = default_limit if max_bytes is None else max(1, min(int(max_bytes), default_limit))
    record = _UploadRecord(
        upload_id=upload_id,
        token_hash=_token_hash(token),
        created_at=now,
        expires_at=now + ttl,
        max_bytes=byte_limit,
        filename_hint=_safe_upload_name(filename),
    )
    with _UPLOAD_LOCK:
        _cleanup_uploads_locked(now)
        _UPLOADS[upload_id] = record
    upload_url = f"{base_url.rstrip('/')}/api/uploads/{upload_id}"
    return {
        "ok": True,
        "upload_id": upload_id,
        "upload_url": upload_url,
        "method": "PUT",
        "expires_at": _utc_timestamp(record.expires_at),
        "max_bytes": byte_limit,
        "headers": {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "X-Filename": record.filename_hint,
        },
    }


def _upload_summary(record: _UploadRecord) -> dict[str, Any]:
    return {
        "upload_id": record.upload_id,
        "filename": record.filename_hint,
        "created_at": _utc_timestamp(record.created_at),
        "expires_at": _utc_timestamp(record.expires_at),
        "uploaded_at": _utc_timestamp(record.uploaded_at) if record.uploaded_at else None,
        "complete": record.upload_path is not None and record.uploaded_at is not None,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "max_bytes": record.max_bytes,
    }


def _redact_url_userinfo(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        if not parsed.username and not parsed.password:
            return url
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        if not host:
            return "[redacted]"
        return urlunsplit(
            (parsed.scheme, f"***@{host}", parsed.path, parsed.query, parsed.fragment)
        )
    except ValueError:
        return "[redacted]" if "@" in url else url


def _redacted_config(config: dict[str, str], use_llm: bool) -> dict[str, object]:
    return {
        "provider": config["provider"],
        "model": config["model"] or "",
        "meta_model": config["meta_model"] or "",
        "structured_output": config["structured_output"] or "json_schema",
        "llm_max_concurrency": int(
            config["llm_max_concurrency"] or str(DEFAULT_LLM_MAX_CONCURRENCY)
        ),
        "base_url": _redact_url_userinfo(config["base_url"]),
        "api_key_supplied": bool(config["api_key"]),
        "use_llm": use_llm,
    }


def _current_env_scan_config(use_llm: bool) -> tuple[dict[str, str], dict[str, object]]:
    provider = os.environ.get("SKILLSPECTOR_PROVIDER", "nv_build")
    if provider not in {"nv_build", "openai", "anthropic"}:
        provider = "nv_build"
    api_key_name = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "nv_build": "NVIDIA_INFERENCE_KEY",
    }[provider]
    config = {
        "provider": provider,
        "model": os.environ.get("SKILLSPECTOR_MODEL", ""),
        "meta_model": os.environ.get("SKILLSPECTOR_META_MODEL", ""),
        "structured_output": os.environ.get(
            "SKILLSPECTOR_STRUCTURED_OUTPUT_METHOD", "json_schema"
        ).replace("-", "_"),
        "llm_max_concurrency": _normalize_llm_max_concurrency(
            os.environ.get("SKILLSPECTOR_LLM_MAX_CONCURRENCY")
        ),
        "api_key": os.environ.get(api_key_name, ""),
        "base_url": os.environ.get("OPENAI_BASE_URL", ""),
    }
    if config["structured_output"] not in STRUCTURED_OUTPUT_METHODS:
        config["structured_output"] = "json_schema"
    return config, _redacted_config(config, use_llm)


def summarize_report(report: dict[str, Any]) -> dict[str, Any]:
    risk = report.get("risk_assessment") or {}
    issues = report.get("issues") or report.get("findings") or []
    components = report.get("components") or []
    raw_score = risk.get("score", 0)
    try:
        score = int(raw_score)
    except (TypeError, ValueError):
        score = 0
    severity = str(risk.get("severity") or "LOW").upper()
    recommendation = str(risk.get("recommendation") or "SAFE").upper()
    if (
        recommendation in {"DO_NOT_INSTALL", "BLOCK", "BLOCKED"}
        or severity
        in {
            "HIGH",
            "CRITICAL",
        }
        or score >= 70
    ):
        verdict = "block"
    elif issues or severity == "MEDIUM" or score >= 30:
        verdict = "warn"
    else:
        verdict = "safe"
    top_findings = []
    for issue in issues[:5]:
        location = issue.get("location") or {}
        top_findings.append(
            {
                "severity": issue.get("severity", ""),
                "rule_id": issue.get("rule_id") or issue.get("id") or "",
                "file": issue.get("file") or location.get("file") or "",
                "line": issue.get("start_line") or location.get("start_line") or "",
                "message": issue.get("message")
                or issue.get("explanation")
                or issue.get("finding")
                or "",
            }
        )
    return {
        "verdict": verdict,
        "score": score,
        "severity": severity,
        "recommendation": recommendation,
        "issue_count": len(issues),
        "component_count": len(components),
        "top_findings": top_findings,
    }


def get_report_item(report_id: str, include_raw: bool = False) -> dict[str, Any]:
    item = _get_history_item(report_id)
    if item is None:
        return {"ok": False, "error": "Report not found"}
    payload = {
        "ok": True,
        "report_id": item["id"],
        "summary": summarize_report(item["report"]),
        "history_item": _history_summary(item),
    }
    if include_raw:
        payload["report"] = item["report"]
    return payload


@contextmanager
def _temporary_scan_environment(config: dict[str, str]) -> Iterator[None]:
    """Apply per-request provider/model env overrides while a scan runs."""
    from skillspector import constants, model_info
    from skillspector.providers import get_metadata_provider

    env_updates: dict[str, str] = {"SKILLSPECTOR_PROVIDER": config["provider"]}
    env_updates["SKILLSPECTOR_STRUCTURED_OUTPUT_METHOD"] = (
        config["structured_output"] or "json_schema"
    )
    env_updates["SKILLSPECTOR_LLM_MAX_CONCURRENCY"] = config["llm_max_concurrency"] or "1"
    if config["model"]:
        env_updates["SKILLSPECTOR_MODEL"] = config["model"]
    if config["base_url"]:
        env_updates["OPENAI_BASE_URL"] = config["base_url"]
    if config["api_key"]:
        key_name = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "nv_build": "NVIDIA_INFERENCE_KEY",
        }[config["provider"]]
        env_updates[key_name] = config["api_key"]

    env_snapshot = {key: os.environ.get(key) for key in SCAN_ENV_KEYS}
    model_config_snapshot = dict(constants.MODEL_CONFIG)
    default_model_snapshot = constants._SKILLSPECTOR_DEFAULT_MODEL
    try:
        for env_key, env_value in env_updates.items():
            os.environ[env_key] = env_value

        provider = get_metadata_provider()
        model_config = {slot: provider.resolve_model(slot) for slot in MODEL_SLOTS}
        if config["model"]:
            model_config = dict.fromkeys(MODEL_SLOTS, config["model"])
        if config["meta_model"]:
            model_config["meta_analyzer"] = config["meta_model"]

        constants.MODEL_CONFIG.clear()
        constants.MODEL_CONFIG.update(model_config)
        constants._SKILLSPECTOR_DEFAULT_MODEL = model_config["default"]
        model_info._resolve_context_length.cache_clear()
        yield
    finally:
        for env_key, value in env_snapshot.items():
            if value is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = value
        constants.MODEL_CONFIG.clear()
        constants.MODEL_CONFIG.update(model_config_snapshot)
        constants._SKILLSPECTOR_DEFAULT_MODEL = default_model_snapshot
        model_info._resolve_context_length.cache_clear()


def _history_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in (
            "id",
            "filename",
            "scanned_at",
            "score",
            "severity",
            "recommendation",
            "issue_count",
            "component_count",
            "elapsed_ms",
            "config",
        )
    }


def _record_history(
    *,
    filename: str,
    report: dict[str, Any],
    config: dict[str, object],
    elapsed_ms: int,
) -> dict[str, Any]:
    risk = report.get("risk_assessment") or {}
    issues = report.get("issues") or report.get("findings") or []
    components = report.get("components") or []
    item = {
        "id": uuid.uuid4().hex,
        "filename": filename,
        "scanned_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "score": risk.get("score", 0),
        "severity": risk.get("severity", "LOW"),
        "recommendation": risk.get("recommendation", "SAFE"),
        "issue_count": len(issues),
        "component_count": len(components),
        "elapsed_ms": elapsed_ms,
        "config": config,
        "report": report,
    }
    with _HISTORY_LOCK:
        _HISTORY.insert(0, item)
        del _HISTORY[MAX_HISTORY_ITEMS:]
    return item


def _scan_log_fields(report: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    metadata = report.get("metadata") or {}
    return {
        "verdict": summary["verdict"],
        "score": summary["score"],
        "severity": summary["severity"],
        "recommendation": summary["recommendation"],
        "issue_count": summary["issue_count"],
        "component_count": summary["component_count"],
        "llm_requested": metadata.get("llm_requested"),
        "llm_available": metadata.get("llm_available"),
        "meta_analysis_applied": metadata.get("meta_analysis_applied"),
    }


def _exception_log_fields(exc: Exception) -> dict[str, Any]:
    frames = [
        {
            "file": Path(frame.filename).name,
            "line": frame.lineno,
            "function": frame.name,
        }
        for frame in traceback.extract_tb(exc.__traceback__)[-8:]
    ]
    return {"exception_type": type(exc).__name__, "frames": frames}


def _scan_failed_payload(request_id: str) -> dict[str, Any]:
    return {"ok": False, "error": f"Scan failed; request_id={request_id}", "request_id": request_id}


def _get_history_item(item_id: str) -> dict[str, Any] | None:
    with _HISTORY_LOCK:
        return next((item for item in _HISTORY if item["id"] == item_id), None)


def _scan_uploaded_file(path: Path, use_llm: bool) -> dict[str, Any]:
    from skillspector.graph import graph

    result = graph.invoke(
        {"input_path": str(path), "output_format": "json", "use_llm": use_llm},
        config={"run_name": "skillspector-web-scan", "tags": ["skillspector", "web"]},
    )
    report_body = str(result.get("report_body") or "{}")
    report = json.loads(report_body)
    if not isinstance(report, dict):
        raise ValueError("Scanner returned a non-object JSON report")
    temp_dir = result.get("temp_dir_for_cleanup")
    if isinstance(temp_dir, str):
        shutil.rmtree(temp_dir, ignore_errors=True)
    return report


def scan_uploaded_artifact(
    upload_id: str,
    use_llm: bool = True,
    graph_scan: Callable[[Path, bool], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    _configure_web_logging()
    request_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _UPLOAD_LOCK:
        _cleanup_uploads_locked(now)
        record = _UPLOADS.get(upload_id)
        if record is None:
            return {"ok": False, "error": "Upload not found"}
        upload_path = record.upload_path
        if upload_path is None or record.uploaded_at is None:
            return {"ok": False, "error": "Upload is not complete"}
        upload_summary = _upload_summary(record)
    if not upload_path.exists():
        return {"ok": False, "error": "Uploaded file is missing"}

    config, safe_config = _current_env_scan_config(use_llm)
    scanner = graph_scan or _scan_uploaded_file
    started = time.perf_counter()
    logger.info(
        "upload_scan_received request_id=%s upload_id=%s filename=%s bytes=%d sha256=%s use_llm=%s config=%s",
        request_id,
        upload_id,
        upload_summary["filename"],
        upload_summary["size_bytes"],
        upload_summary["sha256"],
        use_llm,
        json.dumps(safe_config, sort_keys=True),
    )
    try:
        if _SCAN_LOCK.locked():
            logger.info(
                "upload_scan_waiting_for_lock request_id=%s upload_id=%s",
                request_id,
                upload_id,
            )
        queued_at = time.perf_counter()
        with _SCAN_LOCK:
            queue_ms = int((time.perf_counter() - queued_at) * 1000)
            logger.info(
                "upload_scan_started request_id=%s upload_id=%s queue_ms=%d",
                request_id,
                upload_id,
                queue_ms,
            )
            with _temporary_scan_environment(config):
                report = scanner(upload_path, use_llm)
    except Exception as exc:
        logger.error(
            "upload_scan_failed request_id=%s upload_id=%s filename=%s use_llm=%s error=%s",
            request_id,
            upload_id,
            upload_summary["filename"],
            use_llm,
            json.dumps(_exception_log_fields(exc), sort_keys=True),
        )
        raise
    finally:
        _discard_upload(upload_id)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    summary = summarize_report(report)
    history_item = _record_history(
        filename=upload_summary["filename"],
        report=report,
        config=safe_config,
        elapsed_ms=elapsed_ms,
    )
    logger.info(
        "upload_scan_completed request_id=%s upload_id=%s report_id=%s elapsed_ms=%d result=%s",
        request_id,
        upload_id,
        history_item["id"],
        elapsed_ms,
        json.dumps(_scan_log_fields(report, summary), sort_keys=True),
    )
    return {
        "ok": True,
        "report_id": history_item["id"],
        "upload": upload_summary,
        "summary": summary,
        "history_item": _history_summary(history_item),
    }


class SkillSpectorWebHandler(BaseHTTPRequestHandler):
    """HTTP handler kept small so the Web layer stays isolated from scanner code."""

    max_upload_bytes = _max_upload_bytes()
    graph_scan = staticmethod(_scan_uploaded_file)

    def log_message(self, fmt: str, *args: object) -> None:
        _configure_web_logging()
        logger.debug(fmt, *args)

    def do_GET(self) -> None:
        _configure_web_logging()
        path = urlparse(self.path).path
        if path == "/health":
            self._json(HTTPStatus.OK, _health_payload())
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self._security_headers("image/x-icon", 0)
            self.end_headers()
            return
        if path == "/":
            if not self._require_api_auth(path):
                return
            self._html(INDEX_HTML)
            return
        if path.startswith("/api/"):
            if not self._require_api_auth(path):
                return
            if path == "/api/history":
                with _HISTORY_LOCK:
                    history = [_history_summary(item) for item in _HISTORY]
                self._json(HTTPStatus.OK, {"ok": True, "history": history})
                return
            if path.startswith("/api/reports/"):
                report_id = path.rsplit("/", 1)[-1]
                query = parse_qs(urlparse(self.path).query)
                include_raw = query.get("include_raw", ["false"])[0].lower() == "true"
                payload = get_report_item(report_id, include_raw=include_raw)
                status = HTTPStatus.OK if payload["ok"] else HTTPStatus.NOT_FOUND
                self._json(status, payload)
                return
            if path.startswith("/api/history/"):
                item_id = path.rsplit("/", 1)[-1]
                item = _get_history_item(item_id)
                if item is None:
                    self._json(
                        HTTPStatus.NOT_FOUND, {"ok": False, "error": "History item not found"}
                    )
                    return
                self._json(HTTPStatus.OK, {"ok": True, "item": item})
                return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        _configure_web_logging()
        path = urlparse(self.path).path
        if not self._require_api_auth(path):
            return
        if not self._require_same_origin():
            return
        if path == "/api/tickets":
            self._handle_create_ticket()
            return
        if path.startswith("/api/scans/"):
            upload_id = path.rsplit("/", 1)[-1]
            self._handle_scan_upload(upload_id)
            return
        if path != "/api/scan":
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        self._handle_scan()

    def do_PUT(self) -> None:
        _configure_web_logging()
        path = urlparse(self.path).path
        prefix = "/api/uploads/"
        if not path.startswith(prefix) or path == prefix:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        if not self._require_same_origin():
            return
        upload_id = path[len(prefix) :]
        self._handle_upload_put(upload_id)

    def _require_api_auth(self, path: str) -> bool:
        if path.startswith("/api/uploads/"):
            return True
        if _request_is_authorized(self.headers):
            return True
        self._auth_error()
        return False

    def _require_same_origin(self) -> bool:
        if _request_origin_allowed(self.headers):
            return True
        self.close_connection = True
        self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Cross-site request rejected"})
        return False

    def _auth_error(self) -> None:
        payload = {"ok": False, "error": "Authentication required"}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self._security_headers("application/json; charset=utf-8", len(encoded))
        if os.environ.get(AUTH_TOKEN_ENV):
            self.send_header("WWW-Authenticate", 'Bearer realm="SkillSpector"')
        if os.environ.get(API_USERNAME_ENV) and os.environ.get(API_PASSWORD_ENV):
            self.send_header(
                "WWW-Authenticate", 'Basic realm="SkillSpector", charset="UTF-8"'
            )
        self.end_headers()
        self.wfile.write(encoded)

    def _request_base_url(self) -> str:
        configured = os.environ.get(PUBLIC_URL_ENV, "").strip()
        if configured:
            parsed = urlsplit(configured)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"{PUBLIC_URL_ENV} must be an http(s) URL")
            path = parsed.path.rstrip("/")
            return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

        host = _header_value(self.headers, "Host")
        if not host:
            address = self.server.server_address
            if isinstance(address, tuple) and len(address) >= 2:
                host = f"{address[0]}:{address[1]}"
            else:
                host = str(address)
        return f"http://{host}"

    @staticmethod
    def _json_boolean(payload: dict[str, Any], key: str, default: bool = False) -> bool:
        value = payload.get(key, default)
        if isinstance(value, bool):
            return value
        raise ValueError(f"{key} must be a boolean")

    def _read_json_body(self, max_bytes: int = 16 * 1024) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if content_length < 0 or content_length > max_bytes:
            raise ValueError("Request body is too large")
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _handle_create_ticket(self) -> None:
        try:
            payload = self._read_json_body()
            ticket = create_upload_ticket(
                self._request_base_url(),
                filename=payload.get("filename"),
                max_bytes=payload.get("max_bytes"),
                ttl_seconds=payload.get("ttl_seconds", DEFAULT_UPLOAD_TICKET_TTL_SECONDS),
            )
            self._json(HTTPStatus.CREATED, ticket)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def _handle_scan_upload(self, upload_id: str) -> None:
        request_id = uuid.uuid4().hex[:12]
        try:
            payload = self._read_json_body()
            use_llm = self._json_boolean(payload, "use_llm", default=True)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        logger.info(
            "upload_scan_api_received request_id=%s upload_id=%s use_llm=%s",
            request_id,
            upload_id,
            use_llm,
        )
        try:
            result = scan_uploaded_artifact(upload_id, use_llm=use_llm)
        except Exception as exc:
            logger.error(
                "upload_scan_api_failed request_id=%s upload_id=%s use_llm=%s error=%s",
                request_id,
                upload_id,
                use_llm,
                json.dumps(_exception_log_fields(exc), sort_keys=True),
            )
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, _scan_failed_payload(request_id))
            return
        if result["ok"]:
            self._json(HTTPStatus.OK, result)
            return
        status = (
            HTTPStatus.NOT_FOUND if result["error"] == "Upload not found" else HTTPStatus.CONFLICT
        )
        self._json(status, result)

    def _handle_scan(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        length = self.headers.get("Content-Length")
        if length is None:
            logger.warning("web_scan_rejected request_id=%s reason=missing_content_length", request_id)
            self._json(HTTPStatus.LENGTH_REQUIRED, {"ok": False, "error": "Missing Content-Length"})
            return
        try:
            content_length = int(length)
        except ValueError:
            logger.warning("web_scan_rejected request_id=%s reason=invalid_content_length", request_id)
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid Content-Length"})
            return
        if content_length <= 0:
            logger.warning("web_scan_rejected request_id=%s reason=empty_upload", request_id)
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Empty upload"})
            return
        if content_length > self.max_upload_bytes:
            logger.warning(
                "web_scan_rejected request_id=%s reason=upload_too_large bytes=%d max_bytes=%d",
                request_id,
                content_length,
                self.max_upload_bytes,
            )
            self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "Upload is too large"}
            )
            return

        parsed = urlparse(self.path)
        use_llm = parse_qs(parsed.query).get("use_llm", ["true"])[0].lower() == "true"
        config, safe_config = _current_env_scan_config(use_llm)
        filename = _safe_upload_name(self.headers.get("X-Filename"))
        upload_root = Path(tempfile.mkdtemp(prefix="skillspector_web_"))
        upload_path = upload_root / filename
        total_started = time.perf_counter()
        logger.info(
            "web_scan_received request_id=%s client=%s filename=%s bytes=%d use_llm=%s config=%s",
            request_id,
            self.client_address[0] if self.client_address else "",
            filename,
            content_length,
            use_llm,
            json.dumps(safe_config, sort_keys=True),
        )
        try:
            remaining = content_length
            digest = hashlib.sha256()
            with upload_path.open("wb") as fh:
                while remaining:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            if remaining:
                logger.warning(
                    "web_scan_rejected request_id=%s reason=incomplete_upload missing_bytes=%d",
                    request_id,
                    remaining,
                )
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Incomplete upload"})
                return
            logger.info(
                "web_scan_upload_complete request_id=%s filename=%s bytes=%d sha256=%s",
                request_id,
                filename,
                content_length,
                digest.hexdigest(),
            )
            started = time.perf_counter()
            if _SCAN_LOCK.locked():
                logger.info("web_scan_waiting_for_lock request_id=%s", request_id)
            queued_at = time.perf_counter()
            with _SCAN_LOCK:
                queue_ms = int((time.perf_counter() - queued_at) * 1000)
                logger.info(
                    "web_scan_started request_id=%s queue_ms=%d use_llm=%s",
                    request_id,
                    queue_ms,
                    use_llm,
                )
                with _temporary_scan_environment(config):
                    report = self.graph_scan(upload_path, use_llm)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            total_ms = int((time.perf_counter() - total_started) * 1000)
            history_item = _record_history(
                filename=filename,
                report=report,
                config=safe_config,
                elapsed_ms=elapsed_ms,
            )
            summary = summarize_report(report)
            logger.info(
                "web_scan_completed request_id=%s report_id=%s elapsed_ms=%d total_ms=%d result=%s",
                request_id,
                history_item["id"],
                elapsed_ms,
                total_ms,
                json.dumps(_scan_log_fields(report, summary), sort_keys=True),
            )
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "id": history_item["id"],
                    "filename": filename,
                    "report": report,
                    "history_item": _history_summary(history_item),
                },
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "web_scan_failed request_id=%s filename=%s use_llm=%s error=%s",
                request_id,
                filename,
                use_llm,
                json.dumps(_exception_log_fields(exc), sort_keys=True),
            )
            self._json(HTTPStatus.BAD_REQUEST, _scan_failed_payload(request_id))
        except Exception as exc:
            logger.error(
                "web_scan_failed request_id=%s filename=%s use_llm=%s error=%s",
                request_id,
                filename,
                use_llm,
                json.dumps(_exception_log_fields(exc), sort_keys=True),
            )
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, _scan_failed_payload(request_id))
        finally:
            shutil.rmtree(upload_root, ignore_errors=True)

    def _handle_upload_put(self, upload_id: str) -> None:
        length = self.headers.get("Content-Length")
        if length is None:
            self._json(HTTPStatus.LENGTH_REQUIRED, {"ok": False, "error": "Missing Content-Length"})
            return
        try:
            content_length = int(length)
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid Content-Length"})
            return
        if content_length <= 0:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Empty upload"})
            return
        token = _bearer_token(self.headers)
        if token is None:
            self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Missing bearer token"})
            return

        now = time.time()
        upload_root: Path | None = None
        upload_path: Path | None = None
        with _UPLOAD_LOCK:
            _cleanup_uploads_locked(now)
            record = _UPLOADS.get(upload_id)
            if record is None:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Upload ticket not found"})
                return
            if not secrets.compare_digest(record.token_hash, _token_hash(token)):
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Invalid bearer token"})
                return
            if record.expires_at <= now:
                _UPLOADS.pop(upload_id, None)
                self._json(HTTPStatus.GONE, {"ok": False, "error": "Upload ticket expired"})
                return
            if record.upload_path is not None:
                self._json(
                    HTTPStatus.CONFLICT, {"ok": False, "error": "Upload ticket already used"}
                )
                return
            byte_limit = min(record.max_bytes, self.max_upload_bytes)
            if content_length > byte_limit:
                self._json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"ok": False, "error": "Upload is too large"},
                )
                return
            record.filename_hint = _safe_upload_name(
                self.headers.get("X-Filename") or record.filename_hint
            )
            upload_root = Path(tempfile.mkdtemp(prefix="skillspector_mcp_upload_"))
            upload_path = upload_root / record.filename_hint
            record.upload_path = upload_path

        digest = hashlib.sha256()
        remaining = content_length
        success = False
        try:
            with upload_path.open("wb") as fh:
                while remaining:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            if remaining:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Incomplete upload"})
                return
            with _UPLOAD_LOCK:
                record.uploaded_at = time.time()
                record.size_bytes = content_length
                record.sha256 = digest.hexdigest()
                payload = _upload_summary(record)
            success = True
            logger.info(
                "upload_ticket_upload_complete upload_id=%s filename=%s bytes=%d sha256=%s",
                upload_id,
                payload["filename"],
                payload["size_bytes"],
                payload["sha256"],
            )
            self._json(HTTPStatus.OK, {"ok": True, "upload": payload})
        except OSError as exc:
            logger.warning("Upload failed: %s", exc)
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        finally:
            if not success:
                with _UPLOAD_LOCK:
                    current = _UPLOADS.get(upload_id)
                    if current is record:
                        current.upload_path = None
                if upload_root is not None:
                    shutil.rmtree(upload_root, ignore_errors=True)

    def _html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._security_headers("text/html; charset=utf-8", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._security_headers("application/json; charset=utf-8", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _security_headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )


def run_server(host: str, port: int, max_upload_mb: int) -> None:
    _configure_web_logging()
    if not _is_loopback_host(host) and not _auth_configured():
        raise typer.BadParameter(
            f"Set {AUTH_TOKEN_ENV} or {API_USERNAME_ENV}/{API_PASSWORD_ENV} before binding "
            "the Web/API server to a non-localhost interface."
        )
    handler = type(
        "ConfiguredSkillSpectorWebHandler",
        (SkillSpectorWebHandler,),
        {"max_upload_bytes": _max_upload_bytes(max_upload_mb)},
    )
    server = ThreadingHTTPServer((host, port), handler)
    typer.echo(f"SkillSpector Web listening on http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


@app.callback(invoke_without_command=True)
def main(
    host: Annotated[str, typer.Option("--host", help="Bind host.")] = DEFAULT_HOST,
    port: Annotated[int, typer.Option("--port", help="Bind port.")] = DEFAULT_PORT,
    max_upload_mb: Annotated[
        int,
        typer.Option("--max-upload-mb", help="Maximum uploaded file size."),
    ] = DEFAULT_MAX_UPLOAD_MB,
) -> None:
    """Start the local Web scanner."""
    run_server(host, port, max_upload_mb)
