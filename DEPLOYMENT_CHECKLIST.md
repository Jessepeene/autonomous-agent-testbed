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
      **Raised twice live** as real spend data came in: → `15` (2026-08-10,
      ~16:00 UTC) → `20` (2026-08-10, ~18:05 UTC, alongside the
      `DISCOVERY_INTERVAL_SECONDS` change below). No auto-revert — rolling 24h
      window, stays until manually changed back.
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

**22. [REAL PRODUCTION BUG] `evidence_count` was defined end-to-end and never
written.** Found while answering a user question about idea approval rates: all 4
ideas in the database (approved and rejected alike) had `evidence_count=0`.
Traced it — the field exists on the schema, the ORM column, and the read path
(`next_validated_idea`), but `upsert_idea()`'s actual `INSERT` never mentioned the
column, so every idea silently took the database default regardless of how many
corroborating signals its source cluster actually had. Fixed deterministically
(`_evidence_count_for()` in `discovery/graph.py`, matching `source_url` back to
its cluster — not asking the model to self-report a number it could fabricate),
threaded through a new `upsert_idea(..., evidence_count=...)` parameter. Zero
added LLM cost. Note: this only makes the count honest — it does not yet feed
into scoring or the learning loop; `evidence_count` isn't one of the four scoring
dimensions today, so populating it is necessary-but-not-sufficient if the goal is
for well-evidenced ideas to actually score higher.

**23. [REAL PRODUCTION BUG, severe] Generic escalation subjects silently
swallowed every second failure on the same engine.** `_run_engine`'s
task-failure escalations used subjects templated only on `engine.value` (e.g.
"development task exhausted its retries", "development hit a host/environment
failure, not a code bug") — no task_id, no per-incident identifier.
`interrupts.request` dedupes on `(reason, subject, status)`, so once the first
such escalation for an engine was raised, every *subsequent, unrelated* one on
that engine collided with it via `ON CONFLICT DO NOTHING` and never reached
Telegram at all — no error, no log above debug level, nothing. Found live: a
product's patch task hit a genuine devops error and escalated, but the
message never arrived because an unrelated interrupt from hours earlier (for
an already-completed task) was still sitting open with the identical subject.
Fixed by including `task_id` in the three task-scoped subjects, and the
rationale text in the model-driven `_escalate` node's subject (previously a
completely fixed string regardless of what the model was actually asking
about). Left "Docker daemon unavailable" as a deliberate global singleton —
correct to dedupe there, every task fails identically. Two now-stale
interrupts from before this fix (`c1fe2b85...`, `0b80f408...`, both
referencing a long-completed task) are still sitting open on the live
database — harmless (new escalations no longer collide with them) but not
yet cleaned up; the cleanup `UPDATE` was blocked by the auto-mode safety
classifier and deferred rather than routed around.

**24. [REAL PRODUCTION BUG] Codegen could pin a dependency the sandbox can
never build.** Root-caused live, same investigation as #23: a product's patch
task kept failing on `pydantic==2.6.4`, which has no prebuilt wheel for Python
3.13 (the sandbox's runtime, `python:3.13-slim` — that pydantic release
predates 3.13), forcing a from-source `pydantic-core` build that needs Rust,
which needs to execute a downloaded installer in `/tmp` — mounted `noexec` by
design. An unwinnable combination for any product that pins a compiled-extension
package to an old exact version. Fixed in `SYSTEM_PROMPT`
(`scaffold/generator.py`): use minimum-version lower bounds
(`pydantic>=2.9,<3`) instead of exact old pins, with the exact observed
failure quoted as a concrete example. Not yet verified against a real
generation — will only be confirmed once a product actually builds under it.

**25. [MAJOR FEATURE] The idea validation gate — landing page + real outreach
before any MVP gets built.** Full design discussed with the user, planned via
plan mode (see the design rationale: fixed threshold not learned, one outreach
batch not multiple waves, validation capacity capped separately from real
build capacity, Learning Loop integration limited to one new key so it can
never corrupt the existing outreach/planner trial statistics), then
implemented, tested, and deployed in one pass. Full writeup: see the commit
message on `b8e6bd2`. Two real bugs found and fixed *during* implementation
(not anticipated by the plan): `EVALUATE_VALIDATION` was being blocked by the
sandbox-availability check every other Development task correctly needs but
this pure-DB decision doesn't; `leads_for_outreach` never filtered by
`product_id` at all, a pre-existing gap that would have made validation
outreach measure nothing specific to the idea being tested.

**Not yet observed firing live** — the daily spend cap tripped again right as
this deployed ($20.40/$20), so no idea has been routed through the new
`VALIDATING` path in production yet. First real exercise of this pipeline is
still pending.

**Deliberate follow-up, not built this pass:** while building the landing-page
generator, the user flagged that neither it nor the main MVP codegen has any
visual-design review — one code-focused LLM call writes the HTML/CSS directly,
with Bug Council checking security/quality, never look-and-feel. This matters
more for the landing page specifically, since a bad-looking page could produce
a false "no interest" signal for a genuinely good idea. Applied a cheap
mitigation (strengthened `LANDING_PAGE_SYSTEM`'s visual-polish requirements,
free — same call). The user's direction for the real fix: a new `DESIGNER`
role added to the fixed role catalogue (`agents/roles.py` — currently
`RESEARCHER`/`ANALYST`/`REPAIR_ENGINEER`/`AUDITOR`/`LEAD_SCOUT`), spawnable by
the orchestrator's existing model-driven deliberation when it judges a page
needs design attention — not a hardcoded pipeline step. Real, separate work on
the same scale as this pass; not started.

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

