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

## The operator is an escalation target, not a subscriber (applies to every phase)

Jesse's rule, stated when asked whether inbound customer replies should ping him:
*"the system shouldn't need me, and if it does it would escalate."*

It reads like a preference about notifications and it is not. It fixes what the
human is *for* in this architecture:

- **Needing a person is a failure state, not a feature.** It already has a channel —
  `interrupts` — with a status, a reason vocabulary, a reply path, and a record of
  whether it was ever dealt with.
- **So there is no second, softer channel.** No "FYI" stream, no digest, no per-event
  ping. Anything worth a person's attention is worth an escalation; anything not
  worth an escalation is not worth a notification either. A parallel notify path is
  how the operator quietly becomes a subscriber to work the system was supposed to
  do, and then the backlog of things nobody handled is invisible because it scrolled
  past instead of staying open.
- **Which makes escalation volume load-bearing.** If the only way to reach a person
  is an escalation, escalations must stay rare and each must mean something. That is
  why duplicate storms and false auto-resolutions are not cosmetic bugs: eight
  `provider_outage` notices for one unchanged condition, seven of them closing
  themselves with "it is answering again", is the channel being devalued until a real
  one is indistinguishable from noise.
- **Corollary for anything unclassifiable.** When the system cannot decide what
  something is, the answer is *escalate*, never *notify*. An escalation has to be
  resolved; a message just goes by.

Practical test for any new surface: if you are about to add a way to tell Jesse
something, either it is an escalation or it should not reach him at all.

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

## A value must say how it is known (applies to every phase)

The third companion. Instruments let an agent *see the present*; a living record lets
it know *what has already been learned*; this one is about the third state, which the
system had no way to express at all: **what is not known.**

**Every value here is asserted, and none of them is qualified.** A house style
returned because Design wrote one and a house style returned because nobody has looks
identical. A task marked done because it finished and one marked done because it was
skipped look identical. A provider reported recovered because it answered and one
reported recovered because it went quiet look identical. In each case a default
impersonates a finding, and the consumer cannot tell.

This is not a new idea in this codebase -- it is a principle already stated here, and
applied in four places out of dozens:

- `DepartmentAnswer.confidence` is `established | inferred | unknown`, with the reason
  written on it: *"an answer that sounds confident and is invented is worse than no
  answer, because the asker cannot tell the difference."* **That sentence describes
  most of the defects found on 2026-09-15.**
- `repair.py` refuses an agent's claim and diffs the file tree instead: *"recording at
  the point the agent merely claimed success would fill this file with confident
  accounts of fixes that never worked."*
- `render_sources` names every file a truncated review did not see, and tells the
  reviewer to *"withhold approval rather than reasoning about code you never saw."*
- `decide_release` tells its judge outright when no acceptance criteria exist, rather
  than omitting the section.

Every one of those was written by someone reasoning about **an incident they had
seen**. Every gap found in the audit was a case **nobody had imagined yet** -- an
empty department, a report with no question, a dossier missing a section. So the
principle is not missing from this system; it is applied reactively, one incident at
a time, which is the spot-fixing this file exists to replace.

### The reviewable question

> **What does this return when it has nothing to say?**

Run it down any handoff and the wrong answers are all *plausible*, which is exactly
why nothing caught them: the house style, an empty directory, `{}` read as health,
`done`, the whole report read as a question, a brief listing only what is known.

It is worth stating what this does **not** mean. Not every empty return is a defect:
`ProductProgress.converging` returns `None` rather than `False` when there is no prior
attempt, and that is correct because `None` is a distinct value the consumer can act
on. The test is not "does it return empty" but **"can the consumer tell the difference,
and does the difference change what it should do?"**

### Where it binds

At **boundaries**. Inside one function a default is fine. Across a handoff -- a value
another department consumes, a row another role reads, a prompt another agent acts on
-- an unqualified default is a lie the consumer has no way to detect. Notably, not one
defect found on 2026-09-15 was a component computing the wrong answer internally.
Every single one sat at a joint.

## Phase 1.5 — the record (prerequisite for Phase 2 and Phase 3)

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

#### The blocker is not access. It is that a one-shot call cannot look anything up.

Scoped first as a *manager* problem. It is not — it is every `ctx.llm.parse()` call
site in the system, and managers are only where it was noticed. `generate_scaffold`
and `generate_landing_page` are one-shot structured calls too, so they cannot read a
product's acceptance criteria either; `generate_landing_page` carries a `requirements`
parameter as a workaround for exactly that, and `generate_scaffold` does not even have
the workaround. Jesse's question is what surfaced it: *"I thought the requirements were
a general thing for all of the agents."*

They are. `product_dossier` renders them under "What it must do", and
`read_product_dossier` is a registered tool — so every **agent** can read them, and no
`parse()` caller can. The dividing line is not department or seniority, it is whether
the call site is a loop or a single shot.

Two consequences, and they point in different directions:

- **Short term, per standard**: one chokepoint that answers "what is this product held
  to", resolved once and passed identically to every builder and every reviewer —
  the shape `design_for_product`/`effective_design` already established for design
  guidelines. Today `live_requirements` is resolved in four separate places, each
  deciding for itself whether to pass it on, which is how a regeneration came to be
  judged against criteria it was never shown.
- **Long term**: the fix below (make the call agentic) is not a manager feature. Any
  one-shot call that has to *decide* something against a standard has the same hole.

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

### What is already configured, verified 2026-09-15

- **MX already points at Mailgun EU.** `mail.manifestaudit.com` resolves to
  `mxa/mxb.eu.mailgun.org`, so the DNS half of receiving is done.
