"""Running commands and talking to the machine — behind one thin class so tests can fake it."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


class Shell:
    def run(self, argv: list[str], *, timeout: float = 120, env: dict | None = None,
            inherit: bool = False) -> subprocess.CompletedProcess:
        """Run a command. `inherit` hands over the terminal (needed for a sudo password prompt)."""
        if inherit:
            return subprocess.run(argv, timeout=timeout, env=env, text=True)
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)

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
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
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
