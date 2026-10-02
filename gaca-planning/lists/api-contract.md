# Lists API

Lists for anything you keep on a list — shopping (Costco, Home Depot), gifts (Christmas), to-dos.
Everything needs the normal bearer token and is scoped to that user: someone else's list is a `404`.
Base: `https://goodandcheapagent.com/api`.

The API owns the rules, so the app and any agent behave the same: names are matched **loosely** but never
**guessed**, and an item can't be on a list twice. There is no sharing between users yet; it will be a
membership alongside the owner, without changing items.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/lists?include_archived=` | Your lists, most recently changed first, with `open_count` / `done_count` |
| `POST` | `/lists` | `{name, kind?, notes?}` — `kind`: `shopping` \| `gift` \| `todo` \| `general`. `422` if a list with that name exists (loosely: "Costco" = "costco") |
| `GET` | `/lists/match?q=` | Which list does this name mean? `{match}` when one clearly fits, else `{candidates}` (several fit — ask) or neither |
| `GET` | `/lists/{id}` | The list + items: open first (grouped by category), then checked-off |
| `PATCH` | `/lists/{id}` | Rename, `kind`, `notes`, `archived` |
| `DELETE` | `/lists/{id}` | Delete the list and its items |
| `POST` | `/lists/{id}/items` | `{items: [{text, quantity?, note?, category?, for_person?, url?, price?}]}` (max 100) |
| `PATCH` | `/lists/{id}/items/{item_id}` | Edit or check/un-check (`done`) |
| `DELETE` | `/lists/{id}/items/{item_id}` | Remove one item |
| `POST` | `/lists/{id}/items/check` | `{texts: [...], done: true}` — by name, no ids needed |
| `POST` | `/lists/{id}/items/remove` | `{texts: [...]}` — by name |
| `POST` | `/lists/{id}/clear-done` | Delete every checked-off item → `{removed}` |

## Matching rules

A name resolves in tiers: **identical after normalizing** (case, punctuation, `&`/`and`, plurals —
`Batteries` = `battery`) → **shared words** → **close fuzzy match**.

* The query *contains* a whole name ("the costco run" ⊇ "Costco"): that name is meant (the most specific if several).
* The query is *part of* a name ("christmas" ⊂ "Christmas 2026" and "Christmas Eve dinner"): fine if only one, **ambiguous** if several.
* Ambiguous = more than one fits and nothing clearly wins → `candidates` / `ambiguous`, and **nothing is changed** for that name.

## Adding

```jsonc
// POST /lists/{id}/items → 201
{ "list": { /* summary with counts */ },
  "added":      [ { "id": "…", "text": "milk", "similar_to": "2% milk" } ],  // similar_to: related, still added
  "reopened":   [ /* were checked off; adding again un-checks them */ ],
  "duplicates": [ { "text": "Battery", "existing": { "text": "batteries", … } } ] }  // already there; unchanged
```

## By-name results

```jsonc
// POST …/items/check and …/items/remove
{ "matched": [ … ], "ambiguous": [ { "query": "milk", "candidates": [ … ] } ], "not_found": [ "unicorn" ] }
```

Limits: 500 items per list; item `text` ≤ 200 chars; `url` must be http(s).
