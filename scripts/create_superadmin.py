#!/usr/bin/env python3
"""
Creates the first superadmin user. Necessary because POST /users itself
requires an existing superadmin/admin (see services/config/routers/users.py)
— there is no other way to get the very first account into a fresh
database. Not idempotent in the sense of "safe to re-run for the same
email": users.email is UNIQUE, so a second run for the same address fails
loudly (asyncpg.UniqueViolationError) rather than silently doing nothing —
correct here, unlike seed_default_config.py's config rows, because a second
"same email" call is far more likely to be a mistake than an intentional
re-seed.

Usage: python3 scripts/create_superadmin.py <email> <password>
Requires: POSTGRES_DSN (see services/config/db.py)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.config import users  # noqa: E402


async def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <email> <password>", file=sys.stderr)
        sys.exit(1)
    email, password = sys.argv[1], sys.argv[2]

    user = await users.create_user(email=email, password=password, role="superadmin", tenant_id=None)
    print(f"Created superadmin {user['email']} (id={user['id']})")


if __name__ == "__main__":
    asyncio.run(main())
