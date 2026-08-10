# Deployment checklist — working copy

Our shared, running log for getting the factory live. Check items off as we
actually verify them, not as we merely perform them — several of these are the
first time a given mechanism will ever run against real infrastructure rather than
a unit-test fake, and the point of this pass is to find out what breaks *before*
it's running unattended for weeks. Add a `Note:` line under any item where we hit
something worth remembering.

Reference: `DEPLOYMENT.md` has the full command-level detail for most steps here;
this file is the sequence and the "did we actually check it" record.

**Rule for this whole process: dry run before live, always. Never skip ahead.**

---

## Phase 0 — Environment decision

- [x] Deploy target chosen: **cloud VM, to be provisioned now**.

## Phase 0.5 — Provision the VM

- [x] Provider + region chosen: **DigitalOcean droplet** — public IPv4
      `<server-ip>`, private IP `10.110.0.2`, Ubuntu 24.04.4 LTS.
- [x] VM created and reachable.
- [x] SSH access confirmed (`ssh -i ~/.ssh/id_ed25519 root@<server-ip>`).
- [x] Docker installed via the official apt repo (not the curl-pipe-to-bash
      script): Docker 29.7.2, Compose v5.4.0. `docker run hello-world` succeeded
      end to end (daemon reachable, image pull, container execution, network all
      confirmed working).

## Phase 1 — Host provisioning (one-time, manual)

- [x] Docker + Docker Compose installed and confirmed working (`docker info`).
- [x] Repo transferred to `/opt/superagent` on the host via `git archive HEAD | ssh
      ... tar -x` (exactly the git-tracked tree — no `.env`, no local `ssh/` key
      copy, nothing gitignored came along).
- [x] Workspace host directory created and owned by the sandbox's fixed UID:
      `/srv/factory-workspaces`, confirmed `10001:10001`.
- [ ] (Deferred, not blocking) Pre-baked sandbox image — starting the dry run on
      the default `python:3.13-slim` instead.

