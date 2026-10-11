#!/usr/bin/env bash
# Verifies the gaca-api.service sandbox actually confines a process, by running a probe
# under the SAME settings as the unit and checking what it can and cannot touch. Run as root:
#   sudo ./deploy/check-sandbox.sh [user]
# Exit status is non-zero if anything that should be blocked is reachable.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 2; }
U="${1:-${SUDO_USER:-}}"; [ -n "$U" ] || { echo "usage: sudo $0 <user>" >&2; exit 2; }
H="$(getent passwd "$U" | cut -d: -f6)"
# Under /run, not /root or /tmp: the probe runs as the service user (which can't read /root),
# and the sandbox's PrivateTmp would hide /tmp.
DIR="$(mktemp -d /run/gaca-check.XXXXXX)"; chmod 755 "$DIR"; trap 'rm -rf "$DIR"' EXIT
PROBE="$DIR/probe.py"
cat > "$PROBE" <<PY
import os, pathlib, sys
h = pathlib.Path("$H")
def can_read(p):
    try: open(p, "rb").read(1); return True
    except Exception: return False
def can_write(p):
    try: open(p, "w").close(); os.unlink(p); return True
    except Exception: return False
keys = [p for p in (h / ".ssh").glob("*") if p.is_file() and not p.name.endswith(".pub")] if (h / ".ssh").exists() else []
checks = [  # (description, actual, expected)
 ("read an SSH private key", any(can_read(k) for k in keys), False),
 ("read ~/.bashrc", can_read(h / ".bashrc"), False),
 ("write ~/", can_write(h / ".gaca-probe"), False),
 ("write ~/.ssh", can_write(h / ".ssh" / ".gaca-probe"), False),
 ("write the code dir", can_write(h / "gaca" / ".gaca-probe"), False),
 ("write ~/.hermes/.env's directory", can_write(h / ".hermes" / ".gaca-probe"), False),
 ("write /etc, /usr", can_write("/etc/.gaca-probe") or can_write("/usr/.gaca-probe"), False),
 ("read ~/.hermes/.env", can_read(h / ".hermes" / ".env"), True),
 ("write ~/.hermes/profiles", can_write(h / ".hermes" / "profiles" / ".gaca-probe"), True),
 ("write the data dir", can_write(h / "gaca-data" / ".gaca-probe"), True),
]
bad = 0
for what, actual, expected in checks:
    ok = actual == expected
    bad += not ok
    print(("ok   " if ok else "FAIL ") + what + ("  (allowed)" if actual else "  (blocked)"))
sys.exit(1 if bad else 0)
PY
chmod 644 "$PROBE"
systemd-run --wait --pipe --quiet --uid="$U" \
  -p NoNewPrivileges=yes -p PrivateTmp=yes -p ProtectSystem=strict -p ProtectHome=tmpfs \
  -p BindReadOnlyPaths="$H/gaca $H/.hermes $H/.local" -p BindPaths="$H/gaca-data $H/.hermes/profiles" \
  -p ProtectKernelTunables=yes -p ProtectKernelModules=yes -p ProtectControlGroups=yes \
  -p RestrictSUIDSGID=yes -p LockPersonality=yes -p CapabilityBoundingSet= \
  /usr/bin/python3 "$PROBE"
