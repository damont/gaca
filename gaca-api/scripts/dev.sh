# gaca-api dev helpers — poke the running API by hand.
#
# SOURCE this file, don't execute it:
#     source scripts/dev.sh          # (run from gaca-api/, in each terminal)
#
# The active session id is kept in a temp file, so every terminal that sourced
# this shares it: create once with `gaca-new`, use it everywhere.
#
# Commands:
#   gaca-new            create a new session and remember it (prints the id)
#   gaca-sid            print the current session id
#   gaca-listen [seq]   open the SSE stream (optionally resume after Last-Event-ID)
#   gaca-say <text>     post a user message to the current session
#   gaca-show           show the session + its stored messages (the transcript)
#
# Engine (Hermes gateway) probes — for when our SSE stream times out and we
# need to know whether the run is actually still alive on the engine:
#   hermes-run <run_id> ask the engine for that run's status (404 == gone)
#   hermes-health       how many agents the engine is running right now
#
# Override the targets with:  GACA_BASE=http://host:port  (gaca-api) and
# HERMES_BASE=http://host:port (gateway) before sourcing.

GACA_BASE="${GACA_BASE:-http://localhost:41700}"
GACA_SID_FILE="${GACA_SID_FILE:-/tmp/gaca_sid}"

# Every app route lives under /api. Derived from GACA_BASE rather than folded
# into it, so overriding the host stays a plain host:port with no path.
GACA_API="$GACA_BASE/api"

# Create a new session and remember its id (also prints it).
gaca-new() {
  curl -s -X POST "$GACA_API/sessions" \
    -H 'content-type: application/json' -d '{"title":"dev"}' \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])" \
    | tee "$GACA_SID_FILE"
}

# Print the current session id.
gaca-sid() { cat "$GACA_SID_FILE"; }

# Open the SSE event stream for the current session (stays open; Ctrl-C to stop).
# Pass a seq to replay after that Last-Event-ID, e.g. `gaca-listen 2`.
gaca-listen() {
  local sid; sid=$(cat "$GACA_SID_FILE")
  if [ -n "$1" ]; then
    curl -N -H "Last-Event-ID: $1" "$GACA_API/sessions/$sid/events"
  else
    curl -N "$GACA_API/sessions/$sid/events"
  fi
}

# Post a user message to the current session: `gaca-say Say hi in three words`.
# JSON is built with python so any characters in the text are safe.
gaca-say() {
  local sid; sid=$(cat "$GACA_SID_FILE")
  local body; body=$(python3 -c "import json,sys;print(json.dumps({'content':' '.join(sys.argv[1:])}))" "$@")
  curl -s -X POST "$GACA_API/sessions/$sid/messages" \
    -H 'content-type: application/json' -d "$body"; echo
}

# Show the session + its stored messages (the durable transcript, not events).
gaca-show() {
  local sid; sid=$(cat "$GACA_SID_FILE")
  curl -s "$GACA_API/sessions/$sid" | python3 -m json.tool
}

# --- Engine (Hermes gateway) probes --------------------------------------
# The run itself lives on the engine (:8642), NOT gaca-api. When our SSE stream
# looks stuck we ask the engine directly whether the run is alive. These talk to
# the gateway with its own bearer, loaded fresh from ~/.hermes/.env each call.
HERMES_BASE="${HERMES_BASE:-http://127.0.0.1:8642}"
HERMES_ENV="${HERMES_ENV:-$HOME/.hermes/.env}"

# Print the gateway bearer (API_SERVER_KEY) from ~/.hermes/.env, if present.
_hermes_key() {
  [ -f "$HERMES_ENV" ] && ( set -a; . "$HERMES_ENV"; set +a; printf '%s' "$API_SERVER_KEY" )
}

# Ask the engine for a run's status:  hermes-run <run_id>
# A "running" state == still alive (our stream just went quiet); a 404 == the
# engine no longer knows this run (it finished and was evicted, or it died).
hermes-run() {
  curl -s "$HERMES_BASE/v1/runs/$1" \
    -H "Authorization: Bearer $(_hermes_key)"; echo
}

# How many agents the engine is running right now (delegated sub-agents count).
# If this stays >0 while our stream is silent, the run is alive and working.
hermes-health() {
  curl -s "$HERMES_BASE/health/detailed" \
    -H "Authorization: Bearer $(_hermes_key)"; echo
}

echo "gaca dev helpers loaded (base $GACA_BASE, engine $HERMES_BASE):"
echo "  gaca-new | gaca-sid | gaca-listen [seq] | gaca-say <text> | gaca-show"
echo "  hermes-run <run_id> | hermes-health"
