# autonomous-agent-testbed

**Research question**: can an autonomous LLM system operate under real stakes —
real money, real deployments, live on the public internet — if its safety rules
are enforced in code rather than prompts?

**Honest outcome**: it ran in production for 37 days (2026-08-18 to 2026-09-24).
In that time it built at least 4 products (3 still exist in the final database;
one earlier one, `manifestaudit.com`, is documented throughout the project's own
history but has no row left in the database this repo's numbers are drawn from).
It earned **€0** — no product ever reached a paying customer. It made 95,271
model calls for $94.74 across three providers, reached 9 release decisions —
6 of which its own gates blocked outright (`fix_first`/`rethink`/`abandon`) —
and produced 57 logged production incidents (`BUG_LOG.md`), against 1,392 unit
tests passing in CI. Outbound email was built and gated but never enabled: no
real email was ever sent (the only 2 composed messages were self-tests to an
unmailable `.invalid` address), and no real person ever submitted a form (the
only 2 sign-ups on record were self-tests too). It is now being taken offline.

All of the above is measured, not estimated — see `docs/results-2026.md` for
the exact SQL run against the production database and the date it was
collected.

## How this was built

Most of the code was written by AI coding agents (mainly Claude Code),
working under my direction. I set the research question and architecture,
decided which safety rules the system needed and that they be enforced in
code rather than prompts, drew the lines where a human must stay in charge
(the system may never retire a product or sign up for a service on its
own), and steered the fixes for the incidents in `BUG_LOG.md`. `FINDINGS.md`
was drafted with an AI agent from the bug log and checked by me against
every entry.

Design principle: agents were treated as employees rather than stateless
input-output calls: an employee handbook (`src/factory/handbook.py`) they
could consult before deciding anything, roles with explicit permissions
that an agent cannot widen itself (`src/factory/agents/roles.py`),
department managers, escalation paths to a human, and hard limits on
authority.

## Start here

1. **`FINDINGS.md`** — what broke, grouped into patterns, and what each one
   teaches about running LLM systems in production. The real exhibit; everything
   else here is supporting material.
2. **`src/factory/llm/client.py`** and **`src/factory/llm/router.py`** —
   structured output via `parse()`, a schema-repair retry that shows the model
   its own validation error, jittered backoff, a circuit breaker that drops a
   provider from the candidate list once it's in sustained outage
   (`tripped_providers`) rather than retrying into a known-dead endpoint, a
   `NO_FAILOVER` policy for calls that must never silently change vendor, a
   provider refusal that never fails over (a refusal isn't an outage), and
   per-call cost metering. The daily spend cap is a separate guardrail — see
   the table below.
3. **`src/factory/state/repository.py`** — task claiming via
   `SELECT ... FOR UPDATE SKIP LOCKED`, so multiple workers pull from the same
   queue without colliding or blocking each other.
4. **`src/factory/state/orm.py`** — the ledger's own `CHECK` constraints
   (`balance_cents >= 0`, integer cents, never floats) — a balance cannot go
   negative at the database layer, independent of anything the application code
   does or fails to do.
5. **`src/factory/engines/compliance/minimize.py`** — GDPR Art. 5(1)(c) data
   minimisation enforced at the one sanctioned write path into the `leads`
   table, not as a policy document.
6. **`BUG_LOG.md`** and **`ARCHITECTURE_ROADMAP.md`** — the full incident
   history and the living design record, written as the system was built, not
   after the fact.

## Architecture

```
FastAPI (health, metrics, Stripe webhooks, HITL callback)
                    │
              Supervisor (LangGraph, Postgres checkpointer)
   ┌────────────┬────────────┬────────────┬────────────┬────────────┐
Discovery   Development   Marketing     Finance    Compliance
   └────────────┴────────────┴────────────┴────────────┴────────────┘
                    │  engines communicate ONLY through Postgres rows
   tasks · ideas · products · product_locks · leads · ledger · interrupts · checkpoints
```

Model calls go through a **multi-provider router**: Gemini serves orchestration,
discovery, compliance, and marketing copy; Claude serves code generation and the
Bug Council; DeepSeek serves code repair only, and can never be reached as
another task type's accidental failover target (`BUG_LOG.md` #13). Engines call
`llm.parse(Schema, task_type=...)` and receive a validated Pydantic object — no
free-text parsing anywhere in the system.

Leads come from **B2B data providers** (Hunter.io, Apollo.io), with an operator
CSV drop retained as a manual override. Each adapter reduces the provider
response to five PII fields plus provenance before it travels any further. Lead
sourcing was built and wired in, but the `leads` table never held a single row
in this system's entire run — no product ever reached the stage of actually
contacting anyone.

## Guardrails that live in code, not in prompts

Compliance first, deliberately — these are the ones that matter most if this
system is ever trusted with real contact information:

| Guardrail | Where |
|---|---|
| GDPR data minimization | `engines/compliance/minimize.py` — the only sanctioned write path into `leads`; the table itself has only five PII columns, so there's nowhere to put more even if a caller tried |
| Outbound email blacklist | `engines/marketing/email.py` parses root `blacklist.txt` on every send and fails **closed** |
| Legitimate Interest Assessment | `engines/compliance/lia.py`, required before any contact |
| Data provenance | `leads.source_url` / `leads.scraped_at` are `NOT NULL`; `provenance_kind` decides what the Art. 14 footer may claim |
| Dry-run default | `FACTORY_DRY_RUN=1` stubs every outbound side effect — no email, no deploy, no charge — until explicitly turned off |
| Sandboxed execution | `sandbox/docker_runner.py`; `sandbox/disabled.py` raises when Docker is absent rather than silently running unsandboxed |
| Deploy gate | `engines/development/bug_council.py` — five reviewers, unanimous or no deploy |
| Ad spend on broken MVPs | `supervisor/concurrency.py` pauses paid spend while a patch is in flight |
| Never-negative balance | `ledger` `CHECK` constraint + `engines/finance/reinvest.py` integer cents |
| Discovery backlog flooding | `supervisor/orchestrator.py` holds ideas at `MAX_CONCURRENT_BUILDS` |
| LLM spend | `DAILY_LLM_SPEND_CAP_USD` — a deterministic circuit breaker, never delegated to a model, because asking an LLM whether to stop spending is the one place "just this once" would be catastrophic |

## Trade-offs, stated plainly

- **The Docker socket is mounted into the app container** so it can launch
  sandboxed sibling containers for generated code. That's root-equivalent
  access to the host from inside the app container — a real, acknowledged
  attack surface, not a theoretical one.
- **Generated code ran in plain Docker containers**, not a stronger isolation
  boundary (gVisor, Firecracker, or similar). Sandboxing here means a separate
  container and a resource/time limit, not a security boundary strong enough
  to trust fully adversarial code.

## How to run

```bash
cp .env.example .env      # fill in DATABASE_URL and at least one provider key
pip install -e ".[dev]"
python -m factory.preflight        # tells you exactly what is missing and how to fix it
docker compose up -d postgres
alembic upgrade head
python -m factory.main --once      # one supervisor tick
```

## Testing

- **`pytest tests/unit -q`** — no external services. This is what CI
  (`.github/workflows/tests.yml`) runs on every push and PR.
- **`pytest tests/integration -q`** — needs a real Postgres (`docker compose up -d
  postgres && alembic upgrade head` first). Not run in CI.
- **End-to-end** (an actual supervisor tick against real engines) additionally
  needs Docker available to the process, for the sandbox runner that executes
  generated code. Run locally only.

## Known limitations

- No real customer or real revenue data exists to validate the business logic
  against — every number in `docs/results-2026.md` is real activity, but none of
  it includes a paying customer.
- The two Docker-related trade-offs above were accepted, not solved.
- See `FINDINGS.md` for what each incident pattern teaches, and its own closing
  assessment of whether this was safe to run unattended.

## Current status

Offline research case. Ran in production 2026-08-18 to 2026-09-24, then
deliberately paused (zero LLM spend since). Being decommissioned; this repo is
what's left once it is.
