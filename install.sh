#!/usr/bin/env bash
# Set up GACA on this machine. Safe to re-run (after an interruption or `git pull`): it keeps
# your secrets and data and carries on where it stopped.
#
#   ./install.sh                       # interactive
#   ./install.sh --help                # every option
#
# This script only makes sure `uv` and the Python dependencies are present, then hands over to
# `gaca-api install` (gaca-api/gaca_api/installer/), where the real steps live and are tested.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "GACA's installer supports Linux (it sets up a systemd service)."
command -v curl >/dev/null 2>&1 || die "curl is required (it is only used to fetch uv, if you do not have it)."
command -v systemctl >/dev/null 2>&1 || die "systemd is required."

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  say "uv (the Python package manager GACA uses) is not installed."
  if [ "${GACA_YES:-}" = "1" ] || [[ " $* " == *" --yes "* ]]; then
    answer=y
  else
    read -r -p "Install it for your user from https://astral.sh/uv/install.sh? [Y/n] " answer || answer=n
  fi
  case "${answer:-y}" in
    [Yy]*|"") curl -LsSf https://astral.sh/uv/install.sh | sh ;;
    *) die "uv is required. Install it (https://docs.astral.sh/uv/) and re-run." ;;
  esac
  command -v uv >/dev/null 2>&1 || die "uv installed but is not on PATH; open a new shell and re-run."
fi

say "Installing dependencies..."
(cd gaca-api && uv sync --frozen --no-dev)

exec gaca-api/.venv/bin/gaca-api install --repo-root "$PWD" "$@"
