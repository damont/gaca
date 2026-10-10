"""Doing the few things that need root, without ever working around missing access.

Everything privileged is collected into ONE script file. If we can become root (already root,
passwordless sudo, or a terminal where sudo can ask), it runs. Otherwise the script is left on
disk and `NeedsRoot` tells the operator the one command to run; re-running the installer
afterwards picks up where it stopped.
"""
from __future__ import annotations

import os
from pathlib import Path

from .shell import Shell


class NeedsRoot(Exception):
    def __init__(self, script: Path):
        self.script = script
        super().__init__(f"run: sudo bash {script}")


def run_as_root(shell: Shell, script_text: str, script_path: Path) -> None:
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script_text)
    os.chmod(script_path, 0o700)
    if shell.euid() == 0:
        result = shell.run(["bash", str(script_path)], timeout=300)
    elif shell.run(["sudo", "-n", "true"]).returncode == 0:
        result = shell.run(["sudo", "-n", "bash", str(script_path)], timeout=300)
    elif shell.is_tty():
        result = shell.run(["sudo", "bash", str(script_path)], timeout=600, inherit=True)
    else:
        raise NeedsRoot(script_path)
    if result.returncode != 0:
        raise RuntimeError(f"the privileged step failed (exit {result.returncode}): {getattr(result, 'stderr', '') or ''}".strip())
