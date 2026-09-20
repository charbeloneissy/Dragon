"""Dragon production entrypoint.

Dragon is a Base DEX cross-exchange arbitrage engine. The deployable runtime is
the dedicated cross-DEX scanner in ``dex_cross_exchange_runner.py``; this module
keeps the package entrypoint wired to that same service.
"""


def main():
    from src.dragon.cross_exchange_runtime import run

    return run()


if __name__ == "__main__":
    main()
