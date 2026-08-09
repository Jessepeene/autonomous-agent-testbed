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

- [ ] Docker + Docker Compose installed and confirmed working (`docker info`).
- [ ] Repo cloned onto the host.
- [ ] Workspace host directory created and owned by the sandbox's fixed UID:
      `sudo mkdir -p /srv/factory-workspaces && sudo chown 10001:10001 /srv/factory-workspaces`
      (path adjusted if we changed `WORKSPACE_HOST_PATH`/`WORKSPACE_ROOT`).
- [ ] (Optional) Pre-baked sandbox image built and pushed
      (`docker/sandbox.Dockerfile`), `SANDBOX_IMAGE` noted for `.env`.

## Phase 2 — Configuration

- [ ] `.env` created from `.env.example`.
- [ ] `DATABASE_URL` set.
- [ ] At least one of `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` set.
- [ ] `WORKSPACE_ROOT` and `BLACKLIST_PATH` set explicitly (container defaults are
      wrong otherwise — this is not optional for a containerized deploy).
- [ ] `DAILY_LLM_SPEND_CAP_USD` set to a real number (unset = breaker is inert).
- [ ] Telegram: bot created, chat id discovered, both set in `.env`.
- [ ] Mailgun: domain added + DNS verified, API key, domain, webhook signing key set.
- [ ] Stripe: API key + webhook secret set (if revenue is in scope for this pass).
- [ ] Vercel/Cloudflare token set (if Development deploys are in scope for this pass).

## Phase 3 — Database

- [ ] `docker compose up -d postgres`, healthy.
- [ ] `docker compose run --rm factory alembic upgrade head` — succeeds.
- [ ] `alembic heads` shows exactly one head: `0007_deliverability`.
- [ ] **First-ever run of the integration suite against a real database**:
      `DATABASE_URL=<url> pytest tests/integration -q` — record pass/fail count here.
      - Result: ______________________

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

(Running log — add as we go.)

-
