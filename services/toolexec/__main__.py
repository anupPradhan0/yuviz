from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    port = int(os.environ.get("PORT", "8600"))  # 8400 is campaigns, 8750 is telephony
    uvicorn.run("services.toolexec.app:app", host=os.environ.get("LISTEN_HOST", "127.0.0.1"), port=port, log_level="info")


if __name__ == "__main__":
    main()
