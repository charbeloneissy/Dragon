import asyncio


def main():
    from src.dragon import main as dragon_main
    from src.dragon.hardening import install
    from src.dragon.dashboard import HTML as DASHBOARD_HTML
    from src.dragon.futures_runner import run as run_futures
    from src.dragon.stream_transport import install as install_stream_transport
    import websockets

    install(dragon_main)
    install_stream_transport(dragon_main)
    dragon_main.DASHBOARD = DASHBOARD_HTML

    # Binance can occasionally stop delivering enough traffic for the default
    # websocket keepalive window. Give the connection more recovery headroom.
    original_connect = websockets.connect

    def resilient_connect(*args, **kwargs):
        kwargs["ping_interval"] = 10
        kwargs["ping_timeout"] = 30
        kwargs["close_timeout"] = 5
        return original_connect(*args, **kwargs)

    websockets.connect = resilient_connect

    # Render routes the public service to the root path. Serve the dashboard
    # directly there instead of relying on a redirect from `/`.
    original_get = dragon_main.Handler.do_GET

    def dashboard_root(self):
        if self.path == "/":
            self.path = "/dashboard"
            try:
                return original_get(self)
            finally:
                self.path = "/"
        return original_get(self)

    dragon_main.Handler.do_GET = dashboard_root

    async def supervisor():
        await asyncio.gather(dragon_main.run(), run_futures())

    asyncio.run(supervisor())


if __name__ == "__main__":
    main()
