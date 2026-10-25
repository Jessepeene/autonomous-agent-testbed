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

## The bigger goal, and where everything below actually sits (added 2026-09-23)

Every phase in this file — 1 through the last of Phase 2, Phase 3, Phase 4 — was
written inside one frame: this factory, its five engines, the SaaS products it
ships. None of them once mentions the thing all of it is actually for. That was a
real gap, not a simplification, and it is worth being exact about why it happened
and what it means for reading everything that follows.

This is the actual objective, verbatim, and it has not changed since Jesse stated it:

> An autonomous, AI-driven business organization whose sole high-level objective is
> to maximize sustainable long-term profit. Rather than prescribing a specific
> business model, the system operates as a General Manager that can independently
> research markets, identify opportunities, choose between freelancing, services,
> SaaS, digital products, or other revenue streams, execute projects, acquire
> customers, manage finances, and build tools or modify its own workflows when
> necessary. It uses specialized AI agents and coding environments to carry out
> tasks, while an external persistent memory system stores its mission, strategies,
> experiments, decisions, financial results, and lessons learned. An immutable
> objective, hierarchical decision structure, and recurring strategic review cycle
> ensure that the organization maintains its long-term direction, learns from
> real-world feedback, and continuously reassesses how to generate profit without
> being constrained to a predetermined path.

And this is why a SaaS factory exists at all, rather than building that directly:

> First we test, keep our original goal: the SaaS factory. From that we learn, and
> when it's able to do this we can go further — test it building its own tools,
> browsing the internet, making its own goals.

**Read everything below as work done inside a test harness, not as the goal.** The
SaaS factory was chosen because it is the fastest way to exercise four things the
real objective actually needs — judgement-based decisions instead of hardcoded
rules, learning from real outcomes instead of vibes, finding and fixing defects in
itself, and escalating at a genuine boundary instead of faking through it. Phase 1's
department managers, Phase 2's dozen sub-phases, Phase 3's product ownership, Phase
4's remaining departments: every one of them is proving one of those four things
*on* a SaaS product, not building a company that only ever makes SaaS products.

**Checked against the founding paragraph directly, three things are not covered by
anything numbered below, and each is a real gap rather than a phase nobody got to
yet:**

- **The objective is not actually immutable.** It is held by discipline — everyone
  in this conversation agreeing not to touch it — not by architecture. Nothing in
  the code would notice or stop it drifting.
- **There is no recurring strategic review.** Every review in this system is
  per-product (`decide_release`) or per-department (a manager's own tick). Nothing
  ever asks the question at the level of the whole company: are we making progress
  toward the actual objective, and should we be doing something else entirely?
- **There is no path to "choose between revenue streams... without a predetermined
  path."** Phase 2.13 scoped the mechanism (a method is a registry entry, the same
  shape as a mutable prompt) but a method is still, today, exactly one thing: build
  and validate a SaaS idea.

Phase 5 is that work. It is not a continuation of the SaaS factory's own department
structure — it is the layer that decides whether the SaaS factory, or anything else
below it, is still the right thing to be doing.

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

## Neutral prompting (applies to every phase, added 2026-09-26)

Jesse's question, checking the discipline this project already tries to hold:
*"we don't accidentally point the LLM in a certain direction by using specific
examples and such."* Two real instances, one already fixed and one found while
looking -- and they are opposite failure modes of the same rule: **everything a
model actually reads should be there because someone chose it, reviewed it as an
instruction, and stated it at the right level of generality.** Neither of the two
below broke that by being written carelessly; both broke it by something true
elsewhere (documentation should explain *why*; an example should be concrete) being
true in the wrong place.

### An example that was too narrow, found and half-fixed before today

`HOUSE_STYLE.must_avoid` (`development/requirements.py`) named the Tailwind
slate/gray ramp's exact hex values as a banned palette, because a human had to
catch a page built from exactly that ramp by eye -- "it looks machine-generated"
was true, checkable, and invisible to every automated judge until it was named.
But the page that triggered the investigation used **Bootstrap**, not Tailwind, and
the ban as first written would have missed it: a model reads a specific example as
permission for everything the example does not literally say. This was later
generalised to *"the Tailwind slate/gray ramp ... or any other framework's stock
palette"* -- but only that one entry. Its siblings in the same list kept the same
defect: *"-apple-system, Inter or Space Grotesk as the only typeface"* names three
safe defaults and implicitly clears a fourth; *"three feature cards in a row with
an icon above each"* clears four cards, or three with a checkmark instead of an
icon. Generalised the same way as the palette entry, 2026-09-26 (`must_avoid` in
both `development/requirements.py` and `scaffold/generator.py`, kept in sync).

**The fix is never to remove the concrete example.** A rule stated only as an
abstraction ("avoid generic design") gives a model nothing to recognise. The fix is
always to keep the example *and* add the generalisation it was always standing in
for -- "X, or any other Y" -- so the example teaches the shape of the failure
instead of describing one specific instance of it.

### A leak nobody would have found by reading prompts

Checking every `SYSTEM`/prompt string in the codebase would have missed this
entirely, because it is not in any of them. `ctx.llm.parse(schema, ...)` sends
`schema.model_json_schema()` to the provider as the structured-output schema, and
Pydantic's `model_json_schema()` emits a model's own class **docstring** as the
schema's `description` -- verbatim, to the model, on every single call. Confirmed
live: `StrategicReviewVerdict`'s and `ProductNext`'s docstrings, each several
paragraphs written for the next engineer reading the source -- citing phase
numbers, other class names, file paths (`"Phase 3/4/5c"`, `` `ManagerDecision` ``,
`BUG_LOG.md`) -- were reaching the model as part of the request, an entire
unreviewed second prompt hiding behind something that does not look like a prompt
at all. Neither provider even rejects it, which is exactly why nothing had ever
surfaced the way a rejected request would have.

