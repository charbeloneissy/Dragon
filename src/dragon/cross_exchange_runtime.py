"""Production entrypoint bridge for Dragon's 2-leg cross-DEX runtime.

The deployable Render service lives in ``dex_cross_exchange_runner.py`` so it
can expose the health/dashboard HTTP endpoints. This module keeps the Python
package entrypoint connected to that same runtime instead of leaving a dead
placeholder behind.
"""

import asyncio


def run():
    from dex_cross_exchange_runner import main

    return asyncio.run(main())
