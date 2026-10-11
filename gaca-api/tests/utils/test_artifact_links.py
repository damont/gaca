"""Tests for the artifact-link rule: what's stripped from message text, and what's found."""
from gaca_api.utils import artifact_links

A = "a" * 32
B = "b" * 32


def test_strip_removes_labeled_and_bare_links_and_tidies_the_gaps():
    text = f"Here's your list:  \n\n[Groceries](gaca://artifact/{A})\n\n\n\nAlso gaca://artifact/{B} \nDone."
    assert artifact_links.strip(text) == "Here's your list:\n\nAlso\nDone."


def test_strip_leaves_non_links_alone():
    # Wrong id shape and a non-gaca URL are ordinary text, not links.
    text = "[x](gaca://artifact/NOTHEX) and [y](https://example.com)"
    assert artifact_links.strip(text) == text


def test_strip_of_a_message_that_is_only_a_link_is_empty():
    assert artifact_links.strip(f"  [Page](gaca://artifact/{A})\n") == ""


def test_find_dedupes_in_first_appearance_order_and_keeps_labels():
    text = f"gaca://artifact/{B} then [Groceries](gaca://artifact/{A}) and [Again](gaca://artifact/{B})"
    assert artifact_links.find(text) == [
        artifact_links.ArtifactLink(B, None),
        artifact_links.ArtifactLink(A, "Groceries"),
    ]


def test_find_nothing_in_plain_text():
    assert artifact_links.find("no links here") == []