Fixed the same day, at the one place both structured-output pipelines build a
schema (`llm/schema.py`'s `strip_descriptions`, called from `json_schema_for` for
Anthropic/DeepSeek and separately from Gemini's own schema-cleaning pass, which is
differently shaped and does not otherwise touch `description`). The rule going
forward: **a docstring documents code for the next engineer; if a model needs to be
told something, it goes in `system`/`prompt`, in words written and reviewed as a
prompt.** A schema's `description` is never a place instruction is allowed to live,
so nothing needs auditing there going forward -- it is simply never sent.

### The standing check, for every future prompt or contract

Two different questions, both worth asking before trusting a new LLM call:

- **For a rule with an example**: does the example teach the general shape of what
  to avoid, or does it teach the model to avoid exactly and only what was named?
  If removing the specific instance would leave nothing recognisable, keep it --
  and check that a generalising clause sits next to it, the way "or any other
  framework's stock palette" now does for every sibling in the same list.
- **For anything a `Contract` subclass carries**: is every word that could reach
  the model something someone actually wrote as an instruction, or is some of it
  documentation that happens to be readable by whatever reads the schema? A
  docstring is not neutral just because nobody meant it as a prompt --
  `model_json_schema()` does not know the difference, and now nothing downstream
  has to check by hand.

### Audited against both checks, 2026-09-27

Every `SYSTEM`/`POLICY`/`PROMPT` string in the codebase, on request -- not a sample.
One more real instance of the first failure mode, in a place worth being exact
about: `learning/feedback.py`'s refusal to let a lesson loosen a rail named five
things (do-not-contact, erasure, spend limit, campaign approval, truthfulness),
written 2026-09-21, before Phase 2.13 (2026-09-22) and Phase 5a (2026-09-24) named
the rest of what this company now holds as a rail -- credentials, the objective, the
task queue, the database, deploy and rollback. A reply arguing to loosen any of
those would not have tripped `touches_a_rail`, since none of them were named. Same
mechanism as the palette ban, applied to a safety list instead of a style one, and
arguably worse for it: fixed the same way, with both the missing names and a
closing "and any other hard limit... whether or not it is named here", plus an
explicit instruction to treat uncertainty as a rail rather than resolve it in the
reply's favour. `DISCRIMINATOR_SYSTEM`'s "construction, healthcare, government, and
heavy industry" industry list, judged borderline (the general criteria are stated in
full before the examples), was closed the same way for consistency rather than left
as a judgement call nobody would revisit.

