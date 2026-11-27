"""The library shelf: pages opt into pinned libraries with <script data-gaca-lib="NAME"></script>, and the page
builder inlines the exact vendored files (static/libs/manifest.json) in place of the marker."""
import hashlib
from importlib import resources

import pytest

from gaca_api.services import artifact_page

LIBS = resources.files("gaca_api").joinpath("static/libs")


def test_every_manifest_file_exists_matches_its_hash_and_has_a_license():
    libs = artifact_page.libraries()
    assert {"preact", "chart", "d3", "markdown", "sortable", "confetti", "icons", "font-inter"} <= set(libs)
    for name, lib in libs.items():
        files = lib.get("scripts", []) + ([lib["font"]["file"]] if "font" in lib else [])
        assert files, name
        assert set(lib["sha256"]) == set(files), name
        for f in files:
            assert hashlib.sha256(LIBS.joinpath(f).read_bytes()).hexdigest() == lib["sha256"][f], f
        assert lib["license"] and lib["use"] and lib["version"], name
    assert len(list(LIBS.joinpath("licenses").iterdir())) >= 10


def test_marker_is_replaced_in_place_by_the_library_code():
    page = '<html><head><script data-gaca-lib="sortable"></script><script>Sortable.create(x)</script></head><body></body></html>'
    built = artifact_page.build(page, "a1", "T")
    assert "data-gaca-lib" not in built
    code = LIBS.joinpath("sortable/Sortable.min.js").read_text()
    assert code in built
    # Library first, then the page's own script, and both after the SDK prelude.
    assert built.index("window.__GACA_INIT") < built.index(code) < built.index("Sortable.create(x)")


def test_multi_file_library_inlines_all_files_in_order_and_once():
    page = "<head><script data-gaca-lib='preact'></script><script DATA-GACA-LIB=preact></script></head>"
    built = artifact_page.build(page, "a1", "T")
    order = [built.index(f"/* preact: preact/{f} */") for f in ("preact.umd.js", "hooks.umd.js", "htm.umd.js")]
    assert order == sorted(order)
    assert built.count("/* preact: preact/preact.umd.js */") == 1


def test_font_becomes_a_data_uri_font_face():
    built = artifact_page.build('<head><script data-gaca-lib="font-fraunces"></script></head>', "a1", "T")
    assert "@font-face{font-family:'Fraunces'" in built and "src:url(data:font/woff2;base64," in built


def test_unknown_library_fails_loudly_in_the_console():
    built = artifact_page.build('<head><script data-gaca-lib="jquery"></script></head>', "a1", "T")
    assert "console.error(\"GACA: unknown library 'jquery'. Available: chart, confetti" in built


def test_a_head_inside_library_code_does_not_move_the_prelude():
    # The prelude goes after the page's own <head>, never after a "<head" inside a library's code.
    built = artifact_page.build('<html><head><script data-gaca-lib="d3"></script></head><body>x</body></html>', "a1", "T")
    assert built.startswith("<html><head><meta http-equiv=\"Content-Security-Policy\"")


def test_pages_without_markers_are_unchanged():
    assert artifact_page.inline_libraries("<p>no libraries</p>") == "<p>no libraries</p>"


def test_a_tampered_file_is_refused(monkeypatch):
    lib = artifact_page.libraries()["confetti"]
    monkeypatch.setitem(lib, "sha256", {"confetti/confetti.browser.js": "0" * 64})
    artifact_page.library_markup.cache_clear()
    try:
        with pytest.raises(artifact_page.LibraryError):
            artifact_page.library_markup("confetti")
    finally:
        artifact_page.library_markup.cache_clear()


def test_the_skill_documents_every_library_on_the_shelf():
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[2] / "skills" / "gaca-artifacts" / "references" / "sdk.md").read_text()
    for name in artifact_page.libraries():
        assert f"| `{name}` |" in doc, f"{name} is on the shelf but not in references/sdk.md"