**Note:** repo had zero git history before this deployment pass. Made an initial
commit (`66d1330`) after finding and removing a live `GEMINI_API_KEY` value that
had been sitting in `.env.example` (every other field there was empty — that one
wasn't). User confirmed it was real and should be treated as compromised —
**rotate that key in Google AI Studio, independent of anything else here.**
Also added `ssh/`, `*.pem`, `id_rsa*`, `id_ed25519*` to `.gitignore` — a private
key copy was sitting in the repo's own `ssh/` folder, uncommitted but one `git add
-A` away from being tracked forever.

## Phase 2 — Configuration

- [x] `.env` created from `.env.example` on the server.
- [x] `DATABASE_URL` — constructed by `docker-compose.yml` from `POSTGRES_PASSWORD`
      (generated with `openssl rand -hex 24`, never transmitted in chat).
- [x] `GEMINI_API_KEY` set. **Note:** the first value offered was the same one
      already found leaked in `.env.example` and flagged for rotation — caught
      before it reached the server, user rotated it in Google AI Studio, second
      (genuinely different) key used instead.
- [x] `WORKSPACE_ROOT` / `BLACKLIST_PATH` — carried through correctly from the
      `.env.example` template defaults, verified present.
- [x] `DAILY_LLM_SPEND_CAP_USD=10` set (can be changed anytime — plain env var,
      no migration, just `docker compose restart factory`).
- [x] Telegram: bot created, chat id discovered, both set in `.env`. Verified —
      escalations arriving in Telegram in real time.
- [x] `ANTHROPIC_API_KEY` set.
- [x] Mailgun: **sandbox domain** (not a verified custom domain) + API key set.
      `OUTBOUND_FROM_ADDRESS` uses the sandbox domain, no personal info in it.
      **Open**: `MAILGUN_WEBHOOK_SIGNING_KEY` not set yet (Mailgun dashboard →
      Sending → Webhooks). Sandbox domain can only send to pre-authorized
      recipients — fine for mechanism testing, not for real cold outreach.
- [x] Stripe: **test-mode** secret key set (`sk_test_...`). Live keys
      (`sk_live_...`/`pk_live_...`) were provided but deliberately NOT
      configured yet — using live payment credentials during a dry run
      contradicts the "dry run before live" rule for this pass; switch to them
      at Phase 6. **Open**: `STRIPE_WEBHOOK_SECRET` needs a real webhook
      endpoint pointed at a public HTTPS URL, which this server doesn't have
      (see the domain/TLS decision below).
- [ ] `OUTBOUND_POSTAL_ADDRESS` set (CAN-SPAM requirement) — done, PMB address.
- [x] Public HTTPS domain: `manifestaudit.com` (chosen deliberately separate
      from the user's existing personal/business domain — see privacy note
      below), Caddy added to `docker-compose.yml` as a reverse proxy with
      automatic Let's Encrypt TLS. Verified end to end:
      `curl https://manifestaudit.com/health` returns real JSON over a valid
      certificate. Factory's port 8000 locked to loopback now that Caddy is
      the sole public entrypoint (same pattern as the earlier Postgres fix).
- [ ] `UNSUBSCRIBE_BASE_URL=https://manifestaudit.com` — domain exists now,
      just needs setting in `.env`.
- [ ] `STRIPE_WEBHOOK_SECRET` — domain exists now; add a webhook endpoint in
      the Stripe dashboard pointing at `https://manifestaudit.com/webhooks/stripe`
      (test mode, matching the test secret key already configured), copy its
      signing secret into `.env`.
- [ ] `MAILGUN_WEBHOOK_SIGNING_KEY` — Mailgun dashboard → Sending → Webhooks →
      "HTTP webhook signing key"; also point the complained/failed event
      webhooks at `https://manifestaudit.com/webhooks/mailgun` while there.
- [x] Vercel token set. **Note**: `CLOUDFLARE_API_TOKEN` is a `Settings` field
      and appears in preflight's remedy text ("Vercel or Cloudflare") but has
      zero real usage anywhere in `deploy.py` or elsewhere — the remedy text
      overpromises, Cloudflare isn't actually a working alternative today.
      Considered adding real Netlify support (user has an existing
      subscription) but scoped it as genuinely new work — different API shape
      (digest/upload flow, separate env-var mechanism) comparable to what
      exists for Vercel, not a config swap — user chose the free Vercel token
      instead to unblock now.
- [x] Apollo API key set (lead provider for Marketing's sourcing).
- [x] **All six engines now enabled simultaneously for the first time**:
      `disabled_engines={}`, `ok=True`. This is the real target state for the
      rest of Phase 5 — Development (Bug Council, sandbox builds, Vercel
      deploy) has never run against real infrastructure until now.

**Privacy note (explicit user requirement):** no personal name, personal
address, or personal email anywhere in outbound-facing config. The Mailgun
sandbox domain and its generated from-address are non-identifying by
construction. The postal address is a PMB (private mailbox), not a home
address, so it's safe to use as given for the CAN-SPAM footer. **Still to
check**: Stripe's *account-level* business name / statement descriptor
(Dashboard → Settings → Business/Public details) is not something an API key
or env var controls — verify directly in the Stripe dashboard that it shows
the company name, not a personal one, before going live. If/when a real
domain is registered for Mailgun/webhooks, use WHOIS privacy protection
(offered by every registrar, usually free) so the registrant's name/address
isn't published in public WHOIS records.

**Deferred, not forgotten:** "every tool gets its own domain email" — the
current architecture has one global `MAILGUN_DOMAIN`/`OUTBOUND_FROM_ADDRESS`
for all outreach across every generated product, not a per-product identity.
Doing this properly means each generated product acquiring and verifying its
own sending domain (DNS records, Mailgun domain add) and threading a
per-product sender through `outreach.py`/`email.py` instead of the global
`Settings` fields. Real, legitimate enhancement — just larger than a config
change, and not blocking today's dry run.

**Also fixed while here (not in the original plan, found by inspection):**
`docker-compose.yml` bound Postgres as `"5432:5432"` — on a droplet with a public
IP that exposes the database to the entire internet, reachable by anyone who
guesses/scans for it. The app only ever needs it over the internal compose
network (`postgres:5432`). Changed to `"127.0.0.1:5432:5432"`; still reachable
for manual debugging via `ssh -L 5432:localhost:5432 <host>`.

## Phase 3 — Database

- [x] `docker compose up -d postgres`, healthy.
- [x] `docker compose run --rm factory alembic upgrade head` — succeeded, all 7
      migrations applied cleanly against a real database for the first time ever.
- [x] `alembic heads` shows exactly one head: `0007_deliverability`.
- [x] **First-ever run of the integration suite against a real database**:
      - First attempt: **23 failed, 42 passed.** All 23 traced to root cause and fixed
        (see bug log below). Note: production image doesn't ship `tests/` or dev
        deps by design — ran via `docker compose run --rm -v
        /opt/superagent/tests:/app/tests factory sh -c 'pip install pytest ... &&
        pytest tests/integration -q'` (bind-mount + ad-hoc install, throwaway
        container, nothing persisted to the image).
      - After fixes: **65 passed, 0 failed.**

## Phase 4 — Preflight

- [x] `docker compose run --rm factory python -m factory.preflight` run.
- [x] No HARD_BLOCK findings remain (`ok=True`).
- [x] `SANDBOX_WORKSPACE_SHARING` specifically checked — real probe-container test,
      not a config assumption. **Pass** (docker_available()=True, and
      `check_sandbox_workspace_sharing` returned no finding, meaning the sharing
      probe container's marker write/read round-trip succeeded for real).
- [x] Every DEGRADES finding reviewed: `ANTHROPIC_API_KEY` (development),
      `TELEGRAM_BOT_TOKEN` (escalations), `STRIPE_API_KEY` (finance),
      `VERCEL_TOKEN` (deploy target), `MAILGUN_API_KEY` (marketing) — all expected,
      all deferred to Phase 2's remaining items, none accidental.

**Note (real production bug, fixed):** the `docker.io` apt package on Debian
trixie (this image's base, `python:3.13-slim`) does not ship `/usr/bin/docker` at
all — only `dockerd`/`docker-proxy`/`docker-init`. `dpkg -l` reported it
"installed" the entire time; the CLI binary was simply never in the package.
Every preflight run would have reported `docker=False` forever, permanently
disabling Development on **every** deployment using this Dockerfile, not just
this one. Fixed by copying the static `/usr/local/bin/docker` binary from the
official `docker:cli` image via a multi-stage build instead of installing
`docker.io` from apt — no distro-repo dependency, and it matches the CLI/daemon
pairing philosophy already used on the host. Verified: `docker --version` →
29.7.1 (client) against `29.7.2` (host daemon, via the mounted socket).

## Phase 5 — Dry run (`FACTORY_DRY_RUN=1`, the default — confirm it's still 1)

Watching `docker compose logs -f factory` throughout. Each of these is a
first-contact-with-reality check for something built this session:

- [x] At least one Discovery cycle completes end to end against the real
      Gemini API (`discover_opportunities` task: `engine.start` → real
      `llm.call outcome=ok` → `engine.done`).
- [x] Supervisor/maintenance backlog drains to a genuine idle steady state
      (see bug #14) — `triage-escalated-tasks`, `reduce-llm-cost`,
      `refine-agent-prompts` all ran for the first time ever.
- [ ] Compliance → Development leg of the cycle (needs `ANTHROPIC_API_KEY`,
      still pending in Phase 2).
- [ ] At least one Bug Council review runs and its verdict looks sane (needs
      `ANTHROPIC_API_KEY`/Development enabled).
- [ ] A LEAD_SCOUT sourcing-refinement run appears in logs and terminates cleanly
      (not step-limited every time) — needs Marketing enabled (`MAILGUN_API_KEY`).
- [ ] An entity-resolution run appears in logs; verdict is plausible for a test lead.
- [ ] Mailgun test webhook sent from their dashboard (Sending → Webhooks → pick
      "complained" → Test webhook) — confirm `/webhooks/mailgun` logs a *handled*
      event, not "event type not tracked". This is the one piece flagged as
      unverified against a real payload shape.
      - Result: ______________________

**[REAL PRODUCTION BUG, severe] Every Stripe webhook delivery crashed —
`dict(event)` on a real `stripe.Event` object.** Found triggering the very
first real test event (`stripe trigger payment_intent.succeeded` via the
official `stripe/stripe-cli` Docker image, since this Stripe account's
"thin" event destination only offers CLI-triggered test events, not a
dashboard button). `StripeObject` (the real SDK class, `stripe==15.4.0`)
only implements `__getitem__(str)` — no `keys()`, no `__iter__`. `dict(event)`
therefore fell back to Python's legacy sequence-iteration protocol
(`event[0]`, `event[1]`, ...), which raises `KeyError` instead of the
`IndexError` that protocol needs to detect the end of a sequence — crashing
on every single real delivery, 100% of the time, since day one. No test ever
caught it: every existing webhook test drives `process_event()` directly with
a hand-built plain dict, never touching `verify()` or the real
`stripe.Webhook.construct_event()` return value at all. Fixed by using the
SDK's own `event.to_dict()` (recursive, also converting nested objects like
`data.object`), and added a real signed-payload regression test
(`tests/unit/test_finance_webhooks.py`) that constructs and verifies an
actual HMAC-signed Stripe payload rather than mocking the SDK away. Verified
live: a triggered `payment_intent.succeeded` event now produces a real
`finance.revenue` log line with the correct amount and updated ledger
balance.
- [ ] A real campaign-approval cycle: harvest runs, Telegram brief arrives, we
      reply `#<id> APPROVE`, sending unblocks. Also try one ambiguous reply on a
      second cohort and confirm it stays blocked (fail-closed).
- [ ] (Optional) Temporarily drop `DAILY_LLM_SPEND_CAP_USD` low, confirm the pause
      + Telegram escalation fires, then restore the real value.
- [ ] `/health`, `/ready`, `/metrics` all checked directly.

## Phase 6 — Go live

- [ ] Only after every box above is checked: `FACTORY_DRY_RUN=0`, restart.
- [ ] Confirm via `/health` and one log cycle that real sends/deploys are now live.

## Phase 7 — First week

- [ ] Day-by-day `/metrics` spend checked against the earlier estimate (~$2/day
      moderate-activity baseline) — flag if it's wildly off in either direction.
- [ ] `select * from interrupts where status='open';` checked daily, staying near zero.
- [ ] Telegram arriving as expected, not something we have to go looking for.

**15. [REAL PRODUCTION BUG, severe] Gemini rejects `maxItems`/`minItems` on an
array of complex items — every `discriminate()` call was failing.** Surfaced
as a bare `400 INVALID_ARGUMENT: Request contains an invalid argument`
mid-way through verifying the `adoption_ease` change, with no further detail
in the response body. Bisected directly against the live API (not guessed):
an array capped with `maxItems` works fine when its items are a simple
one-field model; the identical cap on an array of an 11-field item — mixing
string-length, `HttpUrl` (`format: uri`), and numeric-range constraints,
exactly `DiscriminatedIdeas.ideas: list[ScoredIdea]` — fails every time,
confirmed independent of the `adoption_ease` change by reproducing the same
failure with the *original* 3-dimension `ScoredIdea` and with a synthetic
item carrying the same field-type mix under generic names. This predates
today entirely; it was invisible before because every existing test mocks
the client, and `generate_ideas()`'s simpler 3-field `GeneratedIdea` schema
doesn't trip it, only `discriminate()`'s. Fixed by extending the existing
Gemini schema sanitizer (already stripping `additionalProperties`) to also
strip `maxItems`/`minItems` — safe, since Pydantic's own
`model_validate_json()` re-enforces the same bound locally after the
response comes back.

**16. [REAL PRODUCTION BUG, severe] `effort_code="xhigh"` made every
`build_mvp` attempt fail — Claude's adaptive thinking never converged.**
First real Development run: `generate_scaffold()` failed with
`SchemaViolation("claude returned no text content")` on all 3 attempts
(`max_task_attempts` exhausted, task escalated). The enriched diagnostic
added earlier this session (bug #16's own fix, see below) showed exactly
why: `stop_reason='max_tokens'`, `content_block_types=['thinking']` —
100% of the token budget spent thinking, zero text ever produced.
**Doubling `max_tokens_for(CODE_GENERATION)` from 32k to 64k did not fix
it** — the very next attempt failed identically, just at the higher
ceiling, proving this wasn't a budget-size problem. The real cause:
`effort_code="xhigh"` (the maximum reasoning-effort level) on Claude Sonnet
5, which has no `budget_tokens` cap available (rejected outright by the
API on this model) — at max effort, an open-ended "produce the complete
MVP" instruction let thinking run indefinitely regardless of how much room
it was given. Reproduced the identical failing call at `effort="high"`
instead and it completed normally: 15 files, ~37k output tokens. Fixed by
lowering both `effort_code` and `effort_review` from `xhigh` to `high` —
review is lowered preventively (same mechanism, Bug Council just hadn't run
yet to hit it independently), stated as such rather than claimed as
separately proven. `max_tokens=64_000` is kept regardless, since even at
`high` effort the real output (~37k tokens) exceeds the original 32k
ceiling.

**Preflight-relevant note**: this means a Development build can genuinely
fail 3/3 times and escalate for reasons that have nothing to do with the
generated code or the idea itself — worth remembering before assuming an
escalated `build_mvp` task means a bad idea or a sandbox problem.

**17. [REAL PRODUCTION BUG, severe, structural] Development could permanently
deadlock on its own in-flight work.** Found live, mid-dry-run, not from a
test: both products in flight got stuck in `PATCHING` with their
`patch_product` tasks sitting `pending`, unclaimed, while the orchestrator
repeated `HOLD_DISCOVERY` every single tick for over an hour. Root cause:
`orchestrator.decide()`'s capacity gate (`if snapshot.dev_at_capacity:
...`) blocked *all* routing to Development whenever
`active_dev_products >= max_concurrent_builds` — with no distinction
between admitting a **new** product (correctly throttled) and continuing
repair work on a product **already counted** in that capacity (incorrectly
blocked). Since a product cannot leave `PATCHING` without its patch task
running, and the patch task could never be routed to because the product
being `PATCHING` was itself the reason capacity read as full, this was a
permanent, self-sustaining deadlock — dry-run or live, this would have
silently stalled Development forever the first time two products needed
repair at once, without ever raising an interrupt (no error occurred; the
system just never chose to make progress).

Fixed by adding `pending_patch_tasks` to the routing snapshot and checking
it first, ahead of even Marketing: a queued repair for an already-admitted
product now always routes to Development regardless of capacity, while a
**new** `build_mvp` still waits its turn — verified safe by task priority
alone (`patch_product`=40, `build_mvp`=50; lower claims first), not just by
the new capacity carve-out. Verified live: `orchestrator.capacity_prefers_repair`
fired on the very first tick after deploying, and Development immediately
claimed the first queued patch task instead of holding.

**18. [RELIABILITY GAP, not a bug — feature, user-requested] A human reply
confirming an escalation's root cause had no way to actually retry the
task.** Raised by the user after asking what happens on a rate limit, crash,
or other transient failure: `collect_replies()` only ever called
`resolve_interrupt()` — marking the Telegram message answered — with no code
path back to the task itself, which stayed `escalated` forever. Every
escalation this deployment has needed a human for so far required manual DB
surgery (`UPDATE tasks SET status='pending', attempt_count=0 ...`) to move
again, done by hand 5+ times this session. Fixed: `repository
.reopen_escalated_task()` gives an escalated task a fresh attempt budget, and
`collect_replies()` calls it when a reply contains the word "retry" **and**
the interrupt carries a `task_id` — matching the exact word the
DevOps-error/generic-failure escalation text already asks for. Escalations
with no `task_id` (heal-cycle exhaustion, which resets the product to
`PLANNED` instead of leaving one task to retry — confirmed live, see
`f4e49f5b0304`'s escalation at 13:44:59 UTC 2026-08-10) no-op safely instead
of crashing. Verified with a new integration suite against a genuinely
separate throwaway Postgres (see hazard note below) — retry reopens the
task, a plain reply only resolves the interrupt, no-task_id doesn't crash,
and the match is case-insensitive/whole-word. Deployed and confirmed healthy
live (clean restart, no claimed/running tasks disrupted).

**19. [RELIABILITY GAP, not a bug — feature, user-requested] A missed or
forgotten Telegram escalation had no follow-up.** Companion to #18, from the
same "what happens if X fails and nobody notices" question. `deliver_pending()`
guarantees a message was *sent*; nothing previously guaranteed a human *saw*
it. Fixed: a new deterministic maintenance job (`remind-stale-interrupts`,
6h cadence, no LLM call — same family as `prune-memory`/
`reconcile-outreach-conversions`) finds open interrupts older than
`settings.stale_interrupt_reminder_hours` (default 12h,
`repository.stale_open_interrupts()`) and sends **one** consolidated
reminder for all of them, not one message per interrupt. Verified with a new
integration suite (nothing fires for fresh interrupts, stale ones land in
one message, a resolved interrupt is never reminded, an unavailable
notifier no-ops) plus a dispatch test driving `engines.maintenance.run()`
end-to-end. Full 558-test suite (unit + integration) verified clean on the
server against the scratch database before deploying; live restart
confirmed healthy, zero in-flight tasks disrupted. Not yet observed firing
live — the daily spend cap (a separate, correctly-functioning circuit
breaker) is currently paused and blocks *all* routing including maintenance
until the rolling 24h window ages out, so the first live firing is still
pending.

**20. [RELIABILITY GAP, not a bug — feature, user-requested] "Reply with guidance,
or REJECT to retire this product" did nothing.** Discovered when the user forwarded
a real live heal-exhaustion Telegram message and asked how the 3-cycle system
works. Traced the code: heal exhaustion resets the product to `PLANNED` with no
task left to retry, so nothing consumed the human's reply at all — the product just
sat in `PLANNED` forever no matter what was said. Same audit also found the
identical gap in two other places ("Reply APPROVE/REJECT" in compliance/graph.py's
idea-flag and compliance/lia.py's LIA segment-flag) — this pass fixed the
heal-exhaustion one only; the other two are tracked as follow-up, not yet built.

Fixed: migration `0008_interrupt_product_id` adds `Interrupt.product_id` (bare
column, matching `task_id`'s existing pattern) so a reply can be traced to a
product with no task. `collect_replies()` now retires the product on REJECT, or
enqueues exactly **one** more patch attempt for any other non-empty reply —
`heal_attempts` is deliberately left untouched, so a second failure re-escalates
immediately instead of quietly burning further autonomous cycles. Idempotency key
includes the interrupt id, so a product can be guided again after re-escalating
without the second attempt silently no-opping. Backfilled `product_id` on the one
live interrupt that predated the migration (parsed from its subject text) so
replying to it works immediately. Deployed after rebuilding the image — the
migration file didn't exist in the already-running image, a real near-miss caught
by checking `alembic_version` after the first `upgrade head` came back suspiciously
quiet.

**Cost note, since this came up in the same conversation:** real spend breakdown
for the first ~30h live (`llm_calls` table): $9.26 of $10.69 (86%) was
`code_generation` on Claude Sonnet 5 — 14 calls, avg ~44k output tokens each. This
was day one with all six engines enabled for the first time, two brand-new product
builds, and both hitting worst-case heal-cycle × task-attempt retry compounding
(up to 9 code_generation calls per product before final escalation, not 3) — not
representative of steady state. Every dollar this new feature can spend is
human-gated behind an explicit reply, same as the design already used for campaign
approval.

**Also noted, not yet fixed (not requested this pass):** the escalation's failure
detail is truncated to `(tests.stderr or tests.stdout)[-2000:]` before being
handed to the LLM each heal cycle (`graph.py` `_verify_and_ship`). For a build
with many failing tests, this likely hides full tracebacks for everything but the
last failure or two behind pytest's trailing short-summary section — a plausible
compounding cause of "3 cycles isn't enough," separate from the cycle count itself.

**21. [REAL PRODUCTION BUG, structural] Heal cycles regenerate the app blind and
never clean up, so failed attempts leave duplicate app/backend trees.** Found live:
a product's heal cycle got tests passing on attempt 3, but Bug Council rejected the
deploy for having the *same* bugs duplicated across `app/main.py` and
`backend/main.py` — two independent half-copies of the app. Root cause:
`generate_scaffold()` is called fresh every heal cycle and every retried
`build_mvp`, with zero visibility into files a previous attempt already wrote, and
`write_plan()` only ever adds files — never removes any. Two generations for the
same product routinely disagree on file layout, so nothing stopped both versions,
and both sets of bugs, from piling up side by side. This wasn't just a quality
problem — each blind regeneration is a full ~40-60k output-token call, and a
mangled duplicate tree is *more* likely to fail review again, wasting the next
cycle too.

Fixed with two changes: `generate_scaffold()` now takes the existing workspace
contents and shows them to the model as a "repair pass" (soft — depends on the
model following the guidance); `clear_workspace()` wipes the workspace before
`write_plan()` writes the new plan, in both `_build()` and `_patch()` (hard
guarantee — holds regardless of model behavior). Verified with new unit tests
(prompt inclusion/omission, file clearing, and the end-to-end case: write a plan,
clear, write a differently-named plan, assert the old tree is gone) plus the full
567-test suite clean on the server. No schema change, no migration. Deployed
during a natural pause (spend cap had just re-tripped at exactly $15.01, so
nothing was in flight).

## ⚠ Operational hazard discovered this pass

**Running `pytest tests/integration` against a live deployment destroys its
data.** There is no separate test database — `docker compose run --rm factory
... pytest tests/integration` uses the exact same `DATABASE_URL` as the live
`factory` service, and the integration suite drops/recreates tables as part
of its fixture setup. Every integration-suite run this session against this
server silently wiped whatever real dry-run state had accumulated (ideas,
products, learning-loop weights, everything) — discovered when a `build_mvp`
task and its parent idea vanished entirely after a routine
verification run. Nothing of real value was lost (dry-run data only), but
**do not run the integration suite against this server again** once real
data matters — either provision a genuinely separate database for that
purpose, or verify future changes with the unit suite only (isolated, no
shared state) as this pass now does.

**Update (reply-driven-retry pass):** used a genuinely separate throwaway
Postgres (`docker run -d --name scratch-pg ... postgres:16-alpine` on the
`superagent_default` network, `alembic upgrade head` against it, tunneled to
locally for the pytest run) for the new integration test in this pass — the
first time this project has verified DB-interaction-heavy logic without
touching the live database at all. Worth keeping as the standard pattern
going forward instead of the unit-suite-only fallback above.

**Second hazard found this pass, same family:** running unit tests from a
container with `/opt/superagent` bind-mounted picks up the **live `.env`**
(pydantic-settings' `Settings.model_config` uses `env_file=".env"`, resolved
relative to cwd) and silently populates real API keys into every test's
default `Settings` fixture. 19 unit tests that assert "refuses when
unconfigured" (Mailgun send, lead-provider gating, MCP tool withholding,
orchestrator capacity routing, etc.) failed for this reason alone —
`Settings(gemini_api_key=SecretStr(...), ...)` instead of the unconfigured
defaults they expect. Not a code bug; a test-isolation gap. Fixed for this
pass by copying the tree to `/opt/scratch-test` with `.env` deleted from the
copy before mounting it in; all 480 unit tests then passed cleanly, matching
the local (non-server) run exactly. **Do this — or otherwise ensure no `.env`
is reachable from cwd — every time unit tests run against a mounted copy of
this repo on the server.**

**Third hazard found this pass:** running the *entire* integration suite
together (all files in one `pytest` invocation) against the scratch database is
flaky — a run flagged 9 failures across 5 unrelated files
(`test_heal_attempts_and_attribution.py`, `test_webhooks_integration.py`,
`test_scheduler_integration.py`, `test_repository.py`,
`test_hitl_stale_reminder.py`), every one of which passed cleanly both alone and
on a full-suite re-run. A separate local (Windows) run flagged a *different* 10
failures the same way. Root cause not fully chased down (time-boxed given the
session's cost focus), but the pattern — different files fail on different runs,
100% pass in isolation — points at contention between the many integration test
files that each independently `drop_all`/`create_all` the same physical schema in
their own fixture, not a real regression. **If a full-suite run against scratch
shows failures, re-run the specific failing files alone before concluding
anything is actually broken** — this pass's own PR (`0008`) was nearly
misdiagnosed this way before isolating each failure confirmed it was clean.

## Known open items (not blocking, tracked on purpose)

- Untrusted-content handling for text an agent reads off the open web
  (RESEARCHER, LEAD_SCOUT) — flagged during the earlier Fortress-checklist
  review, never picked back up. Both paths fail closed today, so not urgent.

## Bugs / issues found during this pass

**1. [REAL PRODUCTION BUG] `repository.create_marketing_freeze()` — `ON CONFLICT`
didn't match the partial unique index.** `on_conflict_do_nothing(index_elements=
[MarketingFreeze.status])` targeted a plain unique constraint on `status` that
doesn't exist — the actual index (`ux_marketing_freezes_active`) is partial
(`WHERE status = 'active'`), and Postgres only matches an `ON CONFLICT` target
against a partial index when the predicate is repeated verbatim. Every call to
`create_marketing_freeze` (i.e. every time the spam-complaint circuit breaker
would actually trip) raised `InvalidColumnReferenceError` instead of working.
Fixed by adding `index_where=(MarketingFreeze.status == "active")`. This is
exactly the class of bug the whole dry-run process exists to catch — invisible
to mypy/ruff/unit tests, only surfaces against a real Postgres planner.

**2–6. Test-fixture bugs (not production bugs), all from tests that had never
executed against real Postgres before this pass:**
- `test_checkpointer_pool.py` — `model_copy(update=...)` bypassed Pydantic
  coercion, passing a raw `str` where `SecretStr` was required. Same class of
  bug caught once before in `test_email_blacklist.py`; wrapped in `SecretStr(...)`.
- `test_scheduler_integration.py` — stale SQLAlchemy identity-map read: the test
  asserted on an object loaded earlier in its own session, but `housekeeping()`
  mutated the row through a *different* session/connection internally. Needed
  `session.expire_all()` before re-reading.
- `test_campaign_approval.py` + `test_heal_attempts_and_attribution.py` (12 tests
  total) — `monetization_hypothesis="EUR 49/month."` was 13 characters against a
  20-character minimum on `ScoredIdea`. Lengthened the fixture string.
- `test_entity_resolution.py` (2 tests) — `EntityVerdict(evidence="e")` was 1
  character against a 10-character minimum. Lengthened the fixture string.
- `test_webhooks_integration.py` — used a literal, non-existent
  `client_reference_id="prod-abc"` against a real FK
  (`ledger.product_id -> products.id`). Added a `_make_product()` helper
  (matching the pattern already used in sibling test files) and used its real id.

**7. [REAL PRODUCTION BUG] Docker socket group-permission mismatch.** The factory
container's fixed non-root user (uid 10001) had no group membership matching the
host's `docker.sock` GID, so every attempt to reach the socket failed permission
checks silently and `docker_available()` reported false — Development would have
been permanently disabled on every real deployment. Fixed with a compose-level
`group_add: - "${DOCKER_GID:-999}"` (can't be baked into the image; the GID
varies per host/distro), documented in `.env.example` and `DEPLOYMENT.md`.

**8. [REAL PRODUCTION BUG] `docker.io` apt package on Debian trixie ships no
`docker` CLI binary.** Same silent-`docker_available()=False` failure mode as
above, different root cause: `dpkg -l` reported `docker.io` installed, but
`dpkg -L docker.io` showed it only lays down `dockerd`/`docker-proxy`/
`docker-init` — no `/usr/bin/docker`. This would have permanently broken
Development on this Dockerfile's base image regardless of host configuration.
Fixed by copying the static binary from Docker's official `docker:cli` image via
a multi-stage build instead of relying on the distro package.

**9. [REAL PRODUCTION BUG] `write_missing_credentials()` default path used
`REPO_ROOT`**, a module-file-location-derived path that resolves under
site-packages once installed via `pip install .` into a container — owned by
root, unwritable by the non-root `factory` user, crashing preflight with
`PermissionError` before it could even report findings. Fixed by defaulting to
`Path.cwd()` (`/app` in the container, the repo root in local dev), matching the
same fix pattern already used for `WORKSPACE_ROOT`/`BLACKLIST_PATH`.

**10. [REAL PRODUCTION BUG, severe] Every Gemini structured-output call failed —
`response_schema` included a JSON Schema key the live API rejects.** First real
LLM call after Telegram was wired up failed with `400 INVALID_ARGUMENT: Unknown
name "additional_properties" ... Cannot find field.` Root cause: Pydantic's
`model_json_schema()` sets `additionalProperties` wherever a `dict[str, ...]`-typed
field appears (including inside nested `$defs`), and the Gemini *Developer* API
(as opposed to Vertex AI) does not support this JSON Schema keyword in
`response_schema` at all. This blocked **every single** structured Gemini call —
orchestration, discovery, everything — permanently, on every deployment, the
moment any schema had a dict-typed field anywhere in it. Never caught before
because every existing test mocks the LLM client. Fixed in
`gemini_provider.py::parse()` by converting the schema to a dict via
`model_json_schema()` and recursively stripping the key before sending.

**11. [REAL PRODUCTION BUG, same class] The agent tool-use loop (`converse()`)
had the identical bug** in its tool-parameter schemas, surfaced the moment the
Supervisor first spawned an analyst sub-agent
(`tools[0].function_declarations[0].parameters`). Same fix, applied at
`_sanitize_tools_for_gemini()` — with an added subtlety: each tool's schema dict
is the *same shared object* as `ToolDescriptor.input_schema` in
`tools/registry.py` (passed by reference, no copy), which `to_anthropic()` also
reads for Claude's strict-mode tool schemas. Stripping in place would have
silently corrupted Anthropic's tool payloads the next time the same descriptor
was used, so the fix deep-copies before mutating. `parse()`'s own unused-today
`tools` parameter had the same latent bug and got the same fix for consistency,
since `client.py`/`metering.py` already forward it through.

**12. [CONFIG, not a code bug] `MODEL_GENERAL=gemini-2.5-pro` /
`MODEL_FAST=gemini-2.5-flash` are retired for new API keys.** Once bug #10 was
fixed, the very next real call failed with `404 ... no longer available to new
users`. Both models are still listed by Gemini's `ListModels` endpoint (so a
catalog-membership preflight check would **not** have caught this — genuinely
listed, just not invokable for a fresh key/project), which is why no amount of
"validate the model list" defensiveness in `preflight.py` would help; a real
per-boot smoke-generateContent call would catch it but costs tokens and risks
tripping quota on every restart, so this stays a documented operational
gotcha rather than an automated check. Also **found and fixed a false claim**:
`.env.example` said "Preflight validates them against the live APIs" — it
never did; corrected the comment. Fixed by switching to the `-latest` aliases
(`gemini-pro-latest` / `gemini-flash-latest`), which is also more resilient for
a system meant to run unattended for weeks (pin to a dated snapshot instead if
the learning loop's trial comparisons ever need reproducible model behavior).

**13. [EXTERNAL, blocking, not yet resolved] `429 RESOURCE_EXHAUSTED` — quota
exceeded on the just-rotated `GEMINI_API_KEY`.** Hit within the first ~2 minutes
of real traffic, on `gemini-flash-latest` (not just the heavier `-pro` model),
consistent with the Gemini Developer API's free-tier rate limit rather than a
per-model issue — this project's API key most likely has no billing enabled.
Live service paused (`docker compose stop factory`) to stop burning retries
while this is open. **Needs the user to check/enable billing on the Google
Cloud project behind this key** before the dry run can produce a real
Discovery → Compliance → Development cycle — this is outside what I can act on
from here.

**Note on test methodology, not a bug**: running the integration suite via
`docker compose run` (as opposed to plain `docker run`) pulls in the real `.env`
via compose's `env_file:` directive — this caused 12 apparently-unrelated unit
test failures (env vars like `TELEGRAM_BOT_TOKEN` leaking into tests that assert
a credential is absent by default) and one `DeadlockDetectedError` from running
the schema-touching integration suite concurrently with the live service hitting
the same database. Neither is a product bug: use plain `docker run` (bypasses
`env_file`) for unit tests, and stop the live `factory` service before running
the integration suite.

**14. [REAL PRODUCTION BUG, severe, structural] `EngineName.SUPERVISOR` was never
a real graph node — every maintenance/learning-loop task was permanently
stuck at `status=pending`, forever, on every deployment.** Noticed because
Discovery ran once, then the orchestrator spent ~9 straight ticks alternating
`ESCALATE`/`ROUTE→supervisor` on a static "6 open interrupts" count that never
went down. `graph.py`'s `ENGINE_NODES` (the tuple of engines that get a real
`_run_engine` LangGraph node) excluded `SUPERVISOR`, and `_route()` explicitly
redirected any `ROUTE` decision targeting `engine=SUPERVISOR` to the `backlog`
node — which only *enqueues* maintenance tasks (`enqueue_maintenance()`), it
never claims or runs them. `factory.engines.maintenance.run()` — the real
runner, correctly registered in `engines/base.py`'s `_MODULES` table — was
dead code from the graph's perspective; nothing ever called
`claim_next_task(engines=[SUPERVISOR])`. This meant **the entire learning
loop** (prompt-mutation proposals, trial evaluation/rollback), memory pruning,
escalated-task triage, LLM cost-reduction analysis, and prompt refinement
never actually executed in a real run — only ever exercised by unit tests
calling the functions directly, never through the live dispatch path. No test
caught it: `test_graph_wiring.py` tests every other `action`/`engine`
combination except `ROUTE` targeting `supervisor` specifically. The live LLM
orchestrator routes here constantly in practice — its rationale for "handle
interrupts / run maintenance" is indistinguishable from `IDLE_BACKLOG`'s, so
this was not a rare edge case, it was the common case. Fixed by adding
`SUPERVISOR` to `ENGINE_NODES` and removing the special-cased redirect in
`_route()`. **Verified live**: rebuilt and restarted, watched
`triage-escalated-tasks`, `reduce-llm-cost`, and `refine-agent-prompts` each
get claimed and run for the first time ever, the entire 6-item maintenance
backlog drain to empty, and the tick loop reach a genuine
`IDLE_BACKLOG: No task is ready` steady state.

All fourteen fixes are applied, verified against the real server, and ready
to commit. Bug #13 (quota/billing) is resolved — the user enabled billing on
the Google Cloud project.
