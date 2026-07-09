# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Additive MCP adapter for remote upload-based SkillSpector scans.

This module intentionally does not wrap the upstream ``scan_skill(target)``
tool. Remote callers receive upload tickets, put artifact bytes through a
separate HTTP endpoint, then scan by opaque ``upload_id``.
"""

from __future__ import annotations

import os
import secrets
import threading
from enum import StrEnum
from http.server import ThreadingHTTPServer
from typing import TYPE_CHECKING, Annotated, Any

import typer
from pydantic import AnyHttpUrl

from skillspector.web import (
    AUTH_TOKEN_ENV,
    DEFAULT_HOST,
    DEFAULT_UPLOAD_TICKET_TTL_SECONDS,
    SkillSpectorWebHandler,
    _auth_configured,
    _is_loopback_host,
    create_upload_ticket,
    get_report_item,
    scan_uploaded_artifact,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

DEFAULT_MCP_PORT = 8001
MCP_AUTH_TOKEN_ENV = "SKILLSPECTOR_MCP_AUTH_TOKEN"
MCP_PUBLIC_URL_ENV = "SKILLSPECTOR_MCP_PUBLIC_URL"
MCP_REQUIRED_SCOPE = "skillspector:scan"
UPLOAD_HOST_ENV = "SKILLSPECTOR_MCP_UPLOAD_HOST"
UPLOAD_PORT_ENV = "SKILLSPECTOR_MCP_UPLOAD_PORT"
UPLOAD_PUBLIC_URL_ENV = "SKILLSPECTOR_MCP_UPLOAD_PUBLIC_URL"


class Transport(StrEnum):
    stdio = "stdio"
    http = "http"


app = typer.Typer(
    name="skillspector-upload-mcp",
    help="Run the additive SkillSpector upload-ticket MCP adapter.",
    add_completion=False,
)

_UPLOAD_SERVER_LOCK = threading.Lock()
_UPLOAD_SERVER: ThreadingHTTPServer | None = None
_UPLOAD_BASE_URL: str | None = None


def _upload_host() -> str:
    return os.environ.get(UPLOAD_HOST_ENV, DEFAULT_HOST)


def _upload_port() -> int:
    raw = os.environ.get(UPLOAD_PORT_ENV, "0")
    try:
        return max(0, int(raw))
    except ValueError as exc:
        raise ValueError(f"{UPLOAD_PORT_ENV} must be an integer") from exc


def _base_url_for(host: str, port: int) -> str:
    public_url = os.environ.get(UPLOAD_PUBLIC_URL_ENV, "").strip()
    if public_url:
        return public_url.rstrip("/")

    public_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    if ":" in public_host and not public_host.startswith("["):
        public_host = f"[{public_host}]"
    return f"http://{public_host}:{port}"


def _mcp_auth_token() -> str | None:
    return os.environ.get(MCP_AUTH_TOKEN_ENV) or os.environ.get(AUTH_TOKEN_ENV)


def _mcp_resource_url(host: str, port: int) -> str:
    public_url = os.environ.get(MCP_PUBLIC_URL_ENV, "").strip()
    if public_url:
        return public_url.rstrip("/")
    return f"{_base_url_for(host, port)}/mcp"


def _ensure_upload_server() -> str:
    global _UPLOAD_BASE_URL, _UPLOAD_SERVER

    with _UPLOAD_SERVER_LOCK:
        if _UPLOAD_SERVER is not None and _UPLOAD_BASE_URL is not None:
            return _UPLOAD_BASE_URL

        host = _upload_host()
        if not _is_loopback_host(host) and not _auth_configured():
            raise typer.BadParameter(
                f"Set {AUTH_TOKEN_ENV} or Web/API Basic auth before binding the MCP upload "
                "data listener to a non-localhost interface."
            )

        server = ThreadingHTTPServer((host, _upload_port()), SkillSpectorWebHandler)
        thread = threading.Thread(
            target=server.serve_forever,
            name="skillspector-upload-mcp-data",
            daemon=True,
        )
        thread.start()
        _UPLOAD_SERVER = server
        bound_host, bound_port = server.server_address[:2]
        _UPLOAD_BASE_URL = _base_url_for(str(bound_host), int(bound_port))
        return _UPLOAD_BASE_URL


def build_server(
    name: str = "skillspector-upload",
    *,
    auth_token: str | None = None,
    resource_url: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_MCP_PORT,
) -> FastMCP:
    """Construct the upload-ticket MCP server without exposing raw path scans."""
    try:
        from mcp.server.auth.provider import AccessToken
        from mcp.server.auth.settings import AuthSettings
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "The upload MCP adapter requires the optional 'mcp' dependency. "
            "Install it with: pip install 'skillspector[mcp]'"
        ) from exc

    kwargs: dict[str, Any] = {"host": host, "port": port}
    if auth_token:
        required_token = auth_token

        class StaticTokenVerifier:
            async def verify_token(self, token: str) -> AccessToken | None:
                if secrets.compare_digest(token, required_token):
                    return AccessToken(
                        token=token,
                        client_id="skillspector-upload-mcp",
                        scopes=[MCP_REQUIRED_SCOPE],
                    )
                return None

        auth_url = resource_url or _mcp_resource_url(host, port)
        kwargs["token_verifier"] = StaticTokenVerifier()
        kwargs["auth"] = AuthSettings(
            issuer_url=AnyHttpUrl(auth_url),
            resource_server_url=AnyHttpUrl(auth_url),
            required_scopes=[MCP_REQUIRED_SCOPE],
        )

    server = FastMCP(name, **kwargs)

    @server.tool()
    def skills_smoke() -> dict[str, Any]:
        """Return a lightweight health response and the HTTP upload base URL."""
        return {"ok": True, "upload_base_url": _ensure_upload_server()}

    @server.tool()
    def skills_create_upload_ticket(
        filename: str | None = None,
        max_bytes: int | None = None,
        ttl_seconds: int = DEFAULT_UPLOAD_TICKET_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Create a one-use HTTP upload ticket for a skill file or archive."""
        return create_upload_ticket(
            _ensure_upload_server(),
            filename=filename,
            max_bytes=max_bytes,
            ttl_seconds=ttl_seconds,
        )

    @server.tool()
    def skills_scan_upload(upload_id: str, use_llm: bool = False) -> dict[str, Any]:
        """Scan a previously uploaded artifact by upload id and return a compact verdict."""
        return scan_uploaded_artifact(upload_id, use_llm=use_llm)

    @server.tool()
    def skills_get_report(report_id: str, include_raw: bool = False) -> dict[str, Any]:
        """Fetch a compact report summary, optionally including the raw report body."""
        return get_report_item(report_id, include_raw=include_raw)

    return server


