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

## Phase 2.5 — what it costs to be blocked

**Status**: identified 2026-09-15 from the Design department's first live runs.
Scoped here, not built.

### Start from how a human team does this

A developer mid-feature hits a question — 404 or 403 for this case? Four things
happen, and **none of them is "stop and wait"**:

1. **The work in progress is not destroyed.** It sits on a branch. Isolation, not
   deletion — the problem of "a half-finished change must not reach production" was
   solved with branches, not with `rm -rf`.
2. **They leave a note.** A TODO, a draft PR description, a line in a scratch file.
   Resuming is *read your own note*, not *redo the investigation*.
3. **Mostly they do not block at all.** They pick the likely answer, mark it
   visibly as an assumption, and carry on. Being wrong costs one small edit later;
   stopping costs the entire context.
4. **They batch.** Nobody taps a colleague six separate times in an afternoon.

Measured against that, this system does the opposite of all four. That is where the
token and wall-clock waste actually lives — not in the channel's shape.

### The four fixes, in order of waste removed

**1. Look before asking.** There is no "find the answer on its own" step anywhere.
Most of these should never become questions at all — they are lookups.

And the answers are *already in rows*. `product_dossier()` renders **"Who it is
for"** directly from `Idea.target_niche`; the live runs blocked on precisely that
class of fact. Worse, `answer_question_task` pulls the dossier and the deciding
manager does not: the agent whose job is to reply gets the file, the agent that has
to make the call does not.

#### The blocker is not access. It is that a manager is not an agent.

The read layer exists and works: `read_product_dossier`, `read_doc` and `list_docs`
are registered tools in `tools/handbook.py`, and repair, review and growth agents
all hold DB-backed reads today.

A manager cannot use any of them, and not because it is forbidden.
`run_manager_tick` is a single `ctx.llm.parse()` — **one-shot structured output**.
It must produce its entire decision in one breath, so there is no step in which it
*could* look something up, even handed every tool in the registry. Deciding and
looking happen in the same instant, which makes looking impossible.

That is the sharp version of this finding: **the role that most needs to
investigate is the one architecturally incapable of taking an action before
answering.** Its only two moves are guess or ask, which is exactly what the three
live runs did.

#### Three steps, cheapest first

**(a) Dossier into the manager's prompt.** Nearly free, no new machinery: render
`product_dossier()` for the products in the manager's domain. Would have answered
"who is this for" outright. Still a pre-built snapshot — a bigger guess rather than
no guess — but it removes the concrete blocker today.

**(b) Manager tick becomes an agent loop that ends in a structured decision.** Look,
*then* decide, instead of deciding in one breath. At a daily cadence for one
department, 1 call → 4 is negligible against the cost of a wrong or absent decision.
This is the real fix, and it is what makes fix 2 (assume-and-flag) meaningful —
an assumption is only defensible if looking was tried first.

**(c) Broad reads, typed shape.** Jesse's point, and it stands: *"the agents should
at least have full read access to the db"* — a curated view is a guess about what
they will need, and a narrow guess is what caused this. So do **not** restrict which
facts are reachable. Do restrict the form they come back in.

Broad coverage, typed interface, PII redacted by default — not raw SQL, for three
reasons:

- **PII.** `ValidationSignal.email` and `Lead.work_email` are real addresses. The
  schema already embodies minimisation deliberately: `Customer` carries
  "deliberately no email, no name". Unrestricted lead reads cut against the
  Compliance engine's premise and the `erase_lead`/blacklist path.
- **Correctness.** Half these tables mean nothing without their filters —
  `superseded_at IS NULL`, status filters, dedup. `live_requirements` and
  `live_design_guidelines` exist to encode exactly that. An agent writing raw SQL
  reads superseded rows and never knows: the same class as tonight's bugs, where two
  different facts were held as one.
- **Tokens.** An agent that can `SELECT *` will.

*(Adjacent real bug: `product_dossier`'s docstring claims it assembles "real
interest in `validation_signals`, and reach in `outreach_log`" — it renders
neither. That is the gap that would answer the traffic question, so (a) is
incomplete until it is fixed.)*

**2. Assume and flag, instead of blocking.** Today's guidance ends *"the one thing
you must never do is invent the answer and act on it."* That is too strict, and it
is expensive. The real distinction is not assume-versus-ask, it is **visible versus
silent**:

- Run 2's failure was not that it assumed. It asserted "the product is live with
  real users" — contradicted by its own input, which said `planned` — told nobody,
  and asked nobody. Silent.
