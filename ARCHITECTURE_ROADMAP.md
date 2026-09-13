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

## Every entity keeps a living record (applies to every phase)

The companion to the instrumentation principle, and the generalisation of Phase
2c. Instruments let an agent *see the present*. A record lets it know *what has
already been learned*.

**The system has data, not knowledge.** `heal_attempts`, `page_views`,
`validation_signals`, `llm_calls` are rows a query can assemble into an answer --
if someone already knows to ask. What no entity has is a standing account of
itself: this product's monetisation hypothesis was never actually tested because
checkout has been broken since launch; this niche produced four near-duplicate
ideas from one GitHub issue; this capability cannot run tests because the sandbox
is stripped before it starts.

Every entity should hold one, and maintain it:

| Entity | Should know about itself | Today |
|---|---|---|
| Product | design plan, monetisation strategy, what has been tried, what worked | Partial and static: `Idea.monetization_hypothesis` (a launch-time guess, never revisited), `Product.design_brief`, `.factory/ARCHITECTURE.md`. Outcomes live in rows nothing synthesises |
| Capability / role | what this job keeps getting wrong, what the environment actually provides | Nothing. See Phase 2c |
| Department | what works in this niche, which channels failed | Partial -- `engine:*` memory, retrieved by token overlap |
| The factory itself | what broke, what was tried, what the strategy is | **`BUG_LOG.md` and this file -- both written by hand by an operator.** The factory keeps every row and loses every lesson |

That last row is the sharp one. The institutional knowledge of this system
currently exists because a human writes it down. That is a dependency the end
state must not have.

