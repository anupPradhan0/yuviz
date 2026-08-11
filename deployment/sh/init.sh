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

# No admin user is seeded — the first superadmin is created from the Admin
# UI's setup screen (POST /auth/bootstrap), so no clone ships with a known
# password.

echo "→ seeding default agent (ollama at ${OLLAMA_BASE_URL})"
python3 /app/scripts/seed_default_config.py

echo "✓ init complete"