- **EU region**, which is the right side of the GDPR question for data residency.
- **Zero inbound routes exist** — checked against both the US and EU API endpoints.
  Sending is fully configured; receiving has nowhere to go. One route needs creating
  (match the recipient, forward to the new endpoint). That is an account write to a
  live mail domain, so it is Jesse's to make or to explicitly authorise.

### What "GDPR compliant" means concretely here

Decided, not left to the implementer — this is the one part where getting it wrong
is a legal problem rather than a quality one.

- **Minimise at the door.** Store the classification, `replied_at`, and the
  correlation. The body is the only part that is unavoidably personal data, and it
  is kept only as long as it is needed to act on.
- **Retention**: reply bodies purged after 90 days. The classification and timestamp
  survive as the business record, because neither identifies anybody once the body
  is gone.
- **Erasure must reach it.** `erase_lead()` currently deletes the `Lead` and
  tombstones the address; it must also purge reply bodies, exactly as
  `erase_validation_signals()` already does for interest submissions. An erasure that
  misses a store is not an erasure.
- **A reply body reaching a model is a transfer to a third-party processor.** Classify
  from as little as possible, and never put a raw body into a prompt that did not need
  it. The briefing rule — counts, never addresses — binds harder here, not softer.
- **Right of access**: whatever is stored must be retrievable by address, or the
  erasure path cannot be honestly claimed to be complete either.

### What a reply may trigger: the human answer

Jesse's steer was to look at how a human system handles this, and it gives a sharper
rail than the blanket one first written here.

**In a real company, support triages and never decides.** The person reading the
inbox does not choose what gets built; the person with budget authority does. But
support *does* act immediately on one class of thing, and the line it draws is not
"how important" — it is **maintenance versus investment**:

| What arrives | What a human does | Where it goes here |
|---|---|---|
| "It's broken" | Files it. Dev fixes it. **No approval** — fixing what you already sold is not new spend. | An `Issue`, into the existing repair path |
| "How do I…?" | Answers from what is known. | The answering machinery already built |
| "Take me off this list" | Does it immediately, no judgement. | `erase_lead()`, fail-closed |
| "Can you add X?" | Logs it. **Never builds it alone.** One request is a data point; five is a product signal. | Demand evidence, Discovery's domain |
| "We'd pay for X" | Strongest signal, still evidence. Qualifies it, passes it to whoever owns the roadmap. | Demand evidence, flagged |

So the rail is **maintenance is autonomous, new capability is not**. A bug report on a
live product flows straight into `patch_product` — that is repairing what was already
promised, and it is what the department exists to do. A feature request accumulates as
evidence and waits for the effort-to-traction judgement, which is the general
manager's and does not exist yet.

**No new mechanism is needed for any of the four.** Issues already dedup on
`times_seen`, so "one bug reported by four customers" is already the right shape and
was already solved (BUG_LOG 23). Demand evidence already has a home. Questions already
have a channel. Erasure already has a path. Day 2 is a classifier and four existing
doors.

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
- **No reply notifies Jesse.** Per the operator principle above: a reply the system
  can handle is handled, and one it cannot is an *escalation*, not a ping. A reply
  that cannot be classified confidently escalates rather than sitting unread — the
  same conservatism `comms.route()` already applies to an unroutable question.
- **Maintenance is autonomous; new capability is not.** See the triage table above.
  A bug report on a product already sold is not new spend and goes straight into the
  repair path; anything that would build something new accumulates as evidence and
  waits for the effort-to-traction judgement that does not exist yet.

### Why it comes before Phase 3

A Marketing manager without reply data is judging on a 14-day revenue lag. This gives
the learning loop a same-week signal and turns `outreach_conversion_rate` into
something with a numerator.

## The department model (added 2026-09-15, after a full live run)

Jesse's reference for what a software business actually needs, and what this
factory has against it. Everything from Phase 2.7 on is organised around this.

| Needed | Owns | Here today |
|---|---|---|
| **Product / Management** | What are we building? **Who is it for?** Priorities, strategy, roadmap, decisions. | **Missing.** Discovery covers "what" only; Supervisor dispatches work but decides no strategy. |
| **Development / Engineering** | Build, maintain, infrastructure, releases. | ✓ |
| **Marketing / Sales** | Positioning, acquisition, campaigns. | ✓ |
| **Finance / Administration** | Money in and out, budgets, obligations. | ✓ |
| **Customer Support / Success** | Customer problems, feedback, retention. | **Missing** — scoped as Phase 2.6. |

Not departments at this size, per the same reference: Data & Analytics (initially
Product/Management), Data Engineering (Engineering), BI (Finance/Management),
HR, Legal (Compliance is an exception here — an autonomous system sending cold
email genuinely needs it), Security and IT (Engineering).

**Design is deliberately a department despite not being in that list**, and the
justification is a capability rather than an org chart: it is the only role that can
*look* at a page. Everything else reads source, and a page can be correct in every
line and still read as machine-generated. A human org needs no such split because
every human can see. Product owns the audience Design designs *for*.

### What one live run showed was missing

The 2026-09-15 run (idea → plan → product, 8m57s, $0.96) worked at every step and
stopped at the last. Five gaps, and they are not five problems:

1. Development never knew it needed design guidelines.
2. Design does not know who it is building for.
3. No audience understanding exists anywhere — **every idea in the database has
   zero evidence rows.**
4. Nothing decided how much effort the product deserved: a throwaway validation
   page got three Opus reviewers, $0.61 of a $0.96 run.
5. Analytics cannot change anything — both consumers are yes/no gates.

