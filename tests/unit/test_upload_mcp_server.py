from __future__ import annotations

import pytest
import typer

from skillspector import upload_mcp_server


async def test_upload_mcp_server_registers_upload_tools_only() -> None:
    pytest.importorskip("mcp")

    server = upload_mcp_server.build_server()
    tools = {tool.name for tool in await server.list_tools()}

    assert {
        "skills_smoke",
        "skills_create_upload_ticket",
        "skills_scan_upload",
        "skills_get_report",
    } <= tools
    assert "scan_skill" not in tools

    scan_tool = next(tool for tool in await server.list_tools() if tool.name == "skills_scan_upload")
    assert scan_tool.inputSchema["properties"]["use_llm"]["default"] is True


async def test_upload_mcp_scan_returns_generic_error_when_scanner_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("mcp")

    def broken_scan(upload_id: str, use_llm: bool = True) -> dict[str, object]:
        raise RuntimeError(
            f"provider failed for {upload_id} with secret-token "
            "http://url-user:url-pass@localhost:11434/v1"
        )

    monkeypatch.setattr(upload_mcp_server, "scan_uploaded_artifact", broken_scan)
    server = upload_mcp_server.build_server()

    result = await server.call_tool("skills_scan_upload", {"upload_id": "upload-1"})
    _, data = result

    assert data["ok"] is False
    assert data["error"].startswith("Scan failed; request_id=")
    assert data["request_id"]
    assert "secret-token" not in str(result)
    assert "url-user" not in str(result)
    assert "url-pass" not in str(result)


def test_upload_mcp_http_rejects_remote_bind_without_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SKILLSPECTOR_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("SKILLSPECTOR_MCP_AUTH_TOKEN", raising=False)

    with pytest.raises(typer.BadParameter):
        upload_mcp_server.run(transport="http", host="0.0.0.0", port=0)


def test_upload_data_listener_rejects_remote_bind_without_web_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKILLSPECTOR_MCP_AUTH_TOKEN", "mcp-token")
    monkeypatch.delenv("SKILLSPECTOR_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("SKILLSPECTOR_API_USERNAME", raising=False)
    monkeypatch.delenv("SKILLSPECTOR_API_PASSWORD", raising=False)
    monkeypatch.setenv("SKILLSPECTOR_MCP_UPLOAD_HOST", "0.0.0.0")
    monkeypatch.setenv("SKILLSPECTOR_MCP_UPLOAD_PORT", "0")

    with pytest.raises(typer.BadParameter):
        upload_mcp_server.run(transport="http", host="127.0.0.1", port=0)
