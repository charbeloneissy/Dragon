import asyncio


def main():
    from src.dragon import main as dragon_main
    from src.dragon.hardening import install
    from src.dragon.futures_runner import run as run_futures
    install(dragon_main)
    async def supervisor():
        await asyncio.gather(dragon_main.run(), run_futures())
    asyncio.run(supervisor())


if __name__ == "__main__":
    main()
