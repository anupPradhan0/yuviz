#!/usr/bin/env python3
"""
Seeds the platform superadmin (run by init.sh); a no-op once one exists.

Usage: python3 scripts/seed_superadmin.py
Env:   SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD override the defaults below.
Requires: POSTGRES_ADMIN_DSN, falling back to POSTGRES_DSN (NULL-tenant row, bypasses RLS).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.config import db  # noqa: E402
from services.config import users  # noqa: E402

DEFAULT_EMAIL = "superadmin@yuviz.ai"
DEFAULT_PASSWORD = "ChangeMe@123"


async def main() -> None:
    email = os.environ.get("SUPERADMIN_EMAIL", "").strip() or DEFAULT_EMAIL
    password = os.environ.get("SUPERADMIN_PASSWORD", "").strip() or DEFAULT_PASSWORD
    await db.get_pool(dsn=os.environ.get("POSTGRES_ADMIN_DSN") or os.environ["POSTGRES_DSN"])
    user = await users.seed_superadmin(email=email, password=password)
    print(f"  ✓ created superadmin {user['email']}" if user else "  ✓ superadmin already exists")


if __name__ == "__main__":
    asyncio.run(main())