def run(
    transport: Transport | str = Transport.stdio,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_MCP_PORT,
) -> None:
    """Run the additive MCP adapter over stdio or streamable HTTP."""
    transport_value = transport.value if isinstance(transport, Transport) else transport
    auth_token = _mcp_auth_token() if transport_value == Transport.http.value else None
    if transport_value == Transport.http.value and not _is_loopback_host(host) and not auth_token:
        raise typer.BadParameter(
            f"Set {MCP_AUTH_TOKEN_ENV} or {AUTH_TOKEN_ENV} before binding MCP HTTP "
            "to a non-localhost interface."
        )
    server = build_server(
        auth_token=auth_token,
        resource_url=_mcp_resource_url(host, port) if auth_token else None,
        host=host,
        port=port,
    )
    if transport_value == Transport.stdio.value:
        server.run(transport="stdio")
    elif transport_value == Transport.http.value:
        _ensure_upload_server()
        server.settings.host = host
        server.settings.port = port
        server.run(transport="streamable-http")
    else:
        raise ValueError(f"transport must be 'stdio' or 'http', got {transport_value!r}")


@app.callback(invoke_without_command=True)
def main(
    transport: Annotated[
        Transport,
        typer.Option("--transport", help="MCP transport for the control plane."),
    ] = Transport.stdio,
    host: Annotated[str, typer.Option("--host", help="MCP HTTP bind host.")] = DEFAULT_HOST,
    port: Annotated[int, typer.Option("--port", help="MCP HTTP bind port.")] = DEFAULT_MCP_PORT,
) -> None:
    """Start the upload-ticket MCP adapter."""
    run(transport=transport, host=host, port=port)
