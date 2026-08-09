# Deployment

## Requirements

| Component | Why |
|---|---|
| Linux VPS, 2 vCPU / 4 GB minimum | Sandbox containers run alongside the factory |
| Docker + Docker Compose | Sandboxing is mandatory; without it the Development Engine stays disabled |
| PostgreSQL 16 | All state and LangGraph checkpoints (the bundled compose service is fine) |
| Outbound HTTPS | Model providers, market-signal sources, Stripe |

No inbound networking is required. Telegram uses `getUpdates` polling, so the factory
needs no public hostname or TLS certificate unless you want the Stripe webhook — which
does need a reachable `POST /webhooks/stripe`.

## First run

```bash
git clone <your-remote> superagent && cd superagent
cp .env.example .env
$EDITOR .env            # DATABASE_URL + at least one provider key are mandatory

# The sandbox workspace directory must exist on the host and be writable by the
# sandbox's fixed non-root UID *before* anything runs — see "Sandboxing note" below
# for why this can't be automated away.
sudo mkdir -p /srv/factory-workspaces
sudo chown 10001:10001 /srv/factory-workspaces

# The factory's non-root user also needs group access to the host's docker.sock —
# set DOCKER_GID in .env to this host's actual value (varies by distro/install):
stat -c '%g' /var/run/docker.sock   # put this number in .env as DOCKER_GID

docker compose up -d postgres
docker compose run --rm factory alembic upgrade head
docker compose run --rm factory python -m factory.preflight
```

Preflight prints exactly what is missing and how to fix it, and writes the same
report to `MISSING_CREDENTIALS.md`. Fix every **blocking** item; **degrading** items
are safe to leave — the affected engine simply stays off and the Supervisor routes
work elsewhere.

```bash
docker compose up -d          # start the factory
docker compose logs -f factory
curl localhost:8000/health    # per-engine enabled/disabled state with reasons
```

## Telegram setup

