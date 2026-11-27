"""The installer makes sure a headless Chromium is present, for the agent's page previews."""
import subprocess
from pathlib import Path

import pytest

from gaca_api.installer import browser
from gaca_api.installer.privilege import NeedsRoot
from gaca_api.installer.run import Options, Say, setup_browser
from gaca_api.installer.shell import Shell

ARTIFACTS_SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "gaca-artifacts" / "scripts" / "artifacts.py"


class BrowserShell(Shell):
    def __init__(self, *, have=None, sudo_n=False, euid=1000, installs="chromium-browser", rc=0):
        self.have, self.sudo_n, self._euid, self.installs, self.rc = have, sudo_n, euid, installs, rc
        self.calls = []

    def run(self, argv, *, timeout=120, env=None, inherit=False):
        self.calls.append(list(argv))
        if argv[:3] == ["sudo", "-n", "true"]:
            return subprocess.CompletedProcess(argv, 0 if self.sudo_n else 1, "", "")
        if "bash" in argv:  # the root script
            if self.rc == 0:
                self.have = self.installs
            return subprocess.CompletedProcess(argv, self.rc, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def which(self, name):
        return f"/usr/bin/{name}" if name == self.have else None

    def is_tty(self): return False
    def euid(self): return self._euid


def opts(tmp_path, **kw):
    return Options(repo_root=tmp_path, config_dir=tmp_path / "cfg", **kw)


def test_an_installed_browser_is_left_alone(tmp_path):
    shell, lines = BrowserShell(have="google-chrome"), []
    setup_browser(opts(tmp_path), shell, Say(lines.append))
    assert not any("bash" in c for c in shell.calls)
    assert "/usr/bin/google-chrome" in lines[-1]


def test_a_missing_browser_is_installed_as_root(tmp_path):
    shell, lines = BrowserShell(sudo_n=True), []
    setup_browser(opts(tmp_path), shell, Say(lines.append))
    assert ["sudo", "-n", "bash", str(tmp_path / "cfg" / "install-browser.sh")] in shell.calls
    script = (tmp_path / "cfg" / "install-browser.sh").read_text()
    assert "chromium-browser" in script and "apt-get install" in script
    assert "Chromium installed" in lines[-1]


def test_without_root_the_install_stops_with_the_command_to_run(tmp_path):
    with pytest.raises(NeedsRoot) as info:
        setup_browser(opts(tmp_path), BrowserShell(), Say(lambda _: None))
    assert info.value.script == tmp_path / "cfg" / "install-browser.sh"


def test_a_failed_package_install_is_reported(tmp_path):
    with pytest.raises(RuntimeError, match="privileged step failed"):
        setup_browser(opts(tmp_path), BrowserShell(sudo_n=True, rc=1), Say(lambda _: None))


def test_no_browser_skips_it(tmp_path):
    shell, lines = BrowserShell(), []
    setup_browser(opts(tmp_path, no_browser=True), shell, Say(lines.append))
    assert shell.calls == [] and "--no-browser" in lines[-1]


def test_the_installer_and_the_preview_look_for_the_same_browsers():
    source = ARTIFACTS_SCRIPT.read_text()
    for name in browser.BROWSERS:
        assert f'"{name}"' in source
