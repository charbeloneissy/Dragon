"""Dragon deployment entrypoint.

Runs the Base DEX cross-exchange arbitrage scanner as the single managed
process. The optional local WebSocket pool engine can be enabled alongside it
with ``DEX_LOCAL_ENGINE=true``.
"""

import asyncio
import logging
import os
import subprocess
import sys


log = logging.getLogger("dragon.supervisor")


async def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    child = subprocess.Popen([sys.executable, "dex_cross_exchange_runner.py"])
    tasks = []
    local_enabled = os.getenv("DEX_LOCAL_ENGINE", "false").strip().lower() in {"1", "true", "yes", "on"}
    if local_enabled:
        from src.dragon.local_pool_state import BasePoolWebSocket, LocalPoolState

        state = LocalPoolState()
        ws = BasePoolWebSocket(state)
        tasks.append(asyncio.create_task(ws.run()))
        log.info("Local WebSocket pool engine enabled")
    else:
        log.info("Local WebSocket pool engine disabled; direct DEX scanner is active")
    try:
        while child.poll() is None:
            await asyncio.sleep(1)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(child.wait), timeout=10)
            except asyncio.TimeoutError:
                child.kill()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if child.returncode not in (0, None):
            raise SystemExit(child.returncode)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