Telegram is the factory's only channel to a human — the one place `MISSING_CREDENTIAL`
and `LEGAL_UNCERTAINTY` escalations go. It is optional (the factory boots and runs
without it; escalations simply queue in Postgres until it's configured), but nothing
tells you an escalation happened until you configure it, so do this before your first
live run.

**1. Create the bot.** In Telegram, message [@BotFather](https://t.me/BotFather):

```
/newbot
```

Follow the prompts (name, then a username ending in `bot`). BotFather replies with a
token that looks like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` — that is
`TELEGRAM_BOT_TOKEN`. Put it in `.env` now.

**2. Message the bot.** Open a chat with the bot you just created (search its
username) and send it anything — `hi` is enough. This is the step BotFather doesn't
mention: a bot cannot message you until you have messaged it first, and Telegram
gives you no chat id anywhere in the creation flow. If you want escalations in a group
instead of a DM, add the bot to that group and send the message there.

**3. Find the chat id.** With `TELEGRAM_BOT_TOKEN` set, run:

```bash
docker compose run --rm factory factory-telegram-chatid
```

(No `docker compose` yet? `pip install -e ".[dev]"` then `factory-telegram-chatid`
works identically outside the container.) This lists every chat that has messaged the
bot:

```
Found 1 chat(s) that have messaged this bot:

  123456789       private    Jesse P

Set TELEGRAM_CHAT_ID to the id you want the factory to send escalations to.
```

If it instead says "No messages found yet", you skipped step 2 — go send the message,
then run the command again.

**4. Set `TELEGRAM_CHAT_ID`** in `.env` to the id from step 3, then restart:

```bash
docker compose up -d factory
```

**5. Verify.** Run one tick and check Telegram:

```bash
docker compose run --rm factory python -m factory.main --once
```

Every boot re-announces any still-missing credential as a `MISSING_CREDENTIAL`
escalation (deduplicated, so this is safe to run repeatedly), and the tick's
housekeeping step delivers anything queued straight to Telegram — including
escalations raised earlier while Telegram itself was the missing credential. If any
other credential is still unset, expect a message within seconds. Reply to an
escalation with `#<8-char-id> your answer`; the id is in the message itself.

## Public domain / TLS setup

Three things need a real, publicly reachable HTTPS endpoint: the Stripe webhook,
the Mailgun bounce/complaint webhook, and the `/u/{token}` link recipients click to
unsubscribe. None of this is required to run the factory at all (Telegram uses
polling, not a webhook) — skip this section entirely until one of those three is
actually in scope.

**1. Point DNS** at the host: an `A` record for whatever domain (or subdomain)
you're using, pointing at the droplet's public IPv4. Wait for it to resolve
(`dig +short your-domain.com`) before continuing — Caddy's certificate request
will fail otherwise.

**2. Set `PUBLIC_DOMAIN`** in `.env` to that domain, then:

```bash
docker compose up -d caddy
```

Caddy (already in `docker-compose.yml`) reverse-proxies `PUBLIC_DOMAIN` to the
factory's internal port and provisions/renews its own Let's Encrypt certificate
automatically — nothing else to configure. Watch `docker compose logs -f caddy` on
first boot; a certificate failure almost always means DNS hasn't propagated yet.

**3. Verify**: `curl https://your-domain.com/health` from *outside* the host should
return the same JSON as `curl localhost:8000/health` from inside it.

A dedicated domain unrelated to any other identity you'd rather keep separate from
this factory (rather than a subdomain of one) is worth the ~$10/year: the
unsubscribe link is plain text in every outreach email's footer, and DNS/WHOIS
records publicly connect a subdomain back to its parent domain. Enable WHOIS
privacy at the registrar (usually free) regardless of which you choose.

## Mailgun setup

Outbound cold email goes through Mailgun's API, never raw SMTP — a burst of cold
sends via SMTP straight from the primary domain has no IP rotation, no warmup, and no
bounce/complaint feedback loop, and burns sender reputation within days. Optional
(Marketing degrades without it, exactly like every other missing credential), but
required before `FACTORY_DRY_RUN=0` if you want outreach to actually send.

**1. Add and verify a sending domain** in the Mailgun dashboard (Sending → Domains).
Use a subdomain of your primary domain (e.g. `mg.yourcompany.com`), not the bare
domain — this is what gives you IP/domain separation from your main mail if
something goes wrong. Add the DNS records Mailgun gives you (SPF, DKIM, tracking
CNAME) and wait for verification.

**2. Set `MAILGUN_API_KEY`** (Settings → API Keys → Private API key) and
**`MAILGUN_DOMAIN`** (the verified sending domain from step 1) in `.env`. EU accounts
must also set `MAILGUN_BASE_URL=https://api.eu.mailgun.net/v3`.

**3. Set `MAILGUN_WEBHOOK_SIGNING_KEY`** (Sending → Webhooks → "HTTP webhook signing
key" — a different value from the API key). Required for `/webhooks/mailgun` to
accept anything at all; every request is signature-verified and rejected otherwise.

**4. Point Mailgun's webhooks** at `https://<your-host>/webhooks/mailgun` for at
least the "complained" and "failed" (permanent bounce) events (Sending → Webhooks →
add each). This is what feeds the spam-complaint circuit breaker
(`SPAM_COMPLAINT_RATE_THRESHOLD`, default 0.1%) — without it, a reputation problem
accumulates with nothing watching.

**5. Verify the webhook shape before relying on it.** The exact JSON field names
`deliverability.py` reads (`event-data.event`, `event-data.user-variables`) match
Mailgun's documented format but were not verified against a live payload while
building this. Use the dashboard's "Test webhook" button on each event type and
confirm the request logs a handled `complained`/`failed` event, not a silent
"event type not tracked".

## Going live

`FACTORY_DRY_RUN=1` is the default and it stubs **every** outbound side effect: no
email leaves the process, no deploy is published, no card is charged. Turn it off
only after:

1. `pytest -q` passes against the deployed image.
2. `/health` shows the engines you expect, with no unexpected degradations.
3. You have watched at least one full Discovery → Compliance → Development cycle in
   the logs and agreed with the decisions it made.

Then set `FACTORY_DRY_RUN=0` and restart. Nothing else changes.

## Sandboxing note

`docker-compose.yml` mounts `/var/run/docker.sock` so the factory can start *sibling*
containers for generated code. This is a real privilege: a process that can talk to
the Docker socket can escalate on the host. Mitigations already in place:

- Sandbox containers never get `--privileged`, and always get `--cap-drop ALL`,
  `--security-opt no-new-privileges`, a read-only rootfs, a non-root user, and
  `--network none` unless a run explicitly needs egress.
- The socket is mounted into the **factory**, never into a sandbox.
- The Bug Council's privilege scanner rejects generated code that mentions
  `--privileged`, `docker.sock`, `--cap-add`, `sudo`, or `chmod 777`.

Run the factory on a dedicated host. Do not co-locate it with anything whose
compromise you would mind.

### The workspace path must match on both sides of the mount, exactly

Sibling-container sandboxing has one sharp, non-obvious edge: `docker run --volume
SRC:DST`, issued from inside the factory container, is resolved by the **host**
daemon against **its own** filesystem — not against the factory container's view of
its own filesystem. If `WORKSPACE_ROOT` were bind-mounted at `/app/workspaces` inside
the factory container but the host-side source were a named volume or any different
path, the sandbox container would get an empty `/workspace` on every run — none of the
scaffolded code the factory just wrote would be visible to it. `pip install` would run
against a missing `requirements.txt`, the test command would find no tests, and every
build would heal-loop and eventually escalate as if the *generated code* were always
broken. This is invisible in a mocked test environment, since that never launches two
real, separate containers sharing a real filesystem.

The fix already in `docker-compose.yml`: the volume line uses the identical literal
path (`/srv/factory-workspaces` by default) on both sides, and `WORKSPACE_ROOT` in
`.env` is set to that same path. If you change one, change all three (the compose
file's host side, its container side, and `WORKSPACE_ROOT`) together.

This can't be fixed in Python — no application code can discover which host directory
a bind-mounted container path actually corresponds to. What the factory *can* do, and
does, is prove the sharing actually works rather than assume it: every boot,
`factory.preflight.check_sandbox_workspace_sharing` launches a real probe container
that writes a marker into the shared path, then checks whether this process can read
it back. If it can't, Development is disabled with a `SANDBOX_WORKSPACE_SHARING`
finding naming the exact problem, instead of silently producing empty sandboxes
forever.

One remaining manual step, and it *is* manual because a container cannot `chown` a
directory it doesn't yet have permission to write into: the host directory must exist
and be owned by the sandbox's fixed non-root UID (`10001`) before the first run —
`sudo mkdir -p /srv/factory-workspaces && sudo chown 10001:10001
/srv/factory-workspaces` (already in "First run" above). This is the one Linux
permission fact no amount of retrying or self-healing can substitute for.

### Pre-baked sandbox image (optional, recommended)

By default `SANDBOX_IMAGE` is the stock `python:3.13-slim`, so every build's
dependency-install step needs network access and a live PyPI. `docker/sandbox.Dockerfile`
pre-installs the stack every generated app actually needs (FastAPI, Stripe, pytest,
password hashing) so a typical build needs no runtime install at all:

```bash
docker build -f docker/sandbox.Dockerfile -t your-registry/factory-sandbox:latest .
docker push your-registry/factory-sandbox:latest
# .env: SANDBOX_IMAGE=your-registry/factory-sandbox:latest
```

A `requirements.txt` package outside that set is still installed at run time — this
narrows how often that path is used, it does not remove it.

## Operations

| Task | Command |
|---|---|
| Watch decisions | `docker compose logs -f factory \| grep supervisor.decision` |
| Current spend and queue depth | `curl localhost:8000/metrics` |
| Pending escalations | `select * from interrupts where status='open';` |
| Answer an escalation | Reply in Telegram: `#<8-char-id> your answer` |
| One-shot tick (debug) | `docker compose run --rm factory python -m factory.main --once` |
| Stop cleanly | `docker compose stop factory` — releases every task lease first |

### Backups

`factory-pgdata` holds everything: ledger, leads, checkpoints, escalations. Back it
up. Losing it does not just lose history — running products become unattributable and
in-flight work is unrecoverable.

```bash
docker compose exec postgres pg_dump -U factory factory | gzip > backup-$(date +%F).sql.gz
```

### Rotating a leaked credential

1. Revoke at the provider.
2. Update `.env`.
3. `docker compose up -d factory`.

Vaulted secrets never enter a sandbox container, and the Bug Council's secret scanner
blocks a build that hard-codes one — but rotate anyway if a key was ever printed into
a log.

## Upgrading

```bash
git pull
docker compose build factory
docker compose run --rm factory alembic upgrade head
docker compose up -d factory
```

Migrations run against the same `DATABASE_URL` as the app, via psycopg rather than
asyncpg. In-flight tasks survive: leases expire and are reclaimed on the next tick.
