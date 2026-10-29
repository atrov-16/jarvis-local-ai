"""The localhost-only network boundary (security.localhost_only).

The API is meant to be reachable from this computer only: the daemon binds to a loopback
address, and every HTTP request and WebSocket connection must come from a loopback peer.
The peer is the TCP client address; proxy headers are never trusted (the daemon runs uvicorn
with proxy_headers=False).
"""

from __future__ import annotations

import ipaddress
import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

LOG = logging.getLogger(__name__)

LOCALHOST_ONLY_DETAIL = "Jarvis only accepts connections from this computer (localhost_only)."


class UnsafeBindError(RuntimeError):
    """Raised when localhost_only is set but the server would listen on another interface."""


def is_loopback_host(host: str | None) -> bool:
    """True for "localhost" and loopback IPv4/IPv6 addresses (including IPv4-mapped IPv6)."""
    if not host:
        return False
    candidate = host.strip().strip("[]")
    if candidate.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(candidate.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return False  # other host names are not trusted to resolve to loopback
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def check_bind_host(host: str, localhost_only: bool, api_token_enabled: bool = True) -> None:
    if is_loopback_host(host):
        return
    if localhost_only:
        raise UnsafeBindError(
            f"server.host is {host!r}, but security.localhost_only is enabled, so Jarvis only "
            f"listens on a loopback address (127.0.0.1, ::1 or localhost). Use a loopback host, "
            f"or set localhost_only = false to expose the API to the network deliberately."
        )
    if not api_token_enabled:
        raise UnsafeBindError(
            f"server.host is {host!r} and security.api_token_enabled is false: the API, "
            f"including task execution, would be open to the network without authentication. "
            f"Enable the API token or use a loopback host."
        )


class LocalhostOnlyMiddleware:
    """Reject HTTP requests and WebSocket connections whose peer is not a loopback address.

    A connection without a client address is allowed only on a Unix domain socket (no TCP
    server address), which can only be reached from this computer; otherwise it is refused.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        server = scope.get("server")
        unix_socket = server is None or server[1] is None
        if (client is None and unix_socket) or (client is not None and is_loopback_host(client[0])):
            await self.app(scope, receive, send)
            return

        peer = client[0] if client is not None else "an unknown address"
        LOG.warning(f"Rejected {scope['type']} connection from non-local address {peer}.")
        if scope["type"] == "websocket":
            # Closing before accepting rejects the handshake (HTTP 403).
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": LOCALHOST_ONLY_DETAIL}).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
