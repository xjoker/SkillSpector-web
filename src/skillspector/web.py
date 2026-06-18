# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small local Web UI for scanning uploaded SkillSpector inputs."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, unquote, urlparse, urlsplit, urlunsplit

import typer

from skillspector.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_UPLOAD_MB = 50
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
_SCAN_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()
_HISTORY: list[dict[str, Any]] = []

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
    .shell{min-height:100vh;display:grid;grid-template-rows:auto 1fr}header{border-bottom:1px solid var(--line);background:#fff}.bar{max-width:1180px;margin:auto;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}
    h1{font-size:18px;margin:0;font-weight:750}.status{color:var(--muted);font-size:13px}.main{max-width:1180px;width:100%;margin:0 auto;padding:24px;display:grid;grid-template-columns:minmax(310px,390px) 1fr;gap:20px}
    section,.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px}.panel{padding:20px;display:flex;flex-direction:column;gap:16px}.title{font-size:15px;font-weight:700;margin:0 0 4px}.muted{color:var(--muted);margin:0}.file{border:1px dashed var(--line);border-radius:8px;padding:12px;background:#fafbfc}
    label{display:grid;gap:5px;color:#303846;font-weight:650}input,select{width:100%;border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--ink);padding:9px 10px;font:inherit}input[type=file]{padding:0;border:0}.check{display:flex;align-items:center;gap:8px;color:var(--muted);font-weight:500}.check input{width:auto}.row{display:flex;align-items:center;justify-content:space-between;gap:12px}
    button{border:0;border-radius:7px;background:var(--accent);color:#fff;font-weight:700;padding:10px 14px;cursor:pointer}button.ghost{background:#eef2f7;color:#26313d}button:disabled{opacity:.55;cursor:not-allowed}.result{min-height:360px;padding:20px}.score{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:14px 0 18px}
    .metric{border:1px solid var(--line);border-radius:8px;padding:12px}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{font-size:19px}.pill{display:inline-flex;border-radius:999px;padding:3px 8px;font-size:12px;font-weight:700;background:#eef2f7;color:#2f3a4a}.LOW{color:var(--good)}.MEDIUM{color:var(--warn)}.HIGH,.CRITICAL{color:var(--bad)}
    table{width:100%;border-collapse:collapse}th,td{text-align:left;border-top:1px solid var(--line);padding:9px;vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:700}.history{grid-column:1/-1;padding:18px 20px}.history button{padding:6px 9px}.details{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:12px}.kv{border:1px solid var(--line);border-radius:8px;padding:10px;color:var(--muted)}.kv b{display:block;color:var(--ink)}.error{border-color:#f1b4ad;background:#fff6f5;color:#7a271a}
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
      <label>Provider<select id="provider"><option value="nv_build">NVIDIA Build</option><option value="openai">OpenAI / OpenAI-compatible</option><option value="anthropic">Anthropic</option></select></label>
      <label>模型<input id="model" autocomplete="off" placeholder="留空使用 provider 默认模型"></label>
      <label>Meta 模型<input id="metaModel" autocomplete="off" placeholder="可选，仅覆盖 meta_analyzer"></label>
      <label>Structured output<select id="structuredOutput"><option value="json_schema">json_schema</option><option value="text_json">text_json / local compatible</option><option value="json_mode">json_mode</option><option value="function_calling">function_calling</option></select></label>
      <label>LLM 并发<input id="llmConcurrency" type="number" min="1" max="16" value="1"></label>
      <label>API Key<input id="apiKey" type="password" autocomplete="off" placeholder="仅本次请求使用，不写入历史"></label>
      <label>OpenAI Base URL<input id="baseUrl" autocomplete="off" placeholder="仅 openai 兼容端点需要"></label>
      <div class="row"><label class="check"><input id="staticOnly" type="checkbox" checked> 静态扫描</label><button id="scan" type="submit">开始检查</button></div>
    </form>
    <section class="result" id="result"><p class="title">结果详情</p><p class="muted">等待上传。</p></section>
    <section class="history"><div class="row"><div><p class="title">扫描历史</p><p class="muted">保留最近 50 次；不保存 API key。</p></div><button class="ghost" id="refresh" type="button">刷新</button></div><div id="history"></div></section>
  </main>
</div>
<script>
const $=id=>document.getElementById(id),form=$("form"),file=$("file"),scan=$("scan"),state=$("state"),result=$("result"),historyBox=$("history"),staticOnly=$("staticOnly");
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function headersFor(picked){const h={"X-Filename":encodeURIComponent(picked.name),"Content-Type":"application/octet-stream","X-Skillspector-Provider":$("provider").value,"X-Skillspector-Structured-Output":$("structuredOutput").value,"X-Skillspector-LLM-Max-Concurrency":$("llmConcurrency").value||"1"};for(const [id,key] of [["model","X-Skillspector-Model"],["metaModel","X-Skillspector-Meta-Model"],["apiKey","X-Skillspector-Api-Key"],["baseUrl","X-Skillspector-Base-Url"]]){const v=$(id).value.trim();if(v)h[key]=v}return h}
function findingRow(f){const loc=f.location||{};const rule=f.rule_id||f.id||"-",file=f.file||loc.file||"-",line=f.start_line||loc.start_line||"-",msg=f.message||f.explanation||f.finding||"-";return `<tr><td><span class="pill ${esc(f.severity)}">${esc(f.severity)}</span></td><td>${esc(rule)}</td><td>${esc(file)}:${esc(line)}</td><td>${esc(msg)}</td></tr>`}
function renderReport(filename,report,id){const r=report||{},risk=r.risk_assessment||{},items=r.issues||r.findings||[],meta=r.metadata||{},components=r.components||[];result.className="result";result.innerHTML=`<p class="title">${esc(filename||"扫描详情")}</p><div class="score"><div class="metric"><span>风险评分</span><strong class="${esc(risk.severity)}">${esc(risk.score??0)}/100</strong></div><div class="metric"><span>严重度</span><strong class="${esc(risk.severity)}">${esc(risk.severity??"LOW")}</strong></div><div class="metric"><span>建议</span><strong>${esc(risk.recommendation??"SAFE")}</strong></div><div class="metric"><span>问题</span><strong>${items.length}</strong></div></div><div class="details"><div class="kv"><b>${esc(r.skill?.name||"unknown")}</b>Skill</div><div class="kv"><b>${esc(meta.llm_requested?"LLM":"Static")}</b>模式</div><div class="kv"><b>${components.length}</b>组件</div><div class="kv"><b>${esc(id||"-")}</b>Run ID</div></div>${items.length?`<table><thead><tr><th>等级</th><th>规则</th><th>位置</th><th>说明</th></tr></thead><tbody>${items.map(findingRow).join("")}</tbody></table>`:"<p class=muted>未发现安全问题。</p>"}`;}
function renderError(message){result.className="result error";result.innerHTML=`<p class="title">检查失败</p><p>${esc(message)}</p>`}
async function loadHistory(){const res=await fetch("/api/history");const data=await res.json();const rows=data.history||[];historyBox.innerHTML=rows.length?`<table><thead><tr><th>时间</th><th>文件</th><th>风险</th><th>问题</th><th>模型</th><th></th></tr></thead><tbody>${rows.map(x=>`<tr><td>${esc(x.scanned_at)}</td><td>${esc(x.filename)}</td><td><span class="pill ${esc(x.severity)}">${esc(x.score)}/100 ${esc(x.severity)}</span></td><td>${esc(x.issue_count)}</td><td>${esc(x.config?.model||"default")}</td><td><button class="ghost" data-id="${esc(x.id)}">详情</button></td></tr>`).join("")}</tbody></table>`:"<p class=muted>暂无历史。</p>"}
form.addEventListener("submit",async e=>{e.preventDefault();const picked=file.files[0];if(!picked)return;scan.disabled=true;state.textContent="检查中";result.className="result";result.innerHTML="<p class=title>检查中</p><p class=muted>正在分析上传内容。</p>";try{const qs=new URLSearchParams({use_llm:String(!staticOnly.checked)});const res=await fetch(`/api/scan?${qs}`,{method:"POST",headers:headersFor(picked),body:picked});const data=await res.json();if(!data.ok)renderError(data.error);else{renderReport(data.filename,data.report,data.id);await loadHistory()}}catch(err){renderError(err.message)}finally{scan.disabled=false;state.textContent="就绪"}});
historyBox.addEventListener("click",async e=>{const id=e.target?.dataset?.id;if(!id)return;const res=await fetch(`/api/history/${encodeURIComponent(id)}`);const data=await res.json();if(data.ok)renderReport(data.item.filename,data.item.report,data.item.id);else renderError(data.error)});
$("refresh").addEventListener("click",loadHistory);loadHistory();
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
    name = Path(unquote(raw_name or "")).name.strip()
    return name or "upload.bin"


def _header_value(headers: Any, name: str) -> str:
    return (headers.get(name) or "").strip()


def _scan_config_from_headers(headers: Any) -> dict[str, str]:
    provider = _header_value(headers, "X-Skillspector-Provider") or "nv_build"
    if provider not in {"nv_build", "openai", "anthropic"}:
        raise ValueError("Unsupported provider")
    structured_output = _header_value(headers, "X-Skillspector-Structured-Output") or "json_schema"
    structured_output = structured_output.replace("-", "_")
    if structured_output not in STRUCTURED_OUTPUT_METHODS:
        raise ValueError("Unsupported structured output method")
    llm_max_concurrency = _header_value(headers, "X-Skillspector-LLM-Max-Concurrency") or "1"
    try:
        llm_max_concurrency_int = max(1, min(16, int(llm_max_concurrency)))
    except ValueError as exc:
        raise ValueError("Invalid LLM concurrency") from exc
    return {
        "provider": provider,
        "model": _header_value(headers, "X-Skillspector-Model"),
        "meta_model": _header_value(headers, "X-Skillspector-Meta-Model"),
        "structured_output": structured_output,
        "llm_max_concurrency": str(llm_max_concurrency_int),
        "api_key": _header_value(headers, "X-Skillspector-Api-Key"),
        "base_url": _header_value(headers, "X-Skillspector-Base-Url"),
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
        "llm_max_concurrency": int(config["llm_max_concurrency"] or "1"),
        "base_url": _redact_url_userinfo(config["base_url"]),
        "api_key_supplied": bool(config["api_key"]),
        "use_llm": use_llm,
    }


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
        for key, value in env_updates.items():
            os.environ[key] = value

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
        for key, value in env_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
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
    temp_dir = result.get("temp_dir_for_cleanup")
    if isinstance(temp_dir, str):
        shutil.rmtree(temp_dir, ignore_errors=True)
    return report


class SkillSpectorWebHandler(BaseHTTPRequestHandler):
    """HTTP handler kept small so the Web layer stays isolated from scanner code."""

    max_upload_bytes = _max_upload_bytes()
    graph_scan = staticmethod(_scan_uploaded_file)

    def log_message(self, fmt: str, *args: object) -> None:
        logger.debug(fmt, *args)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/history":
            with _HISTORY_LOCK:
                history = [_history_summary(item) for item in _HISTORY]
            self._json(HTTPStatus.OK, {"ok": True, "history": history})
            return
        if path.startswith("/api/history/"):
            item_id = path.rsplit("/", 1)[-1]
            item = _get_history_item(item_id)
            if item is None:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "History item not found"})
                return
            self._json(HTTPStatus.OK, {"ok": True, "item": item})
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self._security_headers("image/x-icon", 0)
            self.end_headers()
            return
        if path == "/":
            self._html(INDEX_HTML)
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/scan":
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        self._handle_scan()

    def _handle_scan(self) -> None:
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
        if content_length > self.max_upload_bytes:
            self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "Upload is too large"}
            )
            return

        parsed = urlparse(self.path)
        use_llm = parse_qs(parsed.query).get("use_llm", ["false"])[0].lower() == "true"
        try:
            config = _scan_config_from_headers(self.headers)
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        safe_config = _redacted_config(config, use_llm)
        filename = _safe_upload_name(self.headers.get("X-Filename"))
        upload_root = Path(tempfile.mkdtemp(prefix="skillspector_web_"))
        upload_path = upload_root / filename
        try:
            remaining = content_length
            with upload_path.open("wb") as fh:
                while remaining:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Incomplete upload"})
                return
            started = time.perf_counter()
            with _SCAN_LOCK:
                with _temporary_scan_environment(config):
                    report = self.graph_scan(upload_path, use_llm)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            history_item = _record_history(
                filename=filename,
                report=report,
                config=safe_config,
                elapsed_ms=elapsed_ms,
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
            logger.warning("Web scan failed: %s", exc)
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("Unexpected Web scan failure")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
        finally:
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
