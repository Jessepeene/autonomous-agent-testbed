# Architecture roadmap: the agentic organisation

Where this factory's autonomy layer is going, why, and what has actually shipped.
Kept in the repo rather than a planning tool so it is versioned alongside the code
it describes — if a phase below no longer matches the code, the code is the truth
and this file is stale; fix it.

**Status at last update (2026-09-12): Phase 1 deployed and confirmed working in
production.** On its first instrumented run it independently found that the live
product's checkout returns `POST /api/billing/checkout → 422` — the payment path
has been broken since launch on 20 August — and queued a patch for it. See "The
instrumentation principle" below for what the two failed attempts before that
taught, which applies to every remaining phase.

---

## The problem this exists to solve

This factory calls itself an organisation — Discovery, Compliance, Development,
Marketing, Finance as "engines", Supervisor above them — but structurally it is a
task dispatcher, not an organisation. Two live incidents made the gap concrete:

1. **A live product's landing page had a broken email-capture form and a wrong
   year, unnoticed for three weeks.** Bug Council reviewed it once at build time
   and approved it. The periodic re-check that might have caught it later
   (`_audit_live_products` → `_content_freshness_issue`) only reads static HTML and
   never tries to *use* the page — and it is idle-gated, so it stops running
   exactly when the factory is busiest (BUG_LOG #21).
2. **46 identical provider-outage escalations filed as "legal uncertainty"**
   (BUG_LOG #20) — because failures are only recognised in the shapes someone
   already wrote a handler for.

Both are the same root cause: *the system reads code and reasons about it; it never
uses what it built, and it only notices what it was pre-told to look for.* A human
manager would have caught both instantly.

## The target structure

**Departments** = the existing engines. Unchanged.

**Department managers** = a standing, scheduled agent per department with real
tools, an open-ended remit ("what would a demanding manager flag here?"), and the
authority to create the work its judgment calls for — bounded by that department's
blast radius.

**General manager** = what Supervisor becomes: cross-department awareness, decides
where its own attention goes, escalates to a human on judgment rather than from a
fixed enum of reasons.

**Authority scales with blast radius — this is why the rollout is staged by
department, not a preference.** A wrong call from a product reviewer costs one
unnecessary patch task. The same open-endedness inside Compliance risks legal
exposure, inside Finance risks real money, inside Discovery risks spend at scale.
Development earns wide discretion first; Compliance and Finance earn it slowly, and
perhaps never to the same degree.

**Hard rails never move**: the budget circuit breaker
(`orchestrator._check_budget`), compliance/LIA gates, campaign approval, the
`MUTABLE_KEYS` allowlist, and `AgentRole`'s capability envelope itself. Authority is
granted by a human editing `agents/roles.py` — "a code change that a human
reviews", per that module's own docstring — never something an agent widens for
itself.

## What this builds on (existing, not invented)

- **`agents/spawner.py::spawn()` + `agents/roles.py`** — agents are already
  capability envelopes: tool categories, step ceiling, deadline, cost cap,
  `mutating` flag.
- **`GROWTH_ANALYST` is a working department-manager prototype already in
  production.** It reads real numbers, decides for itself whether anything is worth
  doing ("most products most weeks need nothing" is explicit in its prompt), and
  genuinely acts. It is just narrow — one question, one product, when told to look.
- **`tools/workspace.py::build_workspace_tools()`** — the precedent for a private,
  per-run tool registry injected via `AgentRuntime(tools=...)` (see
  `development/repair.py`).
- **`scheduler._schedule_periodic()`** — the unconditional cadence mechanism.
- **`growth.py`'s action tools** — the pattern for an action tool that re-validates
  the model's premise server-side rather than trusting it.

## Gaps identified

1. ~~**No hands.** No browser anywhere in `src/`.~~ — closed in Phase 1.
2. ~~**Product oversight is idle-gated.**~~ — closed in Phase 1 (BUG_LOG #21).
3. **Memory has no department or cross-product dimension.** Scopes are exactly
   `global`, `engine:<name>`, `product:<id>` — and *nothing in `src/` ever writes to
   `global`*. Cross-product lessons are smuggled into `engine:*` scopes with a
   `[niche]` text prefix and retrieved by token overlap.
4. **The one real judgment signal is computed and thrown away.**
   `audit.check_business_health()` returns a concerns list; `engines/maintenance.py`
   stringifies its *length* into a summary and discards it.
5. **The Orchestrator is memory-blind and history-blind** — 13 counts plus a
   non-durable in-process `RoutingMemo`. It cannot know what it decided an hour ago.
6. ~~**Role scoping is prompt-level only.**~~ — closed in Phase 1.

---

## Phase 1 — Development department manager ✅ implemented

The pilot: lowest blast radius, already has a floor to walk, directly fixes the
incident that started this.

- `src/factory/tools/browser.py` — `BrowserSession`, a private per-run registry
  holding one Playwright page. Four tools (`browser_open`/`read`/`click`/`fill`),
  every one pinned to an origin allowlist (the product's own `deploy_url` +
  `custom_domain`) — the SSRF guard `http_get` never got.
- `src/factory/tools/product_review.py` — `report_product_issue`, pinned to one
  product, commissioning a `guided` `PATCH_PRODUCT`. Split from the browser
  category by blast radius, mirroring `growth_read` vs `growth_actions`.
- `agents/roles.py` — `AUDITOR` revived from dead code: real browser, open-ended
  remit, `mutating=True`, added to `CALL_SITE_SCOPED_ROLES`.
- `agents/spawner.py` — `can_spawn` now *enforces* call-site scoping (it was
  prompt-level only). `from_own_call_site=True` is how the one legitimate caller
  authorises itself.
- `development/graph.py::_review_live_product` + `TaskKind.REVIEW_LIVE_PRODUCT`.
- `scheduler._schedule_periodic` — daily per live product, **unconditional**.
- `Dockerfile` — Playwright + Chromium, `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`
  so the uid-10001 runtime user can read the browser.

**Acceptance test: passed.** Deployed 2026-09-12; on its first instrumented run
the reviewer independently found a broken checkout (`POST /api/billing/checkout →
422`, with the app rendering `[object Object],[object Object]` under the button)
without being told to look for anything, and queued a `guided` patch. It stopped
on its own at 11 of 14 steps.

**Remaining for Phase 1**: a ~1 week shadow period alongside the deterministic
`audit-live-products` check before retiring `_content_freshness_issue`'s LLM call.
The cheap HTTP status check stays either way — it is strictly better at the uptime
question than any agent.

## The instrumentation principle (applies to every phase)

**Before giving a manager judgment over a domain, ask what an expert in that domain
actually looks at to decide — then check whether the system can show them that. If
it can't, the manager will confabulate from proxies, confidently.**

This was learned the expensive way in Phase 1, and it is the most transferable
result so far:

- **v1** cast AUDITOR as *"a demanding manager on a walkthrough"* and gave it a
  browser that returned only visible page text. Asked whether a form worked, it had
  no way to see that submitting fired a request at all — so it did what a confused
  visitor does: re-filled and re-clicked, hoping the page would change. Two live
  runs, both exhausted their step budget, both produced nothing.
- **v2** reframed it as a senior QA engineer *and* gave it the instruments a tester
  actually uses — `network` (requests fired + status codes) and `console_errors` on
  every action. It settled the question in 11 steps and stopped on its own, having
  found `POST /api/billing/checkout → 422`: the product's entire payment path
  broken since launch.

Same model, same page. The difference was evidence, not intelligence. Access
without instruments produces confident flailing; the fix is rarely a better prompt
alone.

**Corollary for budget safety**: an agent with no way to settle its question will
burn its whole budget failing to settle it. Instrumentation *is* a cost control.
The other two here are structural: a bounded terminal extraction call so a review
always ends in a decision rather than an exhausted ceiling, and a per-role
`cost_cap_usd`.

### Instrument audit for the remaining phases

Do this audit **before** building each manager, not after:

| Phase | What the expert needs to see | Does the system have it? |
|---|---|---|
| 2 — Dev manager, wider context | per-product commercial reality: revenue, lead flow, outreach outcomes | Partly. `read_product_growth` gives traffic, signals, AB state — **no per-product revenue** |
| 3 — Marketing manager | **did outreach actually work**: replies, interest, objections | **No. This is the big one.** There is no inbound email ingestion anywhere in the codebase. It can see sends, bounces and complaints (`deliverability_counts`), never a reply. "It worked" is only knowable via 14-day *product-level* revenue attribution. A marketing manager built today would be judging outreach essentially blind — close this gap first |
| 4 — General manager | cross-department trend, what changed, what's stuck | Mostly exists and is **thrown away**: `check_business_health` computes real concerns and `engines/maintenance.py` discards them; memory has no cross-product scope (gaps 3–4 above) |
| Discovery (if ever) | whether a signal is real and not already mined | Weak — live logs show repeated `discovery.signal_not_grounded`, i.e. it cannot reliably verify its own sources |

## Phase 1.5 — the record (prerequisite for Phase 2 and Phase 4)

Not originally a phase, and it should have been. Phases 2 and 4 both reduce to
"the manager can see what happened": Phase 2 *is* widening context, and a general
manager with no cross-department history has nothing to be a manager over. This is
the instrumentation principle applied to the managers themselves.

Done:
- **Durable logs.** Docker's json-file driver keys logs to a container id and
  `docker compose up -d` replaces the container, so every deploy destroyed the
  record of everything before it. Now a rotated, bind-mounted JSON file that
  outlives any container (`factory/logging.py`). httpx/httpcore silenced to
  WARNING -- they log request URLs at INFO and Telegram carries its bot token in
  the URL path, which a durable file turns into a credential on disk.
- **Tool arguments in `agent.finished`**, not just tool names. `run_workspace_command
  x20` cannot be diagnosed without them.

Still missing, and the piece that would have made today's debugging trivial
instead of archaeological:
- **A causal trail.** A `task_events` table linking task -> engine -> agent ->
  tools -> outcome -> what it caused. Today a task's story is spread across logs,
  `tasks.last_error` (overwritten on each attempt), `heal_attempts`, and
  inference. There is no query that answers "what happened to this product, in
  order". Phase 4's general manager needs exactly that view.

## Phase 2 — widen the manager's context

Feed it what `orchestrator.build_snapshot()` already assembles plus per-product
history (days since touched, outreach/signal counts, prior findings) — handed to
judgment instead of a fixed threshold. Let it choose *which* product to look at
(most-neglected first) rather than round-robin.

## Phase 3 — a second department manager

Most likely Marketing (thin lead flow and stalled validations are a live symptom),
reusing the Phase 1 shape. **Open question to decide at the time**: Marketing vs
Discovery. Discovery is where spend scales fastest and a bad judgment call costs
most, which argues for doing it later, not sooner.

## Phase 4 — the general manager

Closes gaps 3–5: add a department/cross-product memory scope; stop discarding
`check_business_health`'s concerns and make them standing GM context; give
Supervisor durable cross-tick awareness so it can decide where attention goes and
escalate on judgment rather than from a fixed enum.

---

## Handover: how the current system gives way

Nothing is ripped out up front — each phase retires a piece only after its
replacement has been observed working:

| Phase | Retired once proven |
|---|---|
| 1 | `_content_freshness_issue`'s LLM call (deterministic uptime check stays) |
| 1 | ✅ `audit-live-products`' dependence on the factory being idle |
| 3 | per-department fixed maintenance slugs a manager now covers |
| 4 | fixed-enum escalation reasons the GM can judge better; `check_business_health` becomes GM input rather than its own interrupt |

The deterministic pieces that are genuinely *better* than judgment — HTTP status
checks, Fisher's-exact trial evaluation, budget arithmetic, dedup by source URL —
are never replaced. Judgment is added where a rule was standing in for it, not
where a rule is correct.
