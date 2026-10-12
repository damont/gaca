"""Running commands and talking to the machine — behind one thin class so tests can fake it."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


# Looks like a browser so Cloudflare does not mistake a health check for a bot.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36 gaca-api"


class Shell:
    def run(self, argv: list[str], *, timeout: float = 120, env: dict | None = None,
            inherit: bool = False) -> subprocess.CompletedProcess:
        """Run a command. `inherit` hands over the terminal (needed for a sudo password prompt)."""
        if inherit:
            return subprocess.run(argv, timeout=timeout, env=env, text=True)
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)

    def service_started_at(self, name: str) -> float | None:
        """When (unix time) the system service last started, or None if it is not running."""
        out = self.run(["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "--value", f"{name}.service"], timeout=15)
        state = self.run(["systemctl", "is-active", f"{name}.service"], timeout=15)
        try:
            entered = int((out.stdout or "").strip() or 0)
        except ValueError:
            return None
        if state.returncode != 0 or (state.stdout or "").strip() != "active" or entered <= 0:
            return None
        return time.time() - (time.monotonic() - entered / 1_000_000)

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def is_tty(self) -> bool:
        return sys.stdin.isatty() and sys.stdout.isatty()

    def euid(self) -> int:
        return os.geteuid()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def http_get(self, url: str, timeout: float = 8) -> tuple[int, str]:
        """(status, body); status 0 if the request could not be made at all."""
        # Cloudflare answers 403 (error 1010) to Python's default User-Agent, which made a perfectly
        # working public address look broken. A browser-like one gets through.
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read(4096).decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, ""
        except (urllib.error.URLError, OSError, ValueError):
            return 0, ""


def user_systemd_env() -> dict:
    """The environment `systemctl --user` needs, which an ssh session without a login often lacks."""
    env = dict(os.environ)
    runtime = f"/run/user/{os.getuid()}"
    if os.path.isdir(runtime):
        env.setdefault("XDG_RUNTIME_DIR", runtime)
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    return env
