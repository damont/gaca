"""The Hermes helper: the one place hermes runs when gaca-api is sandboxed (see hermes_helper.py)."""
from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from gaca_api.provisioning import hermes_helper
from gaca_api.provisioning.local import LocalError, LocalHermes

from test_local import KEY, box, calls, run  # noqa: F401  (the box fixture)


def spawn(box):
    import asyncio
    return lambda args, timeout: asyncio.run(box.hermes._spawn(*args, timeout=timeout))


def request(**body):
    return (json.dumps(body) + "\n").encode()


# ── what the helper will and will not do ─────────────────────────────

@pytest.mark.parametrize("line", [b"", b"not json\n", b"[]\n", b'{"args": "profile"}\n', b'{"args": [1]}\n', b"{}\n"])
def test_garbage_requests_are_refused_and_nothing_runs(box, line):
    assert hermes_helper.handle(line, spawn(box)) == {"error": "not a valid request"}
    assert calls(box) == []


@pytest.mark.parametrize("args", [["chat"], ["gateway", "install"], ["profile", "create", "x/y", "--no-alias"],
                                  ["profile", "create", "default", "--no-alias"], ["-c", "id"], []])
def test_only_the_allowlisted_commands_run(box, args):
    reply = hermes_helper.handle(request(args=args), spawn(box))
    assert "allowlist" in reply["error"]
    assert calls(box) == []


def test_an_allowed_command_runs_and_its_answer_comes_back(box):
    reply = hermes_helper.handle(request(args=["--version"], timeout=20), spawn(box))
    assert reply == {"code": 0, "out": "Hermes Agent v0.21.5\n", "err": ""}
    assert [c["argv"] for c in calls(box)] == [["--version"]]


def test_the_timeout_is_clamped(box):
    seen = []
    hermes_helper.handle(request(args=["--version"], timeout=10_000), lambda a, t: seen.append(t) or (0, "", ""))
    hermes_helper.handle(request(args=["--version"], timeout=-5), lambda a, t: seen.append(t) or (0, "", ""))
    assert seen == [hermes_helper.MAX_TIMEOUT, 1.0]


def test_a_failure_to_run_hermes_is_an_error_reply_not_a_crash(box):
    def boom(args, timeout):
        raise LocalError("could not run hermes: nope")
    assert hermes_helper.handle(request(args=["--version"]), boom) == {"error": "could not run hermes: nope"}


def test_serve_stdio_answers_one_request_per_process(box, monkeypatch):
    from gaca_api.config import settings
    monkeypatch.setattr(settings, "hermes_home", str(box.home))
    monkeypatch.setattr(settings, "hermes_bin", str(box.stub))
    out = io.BytesIO()
    assert hermes_helper.serve_stdio(io.BytesIO(request(args=["--version"])), out) == 0
    assert json.loads(out.getvalue())["code"] == 0
    big = io.BytesIO(b'{"args": ["' + b"a" * 10_000 + b'"]}\n')
    out = io.BytesIO()
    hermes_helper.serve_stdio(big, out)
    assert json.loads(out.getvalue()) == {"error": "request too large"}


# ── gaca-api talking to it over a real socket ────────────────────────

@pytest.fixture
def helper_socket(box):
    """A listening Unix socket whose server side is the helper's own `handle`."""
    directory = Path(tempfile.mkdtemp(prefix="gh"))  # short: socket paths are limited to ~100 bytes
    path = directory / "hermes.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(4)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                line = conn.makefile("rb").readline()
                conn.sendall((json.dumps(hermes_helper.handle(line, spawn(box))) + "\n").encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield path
    stop.set()
    server.close()
    shutil.rmtree(directory, ignore_errors=True)


def test_gaca_api_creates_a_profile_through_the_helper_and_never_spawns_hermes_itself(box, helper_socket, monkeypatch):
    sandboxed = LocalHermes(box.home, "/nonexistent/hermes", hermes_socket=helper_socket)
    sandboxed._served_by_default = box.hermes._served_by_default
    box.served.update({"toolshed", "fresh"})
    # The sandboxed side has no hermes binary at all: everything must go through the socket.
    assert run(sandboxed.create_profile("fresh", 9999, KEY)) == (8642, "/p/fresh")
    assert [c["argv"] for c in calls(box)][:1] == [["profile", "create", "fresh", "--no-alias"]]


def test_the_helpers_refusal_reaches_the_caller_as_a_local_error(box, helper_socket):
    with pytest.raises(hermes_helper.HelperError, match="allowlist"):
        run(hermes_helper.call(helper_socket, ("chat",), 5))


def test_a_missing_socket_says_how_to_fix_it(box, tmp_path):
    sandboxed = LocalHermes(box.home, "hermes", hermes_socket=tmp_path / "nope.sock")
    with pytest.raises(LocalError, match="re-run ./install.sh"):
        run(sandboxed._hermes("--version", timeout=5))
