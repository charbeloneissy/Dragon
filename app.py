import asyncio
import logging

from src.dragon.cross_exchange_futures import run


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
