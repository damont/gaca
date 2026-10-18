"""Installing GACA's skills into a Hermes skills folder without ever clobbering an agent's edits
or following a symlink out of the folder."""
import os
from pathlib import Path

import pytest

from gaca_api.provisioning import skill_sync
from gaca_api.provisioning.skill_sync import SkillSyncError, sync

REPO_SKILLS = Path(__file__).resolve().parents[3] / "skills"


def make_source(tmp_path, files=None):
    src = tmp_path / "src"
    files = files or {"alpha/SKILL.md": "alpha v1", "alpha/scripts/run.py": "print(1)", "beta/SKILL.md": "beta v1"}
    for rel, text in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return src


@pytest.fixture
def skills(tmp_path):
    d = tmp_path / "hermes" / "skills"
    d.mkdir(parents=True)
    return d


def test_a_first_sync_writes_everything_under_its_own_category(tmp_path, skills):
    src = make_source(tmp_path)
    result = sync(src, skills)
    assert sorted(result.written) == ["alpha/SKILL.md", "alpha/scripts/run.py", "beta/SKILL.md"]
    assert (skills / "gaca" / "alpha" / "scripts" / "run.py").read_text() == "print(1)"
    assert sorted(p.name for p in skills.iterdir()) == ["gaca"]  # nothing else touched


def test_syncing_again_changes_nothing(tmp_path, skills):
    src = make_source(tmp_path)
    sync(src, skills)
    again = sync(src, skills)
    assert not again.changed and again.unchanged == 3 and not again.left_alone


def test_an_unmodified_file_is_updated_to_the_new_version(tmp_path, skills):
    src = make_source(tmp_path)
    sync(src, skills)
    (src / "alpha" / "SKILL.md").write_text("alpha v2")
    result = sync(src, skills)
    assert result.updated == ["alpha/SKILL.md"] and (skills / "gaca" / "alpha" / "SKILL.md").read_text() == "alpha v2"


def test_a_file_the_agent_edited_is_left_alone_and_reported(tmp_path, skills):
    src = make_source(tmp_path)
    sync(src, skills)
    mine = skills / "gaca" / "alpha" / "SKILL.md"
    mine.write_text("alpha, tweaked by the agent")
    (src / "alpha" / "SKILL.md").write_text("alpha v2")
    result = sync(src, skills)
    assert result.left_alone == ["alpha/SKILL.md"] and mine.read_text() == "alpha, tweaked by the agent"
    # ...and it keeps being left alone on later runs, not overwritten the second time round
    assert sync(src, skills).left_alone == ["alpha/SKILL.md"] and mine.read_text() == "alpha, tweaked by the agent"


def test_an_edited_file_that_later_matches_upstream_is_adopted(tmp_path, skills):
    src = make_source(tmp_path)
    sync(src, skills)
    mine = skills / "gaca" / "alpha" / "SKILL.md"
    mine.write_text("alpha v2")
    (src / "alpha" / "SKILL.md").write_text("alpha v2")
    assert sync(src, skills).left_alone == []
    (src / "alpha" / "SKILL.md").write_text("alpha v3")
    assert sync(src, skills).updated == ["alpha/SKILL.md"]  # it is ours again, so it follows upstream


def test_a_file_that_already_exists_and_was_never_ours_is_not_overwritten(tmp_path, skills):
    src = make_source(tmp_path)
    (skills / "gaca" / "alpha").mkdir(parents=True)
    (skills / "gaca" / "alpha" / "SKILL.md").write_text("someone else's")
    result = sync(src, skills)
    assert "alpha/SKILL.md" in result.left_alone and (skills / "gaca" / "alpha" / "SKILL.md").read_text() == "someone else's"


def test_a_file_dropped_upstream_is_removed_only_if_unmodified(tmp_path, skills):
    src = make_source(tmp_path)
    sync(src, skills)
    (skills / "gaca" / "beta" / "SKILL.md").write_text("edited")
    (src / "alpha" / "scripts" / "run.py").unlink()
    (src / "beta" / "SKILL.md").unlink()
    (src / "beta" / "OTHER.md").write_text("x")  # beta stays a skill folder? no SKILL.md -> not a skill
    result = sync(src, skills)
    assert result.removed == ["alpha/scripts/run.py"] and not (skills / "gaca" / "alpha" / "scripts" / "run.py").exists()
    assert (skills / "gaca" / "beta" / "SKILL.md").read_text() == "edited"
    assert "beta/SKILL.md" in result.left_alone


