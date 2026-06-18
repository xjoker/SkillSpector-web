from __future__ import annotations

import http.client
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from skillspector import web as web_module
from skillspector.web import SkillSpectorWebHandler


def _serve(handler: type[SkillSpectorWebHandler]) -> tuple[ThreadingHTTPServer, int]:
    with web_module._HISTORY_LOCK:
        web_module._HISTORY.clear()
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
