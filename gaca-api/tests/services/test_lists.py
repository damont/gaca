"""Lists: matching rules, dedupe, check/remove by name, scoping — against a real SQLite file."""
import asyncio

import pytest

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.schemas.lists import ItemIn, ItemPatch, ListCreate, ListPatch
from gaca_api.services import lists as svc
from gaca_api.services.exceptions import InvalidRequest, NotFound


def _run(coro_fn, tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            return await coro_fn()
        finally:
            await client.close()

    return asyncio.run(main())


def items(*texts):
    return [ItemIn(text=t) for t in texts]


# ── Pure matching ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("a,b", [
    ("Costco", "costco"), ("Home Depot", "home  depot!"), ("Batteries", "battery"),
    ("Eggs", "egg"), ("Mac & Cheese", "mac and cheese"), ("2% Milk", "2% milk"),
])
def test_names_that_normalize_alike_have_the_same_key(a, b):
    assert svc.key(a) == svc.key(b)


def test_different_things_have_different_keys():
    assert svc.key("2% milk") != svc.key("milk")
    assert svc.key("glass") != svc.key("glasses")  # no clever stemming beyond plain plurals


def _pairs(*texts):
    return [(str(i), t) for i, t in enumerate(texts)]


def test_resolve_prefers_exact_then_contained_words_then_fuzzy():
    cands = _pairs("Costco", "Home Depot", "Christmas")
    assert svc.resolve("costco", cands).ids == ["0"]            # exact
    assert svc.resolve("the costco run", cands).ids == ["0"]    # all of "costco" is in the query
    assert svc.resolve("home deopt", cands).ids == ["1"]        # a typo, fuzzy
    assert svc.resolve("groceries", cands).ids == []            # nothing fits
    assert svc.resolve("", cands).ids == []


def test_resolve_reports_ties_instead_of_guessing():
    cands = _pairs("2% milk", "almond milk", "eggs")
    tie = svc.resolve("milk", cands).ids
    assert sorted(tie) == ["0", "1"] and svc.resolve("milk", cands).one is None
    assert svc.resolve("2% milk", cands).one == "0"             # exact beats the tie
    assert svc.resolve("almond", cands).one == "1"              # closest by words wins


# ── Lists ────────────────────────────────────────────────────────────────────
def test_create_list_names_are_unique_loosely_and_per_user(tmp_path):
    async def go():
        await svc.create_list("u1", ListCreate(name="Costco", kind="shopping"))
        with pytest.raises(InvalidRequest):
            await svc.create_list("u1", ListCreate(name="  costco "))
        with pytest.raises(InvalidRequest):
            await svc.create_list("u1", ListCreate(name="!!!"))
        await svc.create_list("u2", ListCreate(name="Costco"))  # a different person's own list
        return (await svc.list_lists("u1")).lists, (await svc.list_lists("u2")).lists

    mine, theirs = _run(go, tmp_path)
    assert [l.name for l in mine] == ["Costco"] and mine[0].kind == "shopping"
    assert len(theirs) == 1 and theirs[0].id != mine[0].id


def test_match_lists(tmp_path):
    async def go():
        for name in ("Costco", "Home Depot", "Christmas 2026", "Christmas Eve dinner"):
            await svc.create_list("u1", ListCreate(name=name))
        return (
            await svc.match_lists("u1", "the costco run"),
            await svc.match_lists("u1", "christmas"),
            await svc.match_lists("u1", "walgreens"),
            await svc.match_lists("u2", "costco"),
        )

    clear, tie, none, other_user = _run(go, tmp_path)
    assert clear.match.name == "Costco" and clear.candidates == []
    assert tie.match is None and {c.name for c in tie.candidates} == {"Christmas 2026", "Christmas Eve dinner"}
    assert none.match is None and none.candidates == []
    assert other_user.match is None  # can't see someone else's lists


def test_rename_archive_and_unarchive_collision(tmp_path):
    async def go():
        a = await svc.create_list("u1", ListCreate(name="Costco"))
        renamed = await svc.update_list("u1", a.id, ListPatch(name="Costco run"))
        archived = await svc.update_list("u1", a.id, ListPatch(archived=True))
        await svc.create_list("u1", ListCreate(name="Costco run"))  # name is free again once archived
        with pytest.raises(InvalidRequest):
            await svc.update_list("u1", a.id, ListPatch(archived=False))
        return renamed, archived, (await svc.list_lists("u1")).lists, (await svc.list_lists("u1", True)).lists

    renamed, archived, active, everything = _run(go, tmp_path)
    assert renamed.name == "Costco run" and archived.archived
    assert len(active) == 1 and len(everything) == 2


def test_delete_list_removes_items_and_is_scoped(tmp_path):
    async def go():
        lst = await svc.create_list("u1", ListCreate(name="Temp"))
        await svc.add_items("u1", lst.id, items("a", "b"))
        with pytest.raises(NotFound):
            await svc.delete_list("u2", lst.id)
        await svc.delete_list("u1", lst.id)
        with pytest.raises(NotFound):
            await svc.get_list("u1", lst.id)
        return await svc.list_lists("u1")

    assert _run(go, tmp_path).lists == []