Everything else read -- `RELEASE_SYSTEM`, `GAP_SYSTEM`, `PAGE_POLICY`,
`LIA_SYSTEM_PROMPT`, the orchestrator's routing prompt, both Discovery prompts, both
role prompts in `agents/roles.py`, `design_brief.py`, `outreach.py`,
`marketing/leads.py`, all three of `marketing/providers/website.py`'s, `comms.py`'s
`ASKING_GUIDANCE`, `hitl/interrupts.py`'s plain-language explainer, and
`learning/maintenance.py`'s extractor -- held up. Several go further than merely
neutral: `RELEASE_SYSTEM` names `rethink`/`abandon` as "real options and
under-used ones" specifically to counter the conservative bias a reviewer with no
stake in shipping would otherwise have; `DEMAND_SYSTEM` states the failure mode in
both directions ("be willing to say thin or insufficient... but do not be
reflexively negative either"); `marketing/providers/website.py`'s query generator
already closes its own platform blocklist with "or similar platforms/aggregators".
Worth noting as the pattern that makes a prompt worth trusting, not just the defect
that makes one worth fixing.

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

**Status**: **built.** `factory/comms.py` carries the whole shape -- `ask`,
`park_until_answered`, `answers_for`, `raise_blocked_question`, `deliver`. The
status line below was never updated after it shipped; identified 2026-09-15 from
the Design department's first live runs.
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

**Status**: **built** -- `factory/facts.py`, `supervisor/product_decision.py` and
`supervisor/attention.py` are this phase. What follows is what has to be true for
Phase 3 to be an architecture
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

**Status**: **half built.** The *seeing* half shipped (`93c69e0`, `f70931b`,
`892a891`): a reviewer now reads the mail it sent, follows the confirmation link
and opens the page behind it. The *owning* half did not -- the confirmation mail
and both plain-text pages are still constants in `api/app.py`, factory
infrastructure identical for every product, which no agent can author or change.
See "the last of Phase 2" below. Found by Jesse signing up to his own live product.

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

## Phase 2.11 — saying which kind of statement this is (added 2026-09-21)

**Status**: built the same day, prompted by a live failure.

A run parked on a human with two blocking MUSTs, neither of which could ever be met:

- `MUST not met: The page does not know its own final address. It is deployed to a
  Vercel subdomain first and a custom domain is attached afterwards, by a person` —
  a `PAGE_POLICY` line, word for word. A sentence explaining why something is *fine*,
  filed as a defect.
- `MUST not met: OUT OF SCOPE: A pricing page or price display` — a thing deliberately
  not built, reported as missing.

Two repair cycles moved nothing, and the release decision — correctly — refused to
spend a third: *"a criterion that should not exist as a MUST at all… continued
attempts to fix the unmet MUST will cycle without progress."* It diagnosed the problem
precisely and had no lever except to stop.

### Neither was the model being careless

`render_for_prompt` emitted four kinds of statement — a goal, graded criteria,
deliberate non-goals, standing policy — as one undifferentiated block, and `gaps.py`
handed the whole thing over under the heading *"The acceptance criteria this product
was built to meet."* The reviewer was told the policy lines were criteria, so it
graded them. And `RequirementGap.tier` is `Literal["must", "should"]`, so a reviewer
correctly instructed that out-of-scope violations are departures had **nowhere to put
one** and squeezed it into `must`, where everything blocks.

`GAP_SYSTEM` did say *"Never invent a criterion. Judge only what you were given."* An
instruction in a prompt standing in for a check in code — and this project has now
paid for that substitution more than once.

### The two general fixes

**Statements carry their force.** Each section now says what a *departure* from it
means, because that is the thing a worker needs and could not recover: a MUST absent
means not done, a SHOULD absent is weighed, an out-of-scope item *present* is scope
creep, and a policy line cannot be departed from by a work product at all. `policy` is
a parameter rather than welded in — standing policy is not a landing-page idea, every
department that judges work against criteria has rules sitting above them, and the
page policy is just the first one written.

**`factory/grounding.py` — does this claim refer to something the claimant was given?**
A worker naming items in prose can name anything; when the name resolves to nothing it
was handed, the report is a statement about the report. Three places already did a
narrow version by hand (`product_decision.py` drops an unknown `Unmet`,
`learning/feedback.py` drops a capability the company lacks), and the number only goes
up as more of this company's internal traffic becomes one part naming something to
another. *An autonomous organisation that cannot tell "I found a problem with X" from
"I have invented an X" accumulates work that cannot be finished.*

So the gap check derives the tier from **where a claim actually grounds** rather than
taking the reviewer's word. Matching is overlap-on-the-shorter-text, not Jaccard: the
live case was a 150-character truncation of a 300-character policy line, every token
present, which Jaccard scores 0.5 and calls a different statement.

### Two things that keep this from becoming a mute button

**Nothing is discarded.** A claim grounding nowhere is still filed — just not as a
failure against a criterion nobody wrote. A reviewer noticing something the standard
never mentioned is the holistic signal a criteria-shaped gate is structurally blind
to, and it is worth more than the noise of one low issue that closes itself.

**Whatever is rendered must be in the catalogue.** Caught while building it: design
`must_hold` is graded by the same pass that grades `must`, so leaving it out would
have silently downgraded a real "stock framework palette, no webfont" gap — the exact
defect that only a human ever caught — to an observation, by a change meant to stop
over-blocking. `must_avoid` needed its own group for the same reason: it is "present
is a departure" like `out_of_scope`, but it is a stated defect rather than scope
creep, so it blocks. `design` is now resolved once and handed to both the judge and
the catalogue, and a test asserts every group the catalogue emits has a departure
rule — the rule, not the values, which is the shape that would have caught DESIGN
missing from `routing.PRIORITY`.

## Phase 2.12 — why this company cannot learn when to stop (added 2026-09-22)

**Status**: investigated, not built. This is the bridge from the SaaS factory to the
profit objective, and it is mostly schema rather than new machinery.

Jesse, on his own instinct and then on the system's: *"I want everything to be perfect
before shipping, but in an actual business case at one point you need to say 'this is
enough'. How do we get an AI agent to do this?"* — and then the correction that made it
the right question: *"instead of finding the answer to that question we should look
into the psychology and decision making behind it."*

Defining "enough" per artifact is the pipeline mistake wearing new clothes. A
model-agnostic general manager cannot have us pre-writing a bar for landing pages, then
services, then freelance deliverables. **"Enough" is not a quality threshold. It is a
comparison** — further effort here has lower expected return than the next-best use of
that effort. It is a property of the alternatives, never of the thing.

### The psychology, which turns out to be structure

Human perfectionism in shipping decisions is not a character trait. Defects are
visible, specific and attributable to a person; the cost of delay is invisible, diffuse
and attributed to nobody. Nobody is ever blamed for the customer they did not get.
Being slow is individually rational even where it is collectively ruinous.

**This system reproduced that asymmetry exactly and became a perfectionist for the same
reason.** Every instrument it owns measures defects. Nothing measures the cost of not
shipping. `RELEASE_SYSTEM` is *told* to weigh it — *"a page that never goes live answers
nothing"* — and is handed no fact about it: no clock, no counterfactual, no alternatives.
It is asked to balance a scale with one pan loaded.

### What is learnable here, exactly

A thing is learnable only if it is a `TunableParameter` (versioned prompt or weight
vector) with one `target_metric`, accumulating `LearningEpisode` rows — Bernoulli
success/failure — that a trial compares against baseline. Four findings follow.

**The allowlist contains only makers.** Seven mutable keys: idea generator,
discriminator, scoring weights, lead planner, outreach writer, landing-page builder,
sender identity. Every one produces an artifact. **Not one judge is on it** — no
`RELEASE_SYSTEM`, no `GAP_SYSTEM`, no `LANDING_PAGE_REQUIREMENTS_SYSTEM`, no
`PAGE_POLICY`, no `product_decision.SYSTEM`. *This company can learn how to make things.
It cannot learn how to decide things.* The bar lives in two frozen prompts — the one
writing the MUSTs and the one enforcing "an unmet MUST is not shippable" — and no
outcome can move either. Simon's adaptive aspiration level, the half everyone drops, is
architecturally forbidden.

**An outcome can only ever teach one thing.** `LearningEpisode` has a single non-null
FK. `graph.py`'s validation result is recorded against `landing_page_prompt_version` and
nothing else, so when an idea fails in the market the only thing that learns is the
prompt that wrote the page. Idea selection, requirements, the release decision and the
outreach never hear. **The page writer is blamed for a bad idea.**

**The metric that would reveal a bar set too low does not exist.**
`landing_page_interest_rate` cannot distinguish *"nobody wants this"* from *"the form was
broken"*: both are `passed=False, sent=40, signals=0`. So the failure mode of a low bar
is invisible to the only metric that bears on it, and there is no evidence path by which
a bar ever comes *down* — only up, by someone adding another MUST. A ratchet with no
pawl on the other side. That, and not anyone's disposition, is the institutional
perfectionism.

**Decisions have no identity.** `release.decided` records decision, rationale, unmet
MUSTs, accepted issues and blocking count — genuinely rich — as JSONB in `task_events`,
with nothing any outcome can reference. Good evidence, unaddressable.

### What it would take

1. **Decisions get identity**, so an outcome can point back at one. The content is
   already being recorded; it needs a row and a key.
2. **An outcome becomes evidence about several decisions** — a join replacing the single
   FK. This is the keystone: 1, 3 and every form of profit attribution are blocked
   behind it.
3. **A metric that measures a decision rather than an artifact.** For the shipping bar,
   *signal validity*: did shipping this produce usable information? Forty visitors and
   zero signups on a working page is a successful experiment with a negative result; the
   same numbers on a broken form is a wasted one. Nothing tells them apart today, and
   that distinction **is** the aspiration level's feedback signal.
4. **Resolve the safety tension deliberately.** The allowlist freezing every judge is not
   an oversight — it is what stands between this and a system that talks itself into
   shipping anything. The honest resolution is to **learn the evidence, not the
   instruction**: judges' standards stay fixed and immutable, while the system learns
   *which facts predict good outcomes* and shows the judge those. The rails do not move;
   the bar adapts. Same line as `learning/feedback.py`'s refusal to file a lesson that
   would relax a constraint.

### Why this is the bridge to the objective

**Profit is a decision-quality metric, not an artifact-quality one.** A company that can
only learn to make things better and never to decide better cannot optimise profit — it
can optimise craft, forever, which is precisely the trap. The end goal's *"learns from
real-world feedback"* is blocked on item 2 and nothing else.

### The authority error this explains

`RELEASE_SYSTEM`'s *"a MUST that is not met means it is not shippable"* hands a
source-reading reviewer an absolute veto over the judge that actually weighs purpose and
cost of delay. The reviewer bears none of the cost of not shipping — no stake, no
horizon, no view of alternatives — so its verdict trends conservative at no cost to
itself. **The right to say "this is enough" belongs to whoever bears the opportunity
cost.** That is what a hierarchical decision structure is *for*: each level holds a
different horizon and a different set of alternatives, so each answers "enough"
differently and legitimately, and a reviewer reports evidence rather than holding a
veto.

## Phase 2.13 — improving everything about itself except the objective (added 2026-09-22)

**Status**: scoped, not built. Depends on Phase 2.12's attribution keystone.

Jesse: *"a lot of these small bugs were small things that the bigger goal of an
autonomous system should catch on its own"*, and then the generalisation — *"make the
ai be able to improve everything about itself except for its objective."*

### Do not build a second system

This already exists, for one artifact kind. The learning loop **is** self-improvement
with a gate: `TunableParameter` (a versioned artifact), `target_metric` (what better
means), `LearningEpisode` (evidence), and `trials.py` running challenger against
baseline with a power analysis and rolling back losers. It is restricted to seven
prompt strings, all of them makers (Phase 2.12).

So the move is to generalise the *unit* from "a tunable prompt" to "a thing about
itself this company may change", and let the allowlist become the honest statement of
what that is. One registry, one question per entry: **may this change, and what
evidence would show the change helped?**

### Two mechanisms, chosen by the kind of question

Jesse's framing was *"it tests the new vs current system and picks the better
performing one"*, which is right and is the second of two. Which applies depends on
whether the change has a *right* answer or a *better* one:

- **Right answer — a defect.** A tri-state read through `bool()` is simply wrong. No
  human engineer A/Bs that; they know, and they verify with a test. Correctness is
  checkable without an experiment, so the loop is: detect the shape, write the failing
  test, make it pass. **The system has none of this today.**
- **Better answer — a judgement.** Prompt wording, who does what first, where a
  threshold sits. No ground truth, so running both and measuring is the only honest
  route. **The system already has this** and it needs generalising, not inventing.

The recurring defect shapes are drawn from bugs that actually cost runs, not from
theory: *something is written and nothing reads it* (`GapReport.undetermined` and
`.met`, both `len()`'d into a log line); *an enum member missing from a table meant to
cover all members* (`DESIGN` absent from `routing.PRIORITY`); *two derivations of one
question* (`_render_options` against `_commission`); *a tri-state read as a boolean*;
*an idempotency key scoped to a lifetime when the question is "is this in flight"*.
The codebase already writes exactly these checks by hand, one at a time, after each
bug — `test_every_engine_is_ranked`, `test_no_prose_field_an_llm_writes_carries_a_max_length`,
`test_every_group_the_catalogue_emits_has_a_departure_rule`. Reactive, human-authored,
and the loop worth closing.

### The asymmetry that stops it inverting

**Adding a check is cheap and near-automatic. Weakening or removing one is a
rail-level change.** A system that can edit its own checks can delete the check that
catches it, and if the metric is "tests pass" then deleting a test is the cheapest
improvement available. The evaluator must not be reachable by the thing being
evaluated — the same line `learning/feedback.py` holds when it refuses to file a
lesson that would relax a constraint.

### The rails, on Jesse's criterion

*"Keep them as limited as possible, protect the main objective and anything that might
kill itself when edited."* That is better than an enumerated list, because the liveness
criterion **derives** most of the compliance rails rather than naming them: losing the
sending domain to spam complaints or the Stripe account to a chargeback pattern is
self-killing, so deliverability and consent fall out of it. What it does not cover is
truthfulness to a customer, which is not fatal in the short run — that belongs under
the objective, since a company that lies to buyers is not earning sustainably.

So the immutable set is: **the objective, and the things whose breakage ends the
company's ability to operate or to recover** — the tick loop, the task queue, the
database, deploy and rollback, credentials, and the evaluator itself. Everything else
is in scope, which is most of the system.

### What blocks it

Attribution, again. A challenger can only be judged if an outcome can be attributed to
it, and `LearningEpisode` has exactly one foreign key. A code change, a workflow change
or a new check has no way to be evaluated at all today. Phase 2.12's join is the
precondition for this phase, not a neighbour of it.

**And champion-challenger needs volume the company does not have** — 0 leads, 0
signals, 0 customers, so nothing market-facing can be measured. But internal outcome
signals exist right now: heal cycles to ship (run 18 shipped at zero; run 17 took three
and was abandoned), first-pass blocking count, ship versus rethink, time to live. Those
are real measures of the build chain, available at one per run, and enough to run
challengers against this company's own judges before a single customer exists.

## Phase 2.14 — an outcome should teach more than one thing (added 2026-09-22)

**Status**: step 1 of 4 built 2026-09-22. This is the keystone Phases 2.12 and 2.13
both sit on, and the precondition for measuring profit at all.

### The problem, in one picture

```
        ┌──────────────────────────────┐
        │  OUTCOME: validation failed  │
        │  40 approached, 0 signed up  │
        └──────────────┬───────────────┘
                       │  one foreign key
                       ▼
        ┌──────────────────────────────┐
        │   LANDING_PAGE_SYSTEM  v7    │ ← the only thing that learns
        └──────────────────────────────┘

   was the idea any good?          ✗ learns nothing
   were the criteria right?        ✗
   was the design right?           ✗
   was "ship it" the right call?   ✗
   was the outreach any good?      ✗
```

`LearningEpisode` has exactly one non-null foreign key, so a market failure is
recorded against the prompt that wrote the page. **The page writer is blamed for a
bad idea.** What it should be:

```
        ┌──────────────────────────────┐
        │  OUTCOME: 40 approached, 0   │
        └──────────────┬───────────────┘
                       │ is evidence about (many)
   ┌───────┬───────────┼───────────┬────────────┐
   ▼       ▼           ▼           ▼            ▼
 idea   criteria    design    "ship it"     outreach
chosen   written    decided   decision      prompt v3
```

### Four steps

**1. The causal chain — built 2026-09-22.** Attribution walks backwards from an
outcome to the decisions that led to it, which needs a chain. There was none:
`task_events.caused_by_task_id`, whose own docstring calls it *"the column that makes
this a trail rather than another log"*, was populated in **0 of 232 rows**. Both ends
existed -- `supervisor/graph.py` reads the parent out of a claimed task's payload,
`enqueue_task` accepts a payload -- and nothing ever put one into the other.

Fixed with `factory/causality.py`: a context variable set once where a task's handler
begins, read by `enqueue_task`. Ambient rather than a parameter, because the
per-call-site convention is what already failed here, and had also cost five unguarded
recall sites and a policy line graded as a criterion in three separate judges.
`contextvars` rather than a global so two engines running concurrently attribute their
own work.

**2. Decisions get to exist.** A release decision currently lives as JSONB inside a log
row: rich content, no identity, nothing can point at it. It becomes a first-class
record — what was chosen, by whom, why, and what the alternatives were.

**3. `learning_episodes` becomes `outcomes` + `outcome_evidence`.** One outcome, many
subjects, through the same `(subject_kind, subject_id)` pattern `requirements` already
uses. `subject_kind` starts as `parameter` and `decision` and extends to `code` or
`workflow` with no schema change, which is exactly what Phase 2.13 needs.

**4. The links are derived, never hand-written.** Walk the chain back from the
outcome's task and collect the decisions made along the way. Hand-written links are
the same convention that left step 1's column empty.

### Migration safety

Running trials must not break. Each `learning_episodes` row copies to one `outcome`
plus one `outcome_evidence` row with `subject_kind='parameter'`; `episode_counts` reads
through the join filtered the same way and returns **byte-identical** numbers. The old
table stays one release as a fallback. No trial's numerator or denominator moves.

### Against the bigger goal

- **Model-agnostic** — `outcomes` carries a *value*, not only success/failure. A
  validation's outcome is signals; a freelance job's is money. One record.
- **Profit** — a `ledger` row becomes an outcome like any other, so *"which decisions
  produced money"* is a query rather than a guess. Profit is a decision-quality
  metric, and this is what makes decisions measurable.
- **Strategic review** — *"what did we decide, and how did it go"* is currently
  unanswerable.
- **Self-improvement** — a challenger of any kind is just another `subject_kind`.

### Honest limits

This does not make learning *faster*. Spreading one outcome across five decisions
creates no new information, and with zero customers there is still almost nothing to
learn from. It makes things learnable **that currently cannot learn at all**. And
crediting every ancestor decision is *evidence, not proof*: a decision in the chain is
not necessarily a cause, and the record should not pretend otherwise.

## The last of Phase 2 — what remains, in build order (scoped 2026-09-23)

Written after auditing every 2.x status line against the code rather than trusting it,
which found three stale ones: 2.5 and 2.8 are **built**, and 2.9 is **half** built. The
original 2a–2d are substantially done too — the `RESULT:` sentinel is parsed
(`agents/runtime.py`), `comms.py` carries blocked reports and parked questions, and
`capability:` memory gives workers notes. What is genuinely left is five items.

### 0. Return on a decision — the method-neutral metric

**Smallest, and it unblocks the rest.** Both halves of the subtraction are already on
disk and nothing computes the difference: `llm_calls.cost_usd` carries the price of
every call as of the moment it ran and is tagged with `task_id`; `outcomes.value_cents`
carries what came back. Since 2.14, a task chains to the decisions above it.

So: `attribution.decision_return(decision_id)` — sum the cost of every task in that
decision's subtree, against the value of every outcome crediting it. That is the first
figure this company has ever had that survives a change of method, and it is the unit a
general manager actually reasons in.

Two honest limits to state in the code rather than discover later: LLM spend is not the
only cost (Vercel, Mailgun, domains are not attributed to anything), and a decision's
subtree overstates its claim, since being upstream of a cost is not the same as having
caused it.

### 1. Phase 2.12 — make the judges learnable

Three pieces.

- **Signal validity for the validation gate — built 2026-09-23.** The gate's own
  settings comment already said it: *"below this many leads contacted, a response
  rate is statistically meaningless."* It was folded into `passed` anyway, so an
  underpowered run charged the landing-page prompt with a failure, taught Discovery
  the niche was bad, and retired the product — three learning paths fed by evidence
  the code itself calls noise. `conclusive` is now held apart from `passed`;
  inconclusive runs record an `Outcome` outside every trial's arithmetic and write a
  `FACT`, never a `FAILURE`, at Discovery's scope.

  Note: outbound email was never enabled in this system. "Contacted" here means
  outreach composed in dry-run mode; no real person was ever emailed (see
  docs/results-2026.md).

  This surfaced a second question, and Jesse's own read of it was the right one:
  *"unless they have concrete evidence the product will not work they won't retire
  it, right?"* The codebase already agreed with him — for exactly one trigger. The
  stalled-validation sweep (zero leads, checked hourly) retries lead sourcing, then
  tries a different channel, then asks a human; it never calls `retire_product`
  itself, only a human's explicit REJECT does. The validation gate's own inconclusive
  branch disagreed with that, retiring directly. Both are the same fact —
  not enough reach for a valid read — noticed at two different moments, so
  `supervisor/lead_sourcing.py::pursue_or_escalate` is now the one ladder both call.
  Retire is a human's word, full stop, from every path that can reach it.

  Fixing this exposed a second bug already waiting behind it: `marketing/graph.py`
  queued the re-evaluation under `evaluate-validation:{product_id}` — a lifetime key,
  the same shape as BUG_LOG 38 and 42. Once the first evaluation came back
  inconclusive and started a retry instead of retiring, a second real outreach batch
  could never queue its own re-evaluation; the enqueue would return `None`, silently,
  and the product would sit at `VALIDATING` forever. Scoped to the sending task now.

- **Give the judges measured evidence, not a mutable standard — corrected 2026-09-23.**
  This item was scoped, wrongly, as "put judges on the mutation allowlist" — the same
  treatment `LANDING_PAGE_SYSTEM` gets: versioned, champion-challenger tested against an
  outcome metric, promoted or rolled back. Building it as written would have put
  `RELEASE_SYSTEM`'s own standard behind the wheel of the traffic it directs, which is
  exactly what Phase 2.13 already rules out — *"the evaluator must not be reachable by
  the thing being evaluated."* A version of the release judge that ships more
  permissively "wins" on almost any downstream metric, since it manufactures more of the
  very outcomes used to score it, and there is no honest way to tell that apart from a
  version that got genuinely better at judging. `learning/allowlist.py`'s own docstring
  already says as much for the reason: every mutable key is *"commercially-lossy, never
  safety-critical,"* and judge/gate prompts (LIA, Bug Council, the orchestrator) are
  *"permanently excluded, by omission."* `RELEASE_SYSTEM` and
  `LANDING_PAGE_REQUIREMENTS_SYSTEM` are that same kind of prompt and belong in that same
  exclusion, not beside them on the list.

  What the judges actually needed, and what "make the judges learnable" was reaching
  for: real measured facts to weigh, with their standard left untouched — the exact
  pattern `Responsiveness` already set for "what does asking another department cost."
  Built:

  - **`attribution.product_spend_to_date`** — total LLM cost across every attempt this
    product has ever had, a direct `Task.product_id` sum rather than a decision-subtree
    walk, since a release judgement needs the whole history, not one commissioning
    decision's share of it.
  - **`repository.repair_cycle_time`** — median wall-clock time from a `fix_first` or
    `rethink` release decision to the next one, company-wide (per-product would be one
    or two points at today's volume, not a median). The same discipline
    `manager_response_times` holds: measured, never asserted, silent when there is
    nothing to report yet.

  Both are handed to `RELEASE_SYSTEM` as facts it is free to weigh however it judges
  right — *"a real measured fact about the other side of the scale, not a nudge toward
  any particular answer"* — never as an instruction. Guarded to the same standard as the
  rest of this pipeline: wrapped so a session that cannot serve these queries degrades to
  no facts shown rather than crashing the judgement (caught by a unit test passing
  `session=None`, exercising exactly the deterministic slice `decide_release`'s own
  docstring already promises never depends on a database).

### 2. Phase 2.13 — improving itself, detection before repair

- **Phase A, read-only. First shape built 2026-09-23:** `factory/selfcheck.py::
  find_lifetime_scoped_keys`, a standing AST scan for BUG_LOG 38/42/46's exact shape —
  a task kind known to recur (`BUILD_MVP`, `BUILD_LANDING_PAGE`, `EVALUATE_VALIDATION`,
  `PATCH_PRODUCT`) enqueued under a key naming only the entity, not the occurrence.
  Calibrated against the live codebase before being trusted: every current call site
  for those four kinds was read by hand first, confirmed to already carry a second
  interpolation (a task id, an AB test id, a heal-cycle bucket) from each historical
  fix, and a deliberately-once kind (`ASSESS_COMPLIANCE`, correctly keyed on just
  `{idea_id}`) confirmed *not* to trip it — the recurring-kind list is curated, not
  inferred, because whether a kind's work recurs is a fact about what the work is, not
  something a key string alone can answer. `tests/unit/test_idempotency_key_shape.py`
  runs it in CI now; a fourth instance of this shape fails the build the moment it is
  written, wherever it is written.

  The other four shapes (a value nothing reads; an enum member missing from a table
  meant to cover all of them; two derivations of one question; a tri-state read as a
  boolean) are scoped, not built. Each needs the same calibration pass — mypy
  structurally cannot catch the tri-state one, since collapsing `Optional[bool]` via
  truthiness is sometimes intentional; a blanket enum-exhaustiveness scan would flag
  every deliberately-partial mapping in the codebase; "two derivations of one
  question" is not mechanically detectable in general. Building four checkers without
  that rigor to hit a count would be exactly the quick fix this phase exists to avoid.

  Wiring this (or its successors) into a task the factory runs on itself
  periodically, with findings reported somewhere a human sees them, is the next
  concrete step and is deliberately not built yet — a real integration decision (which
  maintenance slug, what happens to a finding) that deserves its own scoping rather
  than being rushed in alongside the checker itself. Today it runs as a CI gate, which
  already restores the bug rate's measurability for whichever of these five shapes
  have working checkers.
- **Phase B, propose only.** A failing test plus a diff that a human merges — how you
  onboard a junior engineer to a build system, not commit access.
- **Generalise the registry** from `MUTABLE_KEYS` to entries carrying a subject kind, a
  gate and what evidence would show the change helped. Adding a *method* is one of these
  entries, which is why method-agnosticism is not a separate programme.

### 3. Phase 2.9's other half — built 2026-09-23, reactive

Three real options were scoped and put to Jesse with a visual comparison (bundle it
into every landing-page build; give it its own commissioned ownership like
`DESIGN_GUIDELINES`; write it only when a reviewer actually flags the generic
version). *"Make it reactive."* Cost paid by zero products until the first real
finding, and it mirrors exactly how the underlying problem was found in the first
place — not a rule generating work in advance, but a person actually using the
product and noticing the confirmation page looked wrong.

**`Product.signup_journey`** (migration 0045) — a small JSONB override, empty for
every product until changed: `confirmation_email_subject`, `confirmation_email_body`,
`confirmation_page_text`. Deliberately excludes `/u/{token}` (GDPR erasure) — that
page's job is to be correct, not persuasive, is not scoped to one product at all
(an unsubscribe token resolves through outreach as often as through a validation
signal), and the one thing worth protecting there is that a template bug can never be
mistaken for a bug in whether erasure actually happened. A rail, left alone on purpose
— the artifact's own callout, carried into the build rather than only into the pitch.

**Rendering is pure and separate from writing.** `factory/signup_journey.py` renders;
`engines/development/signup_journey.py` writes. `confirm_validation_signal()` used to
return only an email — changed to return the product too, since a page one click away
from a product had no way to look anything up about it. The confirmation link and the
erasure link are always composed fresh from the real token, never something a stored
override can hold a stale copy of.

**Checking before building surfaced a real, latent bug** (BUG_LOG 47): every live-
review finding, regardless of what it was about, went to `PATCH_PRODUCT` — a repair
engineer against the product's own workspace. Neither the confirmation email nor its
page is deployed with any product's code; a reviewer that had flagged either would
have handed a repair engineer a brief for a file that does not exist in the place it
was told to look. `ReviewVerdict.surface` (`"product"` | `"signup_journey"`, default
`"product"`) is the fork; `queue_review_patch` routes to a new
`PATCH_SIGNUP_JOURNEY` task instead, handled by a single structured judgement rather
than a spawned agent — there is no code to run and no filesystem to act in.

**The risk named in the artifact, held as a real policy.** `SIGNUP_JOURNEY_POLICY`
governs every rewrite regardless of which finding prompted it: no fabricated
evidence, no internal scaffolding, nothing promised this company cannot guarantee,
and the erasure link is never something the model is allowed to write itself. This is
the first place agent-authored text reaches a real person who gave a real email
address through a channel that is not the reviewed, gated landing page — the policy
exists because that is a different risk texture, named here rather than inherited
by accident from wherever the code happened to be written.

### 4. Phase 2.6 — inbound replies

Never built; there is no inbound webhook at all. Blocked on the Mailgun inbound route,
which is Jesse's to configure. Until then the company can see sends, bounces and
complaints, and never a reply.

### What is deliberately not in this list

2b's second half — letting a recipient *question* a finding rather than only comply or
fail — stays deferred until there is data showing it is needed. And the whole list is
gated behind the same wall as everything else: with 0 leads and 0 customers, items 0
and 1 can only be exercised against internal signals (heal cycles to ship, first-pass
blocking count, ship versus rethink), which is enough to start and not enough to finish.

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
    interrupts.request(...)  # → a human
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

## Phase 5 — the layer above the organisation (added 2026-09-23)

**Status**: scoped, not built. This is the work "The bigger goal" section above
points at — the three things checked against the founding paragraph and found
missing from everything numbered 1 through 4. None of it is SaaS-factory work; all
of it is the same regardless of what the company eventually decides to sell.

Sharpened by an external research survey Jesse brought to this conversation
(*"Designing a Persistent Autonomous AI Economic Entity"*) — read in full and
checked against what this project had already found empirically. Roughly half of
that document independently re-derives things this project arrived at from real
failures rather than theory: Goodhart's-Law proxy optimization is "you get what you
instrument" (Phase 2.12); "LLMs cannot self-correct without external feedback" is
the reason the evaluator must never be reachable by the thing being evaluated
(Phase 2.13's asymmetry); "Agentic Witnessing" is `AUDITOR`'s live-browser review
and why `ReviewVerdict` is extracted from prose rather than self-reported. That
convergence is worth noting once and moving on from — it is evidence the approach
is sound, not a reason to import the rest of the document.

**The rest of it does not transfer, on purpose.** Its economic layer assumes the
entity has no human behind it — ERC-4337 crypto wallets and autonomously-formed
Delaware LLCs exist there because *"traditional banking systems rely on human
identity verification, presenting an insurmountable barrier for digital
entities."* Ours has a human behind it, deliberately, permanently — see the
account-authority discussion this same day: a contract needs an accountable party,
and keeping that party human is a rail, not a limitation to engineer around. None
of Sections 14, most of 15, or the "autonomous legal wrapping" stage of that
document's own blueprint belongs here.

### 5a. An objective that is actually immutable, not just agreed

Today the objective is a paragraph everyone in this conversation has read and no
code has ever seen. Nothing would notice if it drifted, because nothing checks
against it.

The fix is small and does not need the machinery the research survey reaches for
(cryptographic root keys, external policy engines in a language like Cedar). It
needs exactly one property: the objective lives in **one row, written once, read by
every strategic review, and reachable by no code path that could silently change
it.** Not a `Decision` — nothing decides it — and not a settings value, which
this codebase's own agents can already propose edits to via `PATCH_PRODUCT`-style
mechanisms elsewhere. A table with one live row and a `superseded_at` column,
exactly like `requirements`, so a change is possible but is never silent: it always
produces a new row, an old one that stops being current, and a "the objective
changed" fact that a strategic review cannot fail to see.

### 5b. The recurring strategic review

**Never built. Every review in this system today is per-product or per-department.**
Nothing ever reads the company's own state as a whole and asks whether the
objective is being served.

**Triggered on a condition, not a clock** — the same preference this project has
held throughout (a decision worth taking again is keyed on the world changing, not
on time passing; see Phase 2.8's `unmet_signature`). The research survey's own
framing is sharper than anything scoped here before it: trigger on *"depletion of a
compute budget, or consecutive failures of a milestone"* — concretely, N consecutive
`rethink`/`abandon` release decisions across products, or the existing periodic
revenue-reconciliation task's own window closing. Both are conditions this system
already tracks; neither needs a new clock invented for it.

**What it reads**: not one product's issues. The company-level dossier —
`attribution.decision_return()` aggregated across every decision in the window, the
founding-paragraph scorecard itself (which clauses are ✅/🟡/⛔, and whether that
has moved since the last review), and `check_business_health`'s concerns, which
Phase 3 already earmarked as "standing context rather than a discarded count" and
which have nowhere to land until this exists.

**What it is allowed to conclude — a three-way split, not a verdict.** The research
survey names a distinction this project has not made explicit: an **execution
problem** (a bug — feed `factory/selfcheck.py` or `BUG_LOG.md`), a **failed
strategy** (a hypothesis proven wrong — mark the relevant learned parameter for
rollback, same as `trials.py` already does one level down), and an **incorrect
foundational assumption** (something about the current *method* is wrong, not just
the current execution of it — the one case that should ever touch 5c below). Three
different kinds of finding, three different owners; collapsing them into one
"rethink" verdict at the company level would be the identical mistake Phase 3
already found and fixed at the product level.

**Undetermined is a real answer here too.** Phase 2.12 built the discipline that an
underpowered validation is not evidence, one way or the other — `succeeded=None`,
never forced into a verdict. A strategic review reading noisy, low-volume company
data (which is exactly today's data: 0 leads, 1 live product) must have the same
option. A review that cannot tell whether a number moved for a real reason or for
lack of any data yet should say so, explicitly, rather than manufacture a
conclusion to look thorough.

**A concrete test worth adopting, not just the review itself.** The research
survey's *"Evaluator Stress Test"* — deliberately present the company with an easy,
unprofitable, off-mission opportunity and confirm it is correctly declined — is a
genuine red-team for the learning loop rather than for one product's judgement.
Cheap to build once 5a's objective and a real profit metric both exist to check the
decision against; worth keeping on this list rather than building early against
proxies that would not actually test the right thing.

### 5c. Method as something the company may change about itself

Scoped already, not duplicated here: Phase 2.13's registry — *"may this change,
and what evidence would show the change helped"* — already names a method as one
more entry in the same set as a mutable prompt or a workflow. What is missing is
not a new mechanism, it is the first entry that is not SaaS: freelance work,
services, or a digital product added to the registry the same way a prompt change
would be, evaluated the same way, with the same evidence bar.

This is deliberately last, not first. Phase 2.13's own asymmetry principle —
*"adding a check is cheap, removing one is a rail-level change"* — applies the
same way to adding a method: cheap only once 5a and 5b exist to notice if a new
method starts eroding the objective rather than serving it. A second business
model chosen before the company can tell a good decision from a bad one is not
progress toward the goal; it is the same measurement gap Phase 2.12 found, doubled.

### Correction, 2026-09-24: construction is not selection, and neither gets pre-built

5c undersells what "adding a method" actually costs, and Jesse's question sharpened
it: *"the layer that controls the SaaS factory should also be able to build systems
like the SaaS factory — in a sense it's you and me, except we are very specific on
this thing."*

Freelance work or services are not a different value for one mutable field — they
need a different organisation. No Discovery (nobody validates a niche; someone finds
a gig). No cold-outreach Marketing engine. A Development engine that delivers
against a spec rather than owning a product's own database. Different rails, since
a client contract carries different exposure than a subscription. **Choosing a
method and constructing one are different capabilities**, and 5c only ever scoped
the first.

The correction is not "so scope the second one now." That is the exact mistake this
file already made once and reversed (Phase 2.8, 2026-09-17): *"a human team never
asks 'what is missing?'… they start from a goal and step back once."* A shelf of
pre-designed organisations — a freelance department, a services department, sitting
built and waiting for 5b to pick one — has the same defect as a flat list of unmet
needs: designed against a guess about the future rather than a real trigger, by
nobody the design would actually be for, and exactly where a rail goes missing
unnoticed, since nothing live is yet testing it.

**So: no organisation gets built ahead of the review that would justify it.** What
Phase 5 needs is not a constructor standing by — it is that when 5b actually
concludes the current method is failing the objective, constructing the next one is
first-class work, not a fallback. The same process this document is itself a record
of: a goal stated once, departments scoped one at a time against it, rails named as
rails before anything is built. Done *then*, for whatever 5b actually points at —
never in advance, and never for a method nobody has yet had a reason to try.

And whether a given method needs that construction at all is not this document's
to decide in advance either. Some fit inside the departments that already exist —
the original, lighter case 5c described, one more registry entry evaluated the
same way a prompt change would be. Others don't. Which is which is a judgement
against the actual opportunity, not a rule fixed here — the same kind of call
Phase 3 already owns for a single product's effort, scaled up to a method. Fixing
it in advance, even as a spectrum with named tiers, would be this same mistake
happening a third time, one level up again.

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
