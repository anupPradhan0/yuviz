"""python -m services.clinic — the port from CLINIC_SERVICE_URL (default 8700), LISTEN_HOST from the
environment (default 127.0.0.1)."""

import os
from urllib.parse import urlparse

import uvicorn

if __name__ == "__main__":
    port = urlparse(os.environ.get("CLINIC_SERVICE_URL", "")).port or 8700
    uvicorn.run("services.clinic.app:app", host=os.environ.get("LISTEN_HOST", "127.0.0.1"), port=port)