# ── Items ────────────────────────────────────────────────────────────────────
def test_add_dedupes_reopens_and_flags_similar(tmp_path):
    async def go():
        lst = await svc.create_list("u1", ListCreate(name="Costco", kind="shopping"))
        first = await svc.add_items("u1", lst.id, items("2% milk", "batteries", "eggs"))
        again = await svc.add_items("u1", lst.id, items("Eggs", "battery", "milk", "paper towels"))
        await svc.check_by_text("u1", lst.id, ["eggs"], True)
        reopened = await svc.add_items("u1", lst.id, [ItemIn(text="eggs", quantity="2 dozen")])
        return first, again, reopened, await svc.get_list("u1", lst.id)

    first, again, reopened, detail = _run(go, tmp_path)
    assert [i.text for i in first.added] == ["2% milk", "batteries", "eggs"]
    assert [d.text for d in again.duplicates] == ["Eggs", "battery"]   # same thing, different wording
    assert [i.text for i in again.added] == ["milk", "paper towels"]
    assert again.added[0].similar_to == "2% milk"                      # related, so flagged — still added
    assert again.added[1].similar_to is None
    assert [i.text for i in reopened.reopened] == ["eggs"] and reopened.reopened[0].quantity == "2 dozen"
    assert detail.open_count == 5 and detail.done_count == 0


def test_detail_orders_open_by_category_then_done_last(tmp_path):
    async def go():
        lst = await svc.create_list("u1", ListCreate(name="Groceries"))
        await svc.add_items("u1", lst.id, [
            ItemIn(text="rice"),
            ItemIn(text="milk", category="dairy"),
            ItemIn(text="apples", category="produce"),
            ItemIn(text="cheese", category="dairy"),
        ])
        await svc.check_by_text("u1", lst.id, ["apples"], True)
        return await svc.get_list("u1", lst.id)

    detail = _run(go, tmp_path)
    assert [(i.text, i.done) for i in detail.items] == [
        ("milk", False), ("cheese", False), ("rice", False), ("apples", True),
    ]


def test_check_and_remove_by_name_report_ambiguity_and_misses(tmp_path):
    async def go():
        lst = await svc.create_list("u1", ListCreate(name="L"))
        await svc.add_items("u1", lst.id, items("2% milk", "almond milk", "bread", "batteries"))
        checked = await svc.check_by_text("u1", lst.id, ["bread", "milk", "unicorn", "Batteries"], True)
        unchecked = await svc.check_by_text("u1", lst.id, ["bread"], False)
        removed = await svc.remove_by_text("u1", lst.id, ["2% milk", "nothing here"])
        return checked, unchecked, removed, await svc.get_list("u1", lst.id)

    checked, unchecked, removed, detail = _run(go, tmp_path)
    assert sorted(i.text for i in checked.matched) == ["batteries", "bread"]
    assert [a.query for a in checked.ambiguous] == ["milk"]
    assert sorted(c.text for c in checked.ambiguous[0].candidates) == ["2% milk", "almond milk"]
    assert checked.not_found == ["unicorn"]
    assert [i.text for i in unchecked.matched] == ["bread"]
    assert [i.text for i in removed.matched] == ["2% milk"] and removed.not_found == ["nothing here"]
    assert sorted((i.text, i.done) for i in detail.items) == [("almond milk", False), ("batteries", True), ("bread", False)]


def test_patch_and_delete_item_and_clear_done(tmp_path):
    async def go():
        lst = await svc.create_list("u1", ListCreate(name="Gifts", kind="gift"))
        res = await svc.add_items("u1", lst.id, [
            ItemIn(text="Lego set", for_person="Sam", url="https://example.com/lego", price="$40"),
            ItemIn(text="Scarf"),
        ])
        lego, scarf = res.added
        edited = await svc.patch_item("u1", lst.id, lego.id, ItemPatch(note="the big one", price=None))
        with pytest.raises(InvalidRequest):
            await svc.patch_item("u1", lst.id, scarf.id, ItemPatch(text="lego sets"))  # collides after normalizing
        done = await svc.patch_item("u1", lst.id, scarf.id, ItemPatch(done=True))
        cleared = await svc.clear_done("u1", lst.id)
        with pytest.raises(NotFound):
            await svc.patch_item("u2", lst.id, lego.id, ItemPatch(note="x"))
        await svc.delete_item("u1", lst.id, lego.id)
        return edited, done, cleared, await svc.get_list("u1", lst.id)

    edited, done, cleared, detail = _run(go, tmp_path)
    assert (edited.note, edited.price, edited.for_person) == ("the big one", None, "Sam")
    assert done.done and cleared == 1 and detail.items == []


def test_empty_text_and_item_cap(tmp_path):
    async def go():
        lst = await svc.create_list("u1", ListCreate(name="L"))
        with pytest.raises(InvalidRequest):
            await svc.add_items("u1", lst.id, [ItemIn(text="!!!")])
        for start in range(0, 500, 100):
            await svc.add_items("u1", lst.id, items(*[f"item {n}" for n in range(start, start + 100)]))
        with pytest.raises(InvalidRequest):
            await svc.add_items("u1", lst.id, items("one too many"))

    _run(go, tmp_path)
