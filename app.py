"""Dragon deployment entrypoint.

Keep the Render/runtime entrypoint aligned with the supervisor so the
cross-DEX scanner and optional local pool feed share one managed process.
"""

import asyncio

from dragon_supervisor import main


if __name__ == "__main__":
    asyncio.run(main())