**Fourth hazard, found writing `/interest` endpoint tests:** `fastapi
.testclient.TestClient` is sync and drives the app from a second,
internally-managed event loop (anyio's `BlockingPortal`). Mixed with a test
file's own async DB fixture, this crashed with an asyncpg cross-loop/
closed-loop error on connection cleanup — reproduced identically on **both**
Windows and Linux, so it is not the Windows-ProactorEventLoop hazard above
despite looking similar at a glance. Fix: use `httpx.AsyncClient(transport=
httpx.ASGITransport(app=app), ...)` instead, which stays on the single
already-running event loop. Also needed the same `_reset_global_engine`
autouse fixture (`db_module.dispose_engine()` after each test) already used
in `test_scheduler_integration.py`/`test_interrupt_subject_uniqueness.py` —
any endpoint that calls `session_scope()` internally (i.e. anything in
`api/app.py`) draws on the same process-wide cached engine those files were
already guarding against connection leakage from.

## Known open items (not blocking, tracked on purpose)

- Untrusted-content handling for text an agent reads off the open web
  (RESEARCHER, LEAD_SCOUT) — flagged during the earlier Fortress-checklist
  review, never picked back up. Both paths fail closed today, so not urgent.
- `DISCOVERY_INTERVAL_SECONDS` — started this deployment at `120` (testing
  value, ~180x the production default) to exercise the pipeline quickly.
  Raised to `10800` (3h) on 2026-08-10 ~18:05 UTC as a middle ground once real
  spend data showed the 120s cadence was a major cost driver (63 discovery
  calls in one day vs. an expected ~4-8 at production cadence). Still not the
  documented production default of `21600` (6h) — revisit before real go-live.
- Two stale, harmless open interrupts (`c1fe2b85...`, `0b80f408...`,
  referencing a task that completed hours ago) predate the bug #23 fix and
  were never cleaned up — the `UPDATE` was blocked by the auto-mode safety
  classifier mid-session and deliberately not routed around. Cosmetic only;
  they no longer block anything since new escalations get unique subjects.

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

## Fix pass on the audit below — 2026-08-11

User directive: "I think it's important all of these bugs are fully fixed before
continuing with throwing more expensive tokens at it." Live spend cap also lowered
to $5/day for this pass. Mid-pass, the user separately asked where the prior day's
$20.40 spend went — live `llm_calls` query showed 83% ($17.03) was
`code_generation`, and of that, $9.09 (more than half) was `patch_product` heal
cycles across 5 products (one already retired after exhausting heal attempts, two
more maxed out at `heal_attempts=3`) — almost no spend went to genuinely new
products. Directly consistent with several findings below (infra failures routed
into the repair loop, sandbox timeout container leaks, Bug Council issues).

**All 10 CRITICAL findings fixed, tested, and verified against real Postgres**
(505 unit tests + 108 integration tests, 0 regressions; new migration `0010`
applied cleanly). Not yet deployed to the live server or committed as of this
checkpoint — see the end of this section.

1. **Ledger race** — `append_ledger` now takes a Postgres transaction-scoped
   advisory lock before reading the running balance, serializing concurrent
   webhook deliveries. Regression test fires two real concurrent `append_ledger`
   calls via `asyncio.gather` against Postgres and asserts both amounts land.
2. **Double-counted revenue** — `REVENUE_EVENTS` narrowed to exactly one canonical
   event per money flow (`checkout.session.completed` +
   `checkout.session.async_payment_succeeded` for one-time payments, `invoice.paid`
   for subscriptions); `invoice.payment_succeeded`/`payment_intent.succeeded`
   removed. `checkout.session.completed` now gated on `payment_status` so an
   async (SEPA/ACH) session isn't booked before it actually settles. `DEPLOYMENT.md`
   gained a "Stripe setup" section listing exactly which events to select in the
   dashboard and which *not* to add.
3. **GDPR erasure crash** — `outreach_log.lead_id` is now nullable with
   `ON DELETE SET NULL` (migration `0010`) instead of a plain `NOT NULL` FK with no
   delete action; `erase_lead` no longer raises. Unsubscribe token mismatch also
   fixed: `send_to_lead` was generating one token for the email body and
   `record_outreach` was silently generating a *different* one to store —
   `record_outreach` now accepts the caller's token instead of minting its own.
4. **Ephemeral blacklist** — `blacklist.txt` is now bind-mounted
   (`docker-compose.yml`, `BLACKLIST_HOST_PATH`, default
   `/srv/factory-blacklist/blacklist.txt`) instead of baked into the image, so
   runtime erasure tombstones survive a redeploy. Seeded on the live server from
   the current in-image copy (no real tombstones existed yet to lose).
5. **Leads re-emailed indefinitely** — `leads_for_outreach` now excludes leads with
   a prior `sent` `OutreachLog` row (a `blocked`/`suppressed` attempt never reached
   the recipient, so those remain retry-eligible).
6. **Dead debit path / monthly card reset** — Stripe Issuing card spending-limit
   `interval` changed from `monthly` to `all_time`, so the card is a genuine
   lifetime ceiling on spend regardless of whether `budget.authorize()` is ever
   wired to a real caller (it currently isn't — no code path in this repo executes
   paid spend yet, so there was nothing to wire up without inventing a feature;
   flagged here rather than silently dropped).
7. **Maintenance tasks silently marked done without running** —
   `enqueue_maintenance` was tagging tasks with their catalogue item's *topic*
   engine (e.g. DEVELOPMENT for `audit-live-products`), but only `SUPERVISOR`'s
   `run()` handles `TaskKind.MAINTENANCE` — now every maintenance task is enqueued
   as `engine=SUPERVISOR` regardless of topic; the topic tag still gates whether an
   item is enqueued at all. `audit-live-products`, `improve-test-coverage`, and
   `reconcile-outreach-conversions` had never executed once before this fix.
8. **Scoring-weights trial could disable the idea quality filter** —
   `WeightsCandidate` now has a `model_validator` rejecting any proposal whose four
   weights don't sum to ~1.0 (±0.02), since `weighted_score` is an unnormalised sum
   calibrated against a fixed `SCORE_THRESHOLD`.
9. **Provider 4xx misclassified as retryable** — both providers now classify a
   permanent 4xx (bad key, bad schema, model not found) as the new
   `ProviderRequestRejected` (a `ProviderUnavailable` subclass), which skips the
   3-attempt same-provider retry loop and fails over immediately instead of
   burning attempts on a request that will never succeed. Anthropic's
   `_use_fallbacks` permanent-disable no longer fires on every 400 — only ones
   whose message actually mentions the beta/fallback header.
10. **Silent Opus-5 failover + $0-cost failed calls** — cross-vendor failover to
    Anthropic for a normally-cheap Gemini task (orchestration/discovery/analysis/
    compliance/marketing copy) now uses a new `model_fallback_anthropic` setting
    (`claude-haiku-4-5`) instead of `model_code` (Opus 5, priced for code work).
    `ProviderRefusal`/`SchemaViolation` now carry the real `Usage` from the
    response that triggered them (both are frequently billed, successful HTTP
    200s) instead of being recorded as 0 tokens / $0; an intermediate
    schema-violation attempt that succeeds on repair is now also recorded
    (previously dropped entirely).

**Verification**: built a `superagent-factory:verify` image from the full change
set, ran `alembic upgrade head` against a fresh scratch Postgres container, then
505 unit tests (Linux, matching the deploy target) and 108 integration tests, all
green.

**Deployed live** (2026-08-11): committed (`920af82`), transferred to
`/opt/superagent` via `git archive HEAD`, image rebuilt, migration `0010` applied
against the live database (`outreach_log` had 0 rows — zero-risk), persistent
blacklist volume confirmed seeded, `factory` service restarted. `/health` reports
`ok` with all six engines enabled and no disabled reasons; boot log clean, no
errors. Spend cap still correctly paused (trailing 24h spend from before this pass
hasn't aged out yet) — this pass made no new LLM calls against the live service.

**Not yet started** (superseded below): the HIGH and MEDIUM/LOW findings below
this line remain unfixed as of this checkpoint.

## Fix pass, HIGH-severity tier — 2026-08-11

Continuation of the same directive. **All 12 HIGH findings fixed, tested, and
deployed live** (541 unit tests + 119 integration tests, 0 regressions, no schema
migration needed for this batch).

1. **Deploy reported success before Vercel finished building** — `deploy()` never
   checked `readyState` on the create-deployment response, so a product could go
   LIVE (and marketing start harvesting leads) before its build had even started,
   let alone succeeded. Now polls `GET /v13/deployments/{id}` (bounded: 6 attempts,
   10s apart) until a terminal `readyState`; `DeployResult` gained `is_infra: bool`.
2. **Infra failures routed into the code-repair loop** — a Vercel 5xx/429/401/403,
   a missing `VERCEL_TOKEN`, or a Bug Council reviewer that could not complete
   (provider outage) all used to spend a heal cycle regenerating code that was
   never the problem. Both now raise `DevOpsError` (`bug_council.CouncilUnavailable`
   for the review case), matching the sandbox's own existing devops-error pattern —
   confirmed via `supervisor/graph.py::_run_engine`'s existing handler, unchanged.
   Fixed the underlying `asyncio.gather` orphan-task risk in the same pass:
   `_model_review` no longer swallows a reviewer's exception into a synthetic
   rejection verdict; `return_exceptions=True` ensures every sibling reviewer is
   awaited to completion instead of left running unretrieved.
3. **Bug Council silently truncated its review corpus at 60,000 characters** (~15-
   20K tokens — the model's real context window is 1M) **with no marker**, while
   `review_corpus` told the reviewer "the complete codebase under review follows"
   regardless. Limit raised 5x to 300,000 chars; when still truncated, an explicit
   marker names every cut-off/omitted file and the framing text is honest about it.
   `dist`/`build`/`.pytest_cache` output shipped via deploy but was excluded from
   review — `deploy._collect_files` and `generator.read_workspace` now share one
   `WORKSPACE_SKIP_DIRS` constant so nothing can ship unreviewed by drifting apart.
4. **The validation-gate trial metric could never resolve on real evidence** —
   `landing_page_interest_rate`'s `MIN_TRIAL_N=20` was structurally unreachable
   within the realistic per-product episode volume; lowered to 10. Separately,
   ANY trial with near-zero episodes auto-*confirmed* once `MAX_TRIAL_DAYS` elapsed
   (`fisher_exact_less(0, 0, ...)` returns 1.0 for every baseline) — added an
   absolute floor (`_MIN_ABSOLUTE_N=5`, both sides of the comparison) below which a
   timed-out trial rolls back as "insufficient data" instead. Also retired the
   dead `TRIAL_DAYS=14` constant (written to `trial_until`, never read; the real
   cutoff was always the unrelated `MAX_TRIAL_DAYS=30`) — `trial_until` is now the
   actual single source of truth.
5. **`EngineName.SUPERVISOR` was missing from the deterministic routing priority
   table** — maintenance-only pending work (a common steady state) returned
   "ambiguous," forcing a real orchestration LLM call every 15s tick until the
   model happened to route there — the exact cost regression `routing.py`'s own
   docstring claims doesn't happen. Added last (lowest priority, matching
   maintenance's own `priority=500`). **Build-capacity deadlock, generalized**: the
   at-capacity gate only ever recognized a pending `PATCH_PRODUCT` as a safe
   retry for a product already counted; a `BUILD_MVP` that threw partway through
   (status set to `BUILDING` before the attempt, never reset on failure) left its
   retry just as invisible — two such products could permanently fill both
   `max_concurrent_builds` slots. New `count_pending_dev_retries` covers both task
   kinds by joining on the product's own status, not the task kind.
6. **The agent loop never checked `stop_reason`** — any turn with no tool calls was
   `"completed"`, including an empty answer (adaptive thinking consuming the whole
   output budget — the identical failure mode already fixed for `parse()`'s
   `SchemaViolation`) or text truncated by hitting the token budget mid-thought. A
   maintenance cycle that produced nothing logged "done"; a truncated half-sentence
   got written into permanent memory as a "lesson" (`spawner.py` already correctly
   gates that write on `result.succeeded`, so fixing the signal here was sufficient
   — no change needed there). New `AgentResult.stopped_because` values
   `"empty_response"`/`"truncated"`, neither counted as success.
7. **Memory dedup's tokenizer never produced a purely-numeric token** — two facts
   differing only in a number ("cap raised to 40 percent" vs "60 percent") hashed
   to identical token sets and were merged as near-duplicates, with the stale
   number sometimes ending up at *higher* confidence than a real correction would
   have. `discovery/cluster.tokenize()` gained an `include_numeric` parameter
   (discovery clustering itself deliberately keeps the letter-only default — a
   bare number in an unrelated public complaint is usually noise, and that
   tokenizer has never been re-tuned against including one); `memory/store.py`'s
   dedup now uses it.
8. **Memory pruning's value score was dominated by content length** — dividing by
   raw character count (20-2000 chars, an 80x spread) against a value term that
   only spans ~0.0-1.0 (a 4x spread) meant the single most valuable memory in a
   scope was frequently the first one evicted. Now divides by `log2(length)`
   (~2.5x spread), preserving the original "prefer concise" intent as a tie-
   breaker without letting it swamp confidence/hits/recency. Separately: `overflow`
   was computed against every row including exempt `PREFERENCE` ones, so a scope
   dominated by preferences (e.g. 205 preferences + 30 lessons, budget 200) evicted
   every learned memory trying to reach a budget only the preferences were pushing
   it past, and was left permanently over budget with nothing left to prune.
   `overflow` now excludes `PREFERENCE` rows from both sides of the comparison.
9. **Marketing's per-domain contact cap was only enforced at LIA (lead-ingestion)
   time** — several leads at the same company could all clear the cap check before
   any of them were actually sent to, then all get contacted in one batch, blowing
   through `MAX_CONTACTS_PER_DOMAIN_PER_MONTH` by however many shared a domain.
   Re-checked live immediately before each send in `_outreach`'s loop (the DB
   session already sees earlier sends in the same batch via `record_outreach`'s
   flush, no extra bookkeeping needed).
10. **`SEND_OUTREACH`'s idempotency key could collide on a coincidental lead
    count** — keyed on `report.stored` (how many new leads a harvest stored), so
    two different harvest runs storing the same count produced identical keys and
    the second run's send task silently no-op'd via `ON CONFLICT DO NOTHING`,
    permanently dropping that batch. Now suffixed with the harvest task's own id
    instead (unique per attempt, stable across retries of the same task).
11. **The campaign-approval interrupt subject could collide** — `Interrupt.subject`
    is a bounded `String(200)`, and the disambiguating `"(N leads)"` suffix was
    appended *after* a potentially-long, multi-line product summary; if the
    summary alone consumed the column budget, two different cohorts for the same
    product (different task, different lead count) could truncate to an identical
    subject, and `open_interrupt`'s subject-based dedup would silently collapse
    the second approval request into the first — whose own `interrupt_id` then
    never gets set, so `sync_decisions()` can never resolve it.
    `task_id[:8]` (unique per task, same convention as `interrupts.tag_for`) now
    goes first, before any truncation risk, with the summary explicitly bounded.
12. **`bounced_at` was recorded on every bounce webhook but never read anywhere**
    — no circuit breaker existed for a sustained high bounce rate, unlike the
    complaint-rate one right next to it in the same module. New
    `bounce_rate_threshold` setting (5%, the industry-standard hard-bounce bar);
    `_check_and_trip` now evaluates both rates independently on every bounce *or*
    complaint event and trips the same freeze/interrupt mechanism either one
    crosses its threshold.

**Deployed live** (2026-08-11): committed (`6a7a33e`), transferred via
`git archive HEAD`, image rebuilt, service restarted. `/health` reports `ok`,
all six engines enabled, boot log clean. No schema migration in this batch.

## Fix pass, MEDIUM/LOW tier — 2026-08-11

Continuation of the same directive. **15 of the MEDIUM/LOW findings fixed and
deployed live** (548 unit tests + 122 integration tests, 0 regressions, no schema
migration). The remaining 4-5 are deliberately deferred — see the end of this
section for exactly which and why.

1. **LIA escalation subject collision** — keyed only on `lead.email_domain`;
   `open_interrupt`'s dedup is on `(reason, subject, status)`, so two different
   uncertain leads at the same company silently collapsed into one escalation,
   and the second was never actually delivered for review. Keyed on the lead's
   own (unique) `work_email` instead.
2. **Generic task-exhaustion escalation's own comment claimed it said "retry"** —
   it didn't (unlike the DevOpsError escalation right next to it, which does).
   Added the missing "reply retry" instruction to the detail text.
3. **LIA uncertain-lead escalation carried no `task_id`/`product_id`** — made it
   impossible to correlate the interrupt row back to a specific task or product.
   Threaded through `_decision_for`/`evaluate_leads`/`evaluate_lead` from
   `leads.py::ingest()`'s already-available `task_id`/`product_id`.
4. **Outreach copy could reference the wrong product** — the non-validating branch
   of `_outreach()` pulled candidates from the *global* lead pool
   (`leads_for_outreach` with no `product_id` filter) while composing copy for one
   specific product (`summary`/`resolved_id`, resolved from the task). A lead
   actually sourced for a different product could receive a mismatched pitch.
   Scoped to `resolved_id`, matching the validating branch (falls back to the
   unscoped pool when there's genuinely no product to scope to).
5. **Hunter/Apollo silent gaps** — Hunter's "no domains"/"key not configured"
   early return, and a malformed-JSON response for one domain mid-loop (which
   previously aborted the whole search and discarded every candidate already
   found for prior domains), both had zero log trace. Apollo only ever fetches
   page 1 with no indication more results existed beyond it. All three now
   logged (`leads.hunter_skipped`, `leads.hunter_bad_json`, `leads.apollo_truncated`).
6. **Missing `STRIPE_API_KEY` silently omitted from a deployed product's
   environment** — ships LIVE with checkout that can never work, with no log
   saying why. Now logs `deploy.stripe_key_missing`.
7. **`is_devops_error`'s keyword list misrouted real application bugs** — a bare
   substring match on "permission denied"/"operation not permitted" also matches
   a generated test's own assertion text quoting that exact phrase; exit 137 was
   treated as automatically infra, but both call sites already check
   `result.timed_out` and return via a dedicated heal category *before*
   `is_devops_error` is ever consulted — so a 137 reaching this function was
   never our own timeout kill, it is almost always the sandbox's own `--memory`
   cgroup OOM-killing a process *inside* the container, i.e. the generated app's
   own unbounded memory growth (exactly the compute-burn defect the security
   reviewer hunts for). The two ambiguous phrases now require an actual
   shell-diagnostic prefix (`<command>: ...: <message>`); 137 removed from the
   automatic exit-code set.
8. **A sandbox timeout only killed the local `docker run` CLI process** — the
   daemon-side container it started kept running indefinitely (`--rm` only fires
   on exit, which a runaway loop never reaches), still consuming its
   `--memory`/`--cpus` slice, with no `--name`/`--label` to find the orphan by
   afterward. Every container now gets a unique `factory-<uuid>` name; the
   timeout path explicitly runs `docker kill <name>` in addition to killing the
   CLI process.
9. **Unbounded stdout/stderr buffering** — a single `communicate()` call read the
   entire output into the factory's own process memory with no cap. Replaced
   with a capped reader (`_read_capped`, 1MB) that keeps draining the pipe past
   the cap (so the child process never blocks on a full pipe buffer) but stops
   retaining bytes past it.
10. **`clear_workspace()`'s `ignore_errors=True` swallowed every deletion failure
    with no trace** — exactly the "stale files silently persist" bug this
    function exists to prevent, one layer deeper (the delete itself silently
    failing instead of the write silently not clearing first). Now logs
    `scaffold.clear_workspace_failed` per failure via `shutil.rmtree`'s `onexc`,
    with the same non-crashing resilience as before.
11. **`run_in_sandbox` (the tool an ephemeral agent uses to run shell commands)
    never acquired `product_lock`** — unlike Development's own writers
    (`_build`/`_patch`, which hold the lock for their whole clear+write+verify
    cycle), the only role with sandbox access (`REPAIR_ENGINEER`, spawned e.g. by
    the "improve-test-coverage" maintenance item) could run commands against a
    product's workspace at the exact moment Development was mid
    `clear_workspace()`/`write_plan()` for that same product. New
    `repository.get_product_by_workspace` resolves the owning product; the tool
    now holds `product_lock` for the command's duration and returns a clean
    "workspace is locked, try again" result (not a crash) if another engine
    already holds it. Workspaces with no tracked product (ad hoc/test) still run
    unguarded — nothing to protect against contention over.
12. **Agent-loop deadline only checked once per step, before the model call** — a
    single turn requesting several slow tool calls (each individually allowed up
    to 120s) could run minutes past the deadline before the *next* step's check
    ever fired. Now also checked before each individual tool call within a step.
13. **Latent crash, no current trigger** — google-genai's own request-config
    schema model (`types.Schema`) is `extra="forbid"` and lacks several JSON
    Schema keywords Claude's side accepts (`exclusiveMinimum`, `multipleOf`,
    `uniqueItems`, `const`, `oneOf`/`allOf`, `discriminator`). A future contract
    using one of those (e.g. `Field(gt=0)`) would have hit the SDK's own internal
    `pydantic.ValidationError`, uncaught by any existing except clause, surfacing
    a confusing error about google's schema model instead of ours. Now caught and
    converted to `ProviderRequestRejected` (permanent for this schema against
    this provider, but still failover-eligible to Anthropic) in both `parse()`
    and `converse()`.

**Deliberately deferred, not silently dropped:**

- **Lead-provider quota tracked by rows stored, not API calls consumed** —
  `leads_created_this_month()` counts rows that survived provenance/entity/LIA
  filtering, which undercounts real provider-side usage (many API results never
  reach storage). Safe-direction (risks an eventual provider 429, not overspend),
  but a correct fix needs a new persistent usage counter incremented at each
  provider `.search()` call, not a quick patch — a real design decision on what
  "quota" should mean, deferred rather than rushed.
- **Stripe-event reconciliation buckets on a ~100s window** — occasionally delays
  a card-limit sync by up to that long; already bounded by an existing hourly
  sweep, so left as documented, low-priority behavior rather than churned.
- **PII persists indefinitely in `interrupts.detail`/`campaign_approvals.brief`**
  — a real data-minimization gap, but fixing it means designing a retention
  policy (how long is defensible?) and a new purge maintenance task — a feature
  decision for the user, not a bug fix.
- **Idea-level confidence-threshold asymmetry** (uncertain-but-possibly-unlawful
  ideas stay live longer than confidently-rejected ones) — a policy judgment
  call about acceptable risk, not a code defect; needs the user's direction on
  the intended behavior before touching it.
- **Maintenance cadence buckets have no catch-up** after a sustained
  capacity-starved period (pending work can burst all at once later) — real, but
  bounded in practice (the backlog only ever holds the fixed 11-item catalogue)
  and a correct fix is a meaningfully larger scheduling change relative to its
  impact.

**Deployed live** (2026-08-11): committed (`fef1f9c`), transferred via
`git archive HEAD`, image rebuilt, service restarted. `/health` reports `ok`, all
six engines enabled, boot log clean.

## Fix pass on the 5 deliberately-deferred items — 2026-08-11

The MEDIUM/LOW pass above deferred 5 items rather than guessing at policy calls
that were the user's to make. Direction given: items 1 (lead quota by rows, not
calls) and 2 (~100s Stripe reconciliation bucket) — leave as-is, already
bounded/safe-direction. Item 5 (maintenance cadence catch-up) — accepted as-is
("as long as maintenance doesn't get a backlog it's not a problem"). Items 3 and
4 got real direction and are fixed below. 552 unit tests + 135 integration tests,
0 regressions, no schema migration.

1. **Idea-level confidence-threshold asymmetry (item 4, first half)** — the real
   bug, found by reading `compliance/assessor.py::assess_idea` against the
   already-correct sibling `finalise_lead_assessment`: the confidence-vs-threshold
   check ran *unconditionally*, so a REJECTED verdict issued with low stated
   confidence got silently upgraded to FLAG_FOR_REVIEW — a needless human
   escalation for an idea that was already safely, conservatively decided, and one
   more "uncertain" idea left sitting in the pipeline pending a reply that was
   never actually needed. Only APPROVED needs a confidence bar now (approving
   requires being sure; rejecting cautiously is already the safe default the
   system prompt asks for) — matches the ordering `finalise_lead_assessment`
   already used.
2. **Flagged-idea HITL replies never resolved anything (item 4, second half)** —
   found while confirming "uncertain ideas shouldn't stay in the pipeline":
   `compliance/graph.py`'s FLAG_FOR_REVIEW escalation asks a human to "Reply
   APPROVE to allow the build, or REJECT to discard the idea," but
   `hitl/interrupts.py::collect_replies` had no code path for either word — it
   only recognized "retry" (task-level) and product-level guidance/REJECT replies.
   The idea just stayed `flagged` forever, invisible to every pipeline count but
   never actually resolved either way, regardless of what a human said. Now
   detected by task kind (`ASSESS_COMPLIANCE`): REJECT sets the idea terminal
   (`rejected`); APPROVE sets it `approved` and re-enqueues `ASSESS_COMPLIANCE`
   under a fresh idempotency key, which rides `compliance/graph.py::run()`'s
   existing "already approved on a previous pass" short-circuit straight to
   `_create_validation` — zero further LLM spend, and the real
   `max_concurrent_validations` capacity check runs instead of this module
   duplicating that business rule.
3. **PII retention tied to product lifecycle (item 3, first half)** — direction:
   "should be tied to the product itself." `repository.retire_product` (the single
   terminal transition for a product, however reached) now also calls the new
   `purge_product_pii`, which redacts `CampaignApproval.brief` and
   `Interrupt.detail` (both directly product-linked interrupts and ones linked via
   `task_id → Task.product_id`, e.g. a campaign-approval escalation) for that
   product. Subject/status/timestamps survive — only the free text a human or
   model wrote role/company detail into does not. `development/graph.py`'s
   validation-gate failure path (the other RETIRED entry point) now goes through
   `retire_product` too, instead of a bare `set_product_status`, so both terminal
   paths get retention enforced through the one choke point.
4. **Escalation-pattern learning for recurring heal cycles (item 3, second half)**
   — direction: "if the same type of escalation happens time after time (for
   instance heal cycle) the system should learn so the same mistake doesn't
   happen again and again." `_heal()` now writes a FAILURE memory on *every*
   cycle (product-scoped, category-keyed content so a recurring category
   reinforces one memory rather than piling up near-duplicates — see
   `memory/store.py::remember`'s existing near-duplicate merge), plus a second,
   engine-scoped memory when the whole heal budget is exhausted (visible to
   future spawned diagnostic agents via `agents/spawner.py`'s own recall, so a
   category that keeps burning the whole budget across *different* products is
   visible there too). The other half of closing the loop: `_patch()` now calls
   `memory_store.recall_block` (scoped to the product) before generating its next
   attempt and folds the result into the prompt — so a product's own heal history
   is actually visible to the model trying to fix it, not just sitting in the
   table for an audit to find later. Cross-product leakage is structurally
   impossible (`recall_block` only ever reads the scopes it's handed, and a
   different product's scope string is never in that list) — covered directly by
   `test_a_different_products_heal_history_is_never_recalled`.

**Two test bugs found and fixed during server-side verification** (not production
bugs): `test_recurring_same_category_reinforces_rather_than_piling_up` expected
`hits==2` for one reinforcement (the `Memory.hits` column defaults to 0, so one
reinforcement lands on `1`), and needed `session.expire_all()` before re-reading a
row updated via a raw `UPDATE` in the same session (the fixture sets
`expire_on_commit=False`). `test_a_different_products_heal_history_is_never_recalled`
was claiming the wrong task via `claim_next_task`'s priority ordering — `_heal()`'s
own auto-enqueued patch task (priority 40) always outranks a default-priority
(100) manually-enqueued test task — fixed by fetching the intended task by id
instead.

**Deployed live** (2026-08-11): committed (`25f072c`, test fixes in `42ae15d`),
transferred via `git archive HEAD`, image rebuilt, service restarted. `/health`
reports `ok`, all six engines enabled, boot log clean. No new alembic migration
(live DB already at `0010_gdpr_erasure_fixes`, unchanged by this pass).

## Comprehensive codebase audit — 2026-08-10 (research only, NOTHING below is fixed yet)

Requested directly: "can you do a comprehensive scan of the entire codebase, to
catch any more of these system breaking problems?" Six parallel agents each
audited one subsystem end-to-end (call sites included, not just the module
itself): marketing, compliance, finance, development-engine internals,
core infra (LLM client/providers/agent runtime/memory), and the Learning Loop +
Supervisor. This section is the consolidated, prioritized result. **Nothing in
this section has been fixed, tested, or deployed** — this pass was scoped as
scan-only; next step is the user picking what to fix and in what order.

Full per-finding detail (exact mechanism, concrete failure scenario) lives in
the session transcript for each subagent; this section is the prioritized
index. File:line references are accurate as of this commit.

### CRITICAL — money integrity, legal exposure, or a shipped feature that cannot work

1. **Concurrent Stripe webhook deliveries corrupt the running ledger balance,
   permanently.** `state/repository.py:764-814` `append_ledger` does
   read-then-write on the last row's balance with no row lock, no advisory
   lock, no serializable isolation. Two webhooks arriving together both read
   balance=0 and both write from that stale base — the ledger silently
   under-counts real revenue forever after, and the reinvestment card limit is
   computed from the wrong number indefinitely.
2. **One Stripe payment gets recorded 2-4 times.** `finance/webhooks.py:26-33`
   `REVENUE_EVENTS` includes multiple event types Stripe fires for the *same*
   money (`invoice.payment_succeeded` + `invoice.paid`; `checkout.session.completed`
   + `payment_intent.succeeded`), and dedup is on `event.id` which differs per
   event. Default outcome for a normal Stripe integration, not an edge case.
3. **GDPR Art. 17 erasure has never once succeeded — it always crashes.**
   Confirmed independently by both the marketing and compliance audits: the
   unsubscribe/erasure path resolves through an `OutreachLog` row that
   references the very `Lead` being deleted, and `OutreachLog.lead_id` has no
   `ondelete` clause, so every erasure attempt raises a foreign-key violation.
   Compounding: the unsubscribe token used to look up the lead is never
   persisted correctly in the first place, so `GET /u/{token}` 404s before it
   would even reach the crash — meaning **no unsubscribe link the factory has
   ever sent has worked.**
4. **The suppression list lives inside the Docker image with no volume.**
   `blacklist.txt` — every redeploy silently resets it, un-suppressing every
   address that ever unsubscribed. Combined with #3, there is currently no
   working mechanism, at any layer, for honoring an unsubscribe or an erasure
   request.
5. **Leads are re-emailed indefinitely — nothing marks a lead "already
   contacted."** `leads_for_outreach` has no filter excluding leads with a
   prior successful send. Confirmed independently by both the marketing and
   compliance audits. This is also a data-integrity problem for the
   just-shipped validation gate: duplicate/repeat sends can inflate or corrupt
   the response-rate math `_evaluate_validation` uses to decide build-or-scrap.
6. **The only ledger debit path is dead code, and the card's spend ceiling
   resets every month regardless of what's actually left.** `budget.authorize()`
   has zero callers; no `kind="spend"` row is ever written, so the
   reinvestment pool only ever grows. The card's Stripe Issuing control uses
   `interval: "monthly"`, a per-interval counter — so once real revenue funds
   a limit, that same limit is available again every month, forever, with no
   corresponding debit anywhere. Directly contradicts `cards.py`'s own
   docstring claim that a budget-module bug "cannot charge money the factory
   has not earned."
7. **Maintenance tasks not tagged `engine=SUPERVISOR` are silently marked DONE
   without running.** `engines/base.py`'s runner map only wires
   `TaskKind.MAINTENANCE` for the Supervisor engine; three catalogue items
   (`improve-test-coverage`, `audit-live-products` — both DEVELOPMENT;
   `reconcile-outreach-conversions` — MARKETING) get claimed by an engine that
   has never heard of that task kind, return an "unsupported task kind"
   string, and complete instantly with no error. Concretely: **revenue
   attribution reconciliation has never executed once**, and live-product
   health auditing has never executed once — both are fully dead code from the
   real dispatch path, exactly like bug #14 above before it was fixed, just
   one layer deeper. (A fourth item, `refresh-market-signals`, is worse in the
   other direction — it silently runs a full real Discovery cycle, doubling
   LLM spend every 12h under a slug that claims to only re-score existing
   ideas.)
8. **A learned scoring-weights trial can be confirmed with weights that don't
   sum to 1, silently disabling the idea quality filter.** `WeightsCandidate`
   bounds each weight to `[0,1]` independently with no sum constraint. A
   proposal like `{0.5, 0.5, 0.5, 0.5}` roughly doubles every idea's score,
   pushes survival rate to ~100%, statistically confirms as a clear win, and
   becomes permanently active — silently turning `SCORE_THRESHOLD` into a
   no-op with no self-correcting mechanism (the loop can ratchet this way but
   never back).
9. **Every permanent provider auth/schema failure (revoked key, bad schema,
   401/403/404) is misclassified as retryable, and silently fails over to the
   most expensive model.** Both `gemini_provider.py:200-204` and
   `anthropic_provider.py:172-175` treat *all* 4xx errors as retryable
   (dead branch: both code paths do the same thing). 3 paid retries, then
   silent cross-vendor failover — and the failover model for anything normally
   routed to cheap Gemini Flash is `settings.model_code` (Opus 5), ~10-25x the
   price, with zero alarm anywhere. A revoked/quota-exhausted `GEMINI_API_KEY`
   would silently move 100% of orchestration/discovery/analysis/compliance/
   marketing traffic onto Opus-5 pricing indefinitely.
10. **The cost ledger is structurally blind to some of the most expensive
    failure modes.** Failed LLM calls (refusal, schema-violation) are recorded
    with `usage=None` → `0/0/0/0` cost, even though they were billed; the
    schema-repair retry path isn't recorded at all. A build that burns a full
    64k-token thinking budget twice (fails schema validation, gets repaired,
    fails again) shows up in `llm_calls` as `$0.00` — meaning the daily spend
    cap and the cost-reduction maintenance agent cannot see or react to this
    exact failure mode.

### HIGH — real money/data loss on a less common path, or a just-shipped feature undermined

- **Deploy reports success before Vercel finishes building** (`deploy.py:141-145`
  never checks `readyState`) — product goes LIVE and marketing starts lead
  harvest against a URL that may still 404.
- **Infra failures get routed into the code-repair loop.** A Vercel 5xx/429/401
  or missing `VERCEL_TOKEN`, and separately any Bug Council reviewer error
  (including a transient 529), both trigger a full rebuild-and-heal cycle on
  code that was never broken — can burn the entire heal budget and escalate
  "repair did not converge" on working code.
- **Bug Council's model reviewers silently see only the first 60KB of a build**
  (`bug_council.py` truncates mid-file with no marker) while being told "the
  complete codebase under review follows" — security/logic review quietly
  degrades on any build over ~150KB, worse the bigger the product gets.
  `dist/`/`build/` output is deployed but excluded from every reviewer's input
  entirely — a secret or malicious code baked into build output ships
  unreviewed by anyone.
- **The just-shipped validation-gate trial metric (`landing_page_interest_rate`)
  can never reach statistical significance.** One episode per product, gated
  behind a human campaign-approval wait, `MIN_TRIAL_N=20` inside a 30-day
  window — the realistic ceiling is ~6-8 episodes, so the trial will always
  auto-confirm by timeout on a near-zero sample. This directly undermines the
  feature built this week: the landing-page prompt "learning" is currently
  statistical noise.
- **Refund larger than the recorded balance 500s the webhook endpoint**, and
  after repeated 500s Stripe auto-disables it — silently stopping *all* future
  revenue recording, not just that one event.
- **A won dispute is never unwound**, and `charge.refunded`'s cumulative
  `amount_refunded` is double-subtracted across partial refunds — both leave
  the ledger permanently and silently wrong in the merchant's favor or against
  it.
- **`EngineName.SUPERVISOR` is missing from `routing.PRIORITY`**, so whenever
  the only pending work is maintenance, the deterministic router returns
  "ambiguous" and fires a real orchestration LLM call every 15s tick — the
  exact cost regression `routing.py`'s own docstring claims doesn't happen
  anymore.
- **The build-capacity gate only accounts for `PATCH_PRODUCT` retries.** Any
  `BUILD_MVP` that throws leaves its product stuck `BUILDING` forever (never
  reset on failure) without being recognized as a pending-retry case — with
  `max_concurrent_builds=2`, two failed builds in a row permanently fills both
  slots and the factory goes idle with pending work it will never claim.
- **The agent loop never checks `stop_reason`.** An empty response counts as
  success (a maintenance cycle that did nothing reports "done"); a
  `max_tokens`-truncated answer is accepted as final and, in at least one
  path, written into permanent memory as a "lesson."
- **Memory dedup can't tell two different numbers apart.** The tokenizer only
  matches alphabetic tokens, so "cap raised to 40 percent" and "cap raised to
  60 percent" hash to the same signature and merge — the stale number can win
  and gets replayed into future prompts with *higher* confidence than a fresh
  correction would have.
- **Memory pruning is dominated by content length, not by the value formula's
  intent**, and a documented overflow-counting bug can evict every non-preference
  memory in a scope on every run without ever bringing it under budget.
- Marketing: per-domain contact cap is checked before any sends in a batch
  happen (bypassable by batch composition); `SEND_OUTREACH` idempotency keys
  and campaign-approval subjects can still collide on count coincidence /
  truncation (same collision bug class fixed elsewhere this session, found
  unfixed here); `bounced_at` is written but never read anywhere, so there is
  no bounce-rate circuit breaker.
- Any Anthropic `400` (bad schema, unsupported param) permanently disables
  server-side model fallbacks for the rest of the process and doubles every
  subsequent request to that provider.

### MEDIUM / LOW — real bugs, smaller blast radius

- Finance: `currency` field exists end-to-end but the real webhook caller
  never populates it (hardcoded USD assumption breaks non-USD/zero-decimal
  currencies); `checkout.session.completed` is booked without checking
  `payment_status` (async bank-debit methods can book revenue that never
  actually arrives); reconciliation scheduling buckets on a ~100s window
  keyed to event time, occasionally delaying a card-limit update by up to an
  hour (bounded by an existing hourly sweep).
- Compliance: PII persists indefinitely in `interrupts.detail` /
  `campaign_approvals.brief` with no retention/purge; LIA escalation subject
  can still collide; idea-level confidence thresholding keeps uncertain-but-
  possibly-unlawful ideas alive longer than confidently-rejected ones; the
  generic task-exhaustion escalation's own code comment claims it says "retry"
  when the actual text doesn't.
- Development: a sandbox timeout kills the `docker` CLI process, not the
  container — a runaway loop keeps burning `sandbox_cpus` on the host
  indefinitely, un-trackable (no `--name`/`--label` anywhere); unbounded
  stdout/stderr buffering in the parent process can OOM-kill the factory
  itself while the sandbox stays under its own limit; `clear_workspace()`'s
  `ignore_errors=True` can silently leave stale files behind on a permission
  or symlink edge case — the exact bug class it exists to prevent; a
  non-DEBUGGER-locked tool path can write into a workspace mid-`clear`;
  `is_devops_error`'s keyword list can misroute a real application bug (an
  auth test asserting "permission denied," or an OOM-killed generated app
  exiting 137) as infrastructure and skip healing entirely; missing
  `STRIPE_API_KEY` is silently dropped from deploy env with no log — product
  ships LIVE with checkout that can never work.
- Core infra: a hung tool call is only checked against the agent-loop deadline
  once per step, not between calls, so a step issuing several hanging tool
  calls can run well past its budget; a build's entire multi-minute
  transaction wrapping every LLM call can be killed by a managed database's
  idle-in-transaction timeout, losing a build that actually succeeded; a
  latent (not yet triggered) crash exists for any future Gemini-routed
  contract using `Field(gt=...)`/`multiple_of`/a `set` field/discriminated
  unions, since Google's schema type forbids fields Claude's does not; a
  Postgres/usage-recording outage is caught and logged with the exception text
  discarded, so root cause is unrecoverable from logs alone.
- Learning Loop: `trial_until`/`TRIAL_DAYS` are written and never read
  anywhere — the real cutoff is the unrelated `MAX_TRIAL_DAYS`; maintenance
  cadence buckets have no catch-up, so a sustained capacity-starved period can
  cause a large backlog of maintenance tasks (each spawning multiple analyst
  agents) to fire all at once later.
- Marketing (smaller): outreach copy can reference the wrong product on a
  mismatch; the Hunter lead provider fails silently with no leads and no
  error; Apollo pagination has an unlogged cap; the LIA compliance-escalation
  flag carries no `task_id`/`product_id` for correlation; provider quota is
  counted by rows stored, not API calls actually consumed (can undercount
  against the real provider-side quota).

### What's next

This was a scan, not a fix pass — by design, matching the request. Given the
volume, the next step is picking priority and scope rather than fixing
everything at once. My recommendation, in order: (1) the three GDPR/unsubscribe
items (#3-5) — this is real legal exposure, not just a bug; (2) the ledger
race + double-counting + dead debit path (#1, #2, #6) — money integrity while
spend is still small is far cheaper to fix than after volume grows; (3) the
"already contacted" gap (#5) and the validation-gate trial-sample-size bug
(HIGH list) — both directly undermine the feature just shipped; (4) the
silent-4xx-failover-to-Opus-5 cost bug (#9) and the maintenance-tasks-marked-
done-without-running bug (#7) — both are silent and compounding the longer
they run. Everything else can wait for a dedicated pass.

## Agentic heal cycles — surgical repair instead of regeneration — 2026-08-11

A heal cycle used to repair a broken product by **regenerating its entire
codebase**: the whole product went into the prompt and came back out, at
code-generation prices, to fix what is often one line — and the model never saw
the real failure, only `graph.py`'s truncated summary of it. `_patch` now runs a
bounded tool-using agent against the real workspace: it runs the suite, reads the
real files, edits what is broken, re-runs. `_verify_and_ship` is **unchanged** —
Bug Council and deploy still rule on the result — and `MAX_HEAL_CYCLES` is
untouched.

**598 unit + 139 integration tests green on Linux, migration `0011` applied live.**

### The cost premise had to be fixed first, or this would have been worse

The original motivation assumed an agentic loop is cheaper. It was not:
`LLMClient.parse()` accepts `cache_prefix`, but `converse()` — the method the
agent loop uses — cached only its *system* block, while the message transcript
(the bulk of the tokens, and it grows every step) was re-sent at full price on
every step. At Opus-5 pricing that put a 14-step run near **$3.80 against ~$1.15
for the single full regeneration it replaces**. Three prerequisites, all shipped:

1. **Conversation-prefix caching.** A cache breakpoint on the final content block
   of each request. The loop only ever appends, so step N's marked prefix is by
   construction a prefix of step N+1's messages — each step now reads everything
   before it at 0.1x instead of re-paying in full.
2. **Head+tail tool-result truncation.** Results were head-truncated at 20k, and
   `run_in_sandbox` returns keys in the order `exit_code, stdout, stderr` — so a
   chatty pytest run filled the budget with collection noise and cut `stderr`,
   the actual traceback, off the end. Uncorrected the agent would have seen
   *strictly less* than today's pre-digested string, defeating the entire premise.
3. **Container leak on cancellation (pre-existing bug, independent of this).**
   `DockerSandbox.execute` cleaned up in `except TimeoutError` only, but an outer
   cancellation raises `CancelledError` — so `process.kill()` and
   `_kill_container()` never ran. The daemon-side container kept running with
   `{workspace}:/workspace:rw` still mounted (`--rm` never fires for a container
   that never exits, and the name lived only in a local), free to keep writing
   into a workspace the next step was about to read and test.

### Routing: what is repaired surgically, and what still regenerates

`_heal()` already tagged every failure with a precise `category`; it is now
threaded into the patch payload and routed:

- **`test_failure`** → surgical. A real traceback to read and a suite to re-run.
- **`guided`** → surgical, and **exempt from the cycle-3 fallback**. Found while
  wiring this: `enqueue_guided_patch` (a human replying "the bug is X, change Y")
  built its payload with no category, so the best-informed repair the factory ever
  performs was falling through to the *most expensive* path — regenerating an
  entire live product from scratch over one instruction, discarding everything
  that already worked. The cycle ceiling is about autonomous retries running out
  of ideas; a human-directed fix is not one.
- **`dependency_*`** → never. The agent's sandbox has no network and cannot run pip.
- **`council_rejection:*` / `deploy_failure`** → unchanged. The agent cannot re-run
  the Council, so it would edit blind to the only opinion that decides the outcome.
- **Heal cycle 3 always regenerates.** A clean rebuild is the only operation that
  can recover an arbitrarily damaged workspace.

### Containment — an agent editing a live workspace cannot be undone by regenerating

- **Byte-exact on-disk snapshot**, restored on any bad outcome, and restored *on
  entry* if a previous attempt died mid-edit. Without that last part one crash
  compounds across the task's three retries into three destructive passes over a
  progressively more broken workspace, each driven by healing text that no longer
  describes reality. Deliberately a real directory copy, not `read_workspace()` —
  that helper drops non-UTF-8 files, so restoring from it would destroy a
  product's images on the first rollback.
- **Evidence, not self-report.** `stopped_because == "completed"` means only that
  the model emitted a turn with no tool calls: a confident prose diagnosis that
  touched nothing reports success just as loudly as a real fix. A repair that
  changed no files never reaches verification — the expensive half.
- **Deterministic guards** reject a repair that deleted a test file, dropped an
  assertion, added `skip`/`xfail`, or deleted `requirements.txt` — the ways to make
  a suite green without fixing anything. Prompts do not bind; this mirrors
  `bug_council`'s existing `scan_secrets`/`scan_privilege` idiom.
- **Blocked ≠ failed.** An outage, deadline, step limit or cost cap defers the task
  (`defer_task`, never touching `attempt_count`) rather than consuming a heal
  cycle — otherwise three consecutive Anthropic outages would permanently escalate
  a product whose repair was never attempted.
- **A per-run cost cap** (`repair_agent_cost_cap_usd`, default $0.75) plus a
  pre-flight budget-headroom check, since the global breaker is only consulted
  between ticks and step count is a poor proxy for spend.
- **The tools do not take `product_lock`, and this is load-bearing.** `_patch`
  already holds it, and its row is *uncommitted* inside `_run_engine`'s session —
  so a second acquisition from another connection does not fail fast, it **blocks**
  on the first transaction. Reusing `run_in_sandbox` would have hung every tool
  call until cancellation: ~28 minutes of zero progress per repair, stalling the
  whole (sequential) tick loop. Regression-tested against real Postgres under a
  hard timeout, so a reintroduction fails loudly instead of hanging.
- The workspace is **closed over, never a model argument** — `resolve_workspace`
  *creates* what it resolves, so a hallucinated name would silently produce an
  empty directory where every command "succeeds" against nothing.
- A separate `workspace_surgeon` role, **filtered out of `role_catalogue()`** so
  the Orchestrator cannot spawn it outside the one caller that pins it to a
  product, holds the lock, and can roll its edits back.

### Durable per-product knowledge

Products now carry `.factory/ARCHITECTURE.md` — seeded **free** from the
`ScaffoldPlan` already in memory (product name, summary, file list; no extra LLM
call), read at the top of every repair, and appended to **only after** a repair is
verified. Writing it when the agent merely *claimed* success would persist an
unverified belief that the next cycle reads back as prior advice, and
near-duplicate reinforcement would make a repeated wrong claim look progressively
more confident.

This is the persistence that makes repair #40 on a months-old product no more
expensive than repair #1: not a long-lived process holding context, but durable
written artifacts plus on-demand reading. It survives reboots and deploys, a human
can read and correct it, and it is excluded from both Bug Council review and
deploy (`WORKSPACE_SKIP_DIRS`) since it is notes *about* the product, not part of
it. A rollback deliberately preserves it — deleting it would be a one-way loss
dressed up as a restore.

### Two test bugs found during verification (not production bugs)

The snapshot helper cleared its destination before copying, which silently
defeated the selective cleanup in `restore_snapshot` and would have destroyed the
knowledge file on every rollback — **caught by the test written to prevent exactly
that**. And the outage test queued one provider failure where `converse` retries
three times against the same provider (`CODE_GENERATION` is `NO_FAILOVER`), so the
retry hit an empty queue.

**Deployed live** (2026-08-11): committed, transferred via `git archive HEAD`,
migration `0011` applied to the live database *before* the restart, image rebuilt,
service restarted. `/health` reports `ok`, all six engines enabled, `alembic
current` is `0011_product_test_command (head)`, boot log clean.

**Still deliberately out of scope**: `council_rejection` routing (revisit once
`test_failure` has real data); agentic `_build` (first generation has no existing
code to be surgical about); git-backed workspaces instead of copytree snapshots;
mid-run task-lease renewal (hence `deadline_seconds` sits well under
`task_lease_seconds`); and a feature-request pathway, which unlike a heal cycle
has no failing test to anchor on.

## Pre-launch validation plan, Phase 1 — staging harness — 2026-08-11

First phase of a 7-phase pre-launch plan (rigorous end-to-end validation before
going live, plus the growth capabilities — SEO, live AB testing, analytics-driven
autonomy — that research turned up as missing entirely). Full plan and remaining
phases: `C:\Users\Jesse\.claude\plans\master-system-prompt-synchronous-blum.md`.

**Finding that reshaped the whole plan**: `FACTORY_DRY_RUN` does not align with cost
or safety at all. It stubs exactly three things (Vercel deploy, outbound email send,
Stripe card-limit sync) and never stubs an LLM call, in either mode — while
`CODE_GENERATION`/`CODE_REVIEW` (MVP scaffolds, landing pages, Bug Council) are 100%
Anthropic-served with no failover and the large majority of real spend. "Test the
cheap calls, mock the expensive ones" needed a new mechanism.

**`settings.staging_mock_expensive_calls`** (default `False`, zero behavior change
unless set): swaps only the Anthropic slot in `build_registry`
(`llm/client.py`) for a new `StagingCodeProvider` (`llm/providers/staging.py`).
Gemini is untouched — Discovery, Compliance, lead planning, and outreach copy still
run for real. Orthogonal to `factory_dry_run`, not a variant of it.

The fixture content is genuinely functional, not a placeholder: a landing-page
fixture (interest-capture form posting to the real per-run interest URL, extracted
deterministically from the prompt the same way `generate_landing_page` composed it)
and an MVP fixture (real password hashing, real token auth, real Stripe Checkout
wiring reading `PRODUCT_ID`/setting `client_reference_id`, exactly as
`SYSTEM_PROMPT` requires of a real generation). Both pass the real, deterministic
Bug Council scanners (`scan_secrets`/`scan_privilege`) and the MVP fixture's own
pytest suite genuinely passes — so the *rest* of the pipeline (real sandbox
install/test, real deploy, real lead sourcing, real outreach composition) can be
exercised repeatedly against a real working product without paying Opus prices on
every run.

**A real regression caught and fixed during this pass, not shipped**: an earlier
version of the fixture-verification test ran `pip install -r <fixture's
requirements.txt>` with `sys.executable` — `cwd=` only changes the working
directory, not the target environment, so this silently installed into the
*shared local dev venv* regardless of which directory it ran from. It downgraded
`stripe` from 15.x (what every Docker build this session has resolved, matching
the project's real unbounded `stripe>=11.0` constraint) to 11.6.0 to satisfy the
fixture's own upper-bound pin, which broke an unrelated test
(`test_finance_webhooks.py`) via a stripe SDK deprecation warning. Caught by the
full local suite before commit, root-caused, stripe restored to latest, and the
test rewritten to run pytest directly against the fixture — relying on
fastapi/httpx/stripe already being real project dependencies — so verifying the
fixture can never again have a side effect on the environment running it.

**Single-idea concurrency and bounded lead sourcing for the actual staging run**
(Phase 7) need no new code — both already exist as settings:
`max_concurrent_builds=1` + `max_concurrent_validations=1` constrain how many
products can be active at once, and `validation_outreach_batch_size` already caps
the lead-harvest limit specifically for a `VALIDATING` product (the exact path a
staging run's single idea takes).

**Verified**: 610 unit + 139 integration tests green on Linux (scratch Postgres).
No schema change this phase. `ruff check`, `ruff format --check`, `mypy src/` clean.

**Deployed live** (2026-08-11): committed (`04ff038`), transferred via
`git archive HEAD`, image rebuilt, service restarted. `/health` reports `ok`, all
six engines enabled, boot log clean. The flag defaults off, so this deploy changes
no live behavior until a staging run explicitly sets it.

## Pre-launch validation plan, Phase 2 — self-hosted analytics beacon — 2026-08-11

Second phase of the 7-phase pre-launch plan. Nothing collected traffic data for a
live or validating product before this — `ValidationSignal` records interest (an
email), not visits — and a growth decision (is this landing page converting, does
this product's traffic justify an SEO or AB-testing pass) needs a volume
denominator that didn't exist.

**Reused the existing `/interest/{product_id}` pattern rather than a third-party
analytics API.** A third-party provider's current query-API contract can't be
verified from inside this codebase and is an unknown integration risk; a new
unauthenticated `POST /analytics/{product_id}` endpoint, mirroring `/interest`
exactly, lands in a new `page_views` table (migration `0012`) this factory fully
controls the schema and the read side for. Anonymous by design, unlike
`/interest` — no email, no dedup, no identity; every real visit is a real signal.
`variant` is nullable and unused until Phase 4 (live AB testing), added now so
that phase needs no second migration on this table.

**Both codegen prompts (`SYSTEM_PROMPT`, `LANDING_PAGE_SYSTEM`) now require a
fire-and-forget beacon script on page load.** `generate_scaffold` gained a
required `product_id` parameter to compose the exact per-product analytics URL
into its prompt — the same reason `generate_landing_page` already does this for
its interest-capture URL: a static frontend has no server-side env var to read
`PRODUCT_ID` from the way a real backend does. Both real call sites in
`graph.py` already had `product_id` in scope.

**The Phase 1 staging fixtures were updated to match** — both now embed a real,
working beacon, extracting the per-run analytics URL from the prompt the same
deterministic way they already extract the interest URL. Caught and fixed a real
bug in that extraction while doing it: the original regex anchored on the first
`:` after a label, which broke the moment a prompt could contain *two* labels
each with their own worked JSON example (each with colons of its own) — a
non-greedy match up to the literal `"verbatim):"` marker fixes it for both.

**Verified**: 610 unit + 148 integration tests green on Linux (scratch
Postgres), migration `0012` applies cleanly.

**Deployed live** (2026-08-11): committed (`ae39f57`), transferred via
`git archive HEAD`, migration `0012` applied to the live database *before* the
restart, image rebuilt, service restarted. `/health` reports `ok`, all six
engines enabled, `alembic current` is `0012_page_views (head)`, boot log clean.

## Pre-launch validation plan, Phase 3 — SEO baseline hygiene — 2026-08-11

Third phase of the 7-phase pre-launch plan. Zero SEO instruction existed anywhere
in the codebase before this — no mention in either codegen prompt, no check in
Bug Council's review criteria. Every generated landing page and MVP frontend
shipped with no enforced `<title>`, no meta description, no
`sitemap.xml`/`robots.txt`: a page nobody can find gathers no interest signal at
all, which matters directly for a factory whose whole validation gate depends on
real people finding and responding to a landing page.

**Both prompts now ask for it, and a deterministic check enforces the checkable
subset.** `SYSTEM_PROMPT`/`LANDING_PAGE_SYSTEM` ask for a non-empty `<title>`, a
meta description, Open Graph tags, a canonical URL, semantic landmarks, and two
standalone files (`sitemap.xml`, `robots.txt`). New
`development/seo.py::missing_seo_requirements()` — same reasoning as Bug
Council's `scan_secrets`/`scan_privilege`, a prompt asking for something is not
the same as it being there — deterministically checks the four unambiguous items
(title, meta description, sitemap, robots); OG tags/canonical/semantic landmarks
stayed prompt-only, real but harder to verify by pattern match without false
positives.

**Runs in `_verify_and_ship` right after the test suite passes, *before* Bug
Council.** Catching a missing `<title>` costs nothing (no LLM call); finding out
only after paying for three Opus reviewers would not. A new `"seo_incomplete"`
heal category was added to `SURGICAL_CATEGORIES` — deliberately *not* folded
into Bug Council's own gate, since an SEO gap is exactly the narrow, targeted
fix surgical repair exists for, and unlike a Council rejection this check is
free to re-run after an edit.

**The Phase 1 staging fixtures needed updating to match** — without it, every
staging build would have immediately heal-cycled on its own fixture's missing
SEO markers, defeating the "always passes cleanly" point of a staging mode.

**Verified**: 626 unit + 148 integration tests green on Linux (scratch
Postgres). No schema change this phase.

**Deployed live** (2026-08-11): committed (`d7d6e63`), transferred via
`git archive HEAD`, image rebuilt, service restarted. `/health` reports `ok`,
all six engines enabled, boot log clean.
