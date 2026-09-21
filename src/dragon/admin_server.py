"""Protected gRPC admin interface for Dragon diagnostics.

The admin server binds to loopback by default so it is not exposed through
Render's public web listener. When DRAGON_ADMIN_TOKEN is configured, every
admin RPC also requires matching x-dragon-admin-token metadata.
"""

from __future__ import annotations

import logging
import os
from concurrent import futures
from threading import Lock

import grpc
import grpc_admin


class _AdminAuthInterceptor(grpc.ServerInterceptor):
    """Fail closed for remote/admin calls when a token is configured."""

    def __init__(self, token: str) -> None:
        self._token = token

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None:
            return None

        metadata = dict(handler_call_details.invocation_metadata or ())
        supplied = metadata.get("x-dragon-admin-token", "")
        if supplied == self._token:
            return handler

        def deny_unary(request, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid Dragon admin token")

        def deny_stream(request_iterator, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid Dragon admin token")

        if handler.unary_unary:
            return grpc.unary_unary_rpc_method_handler(
                deny_unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.unary_stream:
            return grpc.unary_stream_rpc_method_handler(
                deny_stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.stream_unary:
            return grpc.stream_unary_rpc_method_handler(
                deny_unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.stream_stream:
            return grpc.stream_stream_rpc_method_handler(
                deny_stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        return handler


def start_admin_server() -> tuple[grpc.Server | None, dict]:
    """Start Channelz/CSDS admin services on a private loopback listener."""
    enabled = os.getenv("DRAGON_ADMIN_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if not enabled:
        return None, {"enabled": False, "reason": "disabled_by_config"}

    bind = os.getenv("DRAGON_ADMIN_BIND", "127.0.0.1").strip() or "127.0.0.1"
    if bind not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(
            "Dragon admin interface must bind to loopback; "
            f"refusing unsafe bind address {bind!r}"
        )

    port = int(os.getenv("DRAGON_ADMIN_PORT", "50051"))
    if not 1 <= port <= 65535:
        raise ValueError("DRAGON_ADMIN_PORT must be between 1 and 65535")

    token = os.getenv("DRAGON_ADMIN_TOKEN", "").strip()
    interceptors = (_AdminAuthInterceptor(token),) if token else ()

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        interceptors=interceptors,
    )
    grpc_admin.add_admin_servicers(server)

    address = f"{bind}:{port}"
    selected_port = server.add_insecure_port(address)
    if selected_port != port:
        server.stop(0)
        raise RuntimeError(
            f"failed to bind Dragon admin interface to {address}; selected_port={selected_port}"
        )

    server.start()
    auth = "token+loopback" if token else "loopback-only"
    logging.info(
        "Dragon gRPC admin interface listening on %s auth=%s "
        "(Channelz/CSDS via grpc_admin)",
        address,
        auth,
    )
    return server, {
        "enabled": True,
        "bind": bind,
        "port": port,
        "auth": auth,
        "services": ["Channelz", "CSDS"],
    }