def test_the_executable_bit_is_kept(tmp_path, skills):
    src = make_source(tmp_path)
    os.chmod(src / "alpha" / "scripts" / "run.py", 0o755)
    sync(src, skills)
    assert os.stat(skills / "gaca" / "alpha" / "scripts" / "run.py").st_mode & 0o111
    assert not os.stat(skills / "gaca" / "alpha" / "SKILL.md").st_mode & 0o111


def test_pycache_and_non_skill_folders_are_not_copied(tmp_path, skills):
    src = make_source(tmp_path)
    (src / "alpha" / "scripts" / "__pycache__").mkdir()
    (src / "alpha" / "scripts" / "__pycache__" / "run.cpython-313.pyc").write_bytes(b"\0")
    (src / "README.md").write_text("not a skill")
    (src / "notaskill").mkdir()
    (src / "notaskill" / "file.txt").write_text("x")
    result = sync(src, skills)
    assert sorted(result.written) == ["alpha/SKILL.md", "alpha/scripts/run.py", "beta/SKILL.md"]


# ── safety: what it must refuse ──────────────────────────────────────

def test_a_symlinked_skills_dir_is_refused(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "skills"
    link.symlink_to(real)
    with pytest.raises(SkillSyncError):
        sync(make_source(tmp_path), link)
    assert list(real.iterdir()) == []


def test_a_symlinked_category_dir_is_refused(tmp_path, skills):
    outside = tmp_path / "outside"
    outside.mkdir()
    (skills / "gaca").symlink_to(outside)
    with pytest.raises(SkillSyncError):
        sync(make_source(tmp_path), skills)
    assert list(outside.iterdir()) == []


def test_a_symlinked_skill_folder_inside_the_category_is_refused(tmp_path, skills):
    outside = tmp_path / "outside"
    outside.mkdir()
    (skills / "gaca").mkdir()
    (skills / "gaca" / "alpha").symlink_to(outside)
    with pytest.raises(SkillSyncError):
        sync(make_source(tmp_path), skills)
    assert list(outside.iterdir()) == []


def test_a_symlinked_file_at_the_target_is_refused_not_followed(tmp_path, skills):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (skills / "gaca" / "alpha").mkdir(parents=True)
    (skills / "gaca" / "alpha" / "SKILL.md").symlink_to(victim)
    with pytest.raises(SkillSyncError):
        sync(make_source(tmp_path), skills)
    assert victim.read_text() == "precious"


def test_the_skills_dir_must_exist(tmp_path):
    with pytest.raises(SkillSyncError):
        sync(make_source(tmp_path), tmp_path / "no-such-dir")


def test_a_symlink_in_the_source_is_never_read(tmp_path, skills):
    src = make_source(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not copy me")
    (src / "alpha" / "leak.txt").symlink_to(secret)
    with pytest.raises(SkillSyncError):
        sync(src, skills)
    assert not (skills / "gaca").exists() or "leak.txt" not in [p.name for p in (skills / "gaca" / "alpha").glob("*")]


def test_a_symlinked_skill_folder_in_the_source_is_skipped(tmp_path, skills):
    src = make_source(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere).mkdir()
    (elsewhere / "SKILL.md").write_text("sneaky")
    (src / "gamma").symlink_to(elsewhere)
    result = sync(src, skills)
    assert not any(name.startswith("gamma/") for name in result.written)


@pytest.mark.parametrize("bad", ["Upper", "has space", "-lead", "dot.dot", "x" * 80])
def test_odd_skill_names_are_refused(tmp_path, skills, bad):
    src = make_source(tmp_path)
    (src / bad).mkdir()
    (src / bad / "SKILL.md").write_text("x")
    with pytest.raises(SkillSyncError):
        sync(src, skills)


def test_oversized_files_are_refused(tmp_path, skills):
    src = make_source(tmp_path)
    (src / "alpha" / "big.bin").write_bytes(b"x" * (skill_sync._MAX_FILE + 1))
    with pytest.raises(SkillSyncError):
        sync(src, skills)


def test_a_corrupt_manifest_is_treated_as_empty(tmp_path, skills):
    src = make_source(tmp_path)
    sync(src, skills)
    (skills / "gaca" / skill_sync.MANIFEST).write_text("{not json")
    result = sync(src, skills)
    assert not result.changed and result.unchanged == 3  # everything already matches upstream


# ── the real skills in this repo ─────────────────────────────────────

def test_the_repos_skills_install_cleanly(tmp_path, skills):
    result = sync(REPO_SKILLS, skills)
    assert {"gaca", "gaca-alerts", "gaca-briefing", "gaca-artifacts"} <= {r.split("/")[0] for r in result.written}
    assert not result.left_alone