**2, 3, 4 and 5 are one missing owner**, and Jesse's own reference says where it
lives: Product/Management owns "who is it for", priorities, and (initially) data
and analytics. Four thin departments for a company with one product would be the
"human team on steroids" failure this whole plan exists to avoid.

**1 is not an org gap at all.** A human developer does not build blind, but neither
do they usually chase the brief — someone groomed the ticket. Development should
not have to *realise* it lacks guidance; something should have judged the product
not ready to build. That is Product's call, and framing it that way avoids the
hardcoded gate ("no build without guidelines") that would otherwise creep in.

## Phase 2.7 — make absence visible

**Status**: first pass shipped 2026-09-15. This is the applied form of *"a value must
say how it is known"* above -- read that for the general rule and the reviewable
question; this section is what it means for the five gaps the live runs exposed.

Shipped: `product_brief` states what is not known about a product (no stated standard,
no design guidelines, never deployed, never shown to anyone); `product_dossier` says
the same where it used to omit a section, and separates "no open issues" from "nothing
has ever reviewed this"; `_render_domain` distinguishes an empty department from a
covered one; and a blocked report with no request no longer becomes a question.

Still open: `effective_design` returning the house style is now *stated* in the brief
but not at the point of use, and audience remains inferred everywhere with nothing
marking it as inferred. Both are taken up by Phase 2.8 below, which is where "stated"
becomes "usable by something other than a prompt" — the 2026-09-16 investigation found
that stating an absence in prose had not been enough to make anything act on it.

Every one of those gaps is currently a **silent default**. `effective_design(None)`
returns the house style and says nothing. `product_brief` states what is known and
never what is not. So "nobody has researched this audience" and "this audience is
thoroughly understood" produce identical-looking inputs to every decision.

The change is not a rule that blocks a build. It is that **a brief states what is
missing as plainly as what is present**: no audience researched, no design
guidelines written, no review ever performed, zero interest after one approach.

This is the instrumentation principle applied to absence — *ask what an expert looks
at to decide, then check whether the system can show them that.* An absence is
information, and it is the one kind this system systematically hides. Fixing it
requires no new department and no ordering rule, and every decider benefits the same
day.

**Prerequisite running underneath everything below**: asking has to actually work.
Four live manager runs, zero questions ever sent (Phase 2.5). Design was built as a
full department and spent its entire life reporting "nothing worth doing" — first
because it could not ask, then because it could not see its own products, then
because its output could not be stored. **Standing up another department before the
plumbing works repeats that exactly.**

## Phase 2.8 — facts, not stages (added 2026-09-16)

**Status**: proposed. This is what has to be true for Phase 3 to be an architecture
rather than a longer pipeline.

Jesse, shown a proposal to add a Compliance → Design handoff with a build-side gate:
*"I feel like we're building a pipeline again instead of an architecture."*

