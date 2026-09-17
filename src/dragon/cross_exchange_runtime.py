"""Minimal production runtime for the 2-leg cross-DEX engine.

This module intentionally has no imports from the legacy Binance triangle
pipeline. It is a wiring point; live execution remains disabled until the
cross-DEX adapter/executor configuration is explicitly verified.
"""


def run():
    raise RuntimeError(
        "cross-DEX runtime is not configured: wire DexCrossExchangeEngine and "
        "the atomic 2-leg executor before enabling live execution"
    )
