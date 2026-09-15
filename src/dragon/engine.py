import asyncio


def main():
    from src.dragon import main as dragon_main
    from src.dragon.hardening import install
    install(dragon_main)
    asyncio.run(dragon_main.run())


if __name__ == "__main__":
    main()
