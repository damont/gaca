"""The README is the install guide, for people and for the agents they hand it to, so what it says
about flags, exit codes and commands must stay true."""
import argparse
import re
from pathlib import Path

from gaca_api import cli
from gaca_api.installer import run as run_mod

README = (Path(__file__).resolve().parents[2] / "README.md").read_text()


def all_flags(parser) -> set[str]:
    flags = set()
    for action in parser._actions:
        flags.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                flags |= all_flags(sub)
    return flags


def test_every_flag_the_readme_mentions_exists():
    known = all_flags(cli._parser()) | {"--token"}  # --token is cloudflared's, in the Cloudflare instructions
    mentioned = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", README))
    assert mentioned, "the README should mention flags"
    assert mentioned - known == set(), f"the README mentions flags that do not exist: {sorted(mentioned - known)}"


def test_the_exit_codes_in_the_readme_are_the_real_ones():
    rows = dict(re.findall(r"^\| (\d) \| ([^|]+) \|", README, re.M))
    assert set(rows) == {"0", "1", "3", "4"}
    assert run_mod.EXIT_NEEDS_ROOT == 3 and run_mod.EXIT_NEEDS_HERMES == 4 and run_mod.EXIT_FAILED == 1
    assert "root" in rows["3"] and "Hermes" in rows["4"]


def test_the_sudo_command_the_readme_gives_is_the_one_the_installer_prints(tmp_path):
    assert "sudo bash ~/.config/gaca/install-service.sh" in README
    assert (Path("~/.config/gaca") / "install-service.sh").name == "install-service.sh"
    import inspect
    assert "install-service.sh" in inspect.getsource(run_mod)


def test_the_cloudflare_steps_name_the_right_port_and_screens():
    assert "localhost:41700" in README and str(cli._parser().parse_args(["install", "--repo-root", "."]).port) == "41700"
    for phrase in ("Tunnels & Mesh", "published application route", "--tunnel quick", "--tunnel none"):
        assert phrase in README


def test_the_readme_says_what_an_agent_cannot_do():
    section = README.split("### Handing the install to an agent", 1)[1].split("\n## ", 1)[0]
    for human_step in ("Cloudflare", "sudo password", "hermes setup", "QR"):
        assert human_step in section
    assert "--yes" in section and "--no-wait" in section and "gaca-api doctor" in section


def test_every_cli_command_in_the_day_to_day_table_exists():
    parser = cli._parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
    table = README.split("## Day to day", 1)[1].split("\n## ", 1)[0]
    for name in re.findall(r"^\| `gaca-api (\w[\w-]*)", table, re.M):
        assert name in commands, f"the README documents `gaca-api {name}`, which does not exist"


def test_the_readme_says_the_cloudflare_page_waiting_for_the_tunnel_is_normal():
    cloudflare = README.split("## Get an address (Cloudflare)", 1)[1].split("\n## ", 1)[0]
    assert "Waiting for your Tunnel to connect" in cloudflare and "normal" in cloudflare and "Continue" in cloudflare


def test_the_readme_links_out_for_hermes_and_both_apps():
    assert "gaca-planning/hermes-setup.html" in README and (Path(__file__).resolve().parents[2] / "gaca-planning" / "hermes-setup.html").exists()
    for repo in ("gaca-android", "gaca-ios"):
        assert f"https://github.com/damont/{repo}#readme" in README
    assert "SSH access" in README and "Routes" in README and "Service URL" in README


def test_the_diagram_exists_and_the_checklist_links_land_on_real_headings():
    import re
    root = Path(__file__).resolve().parents[2]
    assert (root / "docs" / "images" / "architecture.png").stat().st_size > 10_000
    assert "](docs/images/architecture.png)" in README
    top = README[: README.index("## Set up Hermes (once)")]
    assert "## Before you install gaca-api" in top
    slugs = {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in re.findall(r"^#{2,3} (.+)$", README, re.M)}
    anchors = re.findall(r"\]\(#([a-z0-9-]+)\)", top)
    assert len(anchors) >= 5 and set(anchors) <= slugs