The roadmap had already ruled that out, one section above: *"Development should not
have to realise it lacks guidance ... that framing avoids the hardcoded gate ('no
build without guidelines') that would otherwise creep in."* It crept in anyway, one
day later, proposed by someone who had read that line. Writing the principle down did
not prevent it, because **nothing in the code makes adding an edge harder than
declaring a fact**, and an edge is the shorter diff every time.

### The pipeline is in the type system

```python
class ProductStatus(StrEnum):
    VALIDATING → PLANNED → BUILDING → REVIEWING → PATCHING → LIVE / RETIRED
```

A status is a **position on a conveyor**. It cannot express what a product *has*. Two
products in `VALIDATING` — one with design guidelines, one without — are the same
state to this model, and that is precisely why nothing noticed the missing ones. Every
question worth asking about a product ("is it ready to build?", "does anyone know who
it is for?") is a question about its *contents*, and the state model has no vocabulary
for contents at all.

This is why the fix keeps coming out as an edge. If the only thing you can say about a
product is where it sits in the line, then the only thing you can change is the order
of the line.

### The needs list already exists, and is flattened into prose

`handbook.product_brief` derives exactly the list this phase needs, from rows:

```
nobody has written what this product must do
nobody has written how it should look
it has never been deployed, so nobody has looked at it
nobody outside the company has seen it
```

with a comment stating the property that matters — *"derived from rows like everything
else here, so it cannot claim an absence that is not real."* Then the next line joins
them into an English sentence for a prompt.

Phase 2.7 made absence visible. It is visible **as prose, to a language model**.
Nothing else can act on it, wait on it, count it, or report it — the same shape as
`check_business_health` computing real concerns and stringifying their count.

So this phase is not a new subsystem. The predicates exist and are already correct.
They need names and a reader that is not a prompt.

### What tracing the 2026-09-15 run showed (traced 2026-09-16)

- **Three cross-engine handoffs exist in the entire company**: Compliance →
  Development, Development → Marketing, Supervisor → Development. Every single one of
  Design's task enqueues is Design → Design. **Design has no inbox. It has a
  calendar.**
- `WRITE_DESIGN_GUIDELINES` exists, works, and can only ever be commissioned by
  Design's own 24-hour tick deciding to commission it.
- The Design manager **did** run, at 00:00:07, and `design_domain` already computes
  which products lack guidelines — so it could see the gap. It commissioned nothing,
  and **that was the correct call**: the page had been built, reviewed, repaired and
  closed 1h34m earlier. Guidelines written for a shipped page are documentation, not
  direction. *Design's judgement was right; its position in time was wrong.* That is a
  fourth reason this department has never produced anything, and unlike the first
  three it cannot be fixed by making Design better at its job.
- The page was generated **once** and repaired **eight times**. Reading this run as
  "the page keeps being regenerated" was wrong, and it sent the first day of
  investigation at the generator instead of the repair loop.
- The only artefact in Development actually named a *design brief*
  (`development/design_brief.py`) is generated **from the deployed page, after it
  ships**, for the dashboard. The company writes its design brief by looking at what
  it already built.

### The shape

- **Facts are first-class.** Has requirements, has design guidelines, has an audience,
  is deployed, has reach, has open issues. Already derived — stop flattening them.
- **Owners are structure; sequence is not.** "Design writes guidelines" is a fact about
  the org and belongs in code. "Design runs after Compliance and before Development" is
  the conveyor and belongs nowhere.
- **A manager decides; a resolver does not fire.** The load-bearing rule. If unmet
  facts mechanically trigger work, the conveyor has been rebuilt in a new notation with
  extra steps. It must stay a judgement — *including* the judgement that house style is
  right for this product, which then becomes a recorded decision and makes the
  builder's "decided by the design department for this specific product" label true
  for the first time.
- **Status becomes derived, not assigned.** `LIVE` is a summary of facts, not a step
  someone set. Honest caveat: status is written throughout the codebase, so this is the
  large half. Facts can land alongside status first and status can stay authoritative
  until they have proven themselves.
- **A need must be satisfiable by deciding not to meet it**, or the system deadlocks
  the first time something is genuinely not worth doing.

### The test that tells the two apart

Jesse's own example is the acceptance criterion. Audience understanding does not exist
today — `design/work.py` reads `audience = idea.target_niche if idea else "unknown"`,
one sentence carried from Discovery.

**Adding "we need to know our audience" should cost one fact and one owner. If it costs
an edit to a sequence, this phase was not built.**

### Which of the four 2026-09-16 findings this subsumes

| Finding | Subsumed? |
|---|---|
| The served landing-page prompt was four weeks stale | **No.** Independent infrastructure defect — a bootstrap copy served forever while the code moved on. Fixed at the read (`get_prompt` resolves by provenance); see BUG_LOG. Nothing organisational about it. |
| Design is not in the build chain | **Yes** — and correctly, via Phase 3 judging the product not ready rather than via a gate. This phase is what lets that judgement be expressed without an edge. |
| The house-style fallback is labelled as a decision | **Yes**, and cleanly: once house style only ever reaches a builder through an explicit decision, the label stops being a lie. One change closes two findings. |
| The repair loop cannot verify what it was sent to fix | **No.** See below. |

### What this does not fix, and must be fixed separately

Every branch of the repair objective in `development/repair.py` ends *"Verify your
change with exactly this command"* — the test suite. A missing webfont and a framework
default palette are invisible to it. The suite passed before the eight repair attempts
and after them, so the loop produced eight confirmations of success against a defect it
could not measure. `repair.py` contains no reference to design guidelines at all.

Phase 3 makes this matter *less*, by getting the page built to a real standard the
first time. It does not fix it: any design defect found after a build hits the same
wall, and the loop will keep reporting success. **A repair loop whose finish line
cannot see the defect is its own bug**, in the same family as every other finding here
— the success criterion does not measure the goal.

Related and also unfixed: the landing-page prompt's palette ban names **Tailwind** and
forbids its ramp by hex value; the page that triggered this investigation used
**Bootstrap**. The ban was written against the last failure rather than the general
case, which is the scaffolding-instead-of-judgement pattern in miniature.

### Correction, 2026-09-17: a gap list is a diagnostic, not a plan

Everything above is right about *facts* and wrong about *what to do with them*. Four
live runs found it, and Jesse's question is what named it: **"let's say this logic
emerges in a human team — how would they solve it, or how would this problem not come
up in the first place?"**

**A human team never asks "what is missing?"** Nobody sits down and enumerates *this
product lacks audience research, requirements, design guidelines, deployment and market
reach; which of these has an owner?* They start from the goal and step back once — we
want to know if bakers will pay, so bakers need to see something, so someone has to
make a page, so we need to know what it says — and then they do the first thing.

The question a human asks is **"what is the next thing I can actually do?"** The
question Phase 2.8 built asks **"what is absent, and who owns it?"** Those look alike
and are not: *a list of absences has no direction, and a goal has an arrow.*

#### The specific defect

`Unmet` silently mixes two kinds of thing:

- **Means** — design guidelines, requirements, a built page. Things you do.
- **Ends** — reach, interest. The reason you are doing any of it.

Presented as one flat list of equivalent gaps, the objective becomes indistinguishable
from an obstacle. Run 10's decider, verbatim: *"the more urgent blocker is that nobody
outside the company has encountered this product yet."* It treated the goal as a
blocker, and that sentence is impossible in a human team — nobody says "we are blocked
on having customers". You are blocked on what stands between you and customers.

The third piece is ownership. In a company, *"nobody owns X"* is a **staffing
observation raised upward**, not a reason today's work cannot proceed; a designer does
not down tools because there is no data analyst. `_render_options` told the decider
*"you cannot pursue these"*, and a reasonable decider concluded the product was stuck.
An unownable need is a fact about the company and does not belong in a decision about a
product at all.

#### Why this produced special-casing, which is the tell

Three runs stalled on three different needs, each read as an unclearable wall:
`requirements` (run 8), `deployment` (run 7, a menu bug), `reach` (run 10). Each was
fixed by annotating that one need with its causal story — "requirements come out of the
build", "reach needs something deployed". That is **reconstructing, one entry at a
time, the direction a goal would have supplied for free**, and Jesse called it before
run 10 finished: *"I feel like we're hardcoding a lot again."*

The variance is the evidence. Runs 7, 8, 9 and 10 saw near-identical facts and chose
three different orderings, because from a flat list of absences there is no reason to
prefer any order. That is also what makes *"the system should be smart enough to know
design needs to finish before dev"* mechanical rather than hopeful: with a goal and a
sense of what is possible now, ordering falls out.

#### What changes, and what does not

**Keeps**: `ProductFacts`, `Unmet`, `OWNER`, the inbox, and everything about deriving
absence from rows rather than prose. The diagnostic is good and it is what made all of
this visible.

**Changes**: what the decider is asked. Not *"which unmet needs will you pursue"* but
*"this product exists to find out whether bakery owners will pay for it; here is what
it has and what is possible right now — what is the next thing worth doing?"*

- Ends are stated as the goal, never listed as gaps to close.
- Needs nobody owns are reported upward as a staffing gap, not offered as options.
- What is *possible now* is distinct from what is *absent* — a page cannot be reached
  before it is built, and that is physics rather than a priority.

The line to hold, and it is a fine one: **a physical prerequisite is a fact and may be
encoded; a priority is a judgement and may not.** "Reach requires something deployed"
is the former. "Design before development" is the latter, and stays the decider's call.

## Phase 2.9 — let the reviewer see the whole journey (added 2026-09-21)

**Status**: scoped, not built. Found by Jesse signing up to his own live product.

Run 12 shipped a genuinely good page — Fraunces and Public Sans, a bakery palette,
zero repair cycles. Then Jesse submitted his email, clicked the confirmation link,
and landed on plain monospace text served from **the factory's own domain**, with the
unsubscribe as a bare relative path the browser offered to download as a `.txt`. The
email before it said "Thanks for your interest" with no product name and no sender.

`PAGE_POLICY` forbids exactly this — *"nothing internal to this factory appears on the
page"* — and the journey ends on factory infrastructure one hop past where the policy
applies.

### Why no LLM saw it: the fix for BUG_LOG 36 made the journey a dead end

BUG_LOG 36: AUDITOR reviewed a landing page, was told to "actually try to do that thing
yourself", invented an email address, and its fake signup landed in the table the
build-or-scrap decision is gated on. The fix gave it the reserved
`@factory-selftest.invalid` address, and `api/app.py`'s handling of that address is the
whole story:

```python
if submission.email.strip().lower().endswith(SELF_TEST_SUFFIX):
    # It must not leave a trace: a recorded row would inflate the very count
    # a build-or-scrap decision is gated on, and a confirmation email to an
    # address nobody owns is a guaranteed bounce...
```

Answered identically to a real submission, records nothing, sends nothing. So the
reviewer sees a form that submits successfully and **the journey stops there** — no
email is composed, no token is issued, and everything downstream became invisible to
review by construction. Not an oversight and not a reasoning failure: the previous
bug's fix removed the next bug's evidence.

The conflation is the familiar one. *"Must not corrupt the demand signal"* is true.
*"Must leave no trace at all"* is what got implemented. Those are two different facts,
and the correct isolation is **excluded from counting**, not **not recorded**.

Two further walls sit behind it, real but not binding: `tools/browser.py` pins reviews
to the product's own origin (*"the reviewer only ever needs the product's own origin, so
that is all it gets"* — true when the product was the page), and no employee has an
inbox.

### The fix: expand what the reviewer is allowed to see

Jesse's framing, and it is smaller than specifying the journey in requirements: *"what
if we simply expand the scope of what it's allowed to see, so it sees the email
confirmation that gets sent and the subsequent webpage it directs to?"*

- **Make the self-test journey complete but isolated.** A `@factory-selftest.invalid`
  submission records a signal *marked as self-test and excluded from every demand
  count*, composes the confirmation email and **retains it rather than sending it** (the
  `.invalid` domain is unmailable by design, which is correct — the point is to compose
  and keep it), and issues a real token. The reviewer can then read exactly what a
  visitor would receive and follow exactly where it leads.
- **Widen the origin allowlist by one origin**: the factory's own public API origin,
  read from settings. Not an opening of the allowlist — it is the origin this product's
  own form already posts to.

Then nothing else is needed. The existing reviewers see the whole journey and judge it
against the standards they already hold, the defect is reported through the normal path,
and repair fixes it. **No requirements rewrite, no redirect plumbing, no confirmation-page
template.** We do not write down what a confirmation page should look like; we let the
reviewer look at the one we have and say.

That is the difference between this and the version first scoped here, which specified
the journey, moved the page into the product, added a redirect and composed the email —
four decisions made by us about what good looks like, where two changes to *visibility*
let the system reach them itself.

### Broaden, do not enumerate (Jesse, 2026-09-21)

*"This should broaden its scope, not narrow it down with new rules that are
hardcoded."* The test each change has to pass: **is it removing a narrowing, or adding
a rule?** Stated as three removals, none of which names the confirmation flow:

- **What this company sends is kept and readable.** Not "retain the confirmation
  email". `send_email` is the single chokepoint every message already passes through,
  and `OutreachLog` records a subject and *no body* — so nobody can read what was
  actually said to a real prospect either. Recording subject, body, sender and product
  for every send, including the ones composed but deliberately not sent (dry run,
  suppressed, self-test), makes the whole of what the company says reviewable without
  anyone deciding in advance which messages are interesting. It closes a blind spot on
  outreach nobody had raised.
- **A self-test signal is a signal that does not count.** Not a branch: a flag. Today
  the endpoint has two behaviours and the reserved address takes the silent one. After,
  every submission takes the same path and carries `is_self_test`, and every demand
  count excludes those rows. This *removes* a special case rather than adding one.
- **The reviewer's world is the product's journey, not the product's page.** The
  allowlist gains the origin the product's own confirmation link points at, derived from
  the same setting that builds that link rather than written down as a literal.

The version scoped before this one failed the test: "specify the journey in
requirements", "redirect the visitor back", "compose the email with the product name"
are four decisions about what good looks like, taken by us, in advance. These three are
the reviewer being allowed to see what already happens.

**Carries a real obligation.** Storing message bodies stores personal data about the
recipient, so `erase_lead` and `erase_validation_signals` must delete retained messages
too, or the erasure path silently stops being complete. The address is already stored in
`OutreachLog`, so this is more of an existing category rather than a new one — but the
erasure coverage is part of the work, not a follow-up.

### What this still does not reach

A *real* visitor's email. The reviewer reads the composed message, not a delivered one,
so a transport failure between composition and inbox stays invisible until Phase 2.6.
That is a smaller gap than it sounds: every defect Jesse found — no product name, no
sender, no real unsubscribe link, plain-text confirmation page — is present in the
composed message and the served page, and therefore visible to a reviewer today.

## Phase 2.10 — how this company learns anything (added 2026-09-21)

**Status**: built 2026-09-21 (read half and write half). See *Built 2026-09-21* below.

Jesse's question, and it is the one that generalises: *"how can we get it to do the
thing we want without explicitly needing to ask it? How would a human operate?"*

A designer is not told "brand the confirmation page" on every project. Somebody told
them once, or they watched a bad one land badly, and it became theirs. The standard
lives in the person and travels to every job they ever do. Ours is amnesiac: every
review is a first-day hire who has never been told anything and never will be, so the
only place a standard can live is the brief — which means every standard must be
written down in advance by somebody who already knew to want it. That is the loop this
phase exists to break.

### What exists, and the exact shape of the hole

`memory/store.py` is well built and correctly scoped: `global`, `engine:`,
`capability:<role>` (*"a lesson about how a job goes belongs to the job, not to
whichever product happened to surface it"*), `product:`. `MemoryKind.PREFERENCE`
already means *"an operator decision to honour going forward"* and is the one kind
exempt from eviction.

Ten `remember()` call sites. Every one writes what the **system** observed — a build
outcome, a heal failure, a compliance verdict, a churn lesson. Exactly one writes a
`PREFERENCE`: a human's deliberate deploy-with-known-risk, scoped to that product.

**Zero writes in `hitl/`.** A human reply becomes `enqueue_guided_patch` — one
instruction, for one product, consumed and discarded. On 2026-09-21 Jesse read a
confirmation email and said it was "boring and indescriptive… it could say Alex from
Rise". There is no path by which that sentence becomes anything the company carries.
It reached a human collaborator, who turned it into code. **That is the loop to
escape**: every standard arriving as a code change, authored by whoever happened to be
listening.

### The general shape

An observation from outside this company is **two things at once**: an instruction
about the thing at hand, and sometimes a standard that outlives it. Only the first is
taken today.

- **Extraction is a field, not a step.** The same finding as `Ask`/`needs_to_know`:
  `report_product_issue` shipped as an elected action and two live runs never chose it.
  Whatever handles a reply must always be asked "is there a standard in this that
  outlives this product?", never given the option to skip it.
- **Whose it is, is the judgement.** This product, this role, this department, or the
  company. `scope_for` already expresses all four; picking is a judgement and must not
  become a rule. A standard filed on `capability:` is the one that behaves like a
  professional standard — it applies on the next product, which is the whole point.
- **Sources, in the order they will arrive.** Operator replies to escalations (exist,
  discarded). Operator observations that never became escalations — today those reach a
  human and become commits. Customer replies (Phase 2.6). Market outcomes: conversion,
  churn, revenue. All four are one shape: *something outside said something, and some
  part of this company should carry it forward.* Build it for the shape, not for the
  first source.

### The constraint, and it is not negotiable

**A remembered preference may shape judgement. It may never relax a constraint.**

The end goal is an organisation whose objective is fixed and whose rails are fixed,
with judgement everywhere between (see the memory note on the end goal). A learning
write path is precisely where that line gets crossed by accident: a preference that
softens GDPR erasure, the suppression list, the spend cap or the campaign-approval gate
is one that must never be written, however reasonably it was said. Extraction gets a
allowlist of what a preference may be *about*, or it becomes the mechanism by which an
autonomous system talks its way out of its own safety rails.

### Why this one matters more than it looks

The end goal names *"an external persistent memory system stores its mission,
strategies, experiments, decisions, financial results and lessons learned"* and an
organisation that *"learns from real-world feedback"*. **This is that write path.**
Without it there is no accumulation: every strategic review starts from zero, every
lesson is re-derived at full cost, and the only durable learning mechanism the company
has is a human writing code — which does not scale past the human.

It is also the cheapest correction available to the confirmation-page problem. The
alternative is extending Design's remit to the journey, which is another rule written
by us in advance. This is: say it once, and any reviewer of any product carries it,
including to the things neither of us has thought of yet.

### Built 2026-09-21

`learning/feedback.py`, hooked into `hitl/interrupts.py::resolve_reply` — the single
funnel every human reply passes through exactly once, whether it arrived by Telegram
or through the dashboard, and whichever per-reason branch acted on it.

- **`capture()` runs on every reply, after it has acted.** One insert, no model, so it
  cannot delay a domain attachment or an idea approval. The judgement itself is
  `TaskKind.LEARN_FROM_FEEDBACK`, its own task — because a step inside `resolve_reply`
  would either block the reply on a model being reachable or be wrapped in a
  `try`/`except` that makes a skipped judgement invisible. As a task, a skipped one is
  a task that did not run.
- **The exchange includes what the reply already did.** Without it a model reads
  "attach rise-audit.com" and proposes remembering the hostname — a lesson to redo
  finished work. Knowing the instruction was carried out is what moves the question
  from *what did they want* to *what did they mean that keeps being true*.
- **Scope is judged, then checked against what actually recalls.** `capability:` is
  read by `agents/spawner.py` and `development/preship.py`, both keyed on a
  `RoleName`; `engine:` by `scopes_for`. A lesson filed under `capability:ux_reviewer`
  — a perfectly reasonable string this company has no reader for — would exist, count
  against the scope budget, and never be read by anything, which is indistinguishable
  from never having been written. So an unknown name widens to the company scope,
  over-broad but read, and logs. A `product:` lesson with no product is dropped
  instead: widening a deliberately narrow lesson is the one direction scope must never
  move by accident.
- **The rail holds by refusing, not by softening.** A lesson the extraction judges
  would loosen the suppression list, erasure, the spend cap, campaign approval, or
  what may be claimed to a customer is not filed in any form — it raises
  `CONSTRAINT_CHALLENGED` (its own reason, for the same cause this enum's docstring
  gives for the other three: a spend cap arriving under a `[LEGAL_UNCERTAINTY]` prefix
  misdescribes it to the only reader who matters) and says plainly that a limit which
  should move belongs in code where it is reviewed, not in memory where it applies
  quietly. Replies to that escalation are not themselves learned from, or every turn
  of the cycle is another escalation.

**And the half that would have made all of it write-only.** Ten `remember()` sites and
five `recall()` sites, and *every reader read work that was already finished* — two
reviewers, the heal loop, discovery, agent spawns. Nothing that **writes** recalled
anything. A standard learned from a reply could only ever arrive as a complaint about
a built page, never as a criterion before the build.

So `write_landing_page_requirements` now recalls. There rather than in the builder's
prompt, because criteria are read three times — the builder works to them, the pre-ship
reviewer judges against them, the release decision weighs them — and a standard that
becomes a criterion reaches all three. It is handed over as standards, explicitly *not*
as criteria: turning one into a criterion, or setting it aside, stays the judgement it
should be.

### What this does not reach, and it is the interesting part

Jesse's two observations were the confirmation **email** ("could say Alex from Rise")
and the confirmation **page**. Both are literal strings in `api/app.py` — `subject="Please
confirm your interest"` and a `text/plain` Response. Factory infrastructure, byte-identical
for every product, authored by us.

So the honest answer to *"why didn't the LLMs see it"* is not that they could not see
it. Run 15's pre-ship reviewer opened that exact confirmation URL, screenshotted the
exact page Jesse then screenshotted, and reported `blocking: 0, notes: 0`. **It saw it
and had no lever.** Its only lever is a patch to the product workspace, and neither
string is in the product workspace. A reviewer that cannot change a thing has no reason
to develop a standard about it, and a lesson filed about it would be unactionable by
construction.

That is an ownership gap, not a memory gap, and it is the next question: the signup
journey past the form belongs to the product, not to the factory. Until it does, no
amount of learning reaches it.

## Phase 3 — Product / Management (absorbs the general manager)

**Rewritten 2026-09-15.** This was "a second department manager, most likely
Marketing", with the general manager as a separate Phase 4. That was wrong in two
ways: Marketing is not where the live gaps are, and **in a company this size the
product manager and the general manager are the same person.** Splitting them
produces two thin roles that each need the other's context.

One department, owning:

- **What we build and who it is for.** The audience question, with standing — a
  *step* can be skipped silently, which is exactly what happened; an owner can say
  "we do not know who this is for, so I am not authorising a build yet."
- **How much effort a thing deserves.** The judgement the Design manager already
  reached for and could not make, because there was nobody to ask. Scaling effort to
  traction — a landing page nobody has visited and a product with real customers
  should not get the same treatment.
- **What the numbers mean.** Data and analytics initially, per the reference above.
  Once it can see traction it revises who the product is for, which is gap 5 and
  needs no new machinery — it is the department doing its job a second time.
- The former Phase 4 content: cross-product memory scope, `check_business_health`'s
  concerns as standing context rather than a discarded count, durable cross-tick
  awareness, and escalation on judgement rather than a fixed enum.

### Development currently marks its own homework

Jesse, on watching two runs end in `rethink`: *"a dev or designer don't say 'nah
we're not doing this', it goes further than that — a team discusses instead of just
throwing something out."*

He is right, and the code is explicit about it. `decide_release()` runs with
`engine=EngineName.DEVELOPMENT`, is called from `development/graph.py`, and can
return `rethink` or `abandon` — verdicts that halt the product. **Development decides
whether Development's own work is fundamentally misconceived.**

The verdicts do not deserve the same owner:

- **`ship` / `fix_first`** are legitimately Development's. "Is my work finished, and
  if not, what is the smallest thing that would finish it" is exactly what a
  developer decides.
- **`rethink` / `abandon`** are not. They say the *brief* or the *product* is wrong,
  which is a judgement about what we are building and for whom.

And the two are currently handled by **one branch**:

```python
if decision.decision in ("rethink", "abandon"):
    interrupts.request(...)   # → a human
    set_product_status(PLANNED)
```

Four verdicts, a genuine four-way judgement, dispatched through a two-branch table
where two of them collapse into "ask a person". `rethink` does not escalate because
escalation is right for it — it escalates because **its recipient does not exist**,
and it shares an `if` with the one verdict a human genuinely should own.

The tell is in the escalation's own reply hint: *"Reply with guidance to try again,
or REJECT to retire this product."* It asks the operator for the guidance a product
owner would give. Which makes the operator the missing department, and that is what
the operator principle above rules out.

**What it should be.** `rethink` is a department saying *"I cannot proceed because
something upstream is wrong"* — which is a blocked question with a recipient, a shape
comms already carries. Once Product exists it becomes a **routed question**: the work
parks, Product answers with a revised brief, a re-scope, or a decision to stop, and
the work resumes with that answer. A discussion with a record, rather than one role
throwing the product out.

`abandon` still escalates — but *from Product*, and only after Product has judged it.
Retiring a product is spending Jesse's money, which is correctly his call.

Worth being precise about what does **not** need changing: the evidence is already
multi-party. Bug Council's three reviewers, the gap check, the contract and SEO gates
all contribute, and one role weighs them. That is the shape of a team and it works.
The problem is narrower — **who is allowed to conclude the product is wrong.**

One more observation from the same run, and it is an argument for making this an
agent rather than a one-shot call: the judge flagged that every canonical and API
reference points at `manifestaudit.com` while the product is branded LedgerLens, and
called it *"a sign the build does not have a coherent owner"*. Half right — the
canonical URL genuinely is wrong; the analytics beacon legitimately points home.
**Nothing tells it which domains are the factory's own infrastructure**, and a
one-shot `parse()` cannot look it up or ask. It had to guess, and guessed
confidently.

### How this would have changed the live run

1. Product sees a new product: one sentence of audience, no guidelines, zero
   traction, $29 price.
2. It judges effort — a validation page for an unproven idea is a cheap instrument.
   **That call alone removes the $0.61 of Opus review**, two-thirds of the run.
3. It decides whether we know enough about the buyer to design for them. For a $29
   test it likely says the idea text is enough — and that is a decision on the
   record rather than a step nobody ran.
4. It commissions guidelines, or judges the house style sufficient. Either way the
   builder gets an answer instead of a silent fallback.
5. The build works to a standard that specifies a palette and fonts, so **the two
   findings that blocked this run do not arise** — they existed only because the
   page invented a look and was then judged against a standard nobody had applied.

**Limits, stated honestly.** This fixes causes. The `accepted_issues` defect — the
release judge accepting a finding and the system ignoring it on the next pass — is
an independent bug that would still have blocked this product, and must be fixed
regardless of any org change.

## Phase 4 — the rest of the organisation

Not finished after Phase 3. What remains, in no fixed order:

- **Customer Support / Success** — Phase 2.6, placed after Product because that is
  when an inbound reply has somewhere meaningful to land.
- **A Marketing manager** — the original Phase 3. Still worth doing; it was never
  the most urgent, and it needs Phase 2.6's reply data or it judges on a 14-day
  revenue lag.
- **A Discovery manager** — deliberately later. Discovery is where spend scales
  fastest and a bad judgement costs most.
- **Audience understanding as real work rather than inference.** Zero evidence rows
  today. Product owning the question does not by itself make the answer researched,
  and the difference between "inferred from one sentence" and "observed" must stay
  visible (Phase 2.7).

---

## Department build order (added 2026-09-15)

Distinct from the *record* build order below, which is about documentation types.
This one is about who exists. Each step is a prerequisite for the next, and the
reason is the same each time: **a department nothing can reach will reason well and
commission nothing.** Design proved that over its entire life.

1. **Asking works** (Phase 2.5). Four live runs, zero questions. Everything below
   assumes a department can be reached and can reach others.
2. **Absence is visible** (Phase 2.7). Cheap, no new roles, and it is what lets any
   decider notice it is missing a standard rather than silently accept a default.
3. **Absence is a fact, not a sentence** (Phase 2.8). The same predicates, readable by
   something other than a prompt. Without it Phase 3 has nothing to judge *on*, and
   every gap it finds will be closed by adding one more edge. **Read its 2026-09-17
   correction before building on it**: the facts are right, and asking a decider to
   plan from a flat list of absences is not — a gap list is a diagnostic, and work is
   organised from a goal.
4. **Product / Management** (Phase 3), absorbing the general manager. Closes four of
   the five gaps the live run exposed.
5. **Customer Support / Success** (Phase 2.6), then a **Marketing manager**, then
   **Discovery** (Phase 4).

Independent of all of it, and not blocked by any of it: the `accepted_issues` defect
from the 2026-09-15 run. A release judge that accepts a known flaw and is overruled
by the next pass makes "done" unreachable, which is the exact failure the four-way
release judgement was built to escape. Joining it as of 2026-09-16: the repair loop
verifying every fix with a test command that cannot observe a design defect, which
belongs to no department and blocks no phase.

## Handover: how the current system gives way

Nothing is ripped out up front — each phase retires a piece only after its
replacement has been observed working:

| Phase | Retired once proven |
|---|---|
| 1 | `_content_freshness_issue`'s LLM call (deterministic uptime check stays) |
| 1 | ✅ `audit-live-products`' dependence on the factory being idle |
| 2.7 | silent fallbacks that make an absence indistinguishable from a decision |
| 3 | fixed-enum escalation reasons Product can judge better; `check_business_health` becomes standing context rather than its own interrupt; the assumption that every product deserves the same review spend |
| 4 | per-department fixed maintenance slugs a manager now covers |

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
