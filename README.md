# Autonomous Multi-Agent B2B SaaS Factory

A continuously-running system that discovers B2B micro-SaaS opportunities, validates
demand, builds and deploys MVPs, acquires customers, processes payments, and
reinvests profit — with a human in the loop only for missing credentials and legal
uncertainty.

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

Model calls go through a **dual-provider router**: Gemini serves orchestration,
discovery, compliance, and marketing copy; Claude serves code generation and the
Bug Council. Engines call `llm.parse(Schema, task_type=...)` and receive a validated
Pydantic object — no free-text parsing anywhere in the system.

Leads come from **B2B data providers** (Hunter.io, Apollo.io) queried through the MCP
tool registry, with an operator CSV drop retained as a manual override. Each adapter
reduces the provider response to five PII fields plus provenance before it travels
any further; see `engines/marketing/providers/`.

## Guardrails that live in code, not in prompts

| Guardrail | Where |
|---|---|
| Outbound email blacklist | `engines/marketing/email.py` parses root `blacklist.txt` on every send and fails **closed** |
| GDPR data minimization | Provider adapters narrow at the HTTP boundary; `engines/compliance/minimize.py` enforces again at the DB boundary; the `leads` table has only five PII columns |
| Data provenance | `leads.source_url` / `leads.scraped_at` are `NOT NULL`; `provenance_kind` decides what the Art. 14 footer may claim |
| Legitimate Interest Assessment | `engines/compliance/lia.py`, required before any contact |
| Sandboxed execution | `sandbox/docker_runner.py`; `sandbox/disabled.py` raises when Docker is absent |
| Deploy gate | `engines/development/bug_council.py` — five reviewers, unanimous or no deploy |
| Ad spend on broken MVPs | `supervisor/concurrency.py` pauses paid spend while a patch is in flight |
| Never-negative balance | `ledger` CHECK constraint + `engines/finance/reinvest.py` integer cents |
| Discovery backlog flooding | `supervisor/orchestrator.py` holds ideas at `MAX_CONCURRENT_BUILDS` |

## Quick start

```bash
cp .env.example .env      # fill in DATABASE_URL and at least one provider key
pip install -e ".[dev]"
python -m factory.preflight        # tells you exactly what is missing and how to fix it
docker compose up -d postgres
alembic upgrade head
python -m factory.main --once      # one supervisor tick
pytest -q
```

`FACTORY_DRY_RUN=1` (the default) stubs every outbound side effect: no email is sent,
no deploy runs, no card is charged. Turn it off only after the guardrail test suite
has passed against real credentials.

See `DEPLOYMENT.md` for VPS setup.
