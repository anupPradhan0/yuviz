#!/usr/bin/env bash
# Schemas -> service account -> default agent. Idempotent.
set -euo pipefail

echo "→ applying schemas"
for f in schema knowledge_schema telephony_schema; do
    psql "$POSTGRES_DSN" -v ON_ERROR_STOP=1 -q -f "/app/database/${f}.sql" >/dev/null
    echo "  ✓ ${f}.sql"
done

echo "→ service account"
# create_service_account.py raises on the unique constraint, which is the
# expected outcome on every run after the first.
if err=$(python3 /app/scripts/create_service_account.py \
             "$CONFIG_SERVICE_EMAIL" "$CONFIG_SERVICE_PASSWORD" 2>&1); then
    echo "  ✓ created ${CONFIG_SERVICE_EMAIL}"
elif printf '%s' "$err" | grep -qiE "unique|duplicate|already exists"; then
    echo "  ✓ ${CONFIG_SERVICE_EMAIL} already exists"
else
    printf '%s\n' "$err" >&2
    exit 1
fi

echo "→ admin user"
# Create, or reset the password if the account already exists. Skipping the
# reset would leave a stale password while dev.sh prints the one from .env —
# credentials that are printed but do not work are worse than none.
python3 - <<'PY'
import asyncio, os, sys
sys.path.insert(0, "/app")
from services.config import auth, db, users

async def main():
    email = os.environ["ADMIN_EMAIL"]
    password = os.environ["ADMIN_PASSWORD"]
    if await users.get_user_by_email(email) is None:
        await users.create_user(email=email, password=password,
                                role="superadmin", tenant_id=None)
        print(f"  ✓ created {email}")
    else:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET password_hash = $2, role = 'superadmin', "
                "updated_at = now() WHERE email = $1",
                email, auth.hash_password(password))
        print(f"  ✓ {email} password synced with deployment/.env")

asyncio.run(main())
PY

echo "→ seeding default agent (ollama at ${OLLAMA_BASE_URL})"
python3 /app/scripts/seed_default_config.py

echo "✓ init complete"