**The constraint that keeps this honest.** This codebase is rightly suspicious of
prose: a decision is read from a validated field, never regex'd from text, and
"LLM prose becomes the human-facing ground truth" is already recorded as a known
gap (Tier 3 finding #14). A self-maintained narrative is exactly that failure mode
unless it is anchored. So a record must be **derived from and checkable against
real rows** -- "this product has never converted" cites the ledger; "outreach is
blind" cites the absence of reply data -- never free-floating recollection. The
instrumentation principle supplies the evidence; the record is what persists the
conclusion. A claim that cannot be traced back to a row does not belong in one.

The existing precedents to build on rather than replace: `Product.design_brief`
(generated once from the real deployed page, cached), `.factory/ARCHITECTURE.md`
(a per-product knowledge file the repair agent already reads), and
`memory/store.py`'s reinforce-on-near-duplicate behaviour, which is the right
shape for a record that accumulates instead of piling up restatements.

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

## Phase 2 — the handoffs, not "more judgement"

Originally scoped as "widen the manager's context". That was too shallow, and a
day of real failures showed why. The org chart already mirrors a human team —
Bug Council is the pre-deploy check, its findings are the report, the repair agent
is the dev who receives it. What does *not* mirror a human team is how those roles
communicate: **one-way payloads between stateless workers, where a human team has
conversations between people who accumulate context and can push back.**

Every failure chased on 2026-09-12 is an instance of one of the four gaps below.

### 2a. A blocked worker must be able to say so

The repair agent found the sandbox had no dependencies installed, could not `pip
install` (no network, by design), and had no way to report that. It burned its
budget searching the filesystem and returned a plain failure. That was recorded as
a consumed heal cycle — indistinguishable from "tried and could not fix it" — and
escalated three times without once naming the actual problem.

A human dev says *"I can't run the tests, the environment is broken, who owns
that?"* and is believed. The system needs the same: a distinct *blocked* outcome
carrying what is missing, which does not consume a repair budget and which routes
to whoever can fix it rather than to a generic escalation.

Note this also **replaces most of the give-up constants** rather than tuning them:
`MAX_HEAL_CYCLES`, `GUIDED_MAX_STEPS`, the breaker thresholds. Most exist because
the system cannot tell blocked from failing, so it counts attempts instead. Once
it can tell, the counters become backstops rather than the primary mechanism.

### 2b. Reports must be addressable

Bug Council emits a text blob into a task payload. The recipient can comply or
fail; it cannot say *"finding 4 is wrong, that behaviour is intentional"* or ask
what a finding means. A human report has an author you can go back to.

### 2c. Workers keep notes — statelessness is the wrong default

`spawner.py` deliberately avoids a long-lived agent registry, and that reasoning
holds: a long-running process drifts out of sync with the code. But a human dev is
not a long-running process either — they go home. What makes them non-stateless is
that **they keep notes and the team keeps records.**

The gap is not agent lifetime, it is that **there is nowhere to record a lesson
about a capability.** Memory scopes are `global` (never written by anything),
`engine:<name>`, and `product:<id>`. "The repair sandbox has no dependencies
installed" is a fact about the *repair capability* — it will recur on every
product, so `product:` is wrong, and `engine:development` is too coarse to recall
reliably. The lesson therefore has no home and is rediscovered from scratch every
run, at full cost.

Needed: a capability/role memory scope, written by agents about their own work and
recalled on every spawn of that role. The per-product knowledge file
(`.factory/ARCHITECTURE.md`) is the existing precedent — the same idea, scoped to a
product instead of a job.

### 2d. Operational competence must be learnable

`learning/allowlist.py` holds seven mutable keys: discovery generator,
discriminator, scoring weights, the leads planner, outreach copy, the landing-page
prompt, and the sender identity. **All seven are commercial.** Not on the list: the
repair agent's prompt, the reviewer's, Bug Council's, the orchestrator's.

So the loop optimises *what the factory sells* and never *how competently it
operates*. Every agent that failed on 2026-09-12 is structurally incapable of
learning from it — the repair agent will hunt for pytest again tomorrow.

Bringing operational prompts in needs care, and the existing safety exclusions stay
exactly as they are: LIA, Bug Council's reviewer prompts, the orchestrator's system
prompt, and the real-MVP codegen prompt remain permanently excluded. The candidates
are the *worker* prompts (repair, review), whose failures are commercially lossy
rather than safety-critical — the same bar every current allowlist entry meets.

### 2e. Employees need documentation — to read *and* to write ✅ implemented

Jesse's framing, which is the clearest statement of the gap anyone has made here:

> *"If I'm a dev and I get told to implement a new feature I know what the product
> is about, I can read documentation, I know the purpose of the product so I can
> gauge why it would be important and how to implement it. If something is unclear
> I can ask questions or reference other info in the project directory, and I
> document what I do and justify my actions. And this goes for all employees."*

Before this, an employee could read exactly three things: its own system prompt,
whatever memory recall surfaced, and — only if it was a repair role — one product's
`.factory/ARCHITECTURE.md`. It could not discover that any other employee existed,
what happened to its work after handoff, what rules bound it, or **what the product
it was editing was for.** That is the instrumentation principle again, pointed at
context rather than data: an employee optimising against gates it cannot see.

Built (`handbook.py`, `tools/handbook.py`):

- **Generated, never hand-written.** The org directory derives from `ROLES`, the
  policy brief from the settings and gates that actually enforce it, a product
  dossier from that product's own rows. Hand-written docs drift, and a drifted doc
  is worse than none because an employee acts on it confidently — the same reason
  the living-record principle insists a record be checkable against real rows.
- **Pull, not push.** `list_docs` / `read_doc` / `read_product_dossier` are tools,
  not prompt padding. An employee looks something up when it has a question, which
  is also the honest signal that it had one.
- **Write-back is the half that was missing.** `document_work` records what an
  employee did *and why*, to two places that answer different questions: the
  `task_events` trail (what happened to this product, in order — read back by the
  dossier) and `capability:<role>` memory (how this job tends to go, recalled on
  every future run of that role, on any product).
- **Identity is bound by code, not self-declared.** `document_work` is built
  per-run against the `RoleName` the spawner already resolved, so a note cannot be
  filed under an employee that did not write it. Same split as everywhere else
  here: the agent decides the inputs, deterministic code decides the record.
- **One declared universal grant.** `UNIVERSAL_TOOL_CATEGORIES = ("docs",)` is the
  single exception to "a role gets exactly its `tool_categories`", declared in one
  place so the containment tests can assert against it rather than be loosened.

Still open, and deliberately: **asking a question** (2b) is the one part of Jesse's
list not built — it needs a responder, and what a worker actually gets stuck on is
better learned from the POC run than guessed at now.

### What carries over from the original scope

Still worth doing, now as a consequence rather than the headline: feed the manager
`build_snapshot()`'s data plus per-product history, and let it choose *which*
product to look at rather than round-robin.

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

---

# The documentation architecture

Jesse's framing, 2026-09-13, and the organising idea for everything below:

> *"This type of documentation is what should create the intended workflow. A real
> product in a real environment would have this kind of documentation."*

He mapped out what a small software startup should hold — 35 document types across
product & strategy, product definition, technical docs, testing & quality, issues,
research & feedback, analytics, and releases & operations. That full list is the
target. What follows is how it gets built here without becoming a graveyard.

## The rule: records with types, not documents

**Do not create 35 documents.** Hand-written documentation drifts, and a drifted
document is worse than none because an employee reads it and acts confidently on
something false. That is not a hypothesis — it is why the handbook is generated
from `ROLES` and the product dossier assembled from real rows rather than either
being written by hand.

So: **a "document" is a query, not a file.** Most of the 35 are views over records
the factory already writes. Build a view when an employee actually reaches for it.

## The five types (Jesse's taxonomy — the real contribution)

More useful than the 35-item list, because it is what an agent can reason over:
*this is what we intended → this is what we built → this is how we tested it →
this is what users did → therefore this is the next decision.*

| Type | Question | Where it lives | State (2026-09-13) |
|---|---|---|---|
| **Definition** | What is the product supposed to be? | `ideas`, `products.design_brief` | Partial — no feature specs |
| **Intent** | What are we trying to accomplish? | — | **Absent** |
| **Execution** | What are we doing now? | `tasks`, `task_events` | Good |
| **Evidence** | What actually happened? | `llm_calls`, `heal_attempts`, `issues`, `validation_signals`, `outreach_log`, `learning_episodes`, `page_views`, `ab_tests` | Strong |
| **Decision** | What should we do as a result? | `entity_verdicts` (one narrow cache) | **Absent** |

**The factory is excellent at recording what happened and what it is doing, and has
no memory of what it wanted or what it chose.** That single asymmetry explains the
observed failures: the same checkout bug rediscovered five times (no Decision
record saying "we are fixing this"); the Orchestrator escalating about
`spend_limit_cents` (no Intent record saying what the pool is for); `RoutingMemo`
never checkpointed, so the Orchestrator cannot know what it decided an hour ago.

**Intent and Decision are what turn a task queue into an organisation.** Evidence
and Execution are already there.

## Build order

1. **Issues** ✅ implemented 2026-09-13 — see BUG_LOG #23. A finding with an
   identity, a state, and a closure that must be earned. This is Jesse's #20 and
   its lightweight workflow (detected → triaged → fix → test → verify → closed).
   It is also the prerequisite for a manager being anything real: *a manager with
   no backlog has nothing to manage.*
2. **Decisions** — next. One record type: what was decided, by whom, why, on what
   evidence, what alternatives, what consequence. Unifies Jesse's #5 (product
   decisions) and #13 (technical decisions); `task_events` is the spine to hang it
   on. This is what lets a manager reason across the chain rather than search for
   the next task to execute.
3. **Everything else — derive on demand.** #1, #2, #11, #14, #26, #28 and #29 are
   already queries over existing rows; the product dossier is three of them today.

## Two axes, both needed

Jesse's list is **product** documentation. The handbook shipped on 2026-09-13 —
org directory, policy brief, handoff map — is **organisational** documentation.
Different axes: one tells an employee what it is working on, the other tells it
where it works and what the rules are. Neither substitutes for the other.
