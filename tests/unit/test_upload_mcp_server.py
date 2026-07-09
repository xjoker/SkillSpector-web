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
