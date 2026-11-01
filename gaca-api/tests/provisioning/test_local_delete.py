"""Deleting a Hermes profile from the box: only through `hermes profile delete <name> -y`, never the
default profile, and "gone" is only believed once the directory really is gone."""
import asyncio
import sys
import textwrap

import pytest

from gaca_api.provisioning.local import LocalError, LocalHermes, argv_allowed


def _stub(tmp_path, home, behaviour):
    """A stub hermes. behaviour: "delete" removes the profile dir; "fail" exits 1; "lie" exits 0 and
    leaves it in place."""
    stub = tmp_path / "hermes-stub"
    stub.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import pathlib, shutil, sys
        log = pathlib.Path({str(tmp_path / "calls.txt")!r})
        log.write_text((log.read_text() if log.exists() else "") + " ".join(sys.argv[1:]) + "\\n")
        if sys.argv[1:3] == ["profile", "delete"]:
            mode = {behaviour!r}
            if mode == "delete":
                shutil.rmtree(pathlib.Path({str(home)!r}) / "profiles" / sys.argv[3])
            elif mode == "fail":
                print("profile is in use", file=sys.stderr); sys.exit(1)
        else:
            print("Hermes Agent v0.21.5")
        '''))
    stub.chmod(0o755)
    return stub


def _box(tmp_path, behaviour):
    home = tmp_path / "hermes"
    (home / "profiles" / "david").mkdir(parents=True)
    (home / ".env").write_text("API_SERVER_ENABLED=true\n")
    return home, LocalHermes(home, str(_stub(tmp_path, home, behaviour)))


def _calls(tmp_path):
    f = tmp_path / "calls.txt"
    return f.read_text().splitlines() if f.exists() else []


def test_the_delete_command_is_on_the_allowlist_and_nothing_looser_is():
    assert argv_allowed(("profile", "delete", "david", "-y"))
    for bad in [("profile", "delete", "default", "-y"), ("profile", "delete", "david"), ("profile", "delete", "../x", "-y"),
                ("profile", "delete", "david", "-y", "--all"), ("profile", "delete", "-y", "david"), ("profile", "purge", "david", "-y")]:
        assert not argv_allowed(bad), bad


def test_a_profile_is_deleted_and_the_directory_is_gone(tmp_path):
    home, hermes = _box(tmp_path, "delete")
    assert asyncio.run(hermes.delete_profile("david")) is True
    assert not (home / "profiles" / "david").exists()
    assert _calls(tmp_path) == ["profile delete david -y"]


def test_a_profile_that_is_already_gone_is_a_success_and_runs_nothing(tmp_path):
    _, hermes = _box(tmp_path, "delete")
    assert asyncio.run(hermes.delete_profile("nobody")) is False
    assert _calls(tmp_path) == []


def test_a_failing_command_is_an_error_with_hermes_own_words(tmp_path):
    home, hermes = _box(tmp_path, "fail")
    with pytest.raises(LocalError, match="profile is in use"):
        asyncio.run(hermes.delete_profile("david"))
    assert (home / "profiles" / "david").exists()


def test_exit_zero_that_leaves_the_profile_is_still_a_failure(tmp_path):
    """Believing it would let discovery quietly bring the profile back."""
    _, hermes = _box(tmp_path, "lie")
    with pytest.raises(LocalError, match="still on the box"):
        asyncio.run(hermes.delete_profile("david"))


@pytest.mark.parametrize("name", ["default", "", "../david", "a b", "david/../x"])
def test_the_default_profile_and_odd_names_are_refused_before_anything_runs(tmp_path, name):
    _, hermes = _box(tmp_path, "delete")
    with pytest.raises(LocalError):
        asyncio.run(hermes.delete_profile(name))
    assert _calls(tmp_path) == []
