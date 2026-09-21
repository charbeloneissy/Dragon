from __future__ import annotations

import grpc_testing


def test_grpc_testing_runtime_is_available() -> None:
    clock = grpc_testing.strict_real_time()
    assert clock.time() >= 0
