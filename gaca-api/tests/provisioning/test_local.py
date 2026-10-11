"""Local mode: what it does, and — mostly — what it refuses to do.

The boundary under test (see provisioning/local.py): read a fixed set of files under
HERMES_HOME, write one profile .env, run only `hermes --version` and
`hermes profile create <name> --no-alias`, and never hand gaca-api's own secrets to a child.
Every test that tries to escape uses a real temp directory with real symlinks/FIFOs and a
real stub `hermes` executable that records exactly how it was invoked.
"""
import asyncio
import json
import os
import re
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from gaca_api.provisioning import local
from gaca_api.provisioning.base import ProvisioningError
from gaca_api.provisioning.local import LocalError, LocalHermes, argv_allowed, stock_texts_from_source

KEY = "k" * 32
OUTSIDE_SECRET = "TOP-SECRET-OUTSIDE-HERMES-HOME"
PROVIDER_SECRET = "sk-provider-key-must-never-leave"


def run(coro):
    return asyncio.run(coro)


def write(path: Path, text: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        path.chmod(mode)
    return path


@pytest.fixture
def box(tmp_path):
    """A fake HERMES_HOME plus a stub hermes that logs every invocation."""
    home = tmp_path / "hermes-home"
    write(home / ".env", f"OPENAI_API_KEY={PROVIDER_SECRET}\nAPI_SERVER_ENABLED=true\n"
                         f"API_SERVER_PORT=8642\nAPI_SERVER_KEY={'d' * 32}\n")
    write(home / "profiles" / "toolshed" / ".env",
          f"ANTHROPIC_API_KEY={PROVIDER_SECRET}\nAPI_SERVER_ENABLED=true\n"
          f"API_SERVER_PORT=8643\nAPI_SERVER_KEY={'t' * 32}\n")
    write(home / "profiles" / "toolshed" / "SOUL.md", "I am the toolshed persona.")
    write(home / "profiles" / "toolshed" / "profile.yaml", "description: Tools\n")
    write(home / "profiles" / "quiet" / ".env", "API_SERVER_ENABLED=false\n")
    write(home / "hermes-agent" / "hermes_cli" / "default_soul.py", textwrap.dedent('''\
        DEFAULT_SOUL_MD = ("You are " "Hermes.")
        _LEGACY_TEMPLATE_SOULS = (DEFAULT_SOUL_MD + "!", DEFAULT_SOUL_MD.replace("o", "0"))
        '''))

    log = tmp_path / "hermes-calls.jsonl"
    stub = tmp_path / "bin" / "hermes"
    write(stub, textwrap.dedent(f'''\
        #!{sys.executable}
        import json, os, pathlib, sys
        with open({str(log)!r}, "a") as f:
            f.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}}) + "\\n")
        if sys.argv[1:] == ["--version"]:
            print("Hermes Agent v0.21.5")
        elif sys.argv[1:3] == ["profile", "create"]:
            home = pathlib.Path({str(home)!r})
            (home / "profiles" / sys.argv[3]).mkdir(parents=True, exist_ok=True)
        '''), mode=0o755)
    hermes = LocalHermes(home, str(stub))

    served = set()

    async def fake_served(port, name):
        return name in served
    hermes._served_by_default = staticmethod(fake_served)
    return type("Box", (), {"home": home, "hermes": hermes, "log": log, "stub": stub,
                            "served": served, "tmp": tmp_path})


def calls(box):
    return [json.loads(line) for line in box.log.read_text().splitlines()] if box.log.exists() else []


# ── what it does ─────────────────────────────────────────────────────

def test_discovery_reports_each_enabled_profile_and_nothing_else(box):
    found = run(box.hermes.discover_profiles())
    assert set(found) == {"default", "toolshed"}  # "quiet" has the gateway off
    assert found["toolshed"] == {"port": 8643, "api_key": "t" * 32, "path": ""}
    # The provider keys sitting in those same .env files must not leave the module.
    assert PROVIDER_SECRET not in json.dumps(found)


def test_discovery_follows_the_default_gateway_for_multiplexed_profiles(box):
    box.served.add("toolshed")
    found = run(box.hermes.discover_profiles())
    assert found["toolshed"] == {"port": 8642, "api_key": "t" * 32, "path": "/p/toolshed"}


def test_next_free_port_is_one_past_the_highest(box):
    assert run(box.hermes.next_free_port()) == 8644


def test_reads_purpose_files_and_parses_stock_texts_without_running_them(box):
    files = run(box.hermes.read_profile_files("toolshed"))
    assert files["soul"] == "I am the toolshed persona."
    assert files["profile_yaml"] == "description: Tools\n"
    assert files["stock"] == ["You are Hermes.", "You are Hermes.!", "Y0u are Hermes."]
    assert run(box.hermes.read_profile_files("ghost")) is None
    assert run(box.hermes.read_profile_files("default"))["soul"] is None  # no SOUL.md there


def test_create_profile_runs_the_one_command_and_writes_the_env(box):
    box.served.add("toolshed")  # the default gateway is multiplexing already

    async def go():
        async def serve_soon():
            await asyncio.sleep(0.2)
            box.served.add("fresh")
        asyncio.get_running_loop().create_task(serve_soon())
        return await box.hermes.create_profile("fresh", 9999, KEY)

    port, path = run(go())
    assert (port, path) == (8642, "/p/fresh")  # the default gateway's port, not 9999
    assert [c["argv"] for c in calls(box)] == [["profile", "create", "fresh", "--no-alias"]]
    env = box.home / "profiles" / "fresh" / ".env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert env.read_text().splitlines() == [
        "API_SERVER_ENABLED=true", "API_SERVER_HOST=0.0.0.0", "API_SERVER_PORT=8642", f"API_SERVER_KEY={KEY}",
    ]


