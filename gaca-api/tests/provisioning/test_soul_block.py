"""The one managed paragraph in a profile's SOUL.md: added once, updated in place, never touching what
a person wrote, and never shown as their instructions."""
import os

import pytest

from gaca_api.agent.hermes.translate_purpose import custom_instructions
from gaca_api.provisioning import soul_block
from gaca_api.provisioning.soul_block import BEGIN, BLOCK, END, SoulBlockError, apply


def soul(tmp_path, text):
    (tmp_path / "SOUL.md").write_text(text)
    return tmp_path


def test_a_missing_soul_is_not_created(tmp_path):
    assert apply(tmp_path) == "absent" and not (tmp_path / "SOUL.md").exists()


def test_the_block_is_appended_after_what_the_person_wrote(tmp_path):
    apply(soul(tmp_path, "You help with money.\n"))
    assert (tmp_path / "SOUL.md").read_text() == f"You help with money.\n\n{BLOCK}\n"


def test_applying_twice_changes_nothing(tmp_path):
    apply(soul(tmp_path, "Mine."))
    before = (tmp_path / "SOUL.md").read_text()
    assert apply(tmp_path) == "unchanged" and (tmp_path / "SOUL.md").read_text() == before


def test_an_old_block_is_replaced_and_text_around_it_is_kept(tmp_path):
    soul(tmp_path, f"Before.\n\n{BEGIN}\nold wording\n{END}\n\nAfter.\n")
    assert apply(tmp_path) == "updated"
    assert (tmp_path / "SOUL.md").read_text() == f"Before.\n\n{BLOCK}\n\nAfter.\n"


def test_a_marker_without_its_partner_is_left_for_a_person(tmp_path):
    soul(tmp_path, f"Mine.\n{BEGIN}\nhalf\n")
    assert apply(tmp_path) == "damaged" and BLOCK not in (tmp_path / "SOUL.md").read_text()


def test_a_dry_run_writes_nothing(tmp_path):
    soul(tmp_path, "Mine.")
    assert apply(tmp_path, dry_run=True) == "added" and (tmp_path / "SOUL.md").read_text() == "Mine."


def test_permissions_survive_the_rewrite(tmp_path):
    soul(tmp_path, "Mine.")
    os.chmod(tmp_path / "SOUL.md", 0o600)
    apply(tmp_path)
    assert (os.stat(tmp_path / "SOUL.md").st_mode & 0o777) == 0o600


def test_a_symlinked_soul_is_refused(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("x")
    (tmp_path / "SOUL.md").symlink_to(real)
    with pytest.raises(SoulBlockError):
        apply(tmp_path)
    assert real.read_text() == "x"


def test_the_block_never_counts_as_the_persons_instructions():
    assert custom_instructions(f"{BLOCK}\n") is None
    assert custom_instructions(f"You are a trading analyst.\n\n{BLOCK}\n") == "You are a trading analyst."
    assert soul_block.strip(f"a\n{BLOCK}\nb") == "a\n\nb"
