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
- [ ] Telegram: bot created, chat id discovered, both set in `.env`.
- [ ] Mailgun: domain added + DNS verified, API key, domain, webhook signing key set.
- [ ] Stripe: API key + webhook secret set (if revenue is in scope for this pass).
- [ ] Vercel/Cloudflare token set (if Development deploys are in scope for this pass).

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

- [ ] `docker compose run --rm factory python -m factory.preflight` run.
- [ ] No HARD_BLOCK findings remain.
- [ ] `SANDBOX_WORKSPACE_SHARING` specifically checked — this is a new, real probe
      -container test, not a config assumption. Pass/fail: ___________
- [ ] Every DEGRADES finding reviewed and accepted or fixed on purpose (not just
      ignored by default).

## Phase 5 — Dry run (`FACTORY_DRY_RUN=1`, the default — confirm it's still 1)

Watching `docker compose logs -f factory` throughout. Each of these is a
first-contact-with-reality check for something built this session:

- [ ] At least one full Discovery → Compliance → Development cycle completes.
- [ ] At least one Bug Council review runs and its verdict looks sane.
- [ ] A LEAD_SCOUT sourcing-refinement run appears in logs and terminates cleanly
      (not step-limited every time).
- [ ] An entity-resolution run appears in logs; verdict is plausible for a test lead.
- [ ] Mailgun test webhook sent from their dashboard (Sending → Webhooks → pick
      "complained" → Test webhook) — confirm `/webhooks/mailgun` logs a *handled*
      event, not "event type not tracked". This is the one piece flagged as
      unverified against a real payload shape.
      - Result: ______________________
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

**Pending**: these fixes are applied and verified (rebuilt image, full suite
green) but not yet committed to git — will do that as one commit once we've
finished this phase of the checklist, unless we hit more before then.
