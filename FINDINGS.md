# Findings

Drafted from `BUG_LOG.md` — 57 real, logged production incidents, 37 days
(2026-08-18 to 2026-09-24). Every sentence below is checked against the actual
log entry it cites; nothing here is embellished or invented.

## 1. AI output silently lost (#9, #49, #50)

Three different ways the same class of failure showed up: a model produced a
correct answer and the system discarded it without telling anyone.

- **#9**: Gemini's thinking tokens shared the same token ceiling as its visible
  JSON answer. On small-batch calls, thinking alone could consume the entire
  budget, so the model hit `MAX_TOKENS` having emitted no answer at all —
  `SchemaViolation`, a crashed call, with nothing to point at.
- **#49**: thirty fields across twelve schemas carried a `max_length` the model
  was never told existed. A fully correct, 1,501-character release rationale
  failed validation at a 1,500-character cap and the entire response — the
  system's most important decision — was thrown away.
- **#50**: the release judgement inherited a generic 8,192-token default that
  was never sized for what it actually has to weigh (every open issue, the
  convergence history, a full rationale). Thinking consumed the budget before
  the model could emit its decision.

**Fix, each case**: bound the thinking budget explicitly rather than leaving it
unbounded (#9); stop putting `max_length` on any field a model writes, enforced
by a test that enumerates every schema (#49); size `max_tokens` to the actual
call, not a shared default (#50).

**The lesson**: a silent validation failure and a silent token-budget collision
look identical to "nothing happened" from outside the call. Both are a model
doing its job correctly and the harness around it discarding the result before
anyone — human or system — ever sees it. The fix is never "tell the model to
be shorter"; it's removing the artificial ceiling, because the real ceiling
(the API's own `max_tokens`) already exists and enforces itself.

## 2. Quality checks that lied (#33, #37, #44, #48)

Four different checks that reported a confident, wrong answer instead of the
truth — each one indistinguishable, from outside, from a check that had
actually run correctly.

- **#48**: an issue filed `open` was never closed by the one mechanism designed
  to close it, so the "is this converging" check read the same stale blocker
  forever and reported "not converging" on every attempt after the first,
  regardless of whether the defect still existed.
- **#33**: a convergence comparison read its "before" and "after" numbers from
  the same event, written seconds apart in the same pass — comparing a number
  against itself, which is false unconditionally. Every `fix_first` decision
  escalated instead of repairing, on the first attempt, always.
- **#37**: an analytics beacon was silently broken in every real browser (a
  CORS/preflight restriction `sendBeacon` can't satisfy), and the review gate
  that watched it recorded a pass anyway — because it only failed on an
  *attempted-and-failed* action, and this action was never attempted at all.
- **#44**: `ProductProgress.converging` is a three-state value (true / false /
  "not comparable yet"), read through `bool()`. `bool(None)` is `False`, so
  every state carrying no signal was asserted to the release judge as "this is
  not converging" — including a product sitting at zero blocking issues,
  which is what a finished product looks like.

**Fix, each case**: close issues that no longer reproduce, on every check that
can see the whole product (#48); anchor convergence to the last *repair
attempt*, not the last decision, so a first attempt has nothing to falsely
compare against (#33); make not-attempted and attempted-but-failed both block
release, rather than letting "never tried" pass as "succeeded" (#37); read all
three states, stating a direction only when one exists (#44).

**The lesson**: every one of these is a boolean standing in for a question
with three real answers — met / not met / can't tell yet. Collapsing "can't
tell yet" into "false" is the single most repeated defect shape in this log.
A check that cannot distinguish "I verified this passed" from "I never
verified anything" will eventually report the second as the first, and will
look completely healthy while doing it.

## 3. The agent polluting its own measurement (#36, with #32 as a related but distinct case)

- **#36**: the live reviewer agent was told to "actually try" the thing it was
  reviewing — submitting a real email on a validation landing page. It
  invented one, and that fake signup landed directly in the `validation_signals`
  table a real build-or-scrap decision is gated on. The exact table the system
  uses to measure real demand was being corrupted by the system's own review
  step.

**Fix**: give the reviewer the same reserved, unmailable self-test address the
form-submission endpoint already had, so a review can exercise the real path
without writing into the real signal.

**On #32** — cited alongside #36 in the original grouping for this pattern,
but checking its actual content: it isn't a measurement-pollution bug. A
generated landing page published its own internal Discovery provenance (a
link to the GitHub issue the idea was scraped from) directly to real visitors
— a credibility leak, not a corrupted metric. Its real kinship is with the
"missing context, not missing capability" theme that also produced #49: the
codegen prompt handed the model a field literally named `evidence` with no
framing for what it was or that it must never appear on the page. The model
used it exactly as labeled. Noting this rather than forcing the fit, per the
instruction to flag a mis-grouped entry.

**The lesson (from #36)**: an agent that is told to verify something by *doing*
it needs a way to do it that cannot be mistaken for the real thing. "Act like a
real user" and "don't become a real data point" are not automatically
compatible, and the gap between them doesn't announce itself.

## 4. Escalation to humans going wrong (#7, #20)

- **#7**: a human replied to a heal-exhaustion escalation asking to defer a
  known issue and ship anyway. The handler had exactly two branches — retire,
  or treat any other reply as repair instructions — so a real business
  decision was fed to the coding agent as if it were a bug report, and another
  automated patch attempt got queued against the human's explicit wishes.
- **#20**: a single recurring provider-outage error got relabeled as
  `legal_uncertainty` 40 times in 14 days, because the subject line embedded
  the failing task's own ID — unique every time by construction — which
  defeated the duplicate-escalation collapse built to prevent exactly this.
  Several of the 40 also failed to generate any human-readable explanation at
  all, because the explanation step itself needed a working LLM call, and the
  outage that caused the escalation had taken out every configured provider at
  once.

**Fix**: a genuine third reply path (`DEFER`) that records the accepted risk
and deploys as-is, rather than forcing every reply into "retire" or "fix it"
(#7); a dedicated `provider_outage` reason with a subject built from the
provider and status code, not the failing task's ID, so repeats of the same
cause collapse into one open escalation (#20).

**The lesson**: the channel to a human is only worth as much as its signal
quality. A wrong label costs trust in every future use of that label, not just
the mislabeled one — and a binary "did X or didn't" reply handler will
misfile the first real-world reply that doesn't fit either box.

## 5. Differences between AI vendors (#10, #11, #13, #14, #15)

Five separate, unrelated ways that treating providers as interchangeable broke
something real: a missing `thought_signature` broke every Gemini multi-turn
tool call after the first (#10); Anthropic's cheap fallback model rejected a
request parameter every other Anthropic call sent, defeating the actual
failover safety net the moment it was needed (#11); a provider added for one
task type could still be reached as another task type's accidental failover
target, with no concept of fitness for that task (#13); a newly-added provider
silently received a different vendor's tool-schema shape because only one
vendor had ever been special-cased (#14); and two providers' spend was priced
at a third vendor's rate because the price table's keys didn't match the
model-alias strings actually being logged (#15).

**Fix, each case**: capture and replay the opaque signature Gemini requires
(#10); omit unsupported parameters per-model rather than assuming every model
from one vendor behaves the same (#11); scope which providers are legitimate
fallback targets for which task types (#13); treat an Anthropic-compatible
provider's tool schema as Anthropic-shaped, not whatever the `else` branch
assumed (#14); add the real keys a provider actually logs under (#15).

**The lesson**: "the same interface" is not "the same behavior." Every one of
these shipped because some part of the system assumed providers were
substitutable along a dimension they weren't — token accounting, supported
parameters, task-type fitness, schema shape, or pricing keys. None of them were
caught by a type checker or a schema validator; all of them were caught by
watching real calls fail in production.

## 6. Idempotency (#38, #42)

Both the identical shape: an idempotency key scoped to an entity's entire
lifetime (`validate:{idea_id}`, `build:{idea_id}`), answering "has this ever
happened" when the question the key exists to answer is "is this happening
*now*." A key like that cannot express a second, legitimate occurrence — a
later, correct decision to redo the work silently returns `None` from
`enqueue_task`, nothing gets queued, and the resulting log line reads exactly
like a deliberate pause rather than a dropped decision.

**Fix, both cases**: rescope the key to the specific decision that commissions
the work (`validate:{product_id}:{decision_task_id}`,
`build:{product_id}:{task.id}`), so one decision commissions once and a later,
different decision can commission again.

**The lesson**: this is the shape that cost the most without ever raising an
exception. Nothing crashed, nothing logged an error, and the system's own
state looked exactly as if a careful decision had been made to wait. A key
that silently drops a legitimate repeat is worse than one that raises on
conflict, because the first looks correct from every angle except the one
where you ask whether the thing that should have happened, did.

## Conclusion

Fifty-seven incidents in 37 days of real operation, several of them silent
(no exception, no log line, no symptom beyond a result that quietly never
arrived) and several of them in the exact mechanisms — escalation routing,
idempotency, convergence checks — that this kind of system depends on to
know when to stop and ask a human for help. The guardrails that did hold
(data minimization, the never-negative ledger, the deploy gate, the spend
cap) held because they were enforced in the database or in a deterministic
check, never in a prompt. Everything that failed, failed in the part that was
still judgement, memory, or a boolean standing in for a question with more
than two answers.

**This system was not yet safe to run unattended, which is why it was taken
offline.** The failures were all structural and all fixable — none of them
were the model reasoning badly — but "all fixable, in sequence, as they're
found in production" is not the same claim as "safe to leave running."
