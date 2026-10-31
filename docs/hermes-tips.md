# Hermes setup tips

GACA is the phone app for a [Hermes](https://github.com/NousResearch/hermes-agent) agent, so Hermes has to be working on the
machine before you install GACA. GACA does not install or configure Hermes. These are the things worth knowing, learned by
standing Hermes up by hand one switch at a time. For what GACA itself needs, see
[Set up Hermes](../README.md#set-up-hermes-once) in the README; this page is the longer version, plus everything around it.

## 1. The machine

Hermes is a long-running service. Treat its machine like a server even if it started life as a desktop.

- **Stop it sleeping.** Desktop Linux suspends when idle. A suspended machine drops off the network, the gateway stops
  answering, scheduled jobs never run (they do not run late, they simply never run), and a run in progress dies. The symptom,
  "the assistant is sometimes dead", is miserable to debug because everything looks fine once you touch the machine.
  The Settings toggle only covers a logged-in session, so mask the targets as well:
  ```bash
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
  ```
- **Make sure you can SSH in.** Ubuntu Desktop ships no `openssh-server`:
  `sudo apt install -y openssh-server && sudo systemctl enable --now ssh`. When a port says *Connection refused* but ping works,
  nothing is listening (a firewall would give a timeout instead).
- **Keep user services alive after you log out.** Hermes' gateway is a systemd *user* service. Without lingering it stops when
  your last session ends and does not start after an unattended reboot:
  ```bash
  sudo loginctl enable-linger $USER      # then: loginctl show-user $USER | grep Linger   (Linger=yes)
  ```
  Prefer user services to `@reboot` cron lines (nobody restarts a crashed cron job) and to root services (the agent then runs
  with more privilege than the job needs, and with the wrong file ownership).

## 2. A model

Hermes cannot do anything until it has a model. Run `hermes setup` (or `hermes model`) and choose a provider.

- **API key or subscription.** A metered API key is a line in `~/.hermes/.env`; when the balance runs out, runs die mid-turn.
  Signing in with a subscription (ChatGPT, for example) rides a flat plan instead, and stores refreshable tokens rather than a
  permanent key. The catch is that sign-in is interactive: a person opens a URL and approves.
- **If the device-code sign-in is disabled on the account,** use the browser flow instead, from a machine with a browser:
  `ssh -L 1455:localhost:1455 you@the-host`, then on the host `hermes auth add openai-codex --browser`, and open the link it
  prints in your own browser. Afterwards pick the model with `hermes model`.
- **Check that it worked** by sending one message (`hermes chat`) before going further.

## 3. Start small, then switch things on knowingly

A fresh Hermes registers dozens of plugins, but a registered plugin is only an available slot: nothing works until you supply
its credential and select it. The boot log shows this as capability checks failing closed ("dependent tools will be unavailable
this turn"). That is the intended behaviour. Boot the gateway, confirm it answers, then add capabilities one at a time so you
know where each one's state lives.

**Secrets go in `~/.hermes/.env`; choices go in `config.yaml`.** Web search is the typical example:
```bash
echo 'TAVILY_API_KEY=…' >> ~/.hermes/.env     # the credential switches the web tools on
hermes config set web.backend tavily           # which backend to use (not a secret)
```
Each backend has its own variable (`TAVILY_API_KEY`, `EXA_API_KEY`, `FIRECRAWL_API_KEY`, `BRAVE_SEARCH_API_KEY`,
`SEARXNG_URL`, …), so changing backend later is one key and one config line. A search backend is also worth having early:
without one, an agent reaches for a browser to answer simple questions, which is slow and often unavailable on a server.

**"Enabled" is often two switches in two places.** Memory is the example: `hermes tools` controls whether the memory tool is
offered to the model, but only the `memory:` block turns the subsystem on, and no single command does both. Enable the tool
*and* run `hermes config set memory.memory_enabled true` (and `memory.user_profile_enabled true`). Memory lands as plain
Markdown under `~/.hermes/memories/`, which is easy to read, back up and diff.

## 4. What GACA needs from Hermes

Add to `~/.hermes/.env`:
```
API_SERVER_ENABLED=true
API_SERVER_KEY=<a long random string>      # python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```
Then run the gateway as a service and check it:
```bash
hermes gateway install
curl http://127.0.0.1:8642/health
```
Hermes 0.21 and newer serves every profile from one gateway (`/p/<name>`), and the port defaults to 8642 when `API_SERVER_PORT`
is not set. `gaca-api doctor` re-checks all of this and says how to fix what is wrong.

## 5. Let unattended runs actually run

A profile that posts alerts, writes pages or runs a scheduled job has nobody at the keyboard to click "approve", so with the
default prompts it simply stalls. For every profile:
```bash
hermes --profile <name> config set approvals.mode off
hermes --profile <name> config set approvals.cron_mode approve
hermes --profile <name> cron doctor           # should report no problems
```
These are choices, so they live in each profile's `config.yaml`. Note that `approvals.mode: off` is broader than cron: it turns
off Hermes' approval checks for commands in that profile (the hard blocklist still applies). Do it deliberately, and remember a
profile created by hand starts with the defaults.

## 6. Giving the agent access to other things

- **GitHub.** The simplest route for one person is a personal access token through the `gh` CLI (Hermes can install it
  itself). Make it a *fine-grained* token limited to the repositories the agent really needs; a token that can write to your whole
  account can also force-push or delete. Validate with a read (list your repos) before trusting it with writes. The token lives in
  `gh`'s own credential store, not in `~/.hermes`.
- **Another machine over SSH.** Give the agent its own key for that one destination, and a short `~/.ssh/config` alias with
  `IdentitiesOnly yes` (so SSH does not offer every key and trip the server's attempt limit) and keepalives
  (`ServerAliveInterval 30`) so a long job is not cut off. The key has to be unencrypted for unattended use, so the agent can
  read it: the other machine is only as safe as this one.
- **Google Workspace.** It arrives as a skill, with the OAuth client created in your own Google Cloud project. Start with
  read-only scopes (gmail, drive, sheets, docs). A client left in *testing* mode has its refresh tokens expire after **seven
  days**, and the symptom is an auth failure deep inside a skill. Publish the app (a formality for one user) or plan to
  re-consent. Step-by-step, read-only, including the traps: [Read-only Google access](hermes-google-readonly.md).
- **A chat front end such as Telegram.** A bot is reachable by anyone who finds it, so the allowlist (`TELEGRAM_ALLOWED_USERS`)
  is the whole security model: required, not optional.
- **Docker.** Being in the `docker` group is effectively root (`docker run -v /:/host`). That is the normal trade for a
  single-user agent host, but "the agent has no sudo" overstates its isolation.

## 7. Skills

A skill is a `SKILL.md` plus helper scripts that Hermes loads from a profile's `skills/` folder. GACA ships five (see the README).
Worth knowing:

- Skills are prompts, not enforcement. A security instruction in a skill ("never print the token") is defence in depth, not a
  boundary. Keep tokens out of any output that ends up somewhere public.
- The most durable way for an app to be operated by an agent is the pattern GACA uses: the app publishes an OpenAPI document,
  issues the agent a narrowly scoped, revocable token, and ships a skill that tells the agent to read the live schema before
  acting, so the agent re-reads reality rather than trusting remembered endpoints.
- A skill that updates itself from a repository executes what it fetches. Pin it to a repository and branch you control.
- Skills are discovered from files; there is no need to restart Hermes after adding one.

## 8. Know where everything lives

Backing up `~/.hermes` is not backing up the deployment. State also lives outside it:

| what | where |
|---|---|
| Hermes config, `.env`, memory, skills, sign-in tokens | `~/.hermes/` (and `~/.hermes/profiles/<name>/`) |
| the gateway service and lingering | `~/.config/systemd/user/`, `/var/lib/systemd/linger/` |
| GitHub token | `gh`'s config directory or the system keyring |
| SSH keys the agent uses | `~/.ssh/` |
| GACA's database, settings and push keys | the GACA data directory, `~/.config/gaca/` (`gaca-api backup` makes a copy) |

If you run Hermes' browser dashboard, keep it bound to `127.0.0.1` and reach it through an SSH tunnel: it is an unauthenticated UI
onto an agent that can run commands.
