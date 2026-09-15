import asyncio


def main():
    from src.dragon import main as dragon_main
    from src.dragon.hardening import install
    from src.dragon.dashboard import HTML as DASHBOARD_HTML
    from src.dragon.futures_runner import run as run_futures
    from src.dragon.stream_transport import install as install_stream_transport

    install(dragon_main)
    install_stream_transport(dragon_main)
    dragon_main.DASHBOARD = DASHBOARD_HTML

    async def supervisor():
        await asyncio.gather(dragon_main.run(), run_futures())

    asyncio.run(supervisor())


if __name__ == "__main__":
    main()