- A human who says "assuming this is a core product, flagged, carrying on" is being
  productive, and is corrected cheaply when the answer lands.

So the default should be: proceed under a **recorded** assumption, *and still ask*,
and let the answer correct it. Blocking is for the case where proceeding would
destroy something.

**3. Park without abandoning.** Today, parking *is* abandoning. On BLOCKED,
`_patch_surgically` calls `repair.restore_snapshot()` — which deletes every edit the
agent made and rolls the workspace back — and only then parks. On resume the agent
re-reads the codebase, re-runs the suite, and redoes the whole investigation, paying
for all of it again.

"Ask a question" and "throw away your work" are the same operation, and it is priced
that way for exactly the agent most likely to need to ask.

The snapshot restore exists for a real reason: a half-finished repair must not sit
in a deployable workspace. That is the branch problem, and the answer is isolation:

- Workspaces are **not** git repos today (`.git` is in `WORKSPACE_SKIP_DIRS`
  defensively, and no workspace in production has one). `git init` per workspace
  makes park-on-a-branch nearly free.
- A parking agent should write its handoff note — what it was doing, what it found,
  what it was about to try. **The mechanism already exists**: `.factory/
  ARCHITECTURE.md` is a durable per-product knowledge file that every repair agent
  reads before starting and that `restore_snapshot` deliberately preserves. Nothing
  writes a park note to it.

**4. Then, and only then, the channel.** Below.

### Comms as a thread, not a blocking call

The channel shipped and works end to end — routed, delivered, parked, resumed —
and the first three live runs showed it is not a *conversation*. It is a blocking
RPC with a six-hour timeout wearing a conversation's vocabulary. `Question` has one
`question` column, one `answer` column, one recipient, no parent link, and answers
are retrieved by `blocking_task_id`.

Jesse's framing, and it is the right target: *"why doesn't it work like a sort of
WhatsApp for AI agents?"*

**Four properties it lacks.**

1. **No thread.** One question, one answer, closed. An answer that is inadequate,
   or is itself a clarifying question, has nowhere to go. The asker blocks again
   and files a brand-new question with no link to the first, so the exchange
   structurally cannot have a second turn.
2. **Asking costs the asker its work, not its time.** Correcting an earlier note
   here: the round trip is *fast* by design — the supervisor ticks every 15s, an
   `ANSWER_QUESTION` task lands at priority 110, and `answer_question` sets
   `not_before = now()` and releases the asker immediately. Roughly 30–90 seconds,
   not hours; `BLOCKED_RETRY_HOURS` is the fallback for a question nobody ever
   answers, not the wait.

   **No question has ever traversed this channel in production**, so that is the
   designed latency and not a measured one. The real cost is fix 3 above: what the
   asker loses is its work, not its afternoon.
3. **An answer is a one-time unblock, not knowledge.** Scoped to
   `blocking_task_id`, so once that task finishes the answer is unreachable.
   Tomorrow's tick asks the identical question and pays for the identical answer.
   Scope answers to their subject so they are readable later.
4. **Point-to-point and invisible.** One `asked_of`, no group, no visibility. Half
   the value of asking in a channel is that somebody who was *not* asked knows the
   answer, or simply learns that a department is stuck.

**Two properties it must NOT copy, and this is the load-bearing half.**

- **No inbox anybody has to check.** The whole reason comms is not a tool is that
  agents do not reliably choose to check or send — `report_product_issue` shipped
  as a tool and both live runs spent their entire step budget investigating without
  ever calling it. A question *becoming work* in the recipient's queue is the good
  part and stays. Nobody opts in; nothing goes unread.
- **No free-form chat.** Every message is a paid model call. Agents talking freely
  manufacture expensive consensus.

**So: keep the transport, make the record a thread.**

### The prerequisite, and it is not the channel

Do not build this expecting it to fix the live failure. Across three runs the Design
manager never populated `needs_to_know` at all — no channel shape helps when nothing
is ever handed to it. That is a *decision-schema* problem (below), and it is
confounded by every `ANALYSIS` call being served by the Anthropic fallback model
while Gemini's credits are depleted.

Order: give deciders eyes (fix 1), let them proceed under a flagged assumption
(fix 2), stop charging them their work to wait (fix 3) — then widen what asking can
express. A better phone does not help someone who cannot look anything up and loses
their desk every time they use it.

Get a manager actually asking, on a capable model, before building any of 4.

### The decision schema: optional fields lose to prose

`ManagerDecision` is `assessment` (free prose) + `needs_to_know` (optional list) +
`actions` (optional list). Only the two lists are wired to anything; `assessment` is
recorded and read by nothing.

