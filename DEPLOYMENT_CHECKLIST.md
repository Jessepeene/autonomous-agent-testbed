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

## Pre-launch validation plan, Phase 4 — live AB testing for landing pages — 2026-08-11

Fourth phase of the 7-phase pre-launch plan. Before this, the only version
comparison mechanism was the Learning Loop's sequential prompt trials
(`learning/trials.py`) — one landing-page copy fully live for up to 30 days,
then compared to *historical* episodes. That answers "did this get better over
time," not "which of these two pitches converts better right now" — a
genuinely different, concurrent-arm question your direction asked for
explicitly, real and not just a verification of the existing loop.

**A running `ab_tests` row (migration `0013`) switches `generate_landing_page`
into a dual-variant prompt.** Both variant briefs render into the same static
page as two content blocks (`id="variant-a"`/`id="variant-b"`), split by a
small inline script that assigns and persists the arm via `localStorage` (a
returning visitor always sees the same one) and tags both the interest-form
POST and the Phase 2 analytics beacon with the assigned `variant`. Deliberately
client-side, not a second deployment behind an edge router — fits the existing
"single static deploy, no build step" constraint exactly, and reuses the
interest/analytics endpoints as-is (`variant` is the only new field on either).
`_build_landing_page` checks `get_running_ab_test` fresh on every
(re)generation rather than threading an `ab_test_id` through the task payload,
so a repair pass mid-test still regenerates both variants correctly with no
prior knowledge a test was running.

**The decision reuses `learning/stats.py::fisher_exact_less`** — the same
hypergeometric test the sequential Learning Loop already uses, applied
bidirectionally to two live arms instead of trial-vs-history: the underlying
statistic doesn't care whether its two groups are temporally sequential or
concurrent, only about their (successes, failures) counts. New
`development/ab_testing.py::evaluate_ab_test()` wraps it with the actual
decision logic (minimum sample per arm, which direction is significant). A new
`EVALUATE_AB_TEST` task, swept hourly by the scheduler for every row from
`running_ab_tests()`, tallies `page_views`/`validation_signals` since the test
started, concludes on significance or once `ab_test_max_days` elapses
(inconclusive either way past the deadline), and writes the outcome to memory
as a `LESSON` so a future growth decision can recall it.

**`start_ab_test()` in `graph.py` is the one mutation entry point** — creates
the `ABTest` row and enqueues the regeneration that renders it. Deliberately
built as the public seam Phase 5's `GROWTH_ANALYST` role will call on its own
judgment (a product needs a real reason — underperforming interest rate — to
justify a test), not something this phase wires up a trigger for itself.

