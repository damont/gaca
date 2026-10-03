"""What a crew member is dedicated to doing: summary rules, Hermes translation, the on-box
read, and how the profile service degrades."""
import asyncio
import json
import os
import subprocess
import sys

import pytest

from gaca_api.agent.base import ProfileReadError
from gaca_api.agent.hermes import translate_purpose as tp
from gaca_api.agent.hermes.adapter import HermesAdapter
from gaca_api.models.user import CrewMember, SSHConnection, UserDoc
from gaca_api.provisioning import ssh
from gaca_api.schemas.crew_profile import CrewProfile, ProfileInfo, PurposeInfo
from gaca_api.services import auth as auth_service
from gaca_api.services import crew_profile as service
from gaca_api.utils.purpose import build_purpose, derive_summary

STOCK = tp.STOCK_SOUL
TOOLSHED = """# Toolshed Agent

You are the dedicated engineering, product, and growth agent for Toolshed, the household item-sharing application at https://yourtoolshed.com and in the private GitHub repository `damont/toolshed`.

## Required GitHub workflow — never work on `main`

**ALL repository changes must be made on a non-`main` branch.**
"""
LEGACY_SCAFFOLD = """# Hermes Agent Persona

<!--
This file defines the agent's personality and tone.
Delete the contents (or this file) to use the default personality.
-->"""
TRADING_YAML = """description: Researches markets, understand how I want to trade, and quickly execute
  against those trades in order to make a couple hundred bucks a week
description_auto: false
"""


# ── GACA's own summary rules (engine-neutral) ────────────────────────────────
def test_summary_is_the_opening_sentence_in_the_third_person():
    assert derive_summary(TOOLSHED).startswith("The dedicated engineering, product, and growth agent for Toolshed")
    assert "`" not in derive_summary(TOOLSHED) and "**" not in derive_summary(TOOLSHED)
    assert derive_summary("You are a concise technical expert. No fluff, just facts.") == "A concise technical expert."
    assert derive_summary("# Title\n\n\n<!-- note -->\nHelps with the garden. Also waters plants.") == "Helps with the garden."
    assert derive_summary("# Only a heading\n") is None and derive_summary("   ") is None


def test_long_openings_are_cut_on_a_word():
    out = derive_summary("Handles " + "everything " * 60 + "daily.")
    assert out.endswith("…") and len(out) <= 201
    assert not out[:-1].endswith("everythin")  # cut on a word boundary, not mid-word


def test_build_purpose_prefers_a_written_description_then_instructions_then_generic():
    described = build_purpose(PurposeInfo(description=" Trades  stocks ", instructions="Long text. More."))
    assert (described.summary, described.summary_source, described.is_generic) == ("Trades stocks", "description", False)
    assert described.instructions == "Long text. More."

    derived = build_purpose(PurposeInfo(instructions="You are the garden helper. Be kind."))
    assert (derived.summary, derived.summary_source) == ("The garden helper.", "instructions")

    generic = build_purpose(PurposeInfo())
    assert generic.is_generic and generic.summary is None and generic.instructions is None
    assert build_purpose(PurposeInfo(description="  ", instructions="  ")).is_generic


# ── Hermes' vocabulary -> neutral ────────────────────────────────────────────
def test_hermes_stock_soul_in_any_wrapping_means_no_specific_purpose():
    assert tp.custom_instructions(STOCK) is None
    assert tp.custom_instructions(STOCK + "\n") is None
    assert tp.custom_instructions(STOCK.replace(". ", ".\n")) is None  # the same words, re-wrapped
    assert tp.custom_instructions(LEGACY_SCAFFOLD) is None             # comment-only scaffold from older installers
    assert tp.custom_instructions("") is None and tp.custom_instructions(None) is None


def test_hermes_custom_soul_is_kept_and_additions_to_the_stock_text_survive():
    assert tp.custom_instructions(TOOLSHED).startswith("# Toolshed Agent")
    extended = tp.custom_instructions(STOCK + "\n\n## Focus\nOnly talk about birds.\n")
    assert extended == "## Focus\nOnly talk about birds."  # the stock paragraph is dropped, the rest kept as written


