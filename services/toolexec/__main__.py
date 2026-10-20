from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import uvicorn

from .app import quiet_http_loggers


def configure_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    quiet_http_loggers()


def main() -> None:
    configure_logging()
    port = urlparse(os.environ.get("TOOLEXEC_SERVICE_URL", "")).port or 8600
    uvicorn.run("services.toolexec.app:app", host=os.environ.get("LISTEN_HOST", "127.0.0.1"), port=port, log_level="info")


if __name__ == "__main__":
    main()
