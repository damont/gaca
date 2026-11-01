# Give a Hermes box read-only Google access (Gmail, Docs, Sheets)

How to let a Hermes agent *read* one person's Gmail, Google Docs and Google Sheets, and nothing else: no sending, no
deleting, no Calendar, no Drive browsing. Written from setting up a second Hermes box (2026-10-31); the traps below are real.

You need: SSH to the Hermes box, a Google Cloud OAuth **Desktop** client (one per household is fine, reuse it), and the
person whose mail it is, for about two minutes with a browser.

## 0. Check the Google app is published

Google Cloud console, Audience. If it says **Testing**, refresh tokens die after 7 days: publish it ("In production"; a
formality for personal use) or add the person as a test user and plan to redo this weekly. A published but unverified app
shows a "Google hasn't verified this app" warning at consent: **Advanced, then Go to (app) (unsafe)**. That is expected.

## 1. Copy the client secret to the new box

Stream it between boxes; do not leave copies around.

```bash
ssh existing-box 'cat ~/.hermes/google_client_secret.json' |
  ssh new-box 'umask 077; cat > ~/.hermes/google_client_secret.json'
```

## 2. Find the right Python, and install the Google libraries

Hermes versions differ, and this is where the time goes.

- **Older Hermes**: a plain `~/.hermes/hermes-agent/venv/bin/python`. System Python lacks the Google libraries.
- **Newer Hermes** (has `hermes pm`): packages live in a hashed venv and Hermes refuses installs from any other Python.

On a newer box:

```bash
hermes pm install --extra google
systemctl --user restart hermes-gateway      # user service; no sudo
# Find the venv Python:
cd ~/.hermes/hermes-agent && PYTHONPATH=$PWD ~/.hermes/tools/python-*/bin/python3 -c \
  "from pm import paths, environments as e; print(e.venv_python(e.selected_venv(paths.repo_root())))"
```

Use that venv Python, with `PYTHONPATH=$HOME/.hermes/hermes-agent`, for every command below. Running setup with the bundled
Python fails with "not running from the install's dependency environment". Use `$HOME`, not `~`, inside quotes.

```bash
export PYTHONPATH=$HOME/.hermes/hermes-agent
PY=<venv python from above>
S=$HOME/.hermes/skills/productivity/google-workspace/scripts
```

## 3. Make the skill ask for read-only scopes

**Check this before sending anyone a link.** The skill's `setup.py` and `google_api.py` each carry a `SCOPES` list, and the
default can be broad (send, modify, full Drive, full Docs/Sheets, Calendar). The README's `--services` flag may not exist in
your version. Edit both lists to:

```python
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]
```

Back up the originals first. Add `drive.readonly` only if Hermes must browse or search Drive. A Hermes update can overwrite
the skill and restore the broad list, so re-check after updating. (The token the person approves stays read-only either way:
Google enforces what was granted.)

## 4. Authorize

```bash
$PY $S/setup.py --auth-url        # prints one URL; send it to the person
```

The consent screen must show only three view-only rows (Gmail, Docs, Sheets). If you see send, delete, Drive or Calendar,
stop and fix step 3. After they approve, their browser lands on a broken `http://localhost:1/?code=...` page; that is
expected. They send you the **whole address-bar URL**. Treat it like a password until used (single use, short lived).

```bash
$PY $S/setup.py --auth-code "<that URL>"
$PY $S/setup.py --check            # AUTHENTICATED
chmod 600 ~/.hermes/google_token.json ~/.hermes/google_client_secret.json
```

If the code expired, `--auth-code` returns a fresh URL; use only the newest redirect.

## 5. Verify, read-only

```bash
$PY $S/google_api.py gmail search "in:inbox" --max 3
```

`--check-live` reports a failure because it probes Calendar, which was deliberately not granted. That is expected.

## Revoking

`$PY $S/setup.py --revoke`, or the person removes the app at https://myaccount.google.com/permissions.
