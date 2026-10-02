---
name: gaca-lists
description: Keep the user's lists in GACA — shopping lists (Costco, Home Depot, groceries), gift and Christmas lists, to-dos. Use whenever the user asks to add, remove, check off or see something on a list, or mentions needing to pick something up or get something later. GACA is where lists live; never keep them in memory or notes.
license: MIT
compatibility: Requires network access to GACA and an operator-issued bearer token.
required_environment_variables:
  - name: GACA_API_TOKEN
    prompt: GACA agent bearer token
    help: Generate a token from the Agent tokens section of the GACA profile page.
    required_for: reading and changing the user's lists
metadata:
  author: GACA
  version: "1.0.0"
  hermes:
    tags: [lists, shopping, gifts, todo, gaca]
    category: productivity
    requires_toolsets: [terminal]
---

# Lists

Lists live in GACA, tied to the user's own account, so they are the same from the app, from chat and from any other crew member. **Do not store a list in your memory, a note or a file** — memory is tiny and private to one profile. If the user says "we need batteries", that is a list item, not a memory.

Helper: `python3 ${HERMES_SKILL_DIR}/scripts/lists.py <command>`. It reads the token from the environment and never prints it. **Pass everything as plain arguments** — do not pipe, heredoc or `-c` anything into it (unattended runs block that). The commands:

```
overview                                     all lists with counts
show NAME [--all]                            one list: open items by category (--all adds checked-off)
add NAME ITEM... [--create] [--kind K] [--category C] [--for WHO] [--qty Q] [--note N]
add NAME --items-json '[{"text":"milk","category":"dairy","quantity":"2"}]' [--create] [--kind K]
check NAME ITEM...      /  uncheck NAME ITEM...
remove NAME ITEM...
clear-done NAME                              delete the checked-off items
new NAME [--kind K]  /  rename NAME NEW  /  archive NAME  /  delete NAME --yes
```

`K` is `shopping`, `gift`, `todo` or `general`. List names and item names are matched **loosely** ("the costco run" finds "Costco"; "paper towel" finds "paper towels"), so you rarely need exact text.

## Exit code 3 means "decide first"

When a command exits with 3 it printed why and changed nothing for that part:
- **AMBIGUOUS list** — several lists fit equally ("christmas" → "Christmas 2026" and "Christmas Eve dinner"). Ask the user which, then repeat with the full name.
- **AMBIGUOUS item** — several items fit ("milk" → "2% milk" and "almond milk"). Ask which, or name it exactly.
- **NO LIST matches** — either the user meant a list you can see in the printed names (use it), or it is genuinely new. Create it with `--create` when the intent is clear ("add X to my Home Depot list" when none exists — say you created it); if it's really unclear, ask.

Never guess your way past one of these.

## How to handle requests

- **Adding.** Use one call with all the items. Keep the user's specifics in the item text ("2% milk", "Kirkland paper towels"); put amounts in `quantity` ("2 gallons of milk" → text `milk`, quantity `2 gallons`). `--qty`, `--category`, `--for` and `--note` apply to **every** item in that call, so when items differ, use `--items-json`.
- **Names for new lists.** Give a clean, short, nicely-cased name: `Costco`, `Home Depot`, `Christmas 2026` — not "the costco run" or "my grocery list". One list per store, and one per occasion for gifts.
- **Categories (shopping lists).** Group items the way the store is laid out, using a small consistent set — run `show NAME` first and reuse the categories already there. Grocery/Costco: `produce`, `meat`, `dairy`, `bakery`, `frozen`, `pantry`, `beverages`, `snacks`, `household`, `personal care`, `pharmacy`, `pets`, `other`. Home Depot-type stores: `plumbing`, `electrical`, `tools`, `paint`, `garden`, `lumber`, `hardware`, `lighting`, `other`. Set a category on every shopping item you add.
- **Gift lists** (`--kind gift`): the item text is the gift idea; `for_person` is who it's for; put the link and price in `url`/`price` (via `--items-json`) and sizes, colors or "they mentioned…" in `note`. You are only recording ideas — never buy anything.
- **Duplicates.** The helper tells you what was already on the list and what it un-checked. Just say so ("eggs were already on there"). If it prints a *Heads-up, may be a duplicate* line ("milk" vs the existing "2% milk"), mention it and offer to remove one — don't remove it on your own.
- **Checking off.** "Got the milk" → `check`. Don't `clear-done` unless the user says the trip is finished or asks to clear the list.
- **Removing.** Removing a single item the user asked you to remove is fine. **Deleting a whole list** or clearing checked items only when explicitly asked; for `delete`, confirm with the user first, then repeat with `--yes`. Offer `archive` when they just want it out of the way.
- **Showing.** Reply with a tidy, readable list of what's left, grouped as the helper prints it. Open items only unless asked. For a long list, give the count and the highlights rather than everything.
- **Photos.** If the user sends a photo of a receipt, a pantry shelf or a handwritten list, say what you read, add the items you're confident about, and name the ones you weren't sure of instead of guessing.
- **Reminders to self.** If the user says "remind me to get X when I'm at Costco", that is just an item on the Costco list.

## Failures

- The helper says GACA rejected the token (401/403): stop and tell the user the GACA token needs renewing. Don't retry or look for another way in.
- Any other error: report it plainly. Never pretend a change was made.

## Replying

Keep it short and concrete: what changed, and anything that needs a decision. For example: "Added batteries and paper towels to Costco. Eggs were already on it. You have 7 things on the list now." Don't repeat the whole list after every change.