**A real bug, caught by this phase's own integration tests and fixed, not
shipped**: `create_ab_test`'s `ON CONFLICT ... WHERE status = 'running' DO
NOTHING` raised `InvalidColumnReferenceError` on every call, conflict or not.
Two independent causes, both fixed: (1) `ABTest`'s partial unique index was
declared only in migration `0013`'s raw SQL, never in the ORM
`__table_args__` — `Base.metadata.create_all()` (what every test's `session`
fixture actually builds its schema from, not Alembic) silently never created
it at all; (2) `index_where` was passed as an ORM comparison
(`ABTest.status == "running"`), which compiles to a bound parameter that
Postgres's `ON CONFLICT` inference can never structurally match against the
index's own literal predicate. The identical pattern already existed in
`create_marketing_freeze` (the deliverability circuit breaker's "at most one
active freeze" guarantee) with the same parameter-vs-literal bug — unexercised
by any test until a regression test was added here alongside the fix. Its ORM
index declaration was already correct, so only the `index_where` half applied
there.

**Verified**: 638 unit + 161 integration tests green on Linux (scratch
Postgres), migration `0013` applies cleanly.

**Deployed live** (2026-08-11): committed (`ef695a6`), transferred via
`git archive HEAD`, migration `0013` applied to the live database *before* the
restart, image rebuilt, service restarted. `/health` reports `ok`, all six
engines enabled, `alembic current` is `0013_ab_tests (head)`, boot log clean.

## Pre-launch validation plan, Phase 5 — growth roles + channel discovery — 2026-08-11

Fifth phase of the 7-phase pre-launch plan, expanded mid-plan by explicit
direction: "the thing about this project/system was that it should act like
a dev/growth team... even if there's other growth opportunities, think
reddit/forums anything of the like the orchestrator should be able to find
them, use them, test them and learn from them." Two new Orchestrator-
catalogued roles, both reusing the existing `AgentRole`/`SPAWN_AGENT`
mechanism rather than a new scheduling system — the direct answer to
"the orchestrator should have freedom to call this."

**`GROWTH_ANALYST`** reads one product's traffic/conversion/AB-test state
(new `growth_read` tool category) and decides whether to start a live AB
test or request a growth-motivated SEO pass (new `growth_actions` category)
— or take no action, a legitimate outcome most weeks for most products.
Self-sufficient when spawned without a named product (`list_products_
needing_growth_review`), so the Orchestrator's own ad hoc SPAWN_AGENT choice
works exactly like the periodic backstop.

**`SEO_OPTIMIZER`** is workspace-scoped (excluded from `role_catalogue()`,
same precedent as `WORKSPACE_SURGEON`) and carries out the requested edit
through the *existing* surgical-repair machinery — `development/graph.py`'s
`_patch` pipeline gained a `growth_seo` category rather than new agent-
runtime plumbing. Two correctness properties a naive reuse would have
gotten wrong, both handled explicitly: a discretionary pass that is not
safe to attempt right now (no Docker, heal budget exhausted) is skipped
outright and must never fall through to a full regeneration the way every
other category correctly does; and a declined edit must never touch
`heal_attempts` or enter the `_heal()` escalation ladder — both are correct
for a real build failure and wrong for an optional optimisation that simply
did not pan out (would silently shrink the budget a future real bug fix
gets, and could eventually page a human about what was only ever a
declined SEO experiment).

**`CHANNEL_SCOUT`** researches real, specific communities (the existing
`http_get` tool against public search endpoints — no new tool needed for
Reddit's own public search JSON) and drafts genuine, non-promotional
content for the strongest candidate. Proposing one raises a mandatory
human-approval interrupt (new `InterruptReason.CHANNEL_APPROVAL`, the same
poll-based pattern as `CampaignApproval`, deliberately a *separate* pattern
from platform-posting risk — bans, reputational damage — which is a
different kind of judgement than GDPR-gated cold email). Real posting stays
a human action; the factory never posts to an external platform
automatically. New `channel_experiments` table tracks the lifecycle
(proposed → approved/rejected → concluded); an approved proposal is
measured later, best-effort, via `page_views.referrer`, and the outcome is
written to memory so the next proposal for that product reads it back.

**A real design bug caught by this phase's own tests, not shipped**: the
periodic backstop sweeps initially treated a never-reviewed/never-scouted
product as infinitely overdue, so a product that had just gone live would
immediately compete with `HARVEST_LEADS` for the same tick's `MARKETING`
capacity — three pre-existing scheduler tests caught this immediately.
Fixed by measuring a never-reviewed product's staleness from its own
`created_at` (`COALESCE(last_growth_review_at, created_at)`) instead of
treating `NULL` as maximally stale — reviewing a day-old product with no
traffic data yet is also pure waste on its own terms, not just a test-
compatibility fix.

**Verified**: 652 unit + 174 integration tests green on Linux (scratch
Postgres), migration `0014` applies cleanly.

**Deployed live** (2026-08-11): committed (`4f368ab`), transferred via
`git archive HEAD`, migration `0014` applied to the live database *before*
the restart, image rebuilt, service restarted. `/health` reports `ok`, all
six engines enabled, `alembic current` is `0014_growth_roles (head)`, boot
log clean.

## Pre-launch validation plan, Phase 6 — custom domain request + Vercel attachment — 2026-08-11

Sixth phase of the 7-phase pre-launch plan: "ask me for a domain," the one
remaining item from the original funnel list not yet real. Follows
`CampaignApproval`'s poll pattern in shape only — a product is already
reachable on its Vercel subdomain the instant it goes LIVE, so this is
deliberately **not** a blocking gate the way campaign approval is.
`development/domain.py::ensure_requested` fires a one-time, non-blocking
interrupt the first time a product goes LIVE, guarded by a new
`Product.domain_status` so a later redeploy (a repair pass, a `growth_seo`
edit) never asks twice.

**A human's reply is captured by a new branch in `collect_replies`, checked
deliberately ahead of the existing generic one.** `DOMAIN_REQUIRED`'s
interrupt shape (`product_id` set, `task_id` unset) is identical to a heal-
exhaustion escalation's — and that one's REJECT retires the product outright.
`Interrupt.reason` is what disambiguates them, checked before the generic
branch fires. A valid hostname enqueues `ATTACH_DOMAIN`; SKIP or an empty
reply marks the product `'skipped'` for good; anything unparseable is logged
and left alone — the same convention this module already used for every
other malformed-reply case, not a new gap.

**`ATTACH_DOMAIN` targets the exact Vercel project slug the deploy used**,
now persisted on `Product.vercel_project` at deploy time (`deploy.py`'s
`DeployResult` gained a `project` field) rather than re-derived from
`product_name`, which can drift across builds (a repair pass can rename a
product). The required DNS records are relayed back over Telegram
(`domain.format_dns_instructions`). **`VERIFY_DOMAIN`** is a periodic,
LLM-free sweep (hourly, same cadence as the AB-test sweep) that polls until
Vercel confirms, then flips `domain_status` to `'verified'` and
`deploy_url` to the custom domain.

**Explicitly unverified against a live Vercel project**, matching
`deploy.py`'s own existing disclaimer for the deployment endpoint itself —
the attach/verify endpoint shapes follow Vercel's documented contract, not
a confirmed live call. Confirming the real contract is a Phase 7 manual
runbook item.

**A real bug caught while writing this phase's own integration tests, not
shipped**: the first version of the "domain requested on first LIVE deploy"
tests called `_verify_and_ship` with the context's default `DisabledSandbox`,
which fails before ever reaching the deploy step it meant to exercise —
fixed by giving those tests a passing sandbox fake, the same pattern
`test_seo_gate.py` already established for exercising `_verify_and_ship`'s
success path without a real Docker daemon.

**Verified**: 670 unit + 188 integration tests green on Linux (scratch
Postgres), migration `0015` applies cleanly.

**Deployed live** (2026-08-11): committed (`4cbfd06`), transferred via
`git archive HEAD`, migration `0015` applied to the live database *before*
the restart, image rebuilt, service restarted. `/health` reports `ok`, all
six engines enabled, `alembic current` is `0015_custom_domains (head)`, boot
log clean.

## Pre-launch validation plan, Phase 7 — automated e2e test + manual runbook — 2026-08-11

Seventh and final phase of the pre-launch plan. Two parts, because not
everything can or should be automated.

### 7a — automated e2e test (`tests/e2e/test_full_funnel_staging.py`)

One idea, driven through the real funnel logic end to end: Compliance
approval → a real staging-fixture landing page (real SEO markers, real
analytics beacon, real Bug Council scanners) → real interest/response-rate
math → `EVALUATE_VALIDATION` → a real staging-fixture MVP (real auth, real
Stripe Checkout wiring, real Bug Council) → LIVE → a real `DOMAIN_REQUIRED`
interrupt → a scripted human reply → `ATTACH_DOMAIN` queued → a live AB test
seeded with a clear winner → `EVALUATE_AB_TEST` concludes it correctly →
`GROWTH_REVIEW` still marks the product reviewed even though `GROWTH_ANALYST`
itself is correctly refused under `FACTORY_DRY_RUN` → no interrupt is left
open, nothing half-finished.

**"Under the staging harness" means the Anthropic slot is the real
`StagingCodeProvider`** (Phase 1), not a placeholder — this test exercises
the actual generated-content shape and the real deterministic checks against
it. The Gemini slot is `FakeProvider`, scripted per call: Discovery/
Compliance decisions are cheap structured-output calls, and a real network
call here would make an automated regression test flaky and non-free to run
on every change. `factory_dry_run=True` stubs the Vercel deploy call
(matching existing `FACTORY_DRY_RUN` behaviour exactly); real Apollo/Hunter
sourcing and a real outreach send are seeded directly rather than exercised,
per your own explicit decision to keep outreach stubbed even in a live
staging run. None of these three are testable offline and automatically —
they are 7b's job below.

**A real bug caught while writing this test, not shipped**: the first
version called `_verify_and_ship` with the default `DisabledSandbox`, which
fails before ever reaching deploy — fixed with the same passing-sandbox-fake
pattern already established in `test_seo_gate.py` and Phase 6's own tests.

**Verified**: passes cleanly, twice in a row against a freshly-migrated
scratch Postgres, alongside the full 670 unit + 188 integration suite.

### 7b — manual runbook: the pieces that need real external state

Not automatable: a real domain you own, real (bounded) Apollo/Hunter calls,
and a real Vercel project. Run these against the live deployment, not a
separate staging copy — the live factory already runs under
`FACTORY_DRY_RUN=false` with real credentials, so these checkpoints are
about *watching* real, already-scheduled work rather than standing up a
parallel environment. Work through them in order; each depends on the
previous one having actually happened.

- [ ] **Discovery → one idea.** Watch `docker compose logs factory -f` (or
  Telegram) until a `compliance.idea_approved`-shaped event appears. Confirm
  in the DB that exactly one `products` row exists in `validating` for that
  idea, and that a `build_landing_page` task completed.
- [ ] **The landing page gathers real interest.** Open the deployed
  validation URL (`products.deploy_url`) in a real browser. Confirm the page
  looks credible (not a generic template), submit the interest form
  yourself, and confirm a row lands in `validation_signals`. Confirm the
  analytics beacon fires too (`page_views` gets a row on page load, before
  any form submission).
- [ ] **SEO markers are present in the shipped HTML**, not just asserted by
  the deterministic gate that already ran server-side: `curl` the deployed
  URL and confirm a real `<title>`, `<meta name="description">`, and that
  `/sitemap.xml` and `/robots.txt` both return real content.
- [ ] **Outreach composes correctly.** Once real leads exist
  (`harvest_leads` ran — see below), inspect the composed email body a
  `SEND_OUTREACH` task would have sent (it stays stubbed per your original
  decision — check the task's own record of what it *would* have sent, or
  temporarily point `outbound_from_address` at an inbox you control for one
  cohort if you want to see a real send). Confirm the copy is specific to
  the product and niche, not generic.
- [ ] **Real, bounded lead sourcing.** Confirm `HUNTER_API_KEY`/
  `APOLLO_API_KEY` are configured, and that a `harvest_leads` task actually
  called out (check `llm_calls`/provider logs for the request, not just that
  leads appeared) and respected the configured `limit`. Spot-check 2-3
  returned leads for a real, plausible job title/company — not obviously
  wrong data.
- [ ] **Leads stay current.** Confirm the periodic `HARVEST_LEADS` sweep
  (every 12h, `scheduler.py`) fires again for the same LIVE product without
  you doing anything — a product that has been live for a while should
  accumulate more than one harvest's worth of leads over a few days.
- [ ] **The real build ships with working Stripe Checkout.** Once the
  product goes `live`, open its real MVP URL, attempt a Checkout flow with a
  Stripe test-mode card, and confirm the Checkout Session's
  `client_reference_id` is set to the exact `PRODUCT_ID` (check the Stripe
  dashboard for the session, not just that checkout redirected
  successfully) — that is the only mechanism that lets a webhook later
  attribute revenue back to this product.
- [ ] **Domain: requested, attached, and verified for real.** Reply to the
  Telegram `DOMAIN_REQUIRED` message with a real hostname you control DNS
  for. Confirm the `ATTACH_DOMAIN` task's real Vercel API call succeeds
  (check `domain_status` flips to `'attached'` and you receive the DNS
  records over Telegram) — this is the actual, first-ever live exercise of
  `development/domain.py`'s Vercel contract; if the endpoint shape is wrong,
  this is where you find out. Add the DNS records at your registrar, then
  confirm the periodic `VERIFY_DOMAIN` sweep flips `domain_status` to
  `'verified'` within a few hours (DNS propagation varies) and that
  `deploy_url` switches to the custom domain.
- [ ] **A live AB test runs and concludes with a real winner.** Either wait
  for `GROWTH_ANALYST` to start one on its own judgement (check Telegram/
  memory for its reasoning), or start one yourself via the same
  `start_ab_test_action` path. Visit the product from two different
  browsers/incognito sessions, confirm each is pinned to one variant
  (`localStorage`), and confirm real visits accumulate in `page_views` split
  by `variant`. Let it run until `EVALUATE_AB_TEST` concludes it — confirm
  the winner and the memory `LESSON` it wrote.
- [ ] **The Learning Loop and the growth roles both fire without manual
  intervention.** Over a few days of normal operation, confirm in logs/
  memory that: a landing-page prompt trial advances on its own cadence
  (`learning/trials.py`); `GROWTH_REVIEW` fires for each LIVE product on its
  configured interval (`growth_review_interval_days`, default 7) without you
  enqueuing it; `CHANNEL_SCOUT_REVIEW` fires on its own longer interval
  (`channel_scout_interval_days`, default 14) and, if it finds a genuine
  candidate, raises a real `CHANNEL_APPROVAL` interrupt with a real drafted
  post you can read and judge.

This closes the 7-phase pre-launch plan. Everything above it in this
document is the record of how each phase was built, verified, and deployed;
this runbook is what turns "the code is correct" into "the system actually
works," which only real external state can prove.

## Live operation, 2026-08-12 — staging harness enabled, factory reset, orchestrator bug fix

With all 7 phases live, focus shifted to actually watching the factory run
without spending on Anthropic's expensive coding calls. What happened, in
order:

- **`STAGING_MOCK_EXPENSIVE_CALLS=1`** added to the live `.env` (Phase 1's
  provider-level split, built for the e2e test, turned on for real for the
  first time). Anthropic's slot now serves the scripted `StagingCodeProvider`;
  Gemini stays real for Discovery/Compliance/Orchestrator. `FACTORY_DRY_RUN`
  was already `1`.
- **Two heal-exhausted products retired.** Both were infra-broken, not
  application bugs (one DNS resolution failure that later tested as
  transient, one missing `pydantic-core` wheel) — confirmed live rather than
  assumed, then retired per your explicit call, overriding a more
  conservative RETRY/GUIDED-PATCH suggestion. Found a real gap doing this:
  `DevOpsError`-shaped escalations carry `task_id`, not `product_id`, so
  `collect_replies()` has no REJECT-retires-the-product path for them — worked
  around with a direct `repository.retire_product()` call for these two, the
  gap itself not yet fixed as its own change.
- **Full reset ("start from 0"), your explicit instruction.** Took a real
  `pg_dump -F c` backup (`/opt/backups/factory_backup_20260812_091803.dump`),
  then `TRUNCATE ... RESTART IDENTITY CASCADE` across every data table
  (schema/`alembic_version` untouched). Blocked once on the restart step by
  the auto-mode permission classifier; stopped and asked rather than working
  around it, you confirmed, restart succeeded.
- **Bug found while watching the reset run: `approved_ideas_waiting` was a
  lifetime tally, not a live queue depth.** `build_snapshot()` counted every
  `Idea.status=='approved'` row ever, with no join to the product it produced
  — since Compliance always creates a product in the same pass an idea is
  approved, this count never dropped even once a product shipped or was
  retired, and it was one of the signals the Orchestrator's own LLM
  consultation read before deciding to route to Development. Your own
  question ("doesn't this mean the orchestrator doesn't have a clear
  overview?") pointed straight at it. Fixed with a join to `Product` excluding
  `RETIRED`/`LIVE` statuses; added `tests/integration/
  test_orchestrator_snapshot.py` (3 cases) since `build_snapshot()` had never
  actually been exercised for its real query logic before, only mocked.
  **Deployed live** (`663d6d5`): committed, transferred via `git archive
  HEAD`, image rebuilt, service restarted, `/health` clean.
- **Discovery cadence sped up temporarily**: `DISCOVERY_INTERVAL_SECONDS`
  600 (10 min) instead of the usual 10800 (3h), specifically so a idea would
  surface inside the observation window instead of the usual multi-hour wait.
  A background watcher (`watch_discovery.sh`, polls the live `ideas` count
  every 60s) auto-reverts this to 10800 the moment a real idea is stored, or
  after a 4-hour bound, whichever comes first — left running unattended
  rather than needing a manual check-in to undo the change.
- **Observed while watching it run: the faster cadence didn't actually
  increase variety.** Every cycle inside the fast window returned the
  identical `signals=74, clusters=30` — traced to the fixed, hardcoded
  `COMPLAINT_QUERIES`/`_QUERIES` phrase lists HN/Reddit/GitHub all search off
  every cycle (GitHub additionally sorts by `comments`, i.e. engagement, not
  recency). Reported this proactively, which led directly to the next
  section.

## DISCOVERY_SCOUT — an agentic complement to Discovery's fixed sources — 2026-08-12

You asked for the fixed HN/GitHub/Reddit/Product Hunt sources to get the same
"genuine agentic freedom" already proven by `CHANNEL_SCOUT` — but first, a
quick survey of the rest of the codebase for other candidates matching that
same shape, before building anything.

**Survey result: Discovery's sources were the only real match.**
`LEAD_SCOUT` (Hunter/Apollo) already refines its own queries autonomously via
real tool calls — the provider pair itself is deliberately fixed, tied
directly to the GDPR/LIA guarantee documented in `engines/marketing/
leads.py` ("the planner never sees personal data"); opening it to arbitrary
web scraping for contact data would break that guarantee, not improve it.
`GROWTH_ANALYST`/`SEO_OPTIMIZER` already exercise real judgement over real
data rather than a fixed list. Scheduler cadences (12h lead harvest, hourly
reconciliation) are timing knobs, not "what to look for" decisions — a
different category. The PLG gate, Bug Council, and compliance rules are
deliberately deterministic for legal/safety correctness. Confirmed via grep
that `COMPLAINT_QUERIES` (`sources/base.py`, shared by HN and Reddit) and
`_QUERIES` (`sources/github.py`, `sort="comments"`) are indeed fixed tuples
with no rotation and no model input — the concrete evidence behind both the
"variety didn't increase" observation above and this build.

**Built `RoleName.DISCOVERY_SCOUT`**, following the `CHANNEL_SCOUT` template
exactly: an ephemeral `AgentRole`, `http` tool category (free web search, no
new plumbing), plus a new `discovery_scout` category holding one tool,
`submit_signal` (`tools/discovery.py`) — quote the real text found and its
real URL, never a paraphrase or an invented URL, matching the same standard
already held for `RESEARCHER`/`CHANNEL_SCOUT`. Its system prompt tells it
explicitly what the fixed sources already cover so it looks elsewhere (Indie
Hackers, dev.to, G2/Capterra reviews, Stack Overflow, app-store reviews,
niche subreddits/forums the fixed queries don't reach) and to vary its own
phrasing between runs rather than repeat a query.

**The hand-off problem**: a tool call has no return channel back to its
caller except the database in this codebase — `CHANNEL_SCOUT`'s
`propose_channel` writes a durable, human-facing `ChannelExperiment` row, but
a raw market signal has never been persisted anywhere (the fixed sources
return `Signal` objects in-process, straight into the clusterer, within one
call). Rather than durably storing something this codebase has always kept
ephemeral, added `scout_signals` (`ScoutSignal`, migration `0016`) as a
strictly one-shot hand-off buffer: `submit_signal` writes a row scoped to the
Discovery cycle's own `task_id`, and `engines/discovery/scout.py::
scout_signals()` reads every row back and deletes them the moment the agent
finishes — empty between cycles by design, same as before this existed.
`engines/discovery/graph.py::run()` now merges `fixed_signals + scouted`
before clustering; a fixed-sources-unconfigured or a scout-finds-nothing
cycle both degrade gracefully (mirrors the existing "one source fails, others
continue" behaviour). `DISCOVERY_SCOUT` is excluded from `role_catalogue()`
via a new `PIPELINE_SCOPED_ROLES` set (same reasoning as
`WORKSPACE_SCOPED_ROLES`, different owner): only ever spawned from
`scout.py` itself, once per cycle, with a fixed objective — not a capability
the Orchestrator should hand an arbitrary one. Gated by a new
`discovery_scout_enabled` settings flag (default on) for an operational off
switch with no code change. Kept `mutating=False` — same reasoning
`WORKSPACE_SURGEON`/`SEO_OPTIMIZER` already document: its only effect is an
internal buffer this same cycle drains, no outward effect, so flagging it
would make dry run strictly worse than doing nothing.

**Verified**: 672 unit + 199 integration tests green on Linux (scratch
Postgres), migration `0016` applies cleanly. New coverage: repository
round-trip (write/drain, scoped correctly per task, one-shot), the
`submit_signal` tool directly (records, rejects a non-http URL), `scout_signals()`
end to end via a scripted `FakeProvider` tool call (returns what the agent
submitted, drains the buffer, respects the disabled flag without ever calling
the model, tolerates finding nothing), and one full `graph.run()` pass
proving a scouted signal reaches clustering even with every fixed source
unconfigured. Role-catalogue exclusion and tool-isolation (only `http_get`
+ `submit_signal`, nothing from `growth`) covered in
`tests/unit/test_agent_spawner.py` alongside the existing `CHANNEL_SCOUT`/
`GROWTH_ANALYST` isolation tests.

**Deployed live** (`9412bb0`): committed, transferred via `git archive HEAD`,
migration `0016` applied to the live database *before* the restart (confirmed
via `alembic current`), image rebuilt, service restarted. `/health` reports
`ok`, all six engines enabled, boot log clean, no errors in the minutes
following restart.

## Manually seeding a test idea, and one round of "what would a human team catch that this one can't" — 2026-08-12

With the database empty after the reset and 14 Discovery cycles producing zero
survivors, manually seeded one realistic idea straight into `ideas` (status
`pending`, real `ScoredIdea` fields, source URL deliberately a clearly-fake
`example.test` domain so it's never mistaken for real evidence) and enqueued
its `ASSESS_COMPLIANCE` task directly — bypassing only the discovery-search
step. Everything after that (Compliance, Validation build via the staging
harness, going LIVE, lead harvest, outreach composition + approval, growth
review) runs through the real, unmodified pipeline. Confirmed both
`FACTORY_DRY_RUN=1` and `STAGING_MOCK_EXPENSIVE_CALLS=1` were still set first,
so this exercises real code without spending on the Anthropic API.

**A live demonstration of exactly the blind spot discussed below**: the
background watcher left running since the earlier reset (polls for the first
real idea, then reverts the fast Discovery cadence) fired on the seeded idea
and restarted the factory service right as Compliance had just claimed its
task — a genuine crash-mid-task case. The task sat `claimed` with a dead
lease-holder; `reclaim_expired_leases()` is designed to recover this
automatically within `task_lease_seconds` (900s), which is the kind of thing
worth confirming actually happened rather than assuming.

**Confirmed along the way, reading the real code rather than assuming**:
analytics (Phase 2, `page_views`/`validation_signals`, read by
`GROWTH_ANALYST`) and outreach (real per-lead LLM-composed copy, stored in
`OutreachLog` regardless of send outcome, gated behind a mandatory human
`CampaignApproval` and separately stubbed at the SMTP layer by
`FACTORY_DRY_RUN`) are both real, already-built capabilities — just nothing
had reached them yet. Also confirmed a real, honest gap: no scroll-depth or
time-on-page data exists (`PageView` is pageview-on-load only, no unload/
visibility-based beacon), and no path exists for `GROWTH_ANALYST` or anyone
else to call a third-party analytics API (GA4, Clarity) — the self-hosted
beacon was a deliberate choice over that, documented as a non-goal to revisit
only if the self-hosted data proves insufficient. Neither built yet; flagged,
not assumed.

**The team-setup discussion that followed** turned up a concrete, generalizable
gap: the learning loop (prompt/weight trials, `learning/trials.py`) tunes
*within* a fixed measurement framework — it has no way to audit whether the
framework itself is trustworthy. `approved_ideas_waiting`'s bug was exactly
that shape: not a value the learning loop could have flagged as underperforming,
but a silently-trusted assumption (every approved idea has a product) that no
statistical trial could ever surface, because the bug lived one level below
anything the learning loop's outcome-tracking looks at.

### `audit-snapshot-integrity` — closing that gap for the one concrete instance found so far

A new, fully deterministic (no LLM call) maintenance item, same family as
`prune-memory`/`remind-stale-interrupts`
(`engines/maintenance.py`/`supervisor/backlog.py`, `cadence_hours=24`). New
module `supervisor/audit.py`: `check_approved_idea_orphans()` runs a
genuinely different query than `build_snapshot`'s — an OUTER JOIN from `Idea`
looking for approved ideas with *no* product row at all, rather than the
INNER JOIN the live metric uses — so a future regression of the same shape
(the "Compliance always creates a product" guarantee silently breaking again)
gets caught by an independent check instead of just quietly undercounting
again. Raises a new `InterruptReason.DATA_INTEGRITY` escalation (no DB
migration needed — `interrupts.reason` has no CHECK constraint, just an
application-level enum), deduped by the existing open-interrupt-by-subject
mechanism so a persisting problem raises exactly one open interrupt, not one
per daily check.

Deliberately scoped to the one concrete instance this session actually found,
not a general "invariant framework" for hypothetical future metrics — the
existing `routing.py` stall-detection mechanism (fingerprint-based, triggers a
model consultation after `supervisor_stall_ticks` identical ticks) already
covers "is the whole snapshot stuck"; this is a narrower, complementary check
for "does a specific metric's underlying assumption still hold," which stall
detection cannot see (the bug metric kept the fingerprint moving the whole
time, since other fields kept changing around it).

**Verified**: 672 unit + 206 integration tests green on Linux (scratch
Postgres), no new migration. New coverage: `repository.orphaned_approved_ideas`
(healthy/orphaned/pending-not-yet-approved cases), `check_approved_idea_orphans`
directly (raises exactly once, dedups on repeat calls), and
`engines/maintenance.py`'s dispatch for the new slug end to end with a
`FakeProvider` holding zero queued turns — proof this check can never make an
LLM call, by construction.

**Deployed live** (`3853185`): committed, transferred via `git archive HEAD`,
image rebuilt, service restarted (no migration step needed). `/health` reports
`ok`, all six engines enabled, boot log clean; `audit-snapshot-integrity`
visibly enqueued on the very first tick after restart.

## The seeded product's build failure, root-caused and fixed live — 2026-08-12

The seeded idea from the section above hit heal-cycle exhaustion for real:
`"No module named pytest"`. Root cause: both staging fixtures
(`llm/providers/staging.py`) listed only what their own test files *import*
(fastapi/httpx/stripe) in `requirements.txt`, never `pytest` itself — and the
sandbox installs nothing beyond that file. Invisible until now because (a)
the e2e test's sandbox is a scripted always-passing fake that never actually
runs pip/pytest, and (b) this file's own real-pytest-run unit test
deliberately runs against the ambient dev venv rather than an isolated
install, specifically to avoid corrupting the shared venv (a previous version
of that test did exactly that and downgraded `stripe`, breaking an unrelated
test — see its own docstring). First real live exercise of the staging
harness against a genuinely bare, isolated sandbox; first time this gap could
have shown up. Fixed (`f933bea`): added `pytest` to both fixtures'
`requirements.txt`, hardened the real (non-staging) generator prompts with
the same explicit requirement as defense in depth, and added a deterministic
regression test asserting both fixtures list it, with no subprocess/venv side
effects. **Verified**: 673 unit tests green on Linux (`.env`-free scratch
copy — bind-mounting `/opt/superagent` directly for a quick check first
reproduced the *other* documented hazard, live `.env` leaking into `Settings`
defaults and failing 23 unrelated tests; redone correctly against
`/opt/scratch-test`). Deployed: image rebuilt, service restarted, `/health`
clean.

**A second, separate finding while retrying the stuck product**: a guided
retry (`enqueue_guided_patch`) always routes to *surgical* repair
(`SURGICAL_CATEGORIES` includes `"guided"` unconditionally, never falls back
to full regeneration). Surgical repair edits a workspace via a real
tool-calling agent loop (`.converse()`); the mocked `StagingCodeProvider`
never makes real tool calls. So **no guided/surgical heal cycle can ever
succeed while `STAGING_MOCK_EXPENSIVE_CALLS` is on** — confirmed live
("A surgical repair attempt was rejected: the agent changed nothing"),
regardless of what the underlying bug is or how good the guidance text is.
Only a fresh full regeneration (a new `BUILD_MVP`/`BUILD_LANDING_PAGE`, not a
patch) can pick up a fixture fix. Not fixed this pass — flagged as a real
gap in the staging harness worth a decision: either exclude surgical repair
from being attempted under staging mode (fail fast to a clean rebuild
instead), or teach `StagingCodeProvider` to handle a minimal real edit for
recognized failure signatures. Worked around this time by retiring the stuck
product (`repository.retire_product`, the real function) and reseeding fresh.

**Getting a product through to `validating`, for real, took three attempts** —
and every rejection was Compliance actually doing its job, not noise. Attempt
1 (a tutoring-notes idea) was flagged, correctly: storing lesson
notes/progress for students who may be minors is a real data-protection
question, not something to wave through for a test — rejected rather than
overridden. Attempt 2 (bookkeeping across multiple clients' QuickBooks/Xero
accounts) was also flagged, also correctly reasoned: transaction data can
carry customer/vendor PII, real GDPR data-processor liability. Approved this
one deliberately, as a human-standing-in judgement call specific to this
being a synthetic test with zero real people or real data involved (dry run
throughout) — a call that would not be made the same way for a real idea, and
said so explicitly in the interrupt's resolution text rather than approving
silently.

**Result**: a real product now sits in `validating` with a real (staging-
fixture) landing page deployed (`dry-run://product-e8d273d6ebcf`), and a real
`harvest_leads` task ran against it. Also surfaced a real, live, external
blocker there: `LEAD_SCOUT` planned a real search (6 titles, 0 domains — this
niche has no discoverable company domains to search by) and the Apollo call
came back `403`. `HUNTER_API_KEY` is also configured but was not queried
(the plan had no domains, which is Hunter's own lookup key) — worth checking
the Apollo account itself (key validity/plan/credits) if seeing real leads
flow matters for the rest of this observation window; not a code bug, nothing
fixed here.

## Heal-exhaustion escalations: readable subject, correct reason — 2026-08-12

Caught live, trying to clean up the stale interrupt from the section above:
resolving it threw `UniqueViolationError` on `uq_interrupts_open_subject`.
Root cause: `_heal()`'s escalation subject was `Product {product_id} failed
{N} heal cycles` — keyed only by product id, no per-instance uniqueness. The
first heal-exhaustion for a product resolves fine; if that same product ever
exhausts its heal budget a *second* time (a failed guided-patch retry
re-escalating, or a genuinely new bug later in its life), the second
escalation's subject is byte-identical to the first's, and the moment anyone
tries to resolve it, it collides with the already-resolved first row on
`(reason, subject, status)` — forever. `CHANNEL_SCOUT`'s `propose_channel`
already guards against exactly this class of collision (`experiment_id[:8]`
in its subject); the heal-exhaustion path never got the same treatment.

Separately: the reason was `InterruptReason.LEGAL_UNCERTAINTY` — so the
Telegram message you were reading during this exchange showed
`[LEGAL_UNCERTAINTY]` on top of "No module named pytest," a purely technical
repair failure with nothing to do with legal review. Confusing on its own,
independent of the collision bug.

Fixed both (`ee21e4f`): added a dedicated `InterruptReason.HEAL_EXHAUSTED`;
subject is now the product's real name plus a short fresh id suffix (e.g.
`"Freelance bookkeepers... (a1b2c3d4)"`), with the full `product_id` moved
into the detail body so it stays traceable. `collect_replies`'s reply routing
for this escalation shape keys on `(product_id set, task_id unset)`, not on
the reason value, so this changes nothing about how a reply is handled.

**The one stuck interrupt this was found while cleaning up** (`#2406ca12`,
from the retired product two sections up) is still sitting open — the raw
`DELETE` needed to remove it was blocked by the permission classifier, and
per your decision this pass prioritised the code fix over pushing that
through. Harmless: already delivered, won't be reminded for
`stale_interrupt_reminder_hours` (12h default), and it points at a product
that no longer does anything. Safe to leave, or delete by hand later.

