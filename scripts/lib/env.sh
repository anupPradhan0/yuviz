# Shared .env helpers for the native launchers. Source after setting REPO.
# Sourced into the operator's shell: functions return 0 on a printed hint
# (a nonzero return under `set -e` would close the terminal tab).

_rand() { head -c "$(( ${1:-32} * 3 ))" /dev/urandom | base64 | LC_ALL=C tr -cd 'A-Za-z0-9' | cut -c "1-${1:-32}"; }

_env_set() {  # Replaces KEY's line in .env (value is alphanumeric/url-safe).
  sed -i.bak "s|^$1=.*|$1=$2|" "$REPO/.env" && rm -f "$REPO/.env.bak"
}

_env_get() { grep "^$1=" "$REPO/.env" | cut -d= -f2-; }

# Creates .env from .env.example, adds keys added to the example since, and
# fills any blank platform secret. Never touches a value already set.
_env_init() {
  [ -f "$REPO/.env" ] || { (umask 077; cp "$REPO/.env.example" "$REPO/.env"); echo "✓ created .env from .env.example"; }
  local line key
  while IFS= read -r line; do
    key=${line%%=*}
    case "$line" in ''|'#'*) continue ;; esac
    grep -q "^${key}=" "$REPO/.env" || { echo "$line" >> "$REPO/.env"; echo "✓ added $key to .env"; }
  done < "$REPO/.env.example"
  local spec name len current
  for spec in JWT_SECRET:48 CONFIG_SERVICE_PASSWORD:32 YUVIZ_APP_PASSWORD:32 TOOLEXEC_ARGS_HMAC_KEY:48 SECRET_ENCRYPTION_KEY:fernet; do
    name=${spec%:*}; len=${spec#*:}
    [ -z "$(_env_get "$name")" ] || continue
    # A value already exported in this shell is the one in use: keep it, or
    # tabs sourced later would get a different key.
    current=$(printenv "$name" || true)
    if [ -n "$current" ]; then
      _env_set "$name" "$current"; echo "✓ saved $name from your shell to .env"
    elif [ "$len" = fernet ]; then
      # Fernet: 32 random bytes, url-safe base64.
      _env_set "$name" "$(head -c 32 /dev/urandom | base64 | LC_ALL=C tr '+/' '-_')"; echo "✓ generated $name"
    else
      _env_set "$name" "$(_rand "$len")"; echo "✓ generated $name"
    fi
  done
}

# Exports every non-blank .env value, except variables already set in the
# shell (so a one-off override still works).
_load_env() {
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%=*}; value=${line#*=}
    [ -n "$value" ] && [ -z "$(printenv "$key")" ] && export "$key=$value"
  done < "$REPO/.env"
  export POSTGRES_DSN="${POSTGRES_DSN:-postgresql://$USER@localhost:5432/voiceai}"
}

# Returns 1 with a pointer to .env when a setting is blank. Callers use
# `_require X || return 0` so the hint stays on screen.
_require() {
  local name
  for name in "$@"; do
    [ -n "$(printenv "$name")" ] || { echo "$name is not set — add it to $REPO/.env (see .env.example)" >&2; return 1; }
  done
}