def test_create_profile_upserts_into_an_existing_env_without_dropping_lines(box):
    box.served.update({"toolshed", "keep"})
    write(box.home / "profiles" / "keep" / ".env", "FOO=bar\nAPI_SERVER_PORT=1\n")
    stub_text = box.stub.read_text()
    box.stub.write_text(stub_text)  # profile dir already exists; create is idempotent in the stub
    run(box.hermes.create_profile("keep", 1, KEY))
    lines = (box.home / "profiles" / "keep" / ".env").read_text().splitlines()
    assert lines[0] == "FOO=bar" and "API_SERVER_PORT=8642" in lines and f"API_SERVER_KEY={KEY}" in lines
    assert lines.count("API_SERVER_PORT=8642") == 1


def test_create_profile_refuses_a_hermes_that_cannot_serve_it(box):
    # No other profile is multiplexed and the version is too old: don't pretend it will work.
    box.stub.write_text(box.stub.read_text().replace("v0.21.5", "v0.18.0"))
    with pytest.raises(LocalError, match="0.21"):
        run(box.hermes.create_profile("fresh", 9999, KEY))


# ── names: the only user-controlled input ────────────────────────────

BAD_NAMES = [
    "..", ".", "../etc", "../../etc/passwd", "a/b", "a\\b", "/etc/passwd", "", " ", " a", "a ",
    "a b", "a\nb", "a\x00b", "x" * 65, "-rf", "_x", "a;id", "a$(id)", "a`id`", "a|b", "toolshed/..",
    "toolshed\n", "ｔoolshed", "ünï",
]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_bad_names_are_refused_everywhere_and_nothing_runs(box, name):
    with pytest.raises(LocalError):
        run(box.hermes.read_profile_files(name))
    with pytest.raises(LocalError):
        run(box.hermes.create_profile(name, 8650, KEY))
    assert calls(box) == []
    assert not argv_allowed(("profile", "create", name, "--no-alias"))


def test_default_cannot_be_created_and_other_junk_keys_are_refused(box):
    with pytest.raises(LocalError):
        run(box.hermes.create_profile("default", 8650, KEY))
    for bad_key in ["short", "x" * 300, "a" * 32 + "\nEVIL=1", "a" * 32 + " ", "a" * 31 + ";", ""]:
        with pytest.raises(LocalError):
            run(box.hermes.create_profile("fresh", 8650, bad_key))
    assert calls(box) == []


# ── files: it cannot be steered outside HERMES_HOME ──────────────────

def test_a_symlinked_soul_is_not_followed(box, tmp_path):
    outside = write(tmp_path / "outside" / "secret.txt", OUTSIDE_SECRET)
    soul = box.home / "profiles" / "toolshed" / "SOUL.md"
    soul.unlink()
    soul.symlink_to(outside)
    files = run(box.hermes.read_profile_files("toolshed"))
    assert files["soul"] is None and OUTSIDE_SECRET not in json.dumps(files)


