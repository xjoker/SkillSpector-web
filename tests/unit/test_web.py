from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from skillspector import web as web_module
from skillspector.web import SkillSpectorWebHandler


def _auth_header(token: str = "test-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _serve(handler: type[SkillSpectorWebHandler]) -> tuple[ThreadingHTTPServer, int]:
    with web_module._HISTORY_LOCK:
        web_module._HISTORY.clear()
    with web_module._UPLOAD_LOCK:
        for record in web_module._UPLOADS.values():
            if record.upload_path is not None:
                web_module.shutil.rmtree(record.upload_path.parent, ignore_errors=True)
        web_module._UPLOADS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def test_web_homepage_accepts_query_string() -> None:
    server, port = _serve(SkillSpectorWebHandler)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("GET", "/?v=test")
        response = conn.getresponse()
        body = response.read().decode()
    finally:
        server.shutdown()
        server.server_close()

    assert response.status == 200
    assert "SkillSpector Web" in body


def test_health_includes_release_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLSPECTOR_GIT_COMMIT", "abc1234")
    monkeypatch.setenv("SKILLSPECTOR_SCHEMA_VERSION", "container-v1")
    monkeypatch.setenv("SKILLSPECTOR_RELEASE_VERSION", "20260709.1")
    monkeypatch.setenv("SKILLSPECTOR_AUTH_TOKEN", "test-token")
    server, port = _serve(SkillSpectorWebHandler)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("GET", "/health")
        response = conn.getresponse()
        payload = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()

    assert response.status == 200
    assert payload["ok"] is True
    assert payload["service"] == "skillspector"
    assert payload["version"]
    assert payload["release_version"] == "20260709.1"
    assert payload["git_commit"] == "abc1234"
    assert payload["schema_version"] == "container-v1"


def test_web_scan_uploads_file_to_graph(tmp_path: Path) -> None:
    calls: list[tuple[Path, bool]] = []

    def fake_scan(path: Path, use_llm: bool) -> dict[str, Any]:
        from skillspector import constants

        calls.append((path, use_llm))
        assert path.exists()
        assert os.environ["SKILLSPECTOR_PROVIDER"] == "openai"
        assert os.environ["SKILLSPECTOR_STRUCTURED_OUTPUT_METHOD"] == "text_json"
        assert os.environ["SKILLSPECTOR_LLM_MAX_CONCURRENCY"] == "1"
        assert os.environ["OPENAI_API_KEY"] == "secret-key"
        assert os.environ["OPENAI_BASE_URL"] == "http://url-user:url-pass@localhost:11434/v1"
        assert constants.MODEL_CONFIG["default"] == "custom-model"
        assert constants.MODEL_CONFIG["meta_analyzer"] == "meta-model"
        return {
            "risk_assessment": {"score": 0, "severity": "LOW", "recommendation": "SAFE"},
            "issues": [],
            "components": [{"path": "SKILL.md"}],
            "metadata": {"llm_requested": True},
        }

    handler = type(
        "TestHandler",
        (SkillSpectorWebHandler,),
        {"graph_scan": staticmethod(fake_scan), "max_upload_bytes": 1024},
    )
    server, port = _serve(handler)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request(
            "POST",
            "/api/scan?use_llm=false",
            body=b"# skill",
            headers={
                "Content-Length": "7",
                "Content-Type": "application/octet-stream",
                "X-Filename": "../SKILL.md",
                "X-Skillspector-Provider": "openai",
                "X-Skillspector-Model": "custom-model",
                "X-Skillspector-Meta-Model": "meta-model",
                "X-Skillspector-Structured-Output": "text_json",
                "X-Skillspector-LLM-Max-Concurrency": "1",
                "X-Skillspector-Api-Key": "secret-key",
                "X-Skillspector-Base-Url": "http://url-user:url-pass@localhost:11434/v1",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.request("GET", "/api/history")
        history_response = conn.getresponse()
        history_payload = json.loads(history_response.read())
        conn.request("GET", f"/api/history/{payload['id']}")
        detail_response = conn.getresponse()
        detail_payload = json.loads(detail_response.read())
    finally:
        server.shutdown()
        server.server_close()

    assert response.status == 200
    assert payload["ok"] is True
    assert "secret-key" not in json.dumps(payload)
    assert "url-user" not in json.dumps(payload)
    assert "url-pass" not in json.dumps(payload)
    assert payload["filename"] == "SKILL.md"
    assert calls and calls[0][0].name == "SKILL.md"
    assert calls[0][1] is False
    assert history_response.status == 200
    assert history_payload["history"][0]["id"] == payload["id"]
    assert history_payload["history"][0]["config"]["api_key_supplied"] is True
    assert history_payload["history"][0]["config"]["base_url"] == "http://***@localhost:11434/v1"
    assert history_payload["history"][0]["config"]["structured_output"] == "text_json"
    assert history_payload["history"][0]["config"]["llm_max_concurrency"] == 1
    assert "secret-key" not in json.dumps(history_payload)
    assert detail_response.status == 200
    assert detail_payload["item"]["filename"] == "SKILL.md"
    assert "url-user" not in json.dumps(detail_payload)
    assert "url-pass" not in json.dumps(detail_payload)


def test_web_scan_rejects_oversized_upload() -> None:
    called = False

    def fake_scan(path: Path, use_llm: bool) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    handler = type(
        "SmallLimitHandler",
        (SkillSpectorWebHandler,),
        {"graph_scan": staticmethod(fake_scan), "max_upload_bytes": 3},
    )
    server, port = _serve(handler)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request(
            "POST",
            "/api/scan",
            body=b"abcd",
            headers={"Content-Length": "4", "X-Filename": "SKILL.md"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()

    assert response.status == 413
    assert payload["ok"] is False
    assert called is False


def test_api_requires_configured_bearer_auth(monkeypatch: Any) -> None:
    monkeypatch.setenv("SKILLSPECTOR_AUTH_TOKEN", "test-token")
    server, port = _serve(SkillSpectorWebHandler)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("GET", "/health")
        health_response = conn.getresponse()
        health_payload = json.loads(health_response.read())
        conn.request("GET", "/api/history")
        denied_response = conn.getresponse()
        denied_payload = json.loads(denied_response.read())
        conn.request("GET", "/api/history", headers=_auth_header())
        allowed_response = conn.getresponse()
        allowed_payload = json.loads(allowed_response.read())
    finally:
        server.shutdown()
        server.server_close()

    assert health_response.status == 200
    assert health_payload["ok"] is True
    assert denied_response.status == 401
    assert denied_payload["ok"] is False
    assert allowed_response.status == 200
    assert allowed_payload["ok"] is True


def test_bearer_auth_still_allows_browser_shell(monkeypatch: Any) -> None:
    monkeypatch.setenv("SKILLSPECTOR_AUTH_TOKEN", "test-token")
    server, port = _serve(SkillSpectorWebHandler)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("GET", "/")
        shell_response = conn.getresponse()
        shell_body = shell_response.read().decode()
        conn.request("GET", "/api/history")
        api_response = conn.getresponse()
        api_payload = json.loads(api_response.read())
    finally:
        server.shutdown()
        server.server_close()

    assert shell_response.status == 200
    assert "SkillSpector Web" in shell_body
    assert api_response.status == 401
    assert api_payload["ok"] is False


def test_api_accepts_basic_auth(monkeypatch: Any) -> None:
    monkeypatch.setenv("SKILLSPECTOR_API_USERNAME", "api-user")
    monkeypatch.setenv("SKILLSPECTOR_API_PASSWORD", "api-pass")
    encoded = base64.b64encode(b"api-user:api-pass").decode("ascii")
    server, port = _serve(SkillSpectorWebHandler)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("GET", "/api/history", headers={"Authorization": f"Basic {encoded}"})
        response = conn.getresponse()
        payload = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()

    assert response.status == 200
    assert payload["ok"] is True


def test_ticket_url_ignores_untrusted_forwarded_host(monkeypatch: Any) -> None:
    monkeypatch.setenv("SKILLSPECTOR_AUTH_TOKEN", "test-token")
    server, port = _serve(SkillSpectorWebHandler)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request(
            "POST",
            "/api/tickets",
            body=json.dumps({"filename": "SKILL.md", "max_bytes": 1024}),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer test-token",
                "Host": f"127.0.0.1:{port}",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "attacker.example",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()

    upload_url = urlparse(payload["upload_url"])
    assert response.status == 201
    assert upload_url.scheme == "http"
    assert upload_url.netloc == f"127.0.0.1:{port}"


def test_ticket_url_uses_configured_public_url(monkeypatch: Any) -> None:
    monkeypatch.setenv("SKILLSPECTOR_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("SKILLSPECTOR_PUBLIC_URL", "https://skillspector.example.com/base/")
    server, port = _serve(SkillSpectorWebHandler)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request(
            "POST",
            "/api/tickets",
            body=json.dumps({"filename": "SKILL.md", "max_bytes": 1024}),
            headers={"Content-Type": "application/json", **_auth_header()},
        )
        response = conn.getresponse()
        payload = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()

    assert response.status == 201
    assert payload["upload_url"].startswith("https://skillspector.example.com/base/api/uploads/")


def test_http_api_ticket_upload_scan_and_report(monkeypatch: Any) -> None:
    monkeypatch.setenv("SKILLSPECTOR_AUTH_TOKEN", "test-token")
    calls: list[tuple[str, bool]] = []

    def fake_scan(upload_id: str, use_llm: bool = False) -> dict[str, Any]:
        calls.append((upload_id, use_llm))
        report = {
            "risk_assessment": {"score": 0, "severity": "LOW", "recommendation": "SAFE"},
            "issues": [],
            "components": [{"path": "SKILL.md"}],
        }
        item = web_module._record_history(
            filename="SKILL.md", report=report, config={"use_llm": use_llm}, elapsed_ms=3
        )
        return {
            "ok": True,
            "report_id": item["id"],
            "summary": web_module.summarize_report(report),
            "history_item": web_module._history_summary(item),
        }

    monkeypatch.setattr(web_module, "scan_uploaded_artifact", fake_scan)
    server, port = _serve(SkillSpectorWebHandler)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request(
            "POST",
            "/api/tickets",
            body=json.dumps({"filename": "../SKILL.md", "max_bytes": 1024}),
            headers={"Content-Type": "application/json", **_auth_header()},
        )
        ticket_response = conn.getresponse()
        ticket = json.loads(ticket_response.read())

        upload_path = urlparse(ticket["upload_url"]).path
        conn.request(
            "PUT",
            upload_path,
            body=b"# skill",
            headers={**ticket["headers"], "Content-Length": "7"},
        )
        upload_response = conn.getresponse()
        upload_payload = json.loads(upload_response.read())

        conn.request(
            "POST",
            f"/api/scans/{ticket['upload_id']}",
            body=json.dumps({"use_llm": True}),
            headers={"Content-Type": "application/json", **_auth_header()},
        )
        scan_response = conn.getresponse()
        scan_payload = json.loads(scan_response.read())

        conn.request(
            "GET",
            f"/api/reports/{scan_payload['report_id']}",
            headers=_auth_header(),
        )
        report_response = conn.getresponse()
        report_payload = json.loads(report_response.read())
    finally:
        server.shutdown()
        server.server_close()

    assert ticket_response.status == 201
    assert ticket["ok"] is True
    assert upload_response.status == 200
    assert upload_payload["ok"] is True
    assert upload_payload["upload"]["filename"] == "SKILL.md"
    assert scan_response.status == 200
    assert scan_payload["summary"]["verdict"] == "safe"
    assert calls == [(ticket["upload_id"], True)]
    assert report_response.status == 200
    assert report_payload["ok"] is True
    assert "report" not in report_payload


def test_http_scan_rejects_non_boolean_use_llm(monkeypatch: Any) -> None:
    monkeypatch.setenv("SKILLSPECTOR_AUTH_TOKEN", "test-token")
    called = False

    def fake_scan(upload_id: str, use_llm: bool = False) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(web_module, "scan_uploaded_artifact", fake_scan)
    server, port = _serve(SkillSpectorWebHandler)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request(
            "POST",
            "/api/tickets",
            body=json.dumps({"filename": "SKILL.md", "max_bytes": 1024}),
            headers={"Content-Type": "application/json", **_auth_header()},
        )
        ticket_response = conn.getresponse()
        ticket = json.loads(ticket_response.read())
        upload_path = urlparse(ticket["upload_url"]).path
        conn.request(
            "PUT",
            upload_path,
            body=b"# skill",
            headers={**ticket["headers"], "Content-Length": "7"},
        )
        upload_response = conn.getresponse()
        upload_response.read()
        conn.request(
            "POST",
            f"/api/scans/{ticket['upload_id']}",
            body=json.dumps({"use_llm": "false"}),
            headers={"Content-Type": "application/json", **_auth_header()},
        )
        scan_response = conn.getresponse()
        scan_payload = json.loads(scan_response.read())
    finally:
        server.shutdown()
        server.server_close()

    assert ticket_response.status == 201
    assert upload_response.status == 200
    assert scan_response.status == 400
    assert scan_payload["ok"] is False
    assert "use_llm" in scan_payload["error"]
    assert called is False


def test_upload_ticket_put_then_scan_by_reference() -> None:
    calls: list[tuple[bytes, bool, str]] = []

    def fake_scan(path: Path, use_llm: bool) -> dict[str, Any]:
        calls.append((path.read_bytes(), use_llm, path.name))
        return {
            "risk_assessment": {"score": 0, "severity": "LOW", "recommendation": "SAFE"},
            "issues": [],
            "components": [{"path": "SKILL.md"}],
            "metadata": {"llm_requested": use_llm},
        }

    server, port = _serve(SkillSpectorWebHandler)
    try:
        ticket = web_module.create_upload_ticket(
            f"http://127.0.0.1:{port}", filename="../SKILL.md", max_bytes=1024, ttl_seconds=60
        )
        upload_path = urlparse(ticket["upload_url"]).path
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request(
            "PUT",
            upload_path,
            body=b"# skill",
            headers={**ticket["headers"], "Content-Length": "7", "X-Filename": "../SKILL.md"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read())
        with web_module._UPLOAD_LOCK:
            upload_record = web_module._UPLOADS[ticket["upload_id"]]
            assert upload_record.upload_path is not None
            upload_root = upload_record.upload_path.parent
        result = web_module.scan_uploaded_artifact(
            ticket["upload_id"], use_llm=False, graph_scan=fake_scan
        )
    finally:
        server.shutdown()
        server.server_close()

    assert response.status == 200
    assert payload["ok"] is True
    assert payload["upload"]["filename"] == "SKILL.md"
    assert payload["upload"]["size_bytes"] == 7
    assert payload["upload"]["sha256"] == hashlib.sha256(b"# skill").hexdigest()
    assert result["ok"] is True
    assert result["summary"]["verdict"] == "safe"
    assert "report" not in result
    assert calls == [(b"# skill", False, "SKILL.md")]
    with web_module._UPLOAD_LOCK:
        assert ticket["upload_id"] not in web_module._UPLOADS
    assert not upload_root.exists()


def test_upload_ticket_rejects_wrong_token_without_consuming_ticket() -> None:
    server, port = _serve(SkillSpectorWebHandler)
    try:
        ticket = web_module.create_upload_ticket(
            f"http://127.0.0.1:{port}", filename="SKILL.md", max_bytes=1024, ttl_seconds=60
        )
        upload_path = urlparse(ticket["upload_url"]).path
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request(
            "PUT",
            upload_path,
            body=b"bad",
            headers={
                "Authorization": "Bearer wrong-token",
                "Content-Length": "3",
                "Content-Type": "application/octet-stream",
            },
        )
        bad_response = conn.getresponse()
        bad_payload = json.loads(bad_response.read())
        conn.request(
            "PUT",
            upload_path,
            body=b"# skill",
            headers={**ticket["headers"], "Content-Length": "7"},
        )
        good_response = conn.getresponse()
        good_payload = json.loads(good_response.read())
        conn.request(
            "PUT",
            upload_path,
            body=b"again",
            headers={**ticket["headers"], "Content-Length": "5"},
        )
        repeat_response = conn.getresponse()
        repeat_payload = json.loads(repeat_response.read())
    finally:
        server.shutdown()
        server.server_close()

    assert bad_response.status == 401
    assert bad_payload["ok"] is False
    assert good_response.status == 200
    assert good_payload["ok"] is True
    assert repeat_response.status == 409
    assert repeat_payload["ok"] is False


def test_report_fetch_is_compact_by_default() -> None:
    with web_module._HISTORY_LOCK:
        web_module._HISTORY.clear()
    report = {
        "risk_assessment": {"score": 42, "severity": "MEDIUM", "recommendation": "CAUTION"},
        "issues": [{"severity": "MEDIUM", "rule_id": "T1", "message": "check this"}],
        "components": [{"path": "SKILL.md"}],
    }
    item = web_module._record_history(
        filename="SKILL.md", report=report, config={"use_llm": False}, elapsed_ms=12
    )

    compact = web_module.get_report_item(item["id"])
    raw = web_module.get_report_item(item["id"], include_raw=True)

    assert compact["ok"] is True
    assert compact["summary"]["verdict"] == "warn"
    assert compact["summary"]["issue_count"] == 1
    assert compact["history_item"]["id"] == item["id"]
    assert "report" not in compact
    assert raw["ok"] is True
    assert raw["report"] == report
