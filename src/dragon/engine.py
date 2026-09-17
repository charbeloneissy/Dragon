"""Dragon production entrypoint.

The legacy Binance triangular runtime is intentionally not imported here.
The application runtime is provided by the dedicated cross-DEX service module.
"""


def main():
    from src.dragon.cross_exchange_runtime import run
    return run()


if __name__ == "__main__":
    main()