def test_hermes_profile_yaml_description():
    assert tp.description(TRADING_YAML).startswith("Researches markets, understand how I want to trade, and quickly execute against")
    assert tp.description("description: ''\ndescription_auto: true\n") is None
    assert tp.description("not: [valid") is None and tp.description(None) is None and tp.description("- a list") is None


def test_to_purpose_info_for_the_four_real_shapes():
    generic = tp.to_purpose_info({"soul": STOCK, "profile_yaml": None})                  # default / david
    assert (generic.description, generic.instructions) == (None, None)
    trading = tp.to_purpose_info({"soul": STOCK, "profile_yaml": TRADING_YAML})           # described, stock soul
    assert trading.description and trading.instructions is None
    assert build_purpose(trading).summary_source == "description" and build_purpose(trading).is_generic is False
    toolshed = tp.to_purpose_info({"soul": TOOLSHED, "profile_yaml": "description: Toolshed specialist\n"})
    assert toolshed.description == "Toolshed specialist" and toolshed.instructions.startswith("# Toolshed Agent")


# ── The fixed on-box read, run for real against a fake ~/.hermes ─────────────
def _run_script(home, name):
    out = subprocess.run(
        [sys.executable, "-c", ssh._READ_PROFILE_SCRIPT, name],
        env={**os.environ, "HOME": str(home)}, capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def test_on_box_script_maps_default_and_named_profiles_and_rejects_everything_else(tmp_path):
    hermes = tmp_path / ".hermes"
    (hermes / "profiles" / "toolshed").mkdir(parents=True)
    (hermes / "SOUL.md").write_text("default soul")
    (hermes / "profiles" / "toolshed" / "SOUL.md").write_text("toolshed soul")
    (hermes / "profiles" / "toolshed" / "profile.yaml").write_text("description: x\n")
    (hermes / "profiles" / "bare").mkdir()
    (tmp_path / "secret.txt").write_text("nope")

    assert _run_script(tmp_path, "default") == {"found": True, "soul": "default soul", "profile_yaml": None, "stock": []}
    assert _run_script(tmp_path, "toolshed") == {"found": True, "soul": "toolshed soul", "profile_yaml": "description: x\n", "stock": []}
    assert _run_script(tmp_path, "bare") == {"found": True, "soul": None, "profile_yaml": None, "stock": []}
    for hostile in ("ghost", "../secret", "..", "a/b", "toolshed; id", "$(id)", "", "-x", "default/../x"):
        assert _run_script(tmp_path, hostile) == {"found": False}, hostile


def test_on_box_script_reports_the_installed_hermes_own_stock_texts(tmp_path):
    hermes = tmp_path / ".hermes"
    (hermes / "hermes-agent" / "hermes_cli").mkdir(parents=True)
    (hermes / "SOUL.md").write_text("whatever")
    (hermes / "hermes-agent" / "hermes_cli" / "default_soul.py").write_text(
        'DEFAULT_SOUL_MD = ("You are Future Hermes. " "Be brief.")\n_LEGACY_TEMPLATE_SOULS = ("old text one", "old text two")\n'
    )
    assert _run_script(tmp_path, "default")["stock"] == ["You are Future Hermes. Be brief.", "old text one", "old text two"]
    # a broken or missing definition must never break the read itself
    (hermes / "hermes-agent" / "hermes_cli" / "default_soul.py").write_text("this is not python (")
    assert _run_script(tmp_path, "default")["stock"] == []


def test_a_future_stock_persona_is_recognised_when_hermes_reports_it():
    future = "You are Future Hermes. Be brief and kind."
    assert tp.custom_instructions(future) is not None                     # unknown to us on its own
    assert tp.custom_instructions(future, [future]) is None               # but the installed Hermes says it's stock
    assert tp.custom_instructions(future + "\n\n## Focus\nBirds only.", [future]) == "## Focus\nBirds only."
    assert tp.to_purpose_info({"soul": future, "profile_yaml": None, "stock": [future]}).instructions is None


def test_both_known_stock_generations_are_generic_without_any_help():
    for stock in tp.KNOWN_STOCK_SOULS:
        assert tp.custom_instructions(stock) is None and tp.custom_instructions(stock + "\n") is None


def test_read_profile_files_builds_a_safe_command_and_validates_the_name(monkeypatch):
    seen = {}

    async def fake_run(conn, command, *, timeout=20.0):
        seen["command"] = command
        return json.dumps({"found": True, "soul": "s", "profile_yaml": None})

    monkeypatch.setattr(ssh, "run", fake_run)
    conn = SSHConnection(host="h", username="u", private_key="k")
    assert asyncio.run(ssh.read_profile_files(conn, "toolshed")) == {"found": True, "soul": "s", "profile_yaml": None}
    assert seen["command"].startswith("python3 -c ") and seen["command"].endswith(" toolshed")
    for bad in ("../x", "a b", "x;y", "", "$(id)"):
        with pytest.raises(ssh.SSHError):
            asyncio.run(ssh.read_profile_files(conn, bad))


# ── Adapter + service degrade gracefully ─────────────────────────────────────
class FakeBackend:
    def __init__(self, purpose=None, error=None):
        self._purpose, self._error = purpose, error
        self.asked = None

    async def describe_profile(self):
        return ProfileInfo(status="online", engine="Hermes 0.20.5")

    async def describe_purpose(self, *, name, access):
        self.asked = (name, access.host)
        if self._error:
            raise self._error
        return self._purpose


def _user(ssh_conn):
    return UserDoc(
        id="u1", email="u@example.com", display_name="T", agent_host="h",
        crew=[CrewMember(name="toolshed", port=1, api_key="k")], default_crew="toolshed",
        ssh=ssh_conn,
    )


def _profile(user, backend, monkeypatch):
    monkeypatch.setattr(auth_service, "backend_for_crew", lambda u, m: backend)
    return asyncio.run(service.get(user, "toolshed"))


def test_service_includes_purpose_when_the_box_can_be_read(monkeypatch):
    backend = FakeBackend(PurposeInfo(description="Toolshed specialist", instructions="Long soul."))
    profile = _profile(_user(SSHConnection(host="box", username="u", private_key="k")), backend, monkeypatch)
    assert backend.asked == ("toolshed", "box")
    assert profile.purpose.summary == "Toolshed specialist" and profile.purpose.is_generic is False
    assert "purpose" not in profile.unavailable


def test_service_without_direct_access_or_on_failure_marks_purpose_unavailable(monkeypatch):
    no_ssh = _profile(_user(None), FakeBackend(), monkeypatch)
    assert no_ssh.purpose is None and "purpose" in no_ssh.unavailable and no_ssh.status == "online"  # rest still there

    failing = _profile(_user(SSHConnection(host="b", username="u", private_key="k")),
                       FakeBackend(error=ProfileReadError("boom")), monkeypatch)
    assert failing.purpose is None and "purpose" in failing.unavailable


def test_a_generic_crew_member_is_not_the_same_as_an_unreadable_one(monkeypatch):
    profile = _profile(_user(SSHConnection(host="b", username="u", private_key="k")), FakeBackend(PurposeInfo()), monkeypatch)
    assert profile.purpose is not None and profile.purpose.is_generic and profile.purpose.summary is None
    assert "purpose" not in profile.unavailable


def test_adapter_wraps_ssh_failures_as_profile_read_errors(monkeypatch):
    async def boom(conn, name):
        raise ssh.SSHError("connection refused")

    async def missing(conn, name):
        return None

    adapter = HermesAdapter("http://engine:8642", "k" * 16)
    conn = SSHConnection(host="h", username="u", private_key="k")
    monkeypatch.setattr(ssh, "read_profile_files", boom)
    with pytest.raises(ProfileReadError):
        asyncio.run(adapter.describe_purpose(name="toolshed", access=conn))
    monkeypatch.setattr(ssh, "read_profile_files", missing)
    with pytest.raises(ProfileReadError, match="no profile"):
        asyncio.run(adapter.describe_purpose(name="ghost", access=conn))
