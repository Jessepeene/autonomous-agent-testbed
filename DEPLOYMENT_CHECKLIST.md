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

All thirteen fixes (1 ON CONFLICT bug, 5 test-fixture bugs, 3 Docker-sandboxing
bugs, 2 Gemini schema bugs, and 1 stale-model-ID config fix) are applied,
verified against the real server, and ready to commit. Item 13 (quota/billing)
is open and blocking the rest of Phase 5.
