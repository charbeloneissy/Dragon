from __future__ import annotations

import socket

import grpc
import pytest
from grpc_channelz.v1 import channelz_pb2, channelz_pb2_grpc

from src.dragon import admin_server


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_ready(channel: grpc.Channel) -> None:
    grpc.channel_ready_future(channel).result(timeout=3)


def test_admin_channelz_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _free_port()
    monkeypatch.setenv("DRAGON_ADMIN_ENABLED", "true")
    monkeypatch.setenv("DRAGON_ADMIN_BIND", "127.0.0.1")
    monkeypatch.setenv("DRAGON_ADMIN_PORT", str(port))
    monkeypatch.setenv("DRAGON_ADMIN_TOKEN", "test-secret")

    server, state = admin_server.start_admin_server()
    assert server is not None
    assert state["auth"] == "token+loopback"
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        _wait_for_ready(channel)
        stub = channelz_pb2_grpc.ChannelzStub(channel)

        with pytest.raises(grpc.RpcError) as exc:
            stub.GetServers(channelz_pb2.GetServersRequest(start_server_id=0), timeout=2)
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED

        response = stub.GetServers(
            channelz_pb2.GetServersRequest(start_server_id=0),
            metadata=(("x-dragon-admin-token", "test-secret"),),
            timeout=2,
        )
        assert response is not None
    finally:
        channel.close()
        server.stop(0)


def test_admin_rejects_non_loopback_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRAGON_ADMIN_ENABLED", "true")
    monkeypatch.setenv("DRAGON_ADMIN_BIND", "0.0.0.0")
    monkeypatch.setenv("DRAGON_ADMIN_PORT", "50051")
    with pytest.raises(ValueError, match="loopback"):
        admin_server.start_admin_server()


def test_admin_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRAGON_ADMIN_ENABLED", "false")
    server, state = admin_server.start_admin_server()
    assert server is None
    assert state == {"enabled": False, "reason": "disabled_by_config"}