Observed three times: the model put its question in `assessment` — clearly, and by
the right test, *"that distinction determines the decision entirely"* — and left
`needs_to_know` empty. A free prose field adjacent to structured fields absorbs the
content that belongs in them, and an optional list defaults to empty for free.

Naming the field in the prompt was not enough (commit `cc38622`); the next run
invented the missing fact instead, which is worse.

**The structural candidate**: replace the flat shape with three mutually exclusive
outcomes — `commission(actions)`, `nothing_worth_doing(reason)`, `blocked(questions)`
— so the manager must classify its own state rather than leave a field empty.
"Nothing to do *because* I do not know" then becomes inexpressible: it is a visible
contradiction the model has to commit to, instead of an absence nobody can see.

This is the more promising fix, and it is untested. Try it on a capable model first.

## Phase 2.6 — Day 2: the half of the conversation we cannot see

**Status**: scoped 2026-09-15, not built. Raised independently in an outside review
of this plan ("Customer Success / Ops"), which is decent corroboration — it was the
one gap in that review that this system genuinely has.

**The factory can see sends, bounces and complaints, and never a reply.** Every
"reply" in the codebase today is a Telegram reply *from Jesse*. Outreach success is
currently knowable only through 14-day product-level revenue attribution, which
means a Marketing manager built on it would be judging blind — the exact failure the
instrumentation principle exists to prevent. It also means a live product's users
have no way to reach the company at all.

### It is not a new department, and that distinction is the whole design

The outside review proposed a "Customer Success" node sitting after Marketing. That
is the pipeline reflex, and it is wrong here for the same reason the sideways-comms
insight is right: **a reply is an inbound event, not a stage.** What it concerns is
decided by its content, not by its position in a flow:

- *"Your signup form does nothing"* → Development.
- *"Take me off this list"* → Compliance, and it must reach the erasure path.
- *"Do you support X?"* → a question about the product, answerable from its record.
- *"We'd pay for Y"* → demand evidence, which is Discovery's domain.

So it classifies and routes, reusing `comms.route()` and the answering machinery that
already exists. No new department, no new channel — a fifth entry point into the one
we have. A Customer Success *department* would have to own all four of those and
would own none of them well.

### What already exists (do not rebuild)

- `POST /webhooks/mailgun` in `api/app.py`, signature-verified against
  `mailgun_webhook_signing_key`.
- `OutreachLog.bounced_at` / `.complained_at`, with repository setters, and the
  deliverability freeze that reads them.
- **Correlation is already solved**: `email.py` sets `v:outreach_log_id` on every
  send, and `deliverability.py` already correlates events back by it.
- `erase_lead()` — GDPR Art. 17 erasure that also tombstones the address into
  `blacklist.txt` so `send_email()` refuses it permanently.
- `OutreachLog.lead_id` is nullable `ON DELETE SET NULL` precisely so a send record
  survives erasure as an anonymous audit row.

### What to build

- A Mailgun **inbound** route → a new endpoint, verified the same way.
- `engines/marketing/inbound.py`: correlate to an `OutreachLog` row by the existing
  echo (or reply-to addressing), store the reply, classify it — interested / not
  interested / unsubscribe / question / bug report / out-of-office.
- `replied_at` + classification on `OutreachLog`, mirroring the existing
  bounce/complaint columns.
- Routing: anything actionable becomes an ordinary task or a routed question through
  `comms`, so the reply reaches whoever it actually concerns.

### Rails, and they are not optional

- **An unsubscribe request arriving by reply must reach the blacklist through
  `erase_lead`**, not a new path, and must fail closed. A missed unsubscribe is a
  legal problem, not a quality problem.
- **Reply bodies are personal data written by real people**, and unlike everything
  else in this database they were not generated by us. They need a retention answer
  before the first one is stored, and they must never reach a prompt unredacted —
  the briefing rule (counts, never addresses) applies with more force here, not less.
- **A reply is untrusted input.** It is written by someone outside the company and
  will at some point contain text that reads like an instruction. It is data.
- **What a reply may trigger is a judgement with a spend attached.** "We'd pay for Y"
  autonomously commissioning a build is the GM-shaped decision (effort scaled to
  traction) arriving early. Until that exists, the rail is that a reply can raise
  evidence and can ask, but cannot itself commission paid work.

### Why it comes before Phase 3

A Marketing manager without reply data is judging on a 14-day revenue lag. This gives
the learning loop a same-week signal and turns `outreach_conversion_rate` into
something with a numerator.

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