**Verified**: 673 unit + 208 integration tests green on Linux (scratch
Postgres), no new migration (`InterruptReason` has no DB-level constraint,
just an application enum). New coverage in
`tests/integration/test_heal_memory_learning.py`: the escalation carries the
product's name and the new reason (not the raw id in the subject); two
separate exhaustions for the same product produce different subjects and
both `resolve_interrupt` calls succeed — the second used to raise exactly
the `UniqueViolationError` hit live.

**Deployed live**: committed, transferred via `git archive HEAD`, image
rebuilt, service restarted (no migration step). `/health` reports `ok`, all
six engines enabled, boot log clean.

## `/status` — a Telegram command, not just push notifications — 2026-08-12

You asked for the current status plus a way to ask for it on demand instead
of only ever receiving escalations. Confirmed along the way: products already
had real names (`Product.name = idea.target_niche[:80]`, set at creation in
`compliance/graph.py::_create_validation`) — `_heal()`'s escalation was the
only place in the whole codebase using a raw id instead
(`development/graph.py`, `campaign.py`, `channel.py` all already did this
right); fixed in the section above.

**New `/status` command** (`hitl/commands.py`): sends back every active
product's name, current stage, deploy URL, 7-day page views, validation
signals, lead count, and whether an AB test is running — the same numbers
`GROWTH_ANALYST` reads (`tools/growth.py::read_product_growth`) — plus how
many escalations are currently open. Deterministic, no LLM call, so it costs
nothing to check.

**Dispatched from `collect_replies`, not a second poller**:
`TelegramNotifier.poll_replies` advances a stateful `offset` on every call, so
a second, independent `poll_replies()` loop for commands alone would silently
consume updates the reply-handling loop never saw, or vice versa — one inbound
channel, one poll per tick, routed by message shape (`/status` vs `#<tag>
<answer>`) before the existing tag-matching logic runs.

**Verified**: 680 unit + 213 integration tests green on Linux (scratch
Postgres), no new migration. New coverage: the `STATUS_COMMAND` regex
(matches `/status`, `/status@BotName`, trailing text; rejects `/statuses`,
a tagged reply, and plain text mentioning "status"); `build_status_report`
directly (empty state, a real product's numbers, RETIRED products excluded,
page views outside the 7-day window excluded); and `collect_replies`
end-to-end with a scripted notifier, confirming `/status` never resolves or
touches an unrelated open interrupt sitting in the same poll batch.

**Deployed live**: committed, transferred via `git archive HEAD`, image
rebuilt, service restarted (no migration step). `/health` reports `ok`, all
six engines enabled, boot log clean. Not yet exercised against the real
Telegram API in this pass — that needs an actual message sent from your
account, which only you can do; try texting the bot `/status`.

### Fix, same day: a bare escalation count read as though it belonged to the wrong product

First real use exposed this immediately: `/status` showed one active product
("...retail-shop clients...") directly above "Open escalations: 1", and you
reasonably read the escalation as being about that product. It was actually
about a different, already-retired product with an almost identical name
("...small-business clients..." — the original seeded idea that hit the
missing-`pytest` bug, before the fix). A bare count next to one product's
listing gives no way to tell.

Fixed: each open escalation now names its own product (a fresh id lookup,
independent of whether that product is still in the active list) alongside
its reason and reply tag --
`#2406ca12 (legal_uncertainty) [Freelance bookkeepers managing 5+
small-business clients on QuickBooks or Xero -- retired]`. New
`repository.list_open_interrupts` (oldest first, capped) alongside
`count_open_interrupts`, which still drives the header total and a
"...and N more." tail when the list is capped.

**Verified**: 680 unit + 214 integration tests green on Linux (scratch
Postgres), no new migration. New regression test asserts the exact scenario
hit live: an escalation about a since-retired product must name that product
explicitly, distinct from whatever product is currently active and listed
above it.

**Deployed live**: committed, transferred via `git archive HEAD`, image
rebuilt, service restarted. `/health` reports `ok`, all six engines enabled,
boot log clean.

### Second fix, same day: the name was still confusable, and the layout itself was unclear