def test_a_symlinked_profile_directory_is_refused(box, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    write(elsewhere / "SOUL.md", OUTSIDE_SECRET)
    write(elsewhere / ".env", f"API_SERVER_ENABLED=true\nAPI_SERVER_PORT=1234\nAPI_SERVER_KEY={'z' * 32}\n")
    (box.home / "profiles" / "evil").symlink_to(elsewhere)
    assert run(box.hermes.read_profile_files("evil")) is None
    assert "evil" not in run(box.hermes.discover_profiles())
    # ...and it cannot be "created over" either: hermes is never run for a name that exists as a link
    # (the stub would have logged it), and the link target is untouched.
    assert (elsewhere / ".env").read_text().count("1234") == 1


def test_a_symlinked_profiles_directory_is_refused(box, tmp_path):
    real = tmp_path / "moved-profiles"
    (box.home / "profiles").rename(real)
    (box.home / "profiles").symlink_to(real)
    assert run(box.hermes.read_profile_files("toolshed")) is None
    assert set(run(box.hermes.discover_profiles())) == {"default"}


def test_a_symlinked_env_is_ignored(box, tmp_path):
    outside = write(tmp_path / "outside.env", f"API_SERVER_ENABLED=true\nAPI_SERVER_PORT=1111\nAPI_SERVER_KEY={'o' * 32}\n")
    env = box.home / "profiles" / "toolshed" / ".env"
    env.unlink()
    env.symlink_to(outside)
    assert "toolshed" not in run(box.hermes.discover_profiles())


def test_a_fifo_does_not_hang_the_reader(box):
    soul = box.home / "profiles" / "toolshed" / "SOUL.md"
    soul.unlink()
    os.mkfifo(soul)
    started = time.monotonic()
    files = run(box.hermes.read_profile_files("toolshed"))
    assert files["soul"] is None and time.monotonic() - started < 2


def test_reads_are_size_capped(box):
    (box.home / "profiles" / "toolshed" / "SOUL.md").write_text("x" * 500_000)
    assert len(run(box.hermes.read_profile_files("toolshed"))["soul"]) == 40_000


def test_create_writes_through_a_symlinked_env_by_replacing_the_link_not_the_target(box, tmp_path):
    box.served.update({"toolshed", "victim"})
    victim = write(tmp_path / "victim-target", "DO-NOT-TOUCH\n")
    d = box.home / "profiles" / "victim"
    d.mkdir()
    (d / ".env").symlink_to(victim)
    run(box.hermes.create_profile("victim", 1, KEY))
    assert victim.read_text() == "DO-NOT-TOUCH\n"
    assert not (d / ".env").is_symlink() and f"API_SERVER_KEY={KEY}" in (d / ".env").read_text()


def test_create_touches_nothing_outside_the_new_profile(box):
    box.served.update({"toolshed", "fresh"})

    def snapshot():
        return {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in box.home.rglob("*")
                if p.is_file() and "profiles/fresh" not in str(p)}
    before = snapshot()
    run(box.hermes.create_profile("fresh", 1, KEY))
    assert snapshot() == before


# ── default_soul.py is parsed, never executed ────────────────────────

def test_a_hostile_default_soul_is_never_executed(box, tmp_path):
    marker = tmp_path / "PWNED"
    write(box.home / "hermes-agent" / "hermes_cli" / "default_soul.py", textwrap.dedent(f'''\
        import os
        open({str(marker)!r}, "w").write("x")
        os.system("touch {marker}2")
        DEFAULT_SOUL_MD = __import__("os").getcwd()
        _LEGACY_TEMPLATE_SOULS = ("ok", open("/etc/passwd").read())
        '''))
    files = run(box.hermes.read_profile_files("toolshed"))
    assert not marker.exists() and not Path(str(marker) + "2").exists()
    assert files["stock"] == []  # the legacy tuple holds a call, so none of it is trusted


@pytest.mark.parametrize("source", [
    "x = (", "DEFAULT_SOUL_MD = [c for c in 'ab']", "DEFAULT_SOUL_MD = lambda: 1",
    "DEFAULT_SOUL_MD = f'{1}'", "DEFAULT_SOUL_MD = 'a' * 10", "DEFAULT_SOUL_MD = ('a'.upper())",
    "DEFAULT_SOUL_MD = " + " + ".join(["'a'"] * 300), "\x00",
])
def test_odd_default_soul_sources_yield_no_stock_texts_and_do_not_raise(source):
    assert stock_texts_from_source(source) == []


# ── commands: one allowlist, no shell, no secrets ────────────────────

@pytest.mark.parametrize("argv", [
    ("profile", "delete", "x"), ("profile", "create", "x"), ("profile", "create", "x", "--no-alias", "--extra"),
    ("profile", "create", "default", "--no-alias"), ("profile", "create", "x;id", "--no-alias"),
    ("--version", "--help"), ("chat",), ("config", "set", "a", "b"), ("gateway", "install"),
    ("--profile", "x", "gateway", "install", "--start-now"), ("-c", "id"), ("--version;id",), (),
    ("run", "bash"), ("skills", "install", "http://evil"), ("profile", "create", "-x", "--no-alias"),
])
def test_only_two_invocations_are_on_the_allowlist(box, argv):
    assert not argv_allowed(argv)
    with pytest.raises(LocalError, match="allowlist"):
        run(box.hermes._hermes(*argv, timeout=5))
    assert calls(box) == []


def test_the_allowlist_accepts_exactly_the_two_shapes():
    assert argv_allowed(("--version",))
    assert argv_allowed(("profile", "create", "good-name_1", "--no-alias"))


def test_children_do_not_inherit_gaca_secrets(box, monkeypatch):
    monkeypatch.setenv("GACA_JWT_SECRET", "jwt-must-not-leak")
    monkeypatch.setenv("GACA_FCM_CREDENTIALS_PATH", "/secrets/fcm.json")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-must-not-leak")
    box.served.update({"toolshed", "fresh"})
    run(box.hermes.create_profile("fresh", 1, KEY))
    logged = calls(box)
    assert logged
    for call in logged:
        assert set(call["env"]) <= {"PATH", "HOME", "LANG", "HERMES_HOME", "LC_CTYPE"}
        blob = json.dumps(call["env"])
        assert "must-not-leak" not in blob and "fcm.json" not in blob
        assert not any(k.startswith("GACA_") for k in call["env"])
    # HERMES_HOME is only set when it is not the default location (here it is a temp dir).
    assert logged[0]["env"]["HERMES_HOME"] == str(box.home.resolve())
    assert logged[0]["cwd"] == str(box.home.resolve())


def test_a_shell_metacharacter_in_the_binary_path_is_just_a_path(box, tmp_path):
    weird = tmp_path / "bin; touch PWNED #"
    weird.parent.mkdir(exist_ok=True)
    weird.write_text(box.stub.read_text())
    weird.chmod(0o755)
    hermes = LocalHermes(box.home, str(weird))
    run(hermes._hermes("--version", timeout=10))
    assert not (tmp_path / "PWNED").exists() and not (Path.cwd() / "PWNED").exists()


def test_a_missing_hermes_is_a_clear_error(box):
    with pytest.raises(LocalError, match="not found"):
        run(LocalHermes(box.home, "definitely-not-a-real-binary")._hermes("--version", timeout=5))


def test_a_hung_hermes_is_killed_and_reported(box, tmp_path):
    pidfile = tmp_path / "pid"
    box.stub.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import os, time
        open({str(pidfile)!r}, "w").write(str(os.getpid()))
        time.sleep(60)
        '''))
    with pytest.raises(LocalError, match="timed out"):
        run(box.hermes._hermes("--version", timeout=1))
    pid = int(pidfile.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_failing_create_reports_hermes_stderr(box):
    box.stub.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('profile exists')\nsys.exit(1)\n")
    with pytest.raises(LocalError, match="failed at create: profile exists"):
        run(box.hermes.create_profile("fresh", 1, KEY))


# ── structural guard rails ───────────────────────────────────────────

def test_the_module_has_no_shell_or_dynamic_code_paths():
    source = Path(local.__file__).read_text()
    code = re.sub(r'"""[\s\S]*?"""', "", source)           # docstrings may talk about these
    code = re.sub(r"#.*", "", code)
    for forbidden in ["shell=True", "os.system", "create_subprocess_shell",
                      "importlib", "__import__", "pickle", "yaml.load", "os.popen",
                      "import subprocess", "from subprocess"]:
        assert forbidden not in code, forbidden
    assert not re.search(r"(?<![A-Za-z_.])(eval|exec)\(", code)  # builtins only, not create_subprocess_exec
    assert not re.search(r"(?<!asyncio\.)subprocess\.", code)  # only asyncio.subprocess constants
    # Exactly one place spawns a process, and it is behind the allowlist check.
    assert code.count("create_subprocess_exec") == 1
    assert code.index("argv_allowed(args)") < code.index("create_subprocess_exec")


def test_local_errors_are_provisioning_errors_so_they_map_to_502():
    assert issubclass(LocalError, ProvisioningError)
