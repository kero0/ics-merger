import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from ics_merger.app import create_app
from ics_merger.config import ConfigurationError, load_settings


class LowWakeupServer(uvicorn.Server):
    async def main_loop(self) -> None:
        # Uvicorn's counter is measured in deciseconds; idle work only needs a 1 Hz wakeup.
        counter = 0
        should_exit = await self.on_tick(counter)
        while not should_exit:
            await asyncio.sleep(1.0)
            counter = (counter + 10) % 864000
            should_exit = await self.on_tick(counter)


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def run_server(application: FastAPI, host: str, port: int) -> None:
    config = uvicorn.Config(application, host=host, port=port)
    LowWakeupServer(config).run()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Merge calendar sources into one ICS feed")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        metavar="PATH",
        help="YAML configuration file (default: config.yaml)",
    )
    parser.add_argument("--host", help="Override the configured bind host")
    parser.add_argument("--port", type=_port, help="Override the configured bind port")
    arguments = parser.parse_args(argv)
    try:
        settings = load_settings(arguments.config)
    except ConfigurationError as exc:
        parser.error(str(exc))
    application = create_app(settings, config_path=arguments.config)
    run_server(
        application,
        host=arguments.host if arguments.host is not None else settings.host,
        port=arguments.port if arguments.port is not None else settings.port,
    )


if __name__ == "__main__":
    main()