Two more rounds of live feedback on the same message. First: the escalation's
product name was `[:60]`-truncated -- for two real, similarly-worded products
("...small-business clients on QuickBooks or Xero" vs "...retail-shop
clients on QuickBooks or Xero") the cut landed right before the one word
that told them apart, and produced a visible double space where the cut
text's trailing space collided with the literal `" -- "` separator. Removed
the truncation entirely -- `Product.name` is capped at 80 chars at creation
and the whole report is well under Telegram's 3900-char cap, so there was
never a real need for it.

Second: even with the right name showing, the layout itself didn't make
clear that products and escalations are two different *kinds* of thing --
same bullet, escalation fields crammed onto one bracketed line
(`#tag (reason) [name -- status]`). Redesigned into two headed, numbered
sections with their own distinct fields: `PRODUCTS` shows stage/url/numbers,
`ESCALATIONS` shows reason (now a human label via a new `_REASON_LABEL` map
-- "Repair failed repeatedly", not the raw `heal_exhausted`) /about/issue/
reply, each on its own line. The structural difference, not just the
content, now makes the two sections impossible to conflate.

**Verified**: 680 unit + 215 integration tests green on Linux (scratch
Postgres), no new migration. Regression tests lock in both: a long,
realistic product name renders in full, and the reason label is human-
readable rather than the wire enum value.

**Deployed live**: committed, transferred via `git archive HEAD`, image
rebuilt, service restarted. `/health` reports `ok`, all six engines enabled,
boot log clean.

## `/issue #<tag>` and the stalled-validation sweep — 2026-08-12

Two more pieces from the same conversation. First, `/issue #<tag>`
(`hitl/commands.py`): `/status` only shows one short `issue:` line per
escalation by design; `/issue` returns the full original detail text --
what a human actually needs to answer it -- without hunting back through
Telegram history. Scoped to open interrupts only. New
`repository.find_open_interrupt_by_tag`, a separate lookup from
`interrupts.py`'s own identical one so `commands.py` never has to import
from the module that already imports it.

Second, and bigger: you pointed out that a validating product whose lead
sourcing found nothing (the live Apollo-403 case from earlier) had **no
automatic path forward at all** -- `HARVEST_LEADS` is a one-shot enqueue for
a validating product, so nothing ever asked again and nothing ever told a
human. Your framing: "if the internet goes out in an office, someone tries
to fix it, and if they can't, they ask." New stalled-validation sweep in
`scheduler.py::_schedule_periodic` (hourly check, gated by real elapsed
time via `settings.validation_stalled_hours`, default 24h): first stall
found → one automatic re-harvest; still zero leads after that → a new
`InterruptReason.LEAD_SOURCING_STALLED` escalation instead of trying again.
A product already escalated is excluded from the sweep (no piling a second
automatic action onto a pending human decision). Reply handling matches
every other product-scoped escalation: REJECT retires, anything else
queues one more fresh harvest attempt.

**A real deploy-ordering bug caught by verification, not live**: the
`/issue` command's `commands.py` changes were still sitting uncommitted
when the stalled-validation work landed in the same working tree.
`git add`-ing only the files intended for the stalled-validation commit
picked up `interrupts.py` (which by then called `commands.ISSUE_COMMAND`)
without `commands.py` itself (which defines it) — a broken cross-file
reference that would have shipped if scratch-Postgres verification hadn't
caught the `AttributeError` before the image ever reached the live
service. Fixed by committing the missing files as their own commit, then
re-verifying the corrected tree from a clean rebuild.

**Verified**: 686 unit + 233 integration tests green on Linux (scratch
Postgres), no new migration. New coverage for the sweep: the query's
inclusion/exclusion logic (too young, has leads, already escalated, wrong
status), the retry-then-escalate state transition across two sweep runs,
that an already-escalated product is left alone, and both reply paths
(REJECT retires, anything else queues a fresh attempt).

**Deployed live**: committed (two commits — `/issue` retroactively, then
the stalled-validation sweep), transferred via `git archive HEAD`, image
rebuilt, service restarted. `/health` reports `ok`, all six engines
enabled, boot log clean.

## Classifying lead-provider failures: key problem vs. code problem vs. transient — 2026-08-12

You proposed making the stalled-validation escalation itself smarter first:
a 403 means either the code or the key is wrong — check that before asking
for help, and if it *is* the key, that's the same basket as asking for a
genuinely missing credential, not a novel kind of ask.

HTTP semantics already carry this distinction cleanly: 401/403 means the key
is present but refused (invalid, expired, insufficient plan/credits) — an
account problem no retry will ever fix. 400/422 means the request itself is
malformed — a bug in this factory's own code, not an account problem.
Neither of Apollo's or Hunter's adapters previously captured this: any
non-200 response just logged a bare status code and returned `[]`,
indistinguishable from "searched successfully, found nobody real."

Added `ProviderAuthError`/`ProviderRequestError` (`providers/base.py`),
raised by both adapters on the matching status codes; Hunter's per-domain
search loop stops on the first one rather than repeating the same rejected
key across the remaining domains. `leads.ingest()` catches both and records
them on `IngestReport`. `marketing/graph.py::_harvest()` now escalates
*immediately* when either is diagnosed — not deferred to the
stalled-validation sweep's next check, since there is no reason to make a
human wait up to a day to be asked about something fixable in two minutes.
Reuses the existing `LEAD_SOURCING_STALLED` reason and reply handling
(REJECT retires, anything else retries) rather than a new mechanism, worded
honestly and differently for each case ("check the provider's dashboard" vs.
"this looks like a bug in the request-building code, not an account issue").
Deduped against an already-open escalation, same as the sweep, so a
persistently broken credential does not re-ask on every later harvest cycle
(the periodic live-product sweep runs every 12h).

429/5xx/network failures are untouched — still logged and degraded
gracefully, since those may resolve on their own and are exactly what the
slower, patient stalled-validation sweep already covers.

**Verified**: 699 unit + 238 integration tests green on Linux (scratch
Postgres), no new migration. New unit coverage: both adapters raise the
right exception on 401/403/400/422 and degrade gracefully on 429/5xx/503;
Hunter's domain loop stops after exactly one rejected-key response instead
of repeating it. New integration coverage: the escalation's wording differs
correctly by failure kind, it's deduped against an already-open one, and
`_harvest()` actually calls it when `ingest()` reports a diagnosed problem
(and does *not* when a search just genuinely finds nobody).

**Deployed live**: committed, transferred via `git archive HEAD`, image
rebuilt, service restarted. `/health` reports `ok`, all six engines
enabled, boot log clean.

## Root-cause fix: `uq_interrupts_open_subject` wasn't actually scoped to open interrupts — 2026-08-12

After the lead-provider classification fix above, you asked to see the rest
of the funnel work, keep the Apollo 403 noted (now recorded as a project
memory — it's an account-side dashboard problem, not a code fix), and then
audit and fix every remaining bug. You chose a fresh systematic audit over
reviewing only the sites already touched this session.

The audit's headline finding: `uq_interrupts_open_subject` is a plain
`UniqueConstraint(reason, subject, status)`. Despite its name, that makes a
given `(reason, subject)` globally unique *forever*, across every status —
not just while `status='open'`. The moment one interrupt with a given
`(reason, subject)` resolves, no later, unrelated occurrence of that exact
pair can ever resolve either: `resolve_interrupt()`'s `UPDATE` to
`status='resolved'` collides with the row already sitting at that tuple.
This had already bitten `HEAL_EXHAUSTED` once (worked around locally, at
that one call site, earlier this session) — the audit found the same latent
crash waiting at several other sites that give their subject no
per-occurrence uniqueness at all: `deliverability.py`'s freeze/re-freeze
escalations, and this session's own new `LEAD_SOURCING_STALLED` sites in
`marketing/graph.py` and `scheduler.py`.

Fixed at the root instead of patching each site again: migration `0017`
drops the constraint and replaces it with a partial unique index scoped to
`status='open'` — what the name always claimed to do. `orm.py`'s
`Interrupt.__table_args__` and `repository.open_interrupt()`'s
`ON CONFLICT` clause both updated to match, following the exact
`index_where` convention `create_marketing_freeze` already established
(and already got wrong once, per that function's own docstring): an ORM
column comparison at the DDL level, a literal `text()` clause at the
runtime `on_conflict_do_nothing` call site — a bound parameter can never
structurally match a partial index's own literal predicate.

Every affected call site was judged individually rather than patched
uniformly. Sites that could legitimately fire again while an *identical*
prior occurrence is still open — main.py's preflight, Docker-unavailable,
the orchestrator's same-day budget cap — deliberately keep a stable
subject; that's correct dedup (repeat signals collapsing into one open
interrupt), not the bug, and giving them a fresh id would have been a
regression. Sites that only re-fire once the *previous* occurrence has
already resolved — `deliverability.py`'s two raise sites,
`LEAD_SOURCING_STALLED`'s two sites — now suffix their subject with a fresh
id per raise, safe specifically because each is already guarded against
firing twice while the first is still open (`create_marketing_freeze`'s
None-return, `has_open_interrupt_for_product`).

A second bug surfaced during verification, unrelated to the interrupt logic
itself: migration `0017`'s revision id
(`0017_interrupt_open_subject_partial`, 36 characters) overflowed
`alembic_version.version_num`'s `VARCHAR(32)` column — every prior
revision id in this repo had, apparently deliberately, stayed under that
limit. Caught by the scratch-Postgres `alembic upgrade head` run, which
failed with `StringDataRightTruncation` on the final version-bump `UPDATE`
(and rolled back the whole run, migrations 0001–0017 included, confirming
this project's migrations execute as one transaction). Fixed by shortening
the stored revision id to `0017_interrupt_open_subject`; the file itself
keeps its original, more descriptive name.

**Verified**: 699 unit + 241 integration tests green on Linux (scratch
Postgres), migration `0017` applied cleanly end to end (0001 through head,
after the revision-id fix). New coverage: a repository-level test proving
two separate occurrences of the *identical* `(reason, subject)` can both
reach `resolved` without colliding, and that the still-open dedup case is
untouched; a `deliverability.py` end-to-end test driving two full freeze
episodes through the real `process_event` → `_check_and_trip` →
`sync_freeze_clearance` → `clear_marketing_freeze` path (not hand-built
interrupt rows, unlike every earlier freeze/resume test) — the second
episode's resolve must not collide with the first episode's now-resolved
row.

**Deployed live**: committed (two commits — the fix, then the migration
revision-id length fix caught by verification), transferred via
`git archive HEAD`, image rebuilt, migration `0017` applied to the live
database, service restarted. `/health` reports `ok`, all six engines
enabled, boot log clean.

## A third lead-sourcing path: web-search-driven scraping, ported from a proven reference — 2026-08-13

The Apollo-403 fix above unblocked Hunter but not the actual stuck product:
you supplied a real, working `HUNTER_API_KEY` (confirmed live against
`stripe.com`), but Hunter only searches *by company domain*, and the stuck
product's search plan has none — "freelance bookkeepers" aren't tied to a
company website the way Apollo's title/keyword search assumed. Neither
provider can source leads for this product, or any future one shaped like
it, as things stand.

You'd already solved this for a different product (ValidDraw, construction
pay-application auditing) and shared that Lead Scout's actual code
(`reference files/lead_scout.py`, not committed to this repo — it's a
different product's own file, shared as a reference only). It turned out to
be real, iterated production code with documented incidents baked in as
comments, not the "dumb, hardcoded" thing you called it: a domain-matching
bug that would have emailed a LinkedIn employee ("bfoster@linkedin.com"), a
company-name dedup bug ("Design Electric, Inc." vs "Design Electric Inc."
counted as two companies), and 56% of all spend once wasted re-scraping the
same directory pages repeatedly.

Ported its proven mechanisms rather than redesigning from scratch, replacing
only what was genuinely specific to that one product's audience:

- **Search backend**: Serper (`google.serper.dev`), matching the reference's
  own proven choice — new `SERPER_API_KEY` setting, new
  `src/factory/integrations/serper.py` shared client.
- **New provider** `WebsiteContactProvider`
  (`marketing/providers/website.py`): generates search queries, filters
  results through a ported `NON_EMPLOYER_DOMAINS` blocklist (LinkedIn,
  Indeed, ZoomInfo, etc. — a job-ad or profile page never carries the
  employer's own reachable address) and a robots.txt check (genuinely new —
  Apollo/Hunter call clean APIs, this is the first thing in this codebase
  that visits arbitrary third-party HTML), fetches the page, and extracts
  candidates via an LLM. Every extracted name/email/company must pass
  `_found_in_source()` — literally appear in the scraped text — or the
  candidate is dropped; this is the real defense against both hallucination
  and a prompt-injection attempt riding in on scraped page content.
- **Hunter's role corrected mid-plan**: the reference uses Hunter's *Email
  Finder* (`GET /v2/email-finder`, ported as `hunter.py::find_email`) only
  as a fallback when a named lead's page carries no email at all — not an
  email-verifier call on every scraped address, which is what an earlier
  draft of this plan assumed before the reference file was actually read.
  Ported behavior, not the assumption.
- **Hyper-specific audience targeting** (a question you asked directly): a
  new `QualificationBrief` (positive criteria + strict exclusions +
  borderline guidance) generated once per product by an LLM call and cached
  on `Product.lead_qualification_brief` (migration `0018`) — the same
  precision ValidDraw's hand-written ICP prompt has, generated instead of
  hand-written, since this factory has to work across whatever niche
  Discovery finds next rather than one fixed vertical. Consumed by the
  provider's query generation, extraction, and borderline-verification
  steps.
- **Serper reused beyond lead sourcing** (per your own note that it has
  other uses): a new general-purpose `web_search` agent tool
  (`tools/builtin.py`), and a new `"search"` tool category added to both
  `LEAD_SCOUT` and `CHANNEL_SCOUT` — the latter previously had to guess at
  platform search URLs via `http_get`; it now gets real search.
- **Pre-enrichment dedup and cooldown**, both ported: `has_lead_for_company`
  (normalizes company names — the "Design Electric, Inc." bug's fix) avoids
  a wasted Hunter Finder call on a company+name already known;
  `recently_processed_urls` (7-day cooldown) avoids re-scraping the same
  evergreen directory page on every harvest, the exact thing that burned 56%
  of the reference's spend.

One correction flagged in the plan itself, not silently resolved: your
original 4-category list included "job listings & career pages," but the
reference's own hard-won lesson is the opposite — job boards/recruiter sites
are in the exclusion blocklist, since a job ad never carries the employer's
own reachable address. This provider covers "direct company websites" and
"industry/trade directories" together as one unified search-and-filter
pipeline, matching what the reference actually does, not the original
category list.

`WebsiteContactProvider` needs both `SERPER_API_KEY` and `HUNTER_API_KEY` to
report `available` — deployed with only `HUNTER_API_KEY` live, so it is
currently a safe no-op, filtered out of `_real_adapters()` exactly like any
other unconfigured optional provider. `SERPER_API_KEY` still needs to be
supplied before this does anything.

**Verified**: 984 unit + integration tests green on Linux (scratch
Postgres), migration `0018` applied cleanly (0001 through head). New
coverage: the anti-hallucination backstop actually drops a hallucinated/
injected field, `NON_EMPLOYER_DOMAINS` filtering happens *before* a fetch
(never scraped, not just never stored), a robots.txt-disallowed page is
never fetched, confidence gating and the borderline-verification path (both
directions), pre-enrichment dedup, Serper/Hunter Finder auth/request-error
propagation, and — the real regression target —
`build_qualification_brief` generating once and being reused on a second
call for the same product rather than regenerated (proven against real
Postgres: only one `QualificationBrief` is ever queued in the fake LLM
provider across two calls).

**Deployed live**: committed, transferred via `git archive HEAD`, image
rebuilt, migration `0018` applied to the live database, service restarted.
`/health` reports `ok`, all six engines enabled, boot log clean. Not yet
active — waiting on `SERPER_API_KEY`.

## First real test, and a no-call list for domains that block the scraper — 2026-08-13

You supplied `SERPER_API_KEY`. Set it live, restarted, and ran a real test
harvest against the still-stuck bookkeepers product — the orchestrator was mid
way through building two newly-approved products and its own tick-repeat
detector didn't fire this time (a separate, pre-existing quirk not touched
here), so the harvest task was run directly rather than waiting on the
scheduler.

**Result: the provider works end to end, found zero leads.** Real Serper
results came back, robots.txt was correctly respected (a Reddit thread was
skipped without a fetch), no crash, no auth error — but every single page
fetch failed: `ConnectTimeout` against small sites
(`adequatebookkeeping.com`), `ReadTimeout` against a large one
(`quickbooks.intuit.com`). Confirmed via a direct diagnostic from inside the
live container that this is not a general networking problem —
`example.com`/`python.org` both fetched instantly from the same box. It's
specifically these target sites resisting a request from the server's
DigitalOcean datacenter IP: basic host-level blocking on the small sites,
likely Akamai/Cloudflare-style bot defenses stalling the response on Intuit.
This is a real, structural limitation of scraping from a cloud IP without
bot-evasion infrastructure, not a bug in the request-building code — recorded
as [[project-scraping-datacenter-ip-blocking]] for future reference.

Per your direction: try a bigger sample before concluding anything, but stop
repeatedly paying for the same known-bad domains — put them on a "no call
list." A real scraper-proxy fix (residential IPs) is deliberately deferred,
not forgotten.

**Fix**: `scrape_domain_failures` (migration `0019`) — a running per-domain
failure count, global across products (a site blocking this server's IP
blocks every product's harvest equally). `repository.record_scrape_failure`
upserts and returns the post-increment count via `RETURNING`, avoiding a
second round trip to check the threshold. `_fetch_page_text` now returns
`(page_text, block_reason)`; only `httpx.TimeoutException` and HTTP 403 count
toward blocking (a 404 or a non-HTML content type is a normal skip, not a
rejection). Once a domain crosses `_DOMAIN_BLOCK_THRESHOLD` (2), it's skipped
before a fetch is even attempted — checked once at the top of `search()` and
updated in-call, so a third same-domain result within one harvest is also
skipped, not just on a future one.

**Verified**: 990 unit + integration tests green on Linux (scratch Postgres),
migration `0019` applied cleanly (0001 through head). New coverage: a domain
already on the no-call list is never fetched, a timeout/403 correctly
records a failure, and a same-call third result on a domain that just
crossed the threshold is skipped while the first two (which had no way to be
known-bad in advance) still get attempted. Two new repository-level
integration tests confirm the failure count persists and blocks correctly
against real Postgres, and that two different domains' counts stay
independent.

**Deployed live**: committed, transferred via `git archive HEAD`, image
rebuilt, migration `0019` applied to the live database, service restarted.
`/health` reports `ok`, all six engines enabled, boot log clean.

**A second real harvest, immediately after**, confirmed the no-call list works
live: `quickbooks.intuit.com` timed out twice within the run and a third
result on that domain was skipped on sight (`leads.website_domain_blocked`),
without a wasted fetch. Also surfaced a real observability gap while reading
that run's log: `httpx.TimeoutException`'s subclasses commonly stringify to
`""`, and the timeout branch logged nothing at all — the run read as an
unexplained multi-second gap with no record of what failed or why. Fixed by
logging the exception's class name (`ConnectTimeout`/`ReadTimeout`) instead of
its empty string, and logging the 403 case explicitly too. Pure logging
addition, no logic change — verified with local ruff/mypy/the provider's own
unit suite and deployed directly, skipping the full scratch-Postgres cycle for
once given the size and risk of the change.

Also surfaced, not yet acted on: a `[SSL: CERTIFICATE_VERIFY_FAILED]` fetch
failure against a real small-business site. Correctly not counted toward the
no-call list (neither a timeout nor a 403, per your exact instruction) — most
likely that site's own broken/expired certificate chain, not a problem on this
server's end (well-configured HTTPS sites fetched fine in the same diagnostic
session). No action taken; noting it here in case a pattern emerges later.

This second real run still found zero leads — two real samples now both
returned nothing from real-world small-business/corporate sites, reinforcing
[[project-scraping-datacenter-ip-blocking]] rather than looking like one
unlucky draw. The no-call list and better logging are both working as
designed; the underlying datacenter-IP resistance is the real remaining
constraint, and the scraping-proxy fix for that stays deliberately deferred.

## Telegram escalation redesign: clear structure, honest reply instructions — 2026-08-13

You said the escalation messages were unclear about what they expect you to do
and the structure was messy. An audit of all 18 `interrupts.request()` call
sites confirmed why: reply vocabulary was inconsistent (APPROVE/REJECT,
RESUME, SKIP, "retry," "reply with anything"), 6 sites gave no reply
instruction at all, and every message ended with a generic
`"Reply with: #{tag} <your answer>"` footer stacked on top of whatever ad hoc
sentence was already buried in `detail`.

**New message shape**, agreed after several rounds of concrete before/after
examples:

```
📦 {product name}          <- only if product-scoped
{headline}                  <- emoji + specific category, e.g. "🔑 Account problem"

🔍 The problem:
{detail}

────────────────
👉 What to do:
{reply_hint}
{"Include this tag in your reply: #tag" if needs_reply else "Tag: #tag"}
```

**Schema**: migration `0020` adds `headline: Text`, `reply_hint: Text`,
`needs_reply: Boolean NOT NULL DEFAULT true` to `interrupts` (nullable —
safe for historical rows). `repository.open_interrupt()` and
`interrupts.request()` both made `headline`/`reply_hint` **required** kwargs
(no default) specifically so mypy would catch any of the 18 production call
sites that forgot them — confirmed zero missed sites.

**Shared template**: new `hitl/formatting.py::build_escalation_text()` is the
one place the template above is assembled, used by both the push
(`deliver_pending`) and the `/issue` drill-down (`build_issue_detail`) so they
can never drift apart. Lives as a dependency-free leaf module specifically to
avoid a real circular import — `interrupts.py` already imports `commands.py`
for `/status`/`/issue` dispatch, so `commands.py` can't import back.

**Plain-language rewrite for raw error text**: you pointed out the error
blocks are unreadable without code access. Of the 18 sites, 3 dump genuinely
freeform, unpredictable technical text (heal-cycle failures in
`development/graph.py::_heal`, infra errors and generic task-exhaustion in
`supervisor/graph.py`) — the rest either carry already-structured data or
branch on a known cause with a deterministic explanation. Those 3 now call a
new `interrupts.plain_language_error()`: one cheap Gemini `TaskType.ANALYSIS`
call that rewrites the raw text into 2-4 plain-English sentences (affected
area, what's specifically wrong, a plausible root-cause hypothesis) for a
non-technical operator. Falls back to
`f"(Technical detail, unedited): {raw_text[:500]}"` on any `LLMError` — an
escalation must never be blocked by this call failing.

**Product grouping**: you asked for the product name on top and escalations
sorted per product. `list_open_interrupts`/`stale_open_interrupts` changed
`ORDER BY` to `product_id ASC NULLS LAST, created_at ASC`; `/status`'s
escalation entries now lead with the bare product name (or "(not tied to a
specific product)") instead of a labeled "about:" line, using
`interrupt.headline` in place of the old `_REASON_LABEL` dict (removed
entirely — `headline` is strictly more specific, proven by
`LEAD_SOURCING_STALLED` needing two different headlines, 🔑 vs 🐛, for the
same `InterruptReason` depending on whether the real cause was a bad
credential or a request/code bug).

**Honesty pass on `reply_hint`**: 4 sites where a reply genuinely does
nothing beyond closing the escalation (budget cap, Docker-unavailable, the
data-integrity audit, the orchestrator's own generic `_escalate`) now say so
plainly (`needs_reply=False`, "No reply needed"). Sites where only the literal
word "retry" does anything (checked against `collect_replies()`'s actual
branches, not assumed) say exactly that instead of implying free text works.

**Verified**: local ruff/format/mypy clean; local unit suite green (the two
pre-existing Windows path-separator failures in `test_bug_council.py`/
`test_deploy_env.py` are unrelated and expected to fail here, pass on Linux).
Scratch-Postgres surfaced 9 failures mypy couldn't catch (mypy doesn't scope
`tests/`) on the first full run: 4 needed a `FakeProvider` response queued for
`_PlainLanguageExplanation` wherever a test exercises the heal-exhaustion or
generic-exception paths (both now make a real LLM call before escalating), 3
were `/status` assertions still expecting the old "about: " prefix, and 2 were
this session's own new product-grouping tests reusing a shared idea fixture
twice and hitting `upsert_idea`'s problem/niche fingerprint dedup instead of
getting two distinct ideas. Fixed all 9, re-ran, and a 10th surfaced on the
second pass: a heal-exhaustion test asserted the raw product id was still
readable inside `detail` — true under the old design (which literally
embedded `f"Product: {name} ({product_id})"`), no longer true or desired now
that `detail` is an LLM-authored plain-English explanation. Traceability
already holds via the interrupt row's own `product_id` column and structured
logs, so the assertion was updated rather than re-adding the id to
human-facing text. Third full run: 1003 passed, 0 failed.

**Deployed live**: committed across three commits (the redesign itself, the
first test-fix round, the heal-exhaustion detail-assertion fix), transferred
via `git archive HEAD`, image rebuilt, migration `0020` applied to the live
database, service restarted. `/health` returns 200, boot log clean, all six
engines enabled.

**Visual confirmation, live**: closed out 3 open interrupts (one confirmed
stale — a heal-exhaustion escalation on a product already retired and
reseeded; one legal-review flag on an unrelated idea, left untouched; one
lead-sourcing-stalled retry) to get a clean slate, then re-triggered lead
sourcing for the still-active bookkeepers product to exercise the pipeline
end to end. The harvest ran for real (Serper search, page fetches, Hunter
lookup) and found 0 leads — a 3rd real-world confirmation of
[[project-scraping-datacenter-ip-blocking]], and, combined with task history
(6 harvests on this product, 1 on the DevOps product, both always 0 leads),
proof the block is server-IP-level, not niche-specific. The periodic
stalled-validation sweep independently re-escalated the same product at
almost the same moment; its push arrived on Telegram in the new format —
first live confirmation of this session's redesign, matching the template
exactly.

## Campaign-approval escalations were being silently destroyed on every raise — 2026-08-13

Manually seeded 3 fake `Lead` rows (`lia_passed=True`, clearly-fake `.test`
email domains, real `insert_lead` write path) for the bookkeepers product and
enqueued a `SEND_OUTREACH` task by hand, specifically to exercise the parts of
the pipeline a real harvest has never once reached (0 leads in 7/7 real
attempts always stalls before them): campaign approval, outreach composition,
send. `hitl.raised` logged a `campaign_approval` escalation immediately, as
expected — but it never showed up in `/status`, never reached Telegram, and
the DB had no open interrupt at all. The `SEND_OUTREACH` task just sat
`pending`, silently retrying forever.

**Root cause**: `_run_engine` (`supervisor/graph.py`) runs each task inside one
`session_scope`, which commits on success and rolls back on *any* exception.
`campaign.ensure_approval()` writes the `CampaignApproval` row and calls
`interrupts.request()`, then deliberately raises `CampaignApprovalPending` as
control flow — not a failure, just "defer this task." That exception
propagates out through the same `session_scope` it was written in, so the
whole transaction, interrupt included, gets rolled back every time. The task
correctly defers (via a separate, freshly-committed session), so it retries
forever — but every retry re-writes and re-rolls-back the identical doomed
interrupt. A human was never actually asked. Checked the other three
defer-by-exception paths (`ValidationCapacityExceeded`, `RepairBlocked`,
`MarketingFrozen`) — none of them write new state immediately before raising,
so this was specific to campaign approval, not a wider pattern.

Given "the top-level legal gate: strictly forbidden from opening the SMTP
transport... without an explicit human APPROVE on file" — this means real
campaign approval requests have likely never actually worked in this
deployment's history; nothing had ever exercised that exact code path for
real until this manual test.

**Fix**: `ensure_approval()` now calls `await session.commit()` right after
writing the interrupt and linking it to the `CampaignApproval` row, before
raising `CampaignApprovalPending` — so the rollback that follows has nothing
left to undo.

**Why no existing test caught this**: every test in
`test_campaign_approval.py` called `ensure_approval()` directly with a plain
session and manually committed afterward regardless of the exception —
nothing like `session_scope`'s actual commit-on-success/rollback-on-exception
wrapping. New test
`test_the_escalation_survives_being_raised_inside_a_rolled_back_session_scope`
reproduces that exact wrapping (catch `CampaignApprovalPending`, roll back,
then verify against the same session) — fails without the fix, passes with
it.

**Verified**: local ruff/format/mypy clean, local unit suite green (same two
pre-existing Windows path-separator failures, unrelated). Scratch-Postgres:
`test_campaign_approval.py` alone (6/6, including the new regression test),
then the full suite, 1004 passed. Deployed live: committed, transferred via
`git archive HEAD`, image rebuilt (no migration needed, no schema change),
service restarted. `/health` returns 200, boot log clean.

**Live, end to end**: with the campaign approval now actually reaching
Telegram, replied APPROVE. `sync_decisions()` picked it up correctly. The
`SEND_OUTREACH` task's own 1-hour recheck interval was manually shortened
(`not_before` cleared directly in the DB, test-only) twice during this session
purely to observe the outcome sooner — no application logic changed for that.

## The orchestrator kept routing to Development on a queue it had already emptied — 2026-08-14

Watching the live factory move (no new harvests, just observing the natural
tick loop) surfaced a second real bug: 20 consecutive ticks over 5 minutes, on
every single one the Supervisor's LLM consultation decided "route to
development — an approved idea is waiting to be built," and on every single
one, nothing actually started. Meanwhile Discovery had 3 real pending tasks
and Marketing had the outreach send from the campaign-approval test above,
both sitting untouched.

**Root cause**: `approved_ideas_waiting` (`orchestrator.py::build_snapshot`)
counts products whose status isn't `retired` or `live` — and `VALIDATING`
(landing page live, gathering signals, no MVP code yet by design) is
deliberately included in that count, per `test_orchestrator_snapshot.py`'s
own `test_a_product_still_in_progress_counts_as_waiting`. That's not the bug;
it's intentional. The actual gap was one layer up, in `decide()`
(`supervisor/graph.py` calls this every tick): once the model was consulted,
its `ROUTE` decision was returned as-is, with no check against the snapshot's
own `pending_by_engine` counts — so the model could confidently route to
Development while `pending_by_engine['development']` was provably `0`, and
nothing would catch it. Once the identical stuck state persisted long enough,
`deliberation_reason`'s own "stalled" trigger kept re-consulting the model
every tick to try to break the loop — but re-asking cannot fix a
*consistently* wrong judgement, only a genuinely undecided one, so it just
repeated the same wrong answer instead.

**Fix**: after a model consultation, `decide()` now rejects a `ROUTE` to an
engine with zero pending tasks and falls back to the deterministic priority
order instead — emptiness is a fact the model cannot be right or wrong about.
Two new tests: a route to an eligible-but-empty engine is rejected in favor
of the deterministic answer, and a route to an engine that genuinely has
queued work still passes through unchanged (the guard must not become a
blanket veto on every model judgement call).

**Verified**: local ruff/format/mypy clean, local unit suite green (1006
including two new tests; same two pre-existing Windows failures, unrelated).
Scratch-Postgres: full suite, 1006 passed. Deployed live: committed,
transferred via `git archive HEAD`, image rebuilt (no migration needed),
service restarted. `/health` returns 200.

**Confirmed live, immediately**: the very first tick after restart proposed
the exact same bad route ("An approved idea is waiting to be built... routing
to development"), and this time the new guard caught it
(`orchestrator.route_to_empty_engine`) and fell back to Marketing, which had
real queued work. The full chain moved for the first time in this session:
Marketing claimed the stuck `SEND_OUTREACH` task, composed real per-lead
copy for the 3 seeded test leads, and correctly stopped at the dry-run gate
(3 attempted, 0 sent, 3 suppressed) — then Discovery and Compliance each
picked up their own real backlog on the following ticks.

## Risk-audit hardening, Phase 0: the public `/metrics` leak — 2026-08-14

Two research papers (autonomous-agent failure modes; micro-SaaS death causes)
were audited against this codebase — full report at
https://claude.ai/code/artifact/6c863cf7-4557-4c41-ac32-7b6b60d525c0. 11
partial-or-gap items came back; a phased plan to close all of them (while
explicitly preserving the factory's autonomy, self-learning, and free-form
judgment — no new rigid rules where a learnable weight or a model judgment
already does the job) is under execution now. Phase 0 is the first deploy.

Research for the plan itself surfaced a live issue the audit had missed:
`GET /metrics` — the factory's full financial and operational state, spend,
balance, reinvestment pool, task counts — was both unauthenticated *and*
publicly reachable through Caddy. Anyone who found the path could read it.

**Fix, two independent layers**: the `Caddyfile` now refuses `/metrics`
outright at the edge (`respond 404`), and the app itself requires a bearer
token behind that, so a future Caddyfile edit or proxy misconfiguration can't
silently re-expose the endpoint on its own. Fails closed — an unset
`METRICS_AUTH_TOKEN` means the endpoint refuses every request with 503, never
serves openly. Token comparison uses `hmac.compare_digest` on both scheme and
value, since the endpoint stays reachable from the container network even
with Caddy blocking the public path.

Also fixed in the same Caddyfile edit, ahead of Phase 1's rate limiter which
depends on it: `X-Forwarded-For` is now overwritten with `{remote_host}`
rather than appended. Caddy's default appends, which would let a caller pin
any value it likes and defeat per-IP rate limiting by sending a fresh header
on every request.

**Verified**: local ruff/mypy clean on the changed files (two pre-existing
ruff findings in unrelated files, confirmed identical on clean HEAD, left
untouched). 6 new tests (`test_metrics_auth.py`) — unset/missing/wrong/
wrong-scheme/correct token, plus a guard that `/health` stays unauthenticated
for the deploy pipeline and uptime monitoring. Local unit suite: 1012 passed
(same two pre-existing Windows path-separator failures). Scratch-Postgres:
full suite, 1012 passed.

**Deployed live**: committed, transferred via `git archive HEAD`, image
rebuilt (no migration — no schema change), Caddy and factory both recreated
to pick up the new Caddyfile and image. Production token generated directly
on the server via `openssl rand -hex 32` (never passed through the assistant
session), `.env` backed up first per the existing convention. Confirmed from
the server itself: public `/metrics` → 404, public `/health` → 200 (proxy
config, real HTTP), loopback `/metrics` with the correct bearer token → 200,
loopback `/metrics` with no token → 401.

## Risk-audit hardening, Phase 1: double opt-in and abuse controls on validation signals — 2026-08-14

The single load-bearing gap from the risk audit — the same one demonstrated
live earlier this session, six unconfirmed emails carrying a product through
`EVALUATE_VALIDATION` into a real build. `POST /interest/{product_id}` now
mints a confirmation token; a signal only counts toward the gate once
`GET /confirm/{token}` is clicked. `tally_validation_signals` stayed a single
query (`confirmed_at IS NOT NULL`) rather than growing a dry-run branch — a
row is auto-confirmed *at insert time* (`confirmation_method='dry_run_auto'`)
under `FACTORY_DRY_RUN`, since there's no real inbox to click a link from in
that mode.

**Second, independent guard**: even a fully-confirmed response rate can still
be one submitter clicking through several times. `_evaluate_validation` now
checks IP concentration (once ≥5 confirmed signals exist) and refuses to pass
— raising `InterruptReason.DATA_INTEGRITY` and retiring the product — if one
submitter IP accounts for more than `validation_max_ip_concentration` (50%
default) of them. IPs are never stored raw: HMAC-SHA256 keyed on a configured
secret, or not recorded at all if the secret is unset — fail toward less
data, never a reversible hash (plain `sha256(ip)` is reversible in seconds
for an IPv4 address).

**Both unauthenticated public write endpoints are now rate-limited** —
`/interest` per-IP and per-product, `/analytics` per-IP only (a popular page
should be free to get real traffic from many real visitors). Backed by a new
`rate_limit_buckets` table (Postgres, not in-process memory — this factory
can run more than one API worker) via a fixed-window upsert mirroring
`record_scrape_failure`'s idiom. Implemented as a FastAPI *dependency*, not
middleware, specifically so the signed Stripe/Mailgun webhooks — trusted
callers — are never subject to the same throttle built for anonymous traffic.

**A real, separate erasure gap closed as a side effect**: `GET /u/{token}`
previously resolved only `OutreachLog.unsubscribe_token` — a landing-page
visitor who submitted `/interest` but never received outreach had no
self-serve erasure route at all. It now also resolves a validation-signal
confirmation token.

**Fixed 6 pre-existing tests** broken by `insert_validation_signal` now
defaulting to `require_confirmation=True`: the validation gate's own tests,
AB-test outcome tallying (which reads through the same
`tally_validation_signals` — a good side effect, AB test winners are now
equally protected from unconfirmed signals), a growth-tool read, and
`/status`'s signal count. Each needed an explicit `require_confirmation=False`
since their intent was testing threshold logic, not the confirmation flow
itself.

**Verified**: local ruff/mypy clean. 7 new unit tests (`test_abuse_controls.py`,
pure `hash_ip` — deterministic, secret-dependent, unset-secret-means-no-hash).
Extensive new integration coverage in `test_validation_gate.py`: unconfirmed
signals don't count, confirming flips the tally, double-confirm is a harmless
no-op, dry-run auto-confirms at insert time, a blacklisted address doesn't
crash the flow, the IP-concentration refusal fires and doesn't false-positive
on genuinely spread signals or on unhashed rows, and the rate limiter both
trips and correctly treats different IPs as independent buckets. Extended
`test_gdpr_erasure.py` for the new erasure route. Local unit suite: 763
passed (same two pre-existing Windows failures). Scratch-Postgres: first run
surfaced the 6 pre-existing test breakages above; fixed, re-ran clean —
1031 passed.

**Deployed live**: committed across two commits (the feature, then the test
fixes), transferred via `git archive HEAD`, image rebuilt, migration `0021`
applied to the live database, `ABUSE_IP_HASH_SECRET` generated directly on
the server via `openssl rand -hex 32` (`.env` backed up first), service
restarted. `/health` returns 200.

**Confirmed live, immediately**: the new `expire-unconfirmed-signals`
maintenance item was already picked up and ran clean on the very first tick
after restart. A real interest submission to the live validating product
correctly auto-confirmed at insert time with a real IP hash attached
(`FACTORY_DRY_RUN` is still on). The rate limiter tripped exactly on
schedule — the 6th `/interest` request from this session's testing against
the same IP (including two earlier probe requests) returned 429, the rest
200/404 as expected.

## Risk-audit hardening, Phase 2: deterministic PyPI check ahead of every install — 2026-08-14

`requirements.txt` is now checked against the real PyPI registry — both that
every named package actually exists, and that it wasn't registered
suspiciously recently — before `pip install` ever runs. Bug Council's own
dependency reviewer already asks a model to flag "packages that are
abandoned or typosquat a popular name," but that's a judgement call, not a
verified fact; a hallucinated name plausible enough for the generator to
write can be plausible enough for the reviewer grading it to wave through.

Positioned specifically *before* install, not just before Bug Council:
Council convenes only after a build has already installed cleanly and passed
its tests, so it never actually sees a package that failed to install — a
hallucinated name today just fails the install with a generic
`dependency_failure` and the whole build regenerates from scratch. Catching
it here, before any network call the sandbox itself would make, keeps it a
one-line, surgically-repairable edit to `requirements.txt` instead —
`dependency_not_on_pypi` is the one `dependency_*` heal category now in
`SURGICAL_CATEGORIES`, since the diagnosis re-runs in-process (real network,
not the repair sandbox's, which has none) rather than needing a retry the
surgeon has no way to attempt.

A 404 from PyPI is definitive (fails closed); a transport failure fails
open, same posture as `website.py`'s robots.txt fetch — a PyPI outage must
never block every build in flight, and the install step itself is the real
backstop if a name really is bad. Results are cached per package name for 7
days (`pypi_package_checks`) — existence and registration date are facts
about the package, not about any one build.

**Verified**: local ruff/mypy clean. 16 new unit tests (`test_dependency_precheck.py`)
— PEP 503 normalization, every requirements.txt line shape (comments,
`-r`/`-e`/option lines, direct URLs, VCS refs, local paths, extras/markers),
a real-old-package pass, a nonexistent-package flag, a freshly-registered
flag (the actual slopsquatting scenario), fail-open on transport failure,
cache hit skips the network call, an expired cache entry refreshes, the
settings kill switch, and the empty-requirements short circuit. Plus a
regression guard in `test_surgical_repair.py` asserting
`dependency_not_on_pypi` is surgical and the other two `dependency_*`
categories are not. Local unit suite: 780 passed (same two pre-existing
Windows failures). Scratch-Postgres: full suite, 1048 passed.

**Deployed live**: committed, transferred via `git archive HEAD`, image
rebuilt, migration `0022` applied to the live database, service restarted.
`/health` returns 200. Confirmed live with a real (unmocked) call against the
real PyPI registry from inside the deployed container: `pydantic`/`requests`
passed cleanly, a deliberately hallucinated name was caught —
`totally-hallucinated-pkg-xyz-123 (no such package on PyPI)`.

## Risk-audit hardening, Phase 3: close the Discovery memory loop — 2026-08-14

Found live during the risk audit: `development/graph.py::_evaluate_validation`
wrote its validation-failure lesson at `product:{id}` scope, then retired the
product a few lines later — Discovery's own reader scope (`engine:discovery`)
never overlapped with that, so the single most valuable memory the factory
produces was write-only. Worse, the pass branch wrote no memory at all, so
the factory only ever learned from losses, never from what actually worked.
No migration — this is Discovery/Compliance's own read/write wiring into the
memory store that already existed (`memory/store.py`), not a schema change.

- `development/graph.py`: both outcomes of `_evaluate_validation` now also
  file a niche-level lesson at `engine:discovery`, alongside the existing
  product-scoped one (left untouched).
- `discovery/graph.py` + `score.py`: every cycle recalls prior
  `engine:discovery` lessons into the generator's and discriminator's
  *prompt* — never `system`, which is the tunable value
  `learning/prompts.py::get_prompt` serves; appending there would corrupt
  trial attribution for `GENERATOR_SYSTEM`/`DISCRIMINATOR_SYSTEM`. One
  aggregate lesson (dominant rejection niches and reasons) is written per
  cycle, not one per rejected idea — `remember()`'s near-duplicate
  reinforcement keeps recurring patterns from flooding the scope.
- `compliance/graph.py`: rejected/flagged ideas file a niche lesson at
  Discovery's scope. `compliance/assessor.py` deliberately does **not** read
  memory back into `IDEA_SYSTEM_PROMPT` — that would be a de-facto mutation
  channel into a safety-critical, permanently learning-loop-excluded prompt.
  It reads structured facts instead, via new
  `repository.prior_verdicts_for_niche` (lexical similarity match on past
  idea niches, since `upsert_idea`'s fingerprint dedup already prevents an
  exact-string repeat from ever existing) — a fact from our own table can't
  be poisoned by a prior model's prose the way a memory could, and it's
  framed to the model as evidence, not an auto-reject rule.
- `learning/maintenance.py`: the mutation-proposing analyst's episode summary
  now also carries a `recall_block` scoped to the allowlisted key's own
  engine (new `_engine_for_key` helper), so it sees more than a bare success
  rate when recommending a prompt/weights revision.

**Verified**: local ruff/mypy clean (same two pre-existing findings noted in
earlier phases — `UP037` in this same file, `RUF100` in `preflight.py` —
confirmed unrelated to this change). New `test_discovery_memory_loop.py`
(2 integration tests: a pre-seeded lesson reaches the prompt, never system,
byte-identical to the served tunable value; a rejected idea produces a new
`engine:discovery` lesson the next cycle can read) — `discovery/graph.py`'s
`build_sources` is monkeypatched to a fake in-memory `Source` so the test
never makes a real network call (`HackerNewsSource.available` is
unconditionally `True`, unlike the credential-gated sources). Two new
integration tests in `test_validation_gate.py` cover the pass/fail write
scope fix directly. Two new unit tests in `test_supervisor_and_compliance.py`
assert `IDEA_SYSTEM_PROMPT` reaches the model byte-identical and that
`factory.memory` is never imported by `compliance/assessor.py` at all — a
real, enforced check, not just a convention. Three new unit tests for
`_engine_for_key`. Local unit suite: 785 passed (same two pre-existing
Windows failures). Scratch-Postgres: full suite, 1057 passed.

**Deployed live**: committed, transferred via `git archive HEAD`, image
rebuilt (no migration to apply — `alembic upgrade head` confirmed already at
`0022`), service restarted. `/health` returns 200, boot log clean, all six
engines enabled.

## Risk-audit hardening, Phase 4: defensibility scoring dimension — 2026-08-14

`ScoredIdea` gains a fifth weighted dimension, `defensibility` (0.18 of the
weight), plus two informational, ungated enums — `platform_dependency` and
`replacement_mode`. The prior scoring contract rewarded a thin, easily-cloned
API wrapper the same as a defensible product: `build_feasibility`'s
instructions literally asked for "no proprietary data," which favors exactly
the AI-wrapper failure mode this dimension exists to catch instead.

External research on this phase was reviewed and independently fact-checked
before building: the schema-complexity and enum-collapse concerns were real
and adopted (see below); a proposed Fisher's-exact-test power critique for
the trial evaluator checked out numerically (simulated independently: 28.06%
power vs. the report's claimed 28.37%, for a 30%→15% survival-rate halving at
n=30) but was kept out of this phase's scope — it's a change to
`learning/trials.py`'s shared evaluator serving all six allowlisted keys, not
something specific to scoring weights, and deserves its own verification pass
against real score-variance data rather than being folded into an unrelated
migration.

**Schema**: `defensibility_reasoning: str` (defaulted `""`) placed immediately
before `defensibility: float` (defaulted `0.0`) — auto-regressive generation
is strictly left-to-right, so forcing the model to commit its reasoning to
the context window before the constrained numeric token reduces the risk of
"enum collapse" (a forced classification defaulting to a safe average under
structured decoding). `SCORE_DIMENSIONS: ClassVar[tuple[str, ...]]` is now
the single source of truth `weighted_score` loops over, replacing a hardcoded
4-term sum. New default weights (pain .25, WTP .25, adoption .20, feasibility
.12, defensibility .18) still sum to exactly 1.0, keeping the fixed
`SCORE_THRESHOLD = 0.55` calibrated. `platform_dependency`/`replacement_mode`
are recorded but never gated or weighted this phase, deliberately — a kill
rule before anyone has seen the real distribution of values would be the
rigid rule-following this whole hardening effort exists to avoid.

**Migration `0023_idea_defensibility`**: `ideas` gains all seven
per-dimension columns (today only the blended `score` survives — this makes
a future weights change backtestable against real historical ideas, which
was previously impossible). The live `SCORING_WEIGHTS` row is replaced
outright if it's still the untouched code default, otherwise every existing
weight is scaled by 0.82 and `defensibility` inserted at exactly 0.18 —
preserving whatever relative priorities the learning loop had already
discovered while returning the vector to a 1.0 sum. `DISCRIMINATOR_SYSTEM` is
replaced if untouched, otherwise the new instructions are appended rather
than overwriting tuned prose. Any in-flight `trial`/`candidate` row for
either key is retired (never deleted) — episodes collected under the old
4-dimension contract are statistically uninterpretable against the new one.
The `active` row itself is never retired, even when rewritten in place.
**Confirmed on the real production database before this deploy's service
restart**: both rows were still on version 1 (never mutated by the learning
loop yet), so both took the clean replace branch — verified directly via
`psql` that the live `SCORING_WEIGHTS` value now sums to 1.0 across all five
keys and `DISCRIMINATOR_SYSTEM`'s text matches the new prompt exactly.

**Guardrails**: `learning/prompts.py::get_scoring_weights` now falls back to
the code default (never raises) if a served row's dimension set doesn't
match — a stale row can no longer silently zero out `defensibility` for
every idea via `weighted_score`'s `weights.get(dim, 0.0)`. New
`supervisor/audit.py::check_scoring_contract_drift`, wired into the existing
`audit-snapshot-integrity` maintenance item, raises a Telegram-visible
interrupt on the same mismatch instead of leaving it silent forever.
`WeightsCandidate` (the learning loop's own weight-proposal schema) gained a
`defensibility` field — it very nearly stayed at four fields while
`ScoredIdea` moved to five, which is exactly the kind of half-done dimension
change a new parity test now guards against.

**Pre-merge requirement, satisfied**: a live, unmocked `discriminate()` call
against real Gemini (model `gemini-pro-latest`), run from the server using
the production API key — every existing test mocks the client, which is
exactly why a prior schema-size outage on this same array went undetected
until production (see this file's own earlier entry, `:249-266`). The
15-field schema (up from 12) completed in 28.9s with no error. Both scored
ideas came back with substantive, specific `defensibility_reasoning` text
("heavily dependent on fragmented third-party TMS platforms that often lack
modern APIs" / "Competitors cannot easily migrate this trust") rather than a
generic default — direct evidence the reasoning-before-score field ordering
is doing its job, not just theoretically sound.

**Verified**: local ruff/mypy clean (same two pre-existing, unrelated
findings noted in every earlier phase). New `test_idea_defensibility_migration.py`
(13 tests) loads the migration file directly via `importlib` (its module name
starts with a digit, so it can't be `import`ed normally) and tests the
rewrite logic in isolation — critically, asserts the migration's embedded
copy of the new `DISCRIMINATOR_SYSTEM` text is byte-identical to the real
code constant, and that the rescale math preserves both the sum-to-one
contract and the learning loop's relative weight ratios. `test_scored_idea_weights.py`
rewritten for the 5-dimension reality (every hardcoded arithmetic assertion
depended on the old 4-term sum). `test_learning_maintenance.py` gained the
`WeightsCandidate`↔`SCORE_DIMENSIONS` parity test plus updated call sites.
New integration coverage in `test_learning_store.py` (the dimension-mismatch
fallback), `test_snapshot_audit.py` (`check_scoring_contract_drift`, healthy/
drifted/no-duplicate-interrupt), and `test_repository.py` (`upsert_idea`
persists the per-dimension columns, and leaves them at their defaults for a
pre-Phase-4-shaped idea). Local unit suite: 806 passed (same two pre-existing
Windows failures). Scratch-Postgres: full suite, 1086 passed.

**Deployed live**: committed, transferred via `git archive HEAD`, image
rebuilt, migration `0023` applied to the live database (confirmed via direct
`psql` inspection, see above), service restarted. `/health` returns 200,
boot log clean, all six engines enabled.

## Post-Phase-4 fix: `discriminate()` never scaled `max_tokens` to batch size — 2026-08-14

Found live, not in a test: a 20-idea sample run against the real, deployed
discriminator (requested to see the new 5-dimension scorer's actual output
distribution) failed outright on the second attempt at a larger batch with
`SchemaViolation: gemini hit max_output_tokens before completing the JSON
object`. `discriminate()` never passed `max_tokens` at all, so every call
silently used the client's 8192-token default — fine for the old 11-field
`ScoredIdea`, insufficient for the new 15-field one (two ~800-char free-text
fields per idea) once a batch grew past a handful of ideas. Real Discovery
cycles route through this exact same call with no `try`/`except` around it
in `discovery/graph.py::run()`, so this was a live, uncaught-crash risk on
any cycle whose generator emitted more than a handful of ideas, not a test
artifact.

Fixed with `discovery/score.py::_scaled_discriminator_tokens(n)` — the same
shape as the already-established `compliance/assessor.py::_scaled_batch_tokens`
precedent, capped at the same 16,384 ceiling that function already trusts as
safe. `FakeProvider` now records `max_tokens` on every call so this is
actually testable, not just asserted against the pure scaling function in
isolation. 5 new tests (`test_discriminator_token_scaling.py`). Local unit
suite: 811 passed (same two pre-existing Windows failures). Scratch-Postgres:
full suite, 1091 passed. Deployed live (no migration): committed, image
rebuilt, service restarted, `/health` returns 200.

**Confirmed fixed**: reran the same 20-idea sample against the fixed live
code. All 20 scored in one real call (5991 output tokens, comfortably under
the new cap). Results: 3/20 survive under the new weights vs. 7/20 that
would have survived under the old ones — the defensibility dimension
correctly knocked 4 thin-wrapper ideas from survivor to reject, including a
PDF-to-CSV OCR wrapper explicitly flagged as "commodity... easily replicated
by OpenAI Vision or AWS Textract," the exact AI-wrapper failure mode this
dimension exists to catch.

## Risk-audit hardening, Phase 5: pricing flow-through + codegen contract gate — 2026-08-14

`self_serve_price_usd` was reasoned about extensively — it gates the PLG
constraint, it's part of what the discriminator scores — and then discarded;
nothing downstream ever read it again, so a generated product's real Stripe
Checkout amount had no connection to the number the discriminator and
Compliance already agreed on. Separately, nothing deterministically verified
that generated code actually implemented the contracts its own system prompt
asked for (a `client_reference_id` on Checkout sessions, a privacy page) —
only Bug Council's judgment stood between "the prompt asked for it" and "it's
actually there."

**External feedback on the plan was reviewed and adjusted before building**,
not accepted wholesale: a float-to-cents truncation concern was real but its
own illustrative example (`4.99 * 100`) didn't actually reproduce on real
Python — `19.99 * 100` does (`1998.9999999999998`, truncates to 1998 not
1999) and is a far more common real price point, so the fix (round before
cast) was kept and the example corrected. An AST-parsing suggestion for the
contract gate was downgraded to a tighter regex after checking `seo.py`'s own
documented precedent, which deliberately stays to cheap/unambiguous pattern
matching rather than real parsing. A test-environment-injection concern was
confirmed real by reading `_verify_and_ship`'s actual test-run call (its
`env=` carried only `PYTHONPATH`) — but reframed as a pre-existing gap
already latent for `PRODUCT_ID`/`STRIPE_API_KEY`, not something Phase 5
introduced, and fixed for all three at once.

**Pricing flow-through**: `ideas` gains `self_serve_price_usd` (migration
`0024_idea_price`); `ValidatedIdea` carries it through all three construction
sites (`extra="forbid"` makes a partial miss a hard failure, not a silent
default). `SYSTEM_PROMPT` now requires the Checkout amount come from a
`PRODUCT_PRICE_CENTS` environment variable, read at request time, never
hard-coded — same shape as the existing `client_reference_id`/`PRODUCT_ID`
contract. `deploy.py::_runtime_env` injects it as integer cents via the new
`_price_cents()` helper, which rounds before casting.

**The codegen contract gate** (`development/contract.py`) — same
architectural slot as `seo.py`, right after it (after tests pass, before Bug
Council convenes): checks `PRODUCT_PRICE_CENTS` is actually read (not just
named in a comment — the regex requires an actual `os.environ[...]`/
`os.getenv(...)` call), `client_reference_id` is referenced, the analytics/
interest beacon is wired to this product's own URL, and a privacy page
exists and is linked. `is_validation` (already threaded through
`_verify_and_ship` for the landing-page/real-MVP split) decides which facts
even apply — a validation page has no Stripe wiring at all by design, so
checking for it there would false-positive on every single validation build.
New `contract_incomplete` heal category, added to `SURGICAL_CATEGORIES`. Both
prompts now also require a standalone `privacy.html` page.

**The test-run environment gap, closed for all three vars**: `_verify_and_ship`'s
sandboxed test-run `env=` now carries dummy `PRODUCT_ID`/`STRIPE_API_KEY`/
`PRODUCT_PRICE_CENTS` values. Every prompt says to read these "at request
time" specifically so a lookup never fires during test collection, but that's
a prompt convention a model doesn't have to obey perfectly, not a guarantee.

**Verified**: local ruff/mypy clean (same pre-existing, unrelated findings
noted in every earlier phase). New `test_codegen_contract.py` (16 tests,
including that a `# TODO: PRODUCT_PRICE_CENTS` comment does *not* satisfy the
check — the whole point of requiring an actual read pattern). New price-cents
tests in `test_deploy_env.py`, including the corrected `19.99` truncation
case. Fixing this phase's own contract gate broke three pre-existing test
files that share `_verify_and_ship`'s pipeline for unrelated reasons
(`test_seo_gate.py`, `test_devops_error_routing.py`,
`test_domain_lifecycle_integration.py` — the last one only caught by the
scratch-Postgres run, not locally, since it needs real Postgres) — one of
those, a redeploy test, was silently passing for the wrong reason (it never
asserted the redeploy actually succeeded, only that no domain-request
interrupt fired, which a healed-not-deployed build also satisfies); fixed the
assertion alongside the workspace data. Local unit suite: 839 passed (same
two pre-existing Windows failures). Scratch-Postgres: full suite, 1123
passed.

**Deployed live**: committed across two commits (the feature, then the
pre-existing-test fixes surfaced by scratch-Postgres), transferred via
`git archive HEAD`, image rebuilt, migration `0024` applied to the live
database, service restarted. `/health` returns 200, boot log clean, all six
engines enabled.

## Risk-audit hardening, Phase 6: subscriptions, dunning, and churn — 2026-08-14

Nothing downstream of Stripe ever recognized a subscription as distinct from
a one-time payment — no MRR, no churn signal, no way for the factory to
notice a product bleeding customers. Inert until a real recurring sale
exists (no generated product creates a Stripe Subscription yet — Phase 5
only wired up a one-time Checkout amount), but the ingestion side is ready
the moment one does, without another migration in the critical path then.

**External feedback was reviewed across three rounds before building**, each
adjusted rather than accepted or rejected wholesale:
- *Round 1*: confirmed the FK-avoidance and append_ledger-invariant-as-test
  points were already the plan; adopted the `churn_kind`-only-on-`.deleted`
  refinement and the `.updated`-must-refresh-price nuance, both genuine gaps
  in the original design's precision, not just restatements of it.
- *Round 2* (webhook latency): the recommended fix (keep the webhook handler
  DB-state-only, defer the memory write to the maintenance tick) was
  adopted, but not for the reason given — `memory_store.remember` is lexical,
  no LLM call, no vector index (confirmed by re-reading Phase 3's own module
  docstring), nowhere near a real timeout risk. Adopted instead because
  every other handler in `finance/webhooks.py` already keeps to that same
  narrow lane, and introducing the first-ever memory write inside an HTTP
  handler would be a real architectural departure independent of today's
  actual latency.
- *Round 3* (Discovery feedback loop): pushed back on "discard involuntary
  churn as noise" — a meaningful share of involuntary churn is voluntary
  churn in disguise (a disengaged customer who never bothers updating a
  failed card), the factory's niche-level sample sizes are too small to
  cleanly separate the two anyway, and unilaterally deciding in code what
  Discovery gets to see would contradict the same "evidence, not an
  auto-reject" principle already built into Compliance's own memory design
  (Phase 3c). Adopted instead: both churn kinds write a memory, distinctly
  worded so the model does the weighing.
- Adopted outright, no pushback: the zombie-dunning-row fix (a subscription
  that never recovers and is eventually deleted must still close its open
  dunning row, not leak it forever).

**Schema (`0025_subscriptions`)**: `customers`/`subscriptions`/
`dunning_events`, plus `stripe_subscription_id`/`stripe_customer_id` as
plain nullable columns on `ledger` (not FKs — Stripe webhook delivery order
across different event types is not guaranteed, and a FK would reject real
money on a benign race, same reasoning `ledger.product_id` already
established). `customers`/`subscriptions` store no personal data, mirroring
`Lead`'s own minimisation stance — MRR/churn math needs only the Stripe ids,
and the actual PII stays inside Stripe, where GDPR erasure for that person
already has to happen regardless.

**`finance/webhooks.py`**: `customer.subscription.created/updated/deleted`
and `invoice.payment_failed` handled as pure state notifications — none of
them ever call `append_ledger`. `churn_kind` is set exclusively on the
terminal `.deleted` event, reading `cancellation_details.reason` — never on
`.updated`, which only means `cancel_at_period_end` was toggled, a
still-reversible scheduled cancellation Stripe allows a customer to reverse
before the period ends. `.updated` refreshes `price_cents`/`status` on every
delivery so `mrr_cents` never silently drifts from reality. Dunning rows
close on recovery (`invoice.paid`) or on the subscription reaching a
terminal state (the zombie-row fix) — a customer who never recovers would
otherwise leak an open row forever.

**`finance/churn.py`**: writes a niche-level Discovery memory for churn,
deliberately deferred out of the webhook handler's request/response cycle
(see Round 2 above). New `review-churn` maintenance item (24h cadence)
spawns the existing `GROWTH_ANALYST` role once per currently-churning
product — no new role, no new tool, just a churn-motivated reason to look
with the same agent that already reviews traffic/AB-test state.

**Metrics/autonomy surfacing**: `FactoryMetrics` gains `mrr_cents`,
`active_subscriptions`, `churn_rate_30d`, `involuntary_churn_30d`,
`voluntary_churn_30d`. `FactorySnapshot` gains `churning_products`, also
folded into the routing fingerprint — a product starting to churn is exactly
the kind of decision-relevant change that should end a repeat streak and
prompt fresh deliberation, the same reasoning already applied to
`open_interrupts`/`approved_ideas_waiting`. Before this, the Orchestrator
had no way to see a product bleeding customers, so it could never choose to
act on one.

**Verified**: local ruff/mypy clean (same pre-existing, unrelated findings
noted in every earlier phase). New `test_subscription_lifecycle.py` (18
tests) covers created/updated/deleted correctness, the `cancel_at_period_end`
-vs-actual-churn distinction, dunning open/bump/recover/zombie-close, MRR/
churn metrics, and — the most important test in the phase — an executed
invariant asserting `append_ledger` is never called for any subscription or
dunning event (monkeypatched to raise if it is, run across every event
type). New `test_churn_review.py` (5 tests) covers both churn kinds getting
distinctly-worded lessons, unattributed churn being skipped-but-marked, the
`memory_written_at` dedup, and `review_churn` spawning `GROWTH_ANALYST`
exactly once per churning product. Two real bugs were caught only against
real Postgres, not locally: a `recall()` Jaccard-relevance false negative
(a single matching token against a ~35-token sentence can land below
`MIN_RELEVANCE` even though the token is genuinely present — fixed by
asserting against the `Memory` table directly instead of through the fuzzy
lexical-recall heuristic, which was never the right tool for that
assertion) and a substring collision in the test's own niche names
(`"involuntary-..."` literally contains `"voluntary-..."`, so the `in` check
for one matched the other's row). Local unit suite: 840 passed (same two
pre-existing Windows failures). Scratch-Postgres: full suite, 1144 passed.

**Deployed live**: committed across three commits (the feature, then two
scratch-Postgres-only test fixes), transferred via `git archive HEAD`, image
rebuilt, migration `0025` applied to the live database, service restarted.
`/health` returns 200, boot log clean, all six engines enabled — and the new
`review-churn` maintenance item was already picked up and enqueued on the
very first tick after restart.

## Risk-audit hardening, Phase 7: datacenter-proxy fallback + channel diversification — 2026-08-17

The last phase of the plan. Two independent halves: a narrow, honestly-scoped
mitigation for the datacenter-IP blocking diagnosed live in an earlier
session (see the `project_scraping_datacenter_ip_blocking` memory), and —
the higher-value half — a distribution system that tries a different channel
on its own before ever asking a human.

**The compliance line held throughout.** `compliance/assessor.py`'s
`IDEA_SYSTEM_PROMPT` (permanently immutable) rejects any idea requiring
"circumventing authentication or rate limits." A rotating residential proxy
evading a deliberate datacenter-IP block is, read fairly, exactly that — the
factory doing to a third party what it refuses to let a generated product
do to anyone else. So the real fix (residential/rotating proxies) was
refused on purpose, and what shipped instead is deliberately narrower:
datacenter-class egress proxy only, never residential/rotating, no
User-Agent spoofing, and — the guardrail that mattered most — a 429 must
never trigger a proxy retry anywhere, since that would be functionally
evading the rate limit itself. Stated honestly to the operator, not
oversold: a plausible partial mitigation for simple IP-reputation blocking,
not a fix for ASN-class bot defense (Akamai/Cloudflare-style).

**External feedback was reviewed across three rounds before building**, each
verified against the actual code rather than accepted at face value:
- *Round 1* (burned-timeout inefficiency): confirmed the real cost
  (`TIMEOUT = httpx.Timeout(20.0, connect=5.0)` — up to 5s connect, 20s
  read per doomed direct attempt, correcting the feedback's own imprecise
  "10-30s" claim) and adopted `ScrapeDomainFailure.requires_proxy` so a
  domain already known to need the proxy skips the direct attempt entirely
  on every later fetch.
- *Round 2* (the 429 circumvention trap): confirmed `_fetch_page_text` had
  zero existing 429 handling before this phase — a 429 fell into the
  generic "not 200" branch and was silently swallowed. Adopted an explicit
  `_RETRIABLE_VIA_PROXY = frozenset({"timeout", "forbidden"})` allowlist
  that excludes `"rate_limited"` by design, not by omission.
- *Round 3* (context-blind channel scouting): confirmed `IngestReport` — the
  object that actually knows *why* a harvest found nothing — is genuinely
  ephemeral, built fresh per `_harvest()` call and discarded once the
  immediate credential-problem check consumes it. `report.summary()` already
  produced exactly the needed text; it was just being thrown away. Adopted a
  new `Product.last_lead_sourcing_summary` column instead of inventing new
  plumbing — `SpawnRequest.context` already existed for exactly this kind of
  hand-off.
- Direct question answered the same way the feedback framed it: extend
  `ScrapeDomainFailure` rather than add a new table — same primary-key grain
  (`domain`), avoids an extra join for what's conceptually one fact about
  one domain.

**Schema (`0026_scrape_proxy`)**: `scrape_domain_failures` gains
`requires_proxy`/`proxy_failure_count` (a separate counter from the
existing `failure_count`, so a proxy failure never counts toward blocking a
domain on its *direct* threshold — a different egress IP was never going to
fix a proxy-side failure anyway). `products` gains
`last_lead_sourcing_summary` (truncated to 500 chars).

**New `integrations/outbound.py`**: the canonical owner of `USER_AGENT`/
`TIMEOUT`/the plain client constructor, moved here from
`sources/base.py` (which now delegates via a one-line alias, `client()` ->
`direct_client()`, so every existing caller — the fixed HN/GitHub/Reddit/
Product Hunt sources — keeps behaving identically). `proxy_client(settings)`
returns `None` when `SCRAPE_PROXY_URL` is unset, matching the codebase's
existing "missing optional credential degrades gracefully" idiom rather than
raising.

**`website.py`**: `_fetch_page_text` now returns a 3-tuple
(`page_text, block_reason, used_proxy`). A domain flagged `requires_proxy`
skips the direct attempt outright; otherwise a direct failure classified as
`"timeout"` or `"forbidden"` gets exactly one retry through the proxy, and
`"rate_limited"` (429) is never retried under any code path — checked by a
dedicated test. The same `_FETCH_LIMITER` gates both the direct and proxy
attempts, since it protects the *target site*, not this factory's own egress
point. `search()` bulk-fetches `proxy_required_domains` alongside the
existing `blocked_scrape_domains`, threads `requires_proxy` through both the
robots check and the fetch, and the first time a domain is seen succeeding
via the proxy, calls `mark_requires_proxy` so every later fetch from that
domain skips the doomed direct attempt too.

**Channel diversification** — the higher-value half:
- `products_needing_channel_scout` now also covers `VALIDATING` products,
  not just `LIVE` ones — a validating product blocked on its one channel
  previously had no periodic net at all, even though it needs the
  "try another channel" backstop more than an established live product does.
- `scheduler.py`'s stalled-validation sweep gained a middle rung: first
  strike retries `HARVEST_LEADS` once (unchanged); second strike now
  enqueues `CHANNEL_SCOUT_REVIEW` instead of escalating immediately; only a
  third strike (retry *and* channel scout both found nothing) raises
  `LEAD_SOURCING_STALLED` to a human. The system tries a different route on
  its own before ever asking — a direct autonomy increase, and a better
  answer to "distribution blocked" than the proxy is.
- `_harvest()` now writes `last_lead_sourcing_summary` on every call;
  `_channel_scout_review()` reads it and passes it as the spawned
  `CHANNEL_SCOUT` agent's `context`, so the scout reasons from *why* the
  last channel came up empty instead of blind.
- `_measure_channel_experiment()` now writes a second, niche-scoped memory
  (`scope_for(engine=EngineName.MARKETING)`) alongside its existing
  product-scoped one — the product-scoped memory dies with that one product,
  but a niche-scoped outcome is what `CHANNEL_SCOUT`'s next spawn on a
  *different* product in the same niche can actually draw on
  (`agents/spawner.py::spawn` already recalls on every spawn — this was the
  write side of a loop that was silently product-scoped only, the same gap
  Phase 3 fixed for validation outcomes).

**`settings.py`/`preflight.py`**: new `SCRAPE_PROXY_URL` credential,
`Severity.DEGRADES`, no engine disabled by its absence — Marketing works
direct-only without it, exactly as it did before this phase.

**Verified**: local ruff/mypy clean (same two pre-existing, unrelated
findings noted in every earlier phase — `UP037` in `learning/maintenance.py`,
`RUF100` in `preflight.py`). New `test_outbound_proxy.py` (11 tests) covers
client construction and `_fetch_page_text`'s full retry sequencing in
isolation, including the two critical guardrails as dedicated tests: 429 is
never retried via proxy, and `requires_proxy=True` skips the direct attempt
entirely. `test_website_provider.py` extended with `search()`-level proxy
wiring tests (`requires_proxy` threaded through, first-time proxy success
marks the domain, a proxy-path failure records `via_proxy=True`) and its
existing mocks updated for `_fetch_page_text`/`_robots_allowed`'s new
3-tuple/keyword-only signatures. New integration coverage across
`test_stalled_validation.py` (the three-strike ladder, `has_attempted_
channel_scout`), `test_lead_sourcing_escalation.py` (the summary write),
and `test_growth_roles_integration.py` (the widened channel-scout sweep,
context passed into the scout's prompt, the niche-scoped memory write).
Local unit suite: 856 passed (same two pre-existing Windows failures).
Scratch-Postgres: full suite, 1167 passed, no failures.

**Deployed live**: single commit, transferred via `git archive HEAD`, image
rebuilt, migration `0026` applied to the live database, service restarted.
`/health` returns 200, boot log clean, all six engines still enabled — the
only new preflight finding is the expected `SCRAPE_PROXY_URL is not
configured` degrading-credential interrupt, delivered once over Telegram,
exactly as designed.

## Risk-audit hardening, Phase 8: context-saturation budget — 2026-08-17

The last phase of the plan. No migration, no schema, no new credential —
pure code, and the smallest-surface phase of the nine, but it closed a real
gap found by tracing the existing cost-cap code rather than just following
the plan doc's own description of it.

**The problem, concretely**: `AgentRuntime.run` had exactly one growth
control, `max_steps` (6-14 depending on role). Nothing tracked the actual
size of the accumulated transcript, and it only ever grows — two turns
appended per step, capped per-result (`max_result_chars`, 20k default /
6k on the repair path) but never capped in aggregate. A long run with
several sizeable tool results could saturate the real context window with
no controlled stop — unlike cost, which already had one.

**A second, sharper gap found while tracing "the repair path caps cost"**:
`AgentRole` had no `cost_cap_usd` field at all. `spawn()` never passed one
for any of the eight roles it reaches (RESEARCHER, ANALYST, REPAIR_ENGINEER,
AUDITOR, LEAD_SCOUT, GROWTH_ANALYST, CHANNEL_SCOUT, DISCOVERY_SCOUT — all
fully uncapped). The one place a cap existed was `repair.py`'s direct
`AgentRuntime.run()` call (bypassing `spawn()` entirely), hardcoded to the
single global `settings.repair_agent_cost_cap_usd` for WORKSPACE_SURGEON/
SEO_OPTIMIZER only. Confirmed precisely by reading the call site, not
assumed from the plan document's own paraphrase of it.

**What shipped**:
- `AgentRole` gains `cost_cap_usd: float | None` and
  `max_context_chars: int | None`, sized per role — proportional to each
  role's step count and typical tool-result weight, generous headroom above
  any observed normal run (a safety net against a runaway loop, not a tight
  budget). REPAIR_ENGINEER highest at `$1.00`/`300_000` chars (14 steps,
  sandbox+git+http, the widest tool set of anything not gated behind a
  product lock); AUDITOR lowest at `$0.15`/`100_000` (6 steps, read-only
  health checks). WORKSPACE_SURGEON/SEO_OPTIMIZER keep `cost_cap_usd` at
  `None` on purpose — they never reach `spawn()` (see
  `WORKSPACE_SCOPED_ROLES`), and `repair.py`'s existing, separately-tuned
  `repair_agent_cost_cap_usd` must not be silently shadowed by a second
  value; `max_context_chars` has no equivalent pre-existing setting, so it
  lives on the role and `repair.py` reads it directly.
- `runtime.py`: `max_context_chars` tracks the transcript's character count
  (chars, not tokens — matching the existing `max_result_chars` convention)
  every step, recording `peak_context_chars` regardless of whether a budget
  is set, so the instrumentation is useful even for roles that never hit
  it. On exceeding budget: compact deterministically first —
  `_compact_to_budget` drops the oldest droppable tool-result turns' content
  (replaced with an elision marker, same honesty principle as
  `_truncate_middle`'s own), oldest first, stopping as soon as back under
  budget. Never touches the first turn (the objective, and any recalled
  memory/context joined into it) or the last two turns (the model's own
  most recent action and its result) — so a compacted run never loses its
  objective and the model is never asked to continue from a state it cannot
  see. Only stops the run (`stopped_because = "context_budget"`, same shape
  as `"cost_cap"`) when nothing left to drop still leaves it over budget —
  a pathologically large objective, or a single turn that alone exceeds the
  whole configured budget. No LLM summarization anywhere in this — costs
  money and can lose the diagnosis, the same reasoning that kept this
  deterministic everywhere else in the codebase.
- `spawner.py`: `spawn()` now threads `cost_cap_usd`/`max_context_chars`
  from the role. `cost_cap_usd` is priced against `model_code` only for
  CODE_GENERATION/CODE_REVIEW roles — the only ones `router.NO_FAILOVER`
  serves deterministically from one configured model; every other role's
  task type can fail over between providers mid-run, so there is no single
  correct model to price against, and `None` lets `_spent_usd` fall back to
  its existing mid-range default price (errs toward stopping early, not
  overspending).
- `repair.py`: threads `max_context_chars=role.max_context_chars` through
  the same direct `AgentRuntime.run()` call that already applies
  `settings.repair_agent_cost_cap_usd` — a one-line addition next to the
  existing cost-cap line.

**Framing**: the same honesty principle as `_truncate_middle` — this is a
capability increase, not a new restriction. A run that would previously
have hit the window and returned a truncated answer, or crashed on a raw
provider context-length error, now degrades in a controlled way and keeps
working.

**Verified**: local ruff/mypy clean (same two pre-existing, unrelated
findings noted in every earlier phase). New coverage in
`test_agent_runtime.py`: `_turn_chars`/`_context_chars` as pure functions,
three white-box tests of `_compact_to_budget` (drops oldest-first, stops
once satisfied, is a no-op when nothing is droppable), and four
`AgentRuntime.run`-level tests (no budget set leaves the run governed only
by steps; a tight budget compacts and lets the run reach its own natural
completion — verified via `peak_context_chars` crossing the budget as proof
compaction actually fired, not that the budget was simply never exceeded;
the pathological case stops cleanly with `context_budget`). New coverage
in `test_agent_spawner.py`: a role-catalogue invariant (every role declares
`max_context_chars`; every `spawn()`-reachable role declares `cost_cap_usd`,
the two workspace-scoped roles do not) and two wiring tests proving
`spawn()` actually passes the role's values through, including that an
ANALYSIS-type role prices against no fixed model while a CODE_GENERATION
role prices against `model_code`. One matching wiring test added to
`test_surgical_repair.py` for `repair.py`'s own direct call site. Local
unit suite: 867 passed (same two pre-existing Windows failures).
Scratch-Postgres: full suite, 1180 passed, no failures — the fresh scratch
database also confirmed this phase adds no migration (chain still ends at
`0026`).

**Deployed live**: single commit, transferred via `git archive HEAD`, image
rebuilt, service restarted (no migration to apply). `/health` returns 200,
boot log clean, all six engines still enabled, no new preflight findings —
expected, since this phase touches no credential or schema surface.

This closes the nine-phase risk-hardening plan.

## Operator dashboard: wired the mocked Next.js UI onto the real backend — 2026-08-17

Jesse had a polished Next.js/React dashboard (`dashboard/`, built via Google AI
Studio) sitting unused — every number on every screen was `Math.random()`-simulated
client-side, no backend connection, no authentication anywhere in the app's
human-facing surface. Wired it onto the real factory: a new authenticated
`GET /dashboard/state` endpoint, a real session-cookie login, and every view
component reconciled field-by-field against the real schema instead of the
mock's invented one.

**Backend** — `observability/dashboard.py::collect()` assembles a
`DashboardSnapshot` from real rows: `metrics.collect()` reused wholesale, plus
new repository functions (`recent_tasks`, `tasks_in_flight_by_engine` —
lease-aware, excludes a stale claim from an already-reclaimed worker,
`products_with_idea`, `mrr_by_product`, `latest_ledger_entry`),
`learning/store.py::list_all` (no status filter, unlike `list_trials`), and
`observability/costs.py::provider_outcome_rates` for a new provider-health
heuristic (0 calls -> up unless the credential itself is unconfigured; <10%
error -> up; 10-50% -> degraded; >=50% with >=3 calls -> down). Per-trial
significance reuses `learning/trials.py::_evaluate_one`'s exact
`fisher_exact_less` call shape — the raw p-value is surfaced, not a fabricated
"confidence %". `GET /dashboard/state` (`api/dashboard.py`) shares the same
bearer-check dependency `/metrics` now uses (`api/auth.py::require_bearer_token`,
extracted from the inline check that used to live only in `metrics_endpoint`),
gated by a new `DASHBOARD_AUTH_TOKEN` setting — 503 unset, 401 wrong, same
fail-closed shape as `METRICS_AUTH_TOKEN`. Two settings fields added mid-pass
once the frontend needed real values instead of fabricated ones for two
safety-relevant numbers: `HealthSection.daily_llm_spend_cap_usd` (backs the
budget-circuit-breaker card) and `MarketingSection.spam_complaint_rate_threshold`
/ `bounce_rate_threshold` (back the deliverability-circuit-breaker card) — both
already existed as `Settings` fields, just not previously surfaced to any API
response.

**Frontend** — `hooks/use-factory-stream.ts` rewritten completely: polls
`GET /api/dashboard` every 5s (was 450ms, tuned to make fake numbers look
alive) instead of generating random values; every exported type now matches
the backend's real snake_case JSON field-for-field. Auth:
`lib/session.ts` signs a stateless session cookie via Web Crypto
`SubtleCrypto` HMAC-SHA256 (has to run identically in `middleware.ts`'s Edge
runtime and the Node-runtime route handlers), keyed off
`sha256(DASHBOARD_AUTH_TOKEN + \":session\")` — the raw token is never put in
the cookie. `middleware.ts` gates everything except `/login` and `/api/auth/*`;
`lib/factory-api.ts` is the only place that ever holds the real token or talks
to the factory container, guarded by the `server-only` import so an accidental
client-side import fails the build rather than leaking the token into a
browser bundle.

Every view component was reconciled against the real data model rather than
kept on the mock's invented one — the mock's `Product.stage` taxonomy
(`DISCOVERY|COMPLIANCE|VALIDATION|BUILD|LIVE`) doesn't exist on `Product` at
all; Discovery and Compliance are things an *Idea* goes through before a
`Product` row exists. `SystemFlowVisualizer`'s "Market Intel" and "Compliance"
nodes were repurposed to show real `ideas_by_status` counts and the real
`planned` product bucket rather than being deleted or fed fake data.
`PortfolioPipeline`'s stage columns now use the real 7-value `ProductStatus`
enum. `LearningLoop` shows the raw Fisher's-exact p-value instead of a
fabricated confidence percentage. `Interrupts`' "Approve (Override)"/"Reject &
Requeue" buttons had no `onClick` at all in the original mock — disabled with
a tooltip pointing at the real resolution path (Telegram) rather than left
looking functional; the fabricated per-interrupt "Diagnostic Payload" stack
trace (identical fake text on every card) was deleted, replaced by the real
`headline`/`detail`/`reply_hint` fields. `SupervisorLogs.tsx` was fully built
but never imported or routed anywhere — confirmed still orphaned and deleted,
since `SystemHealthView`'s "Recent Activity" panel (synthesized from real task/
interrupt rows, not a log tail — no log table exists anywhere in this codebase)
already covers the same ground with real data.

**The most load-bearing fix**: `FinanceSpend.tsx` had two rows that were
*always* fake-positive regardless of real state — "Negative Stripe Balance
Detected -> FALSE - CLEAR" (hardcoded, no query behind it at all) and "Ledger
Sync Status -> SYNCED" (same). Shipping either unchanged would mean the
dashboard permanently lies about system health, in the specific worst
direction (silently claiming "clear" during a real problem) that defeats the
entire point of a monitoring surface. Replaced with the real thing that
exists: current ledger balance + reinvest pool (`repository.current_balance`),
and real ledger-write recency (new `repository.latest_ledger_entry`) rendered
as "Xm ago" instead of a static claim.

**Deployment** — new `dashboard/Dockerfile` (Bun install -> `next build`
with `output: 'standalone'` already set in `next.config.ts` -> slim
`node:20-alpine` runtime, non-root user, standalone server), new
`dashboard` service in `docker-compose.yml` matching the existing
loopback-bind pattern (`127.0.0.1:3000:3000`), and a new
`dash.{$PUBLIC_DOMAIN}` Caddy site block plus a defense-in-depth 404 for
`/dashboard/*` added to the existing `/metrics` block on the main domain (the
dashboard is only ever served from the subdomain). `DASHBOARD_AUTH_TOKEN`
generated via `openssl rand -hex 32` and added directly to the server's
`.env` over SSH — never pasted into chat.

**Verification, in order**: `ruff check`/`ruff format`/`mypy src/factory`
clean (only the two permanent baseline findings); `bun run lint`/
`bunx tsc --noEmit`/`bun run build` clean on the dashboard side (one real
lint catch along the way — `SystemFlowVisualizer.tsx` carried over the
mock's `useEffect(() => setMounted(true), [])` hydration-guard pattern
unchanged, which `eslint-plugin-react-hooks`'s newer `set-state-in-effect`
rule flags; removed it since nothing in the component is actually
nondeterministic between server and client anymore). Local unit suite: 873
passed (same two permanent Windows-only path-separator failures). New
integration test file (`tests/integration/test_dashboard_state.py`, 11 tests
covering every new repository function plus `collect()` assembling a full
snapshot) run against a genuine scratch Postgres — provisioned fresh on the
server (`docker run postgres:16-alpine` on the `superagent_default` network,
source copied to `/opt/scratch-dashboard-test`, `alembic upgrade head` run
clean through all 26 revisions, `.[dev]` installed fresh in a throwaway
`python:3.13-slim` container) rather than against the live database — all 11
passed; scratch container and directory torn down afterward.

**Deployed live**: committed locally, transferred via `git archive HEAD`
(this repo has no configured git remote — `/opt/superagent` on the server is
a plain synced tree, not a git checkout; `git archive` only carries tracked
files, so it never touches the server's `.env`, `.env.bak-*` backups, or
anything not committed). Both `factory` and `dashboard` images rebuilt,
`docker compose up -d` recreated `factory` and created `dashboard`; Caddy's
Caddyfile is a read-only bind mount so compose doesn't detect the content
change as requiring a container recreate — reloaded explicitly with
`docker exec superagent-caddy-1 caddy reload`. Verified end-to-end against
the running containers: `/health` 200, `/dashboard/state` returns real
production data (411 done tasks, 5 open interrupts, real MRR/balance/spend
figures) and correctly 401s with no or a wrong token; full login -> session
cookie -> authenticated `/api/dashboard` -> logout -> re-redirect flow tested
against the dashboard container directly; Caddy confirmed routing
`dash.manifestaudit.com` to the dashboard site block and blocking
`/dashboard/*` on the main domain, both via `Host`-header curl against
loopback (real HTTPS not yet reachable — no DNS record for the subdomain
exists yet).

**Not done, needs Jesse**: add the `dash.manifestaudit.com` DNS A record
pointing at the droplet's IP. Caddy will provision its own Let's Encrypt cert
automatically on first real request once that resolves — nothing else to
configure server-side.
