# Bug Log

Real bugs found in production, one entry per bug: what broke, why, exactly
what was touched, and how the fix was verified. Ordered most-recent-first.
This is a reference for coming back to later, not a deploy narrative — see
`DEPLOYMENT_CHECKLIST.md` for the full blow-by-blow of any entry below.

**Convention going forward: every real bug found gets an entry here at the
time it's fixed, not batched up afterward.**

---

## Index

1. [Four products in a row failed billing the same way: the codegen prompt never named the Stripe key's env var](#1-four-products-in-a-row-failed-billing-the-same-way-the-codegen-prompt-never-named-the-stripe-keys-env-var)
2. [Generated apps crashed on every route once deployed: an unconditional static-file mount](#2-generated-apps-crashed-on-every-route-once-deployed-an-unconditional-static-file-mount)
3. [The one real live product was invisible behind Vercel's own SSO wall](#3-the-one-real-live-product-was-invisible-behind-vercels-own-sso-wall)
4. [Vercel deployments never actually built the Python backend](#4-vercel-deployments-never-actually-built-the-python-backend)
5. [A "defer" reply to heal-exhaustion silently became patch guidance instead](#5-a-defer-reply-to-heal-exhaustion-silently-became-patch-guidance-instead)
6. [Discovery's raw sources: HN contributed nothing, GitHub surfaced the wrong things](#6-discoverys-raw-sources-hn-contributed-nothing-github-surfaced-the-wrong-things)
7. [Gemini: unbounded thinking budget crowds out the JSON answer in `parse()`](#7-gemini-unbounded-thinking-budget-crowds-out-the-json-answer-in-parse)
8. [Gemini: missing `thought_signature` breaks every multi-turn tool call](#8-gemini-missing-thought_signature-breaks-every-multi-turn-tool-call)
9. [Anthropic: `effort` param unsupported by the Haiku 4.5 fallback model](#9-anthropic-effort-param-unsupported-by-the-haiku-45-fallback-model)
10. [Dashboard container reports unhealthy despite working correctly](#10-dashboard-container-reports-unhealthy-despite-working-correctly)
11. [DeepSeek could accidentally serve as fallback for unrelated tasks](#11-deepseek-could-accidentally-serve-as-fallback-for-unrelated-tasks)
12. [DeepSeek sent Gemini-shaped tool schemas, not Anthropic-shaped](#12-deepseek-sent-gemini-shaped-tool-schemas-not-anthropic-shaped)
13. [Gemini and DeepSeek spend silently mispriced](#13-gemini-and-deepseek-spend-silently-mispriced)
14. [Real code generation was mocked in production](#14-real-code-generation-was-mocked-in-production)

---

## 1. Four products in a row failed billing the same way: the codegen prompt never named the Stripe key's env var

**Found**: 2026-08-21, Jesse noticed the pattern directly — five heal-exhaustion
escalations about "pricing," and asked why a system that clearly knows what's
wrong needs a human to fix something so evidently mechanical.

**Severity**: Real and systemic — every one of the 4 non-`Shardline` products
this factory has ever built hit this identically. Every checkout attempt on
any of them would 503 with "Billing is not configured," permanently, with
no way to self-heal.

**Root cause**: Not what the plain-language escalation summaries said. Each
one described "a fixed, unchangeable" price — the real bug was one level
underneath that: all 4 generated backends correctly read
`PRODUCT_PRICE_CENTS` and correctly used it for the Checkout amount, but
read the Stripe secret key from `STRIPE_SECRET_KEY` — the name Stripe's own
documentation uses — instead of `STRIPE_API_KEY`, the only name
`deploy.py::_runtime_env` actually sets. `SYSTEM_PROMPT` names
`PRODUCT_PRICE_CENTS` and `PRODUCT_ID` explicitly, literally, every time —
but for the Stripe key it only ever said "reading the key from an
environment variable," never naming it. The model filled that gap with a
plausible guess, the same wrong guess, all 4 times. Because the key read as
empty at runtime, `create_checkout_session` failed closed before ever
reaching the price logic — a human summarizing Bug Council's finding
reasonably described the practical effect ("the price can't actually take
effect") as "hard-coded," which sent the investigation in the wrong
direction until the real generated code was read directly.

**Affected**: `src/factory/engines/development/scaffold/generator.py`
(`SYSTEM_PROMPT`), `src/factory/engines/development/contract.py`
(`missing_contract_requirements`, new `_STRIPE_KEY_ENV_READ_RE`).

**Fix**: Two layers, matching this file's own emerging pattern (entry #2 is
the same shape of fix for a different fact). `SYSTEM_PROMPT` now names
`STRIPE_API_KEY` explicitly. `missing_contract_requirements` now also
checks for a real read of it — the same cheap, deterministic,
pre-Bug-Council check already used for `PRODUCT_PRICE_CENTS` — so a future
regression gets caught as `contract_incomplete`, which is a surgical-repair
category: fixable by the system itself in one targeted attempt, not a
5th human escalation. Also added an explicit constraint against hard-coding
a copyright year (e.g. "© 2024") for the same underlying reason — a fact
the model can't know from training data alone needs to either be omitted or
computed, not guessed.

**Unstuck live**: Replied to all 4 open heal-exhaustion interrupts with the
precise fix as guidance, through the normal reply pipeline (not a risk
override — this is a real, understood, fixable bug, not a deliberate
accepted risk). Queued as guided `patch_product` tasks; execution is
currently paused behind the daily LLM spend cap (today's real usage put the
trailing 24h total just over $10), Jesse's explicit choice to let it clear
naturally rather than raise the cap.

**Verification**: New tests in `test_codegen_contract.py`
(`test_a_missing_stripe_api_key_read_is_flagged`,
`test_a_bare_getenv_call_also_satisfies_the_stripe_key_check`), existing
fixtures across `test_codegen_contract.py`/`test_seo_gate.py` updated to
read `STRIPE_API_KEY` so "fully compliant" stays truthfully compliant. Full
suite green (1246 tests) on scratch Postgres before deploying.

**Status**: Fixed, deployed, commit `fb63f43`. The 4 real products are queued
for repair, not yet confirmed fixed live (blocked on spend cap).

---

## 2. Generated apps crashed on every route once deployed: an unconditional static-file mount

**Found**: 2026-08-20, live end-to-end QA of the first product this factory
ever deployed with a working backend (bug #4's fix made the backend
actually run — this is what it hit next).

**Severity**: Total, for every product this factory has ever generated.
Not the deferred security issue Jesse had explicitly accepted — a distinct,
new crash that took down literally every route, including `/health`.

**Root cause**: Every generated MVP's `app/main.py` ends with
`app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")`
to self-serve the frontend from `public/`. Vercel serves `public/**`
automatically from its own CDN, entirely separately from the FastAPI
function, and the deployed function's bundle does not necessarily contain
`public/` at the same relative path the source tree has it. The mount
call is unconditional and runs at *import time*, so a missing directory
raises before any route can ever be served. Reproduced live in a matching
Python 3.12 container: `RuntimeError: Directory '/var/task/public' does
not exist` — `/var/task` is Vercel's real Lambda code path. The scaffold
system prompt never mentioned Vercel or its static-asset convention at
all, so the model reasonably defaulted to a normal-FastAPI pattern that
happens to be exactly wrong for this deployment target.

**Affected**: `src/factory/engines/development/scaffold/generator.py`
(`SYSTEM_PROMPT`); the one already-deployed product's own
`app/main.py` (patched directly, see below).

**Fix**: Added an explicit hard constraint to `SYSTEM_PROMPT` telling the
model Vercel serves `public/**` itself and to guard any self-mount with
`if STATIC_DIR.is_dir():` so it only ever runs where the directory
genuinely exists (local dev / the pytest suite) — future generated apps
should no longer hit this. Patched the one real affected product's
existing `app/main.py` with the same guard directly, since regenerating
the whole app via the LLM for a one-line fix wasn't warranted.

**Verification**: Reproduced the crash live pre-fix in a container matching
Vercel's actual Python 3.12 runtime. Redeployed the patched product and
confirmed a complete real flow end-to-end: `/health` → 200, signup → 201
with a real token, login → 200, and the actual core feature
(`/api/shards/plan`, a longest-processing-time-first bin-packing
scheduler) → 200 with mathematically correct shard balancing. Full suite
green (1244 tests) on scratch Postgres before deploying the prompt change.

**Status**: Fixed, deployed, commit `fb63f43`.

---

## 3. The one real live product was invisible behind Vercel's own SSO wall

**Found**: 2026-08-20, immediately after bug #4's fix — the deployed
product's URL redirected through `vercel.com/sso-api` to a login page for
any visitor, confirmed live via `curl -I`.

**Severity**: Real — a "live" product that only its own operator could
actually see defeats the entire point of shipping it to test market
traction.

**Root cause**: Not a code bug. `GET /v9/projects/{id}` showed
`"ssoProtection": {"deploymentType": "all_except_custom_domains"}` — a
Vercel account/project setting that gates every deployment URL except a
real attached custom domain, protecting Preview Deployments by default.
No custom domain was attached yet (the `domain_required` interrupt that
exists for exactly this was still open), so every route on the raw
`*.vercel.app` URL required a Vercel login.

**Affected**: Vercel project configuration only, no application code.

**Fix**: `PATCH /v9/projects/{id}` with `{"ssoProtection": null}`,
disabling the protection directly (Jesse's explicit choice over attaching
a domain, given as a faster path to a testable public URL right now).

**Verification**: Live: `curl` against the deployment root before the
change returned a 302 to `vercel.com/sso-api`; after, a genuine 200 with
the real page content, no login required.

**Known residual gap, not fixed**: this was disabled on one project only,
and disables protection for *all* deployments on it, including previews —
a real security-posture tradeoff made once, deliberately, not a new
default. Every future product's Vercel project will need the same manual
step (or a real custom domain, which is exempt by design) until this is
automated in `deploy.py` itself.

**Status**: Fixed live via the Vercel API. No application code changed,
no commit.

---

## 4. Vercel deployments never actually built the Python backend

**Found**: 2026-08-20, live QA of the first product this factory ever
deployed to production — Jesse noticed login didn't work and there was no
Google/OAuth option at all.

**Severity**: Catastrophic in scope, only just discovered. Every product
this factory has ever built would have had a completely non-functional
backend once deployed — auth, payments, everything server-side. This was
the first product to ever reach a real live deployment, so the bug was
dormant and undetected until now.

**Root cause**: `deploy.py::deploy()` sent `projectSettings: {"framework":
None}` unconditionally on every deployment — explicitly telling Vercel not
to attempt any framework detection at all. Confirmed live via the
Deployments API: the first real deployment showed `"functions": null,
"builds": []` — no Python serverless function was ever built, so every
`/api/*` call the frontend made 404'd against static hosting. The generated
code itself (`app/main.py` exporting a FastAPI `app` instance, static
assets under `public/**`) already matched Vercel's documented zero-config
FastAPI conventions exactly — the deploy code just never told Vercel to
look.

**Affected**: `src/factory/engines/development/deploy.py` (`deploy`, new
`_has_fastapi_entrypoint`).

**Fix**: Added `_has_fastapi_entrypoint(files)`, checking the real file
list being uploaded against Vercel's documented entrypoint conventions
(`app.py`/`index.py`/`server.py`/`main.py`/`wsgi.py`/`asgi.py` at the
project root, or one level under `src/`/`app/`/`api/`). `deploy()` now
declares `framework: "fastapi"` only when a real entrypoint is present,
leaving `framework: None` for pure-static validation landing pages (which
have no backend by design — see `LANDING_PAGE_SYSTEM`'s own "no backend
business logic" constraint). Also added `skipAutoDetectionConfirmation=1`
to the deployment request, so a rare mismatch between our declared
framework and Vercel's own detection can't 400 an unattended pipeline
waiting on interactive confirmation.

**Verification**: 8 new unit tests in `test_deploy_env.py` covering
detection at every documented location, the pure-static negative case, and
that `deploy()` sends the correct `framework` value in each shape. Full
suite green (1244 tests) on a genuine scratch Postgres before deploying.
Live, definitive proof: redeployed the one real product affected and
confirmed via the Vercel Deployments API that a real lambda now exists
(`lambdaRuntimeStats: {"python":1}`, a `lambdas` entry with `readyState:
"READY"`) — where the pre-fix deployment showed `functions: null, builds:
[]` for the identical workspace.

**Known residual gap, not fixed**: the deployed URL (every route on it,
static or API) redirects through Vercel's own SSO login wall
(`vercel.com/sso-api`) for any visitor not logged into the Vercel account
that owns the project — confirmed live via `curl -I`. This is a separate,
Vercel account-level Deployment Protection setting, untouched by this fix.
The product's backend now genuinely works; it still isn't reachable by a
real, anonymous visitor yet.

**Status**: Fixed, deployed, commit `3453459`.

---

## 5. A "defer" reply to heal-exhaustion silently became patch guidance instead

**Found**: 2026-08-20, Jesse replied to a real heal-exhaustion escalation
asking to defer a known security/performance issue and ship the MVP anyway
to test market traction — the system queued another automated patch
attempt using his own sentence as literal repair instructions instead.

**Severity**: Real, and defeats the one thing a heal-exhaustion escalation
exists for — letting a human make the final call. The escalation's own
`reply_hint` only ever documented two outcomes ("Reply with guidance... or
REJECT to retire this product"); there was no way to express "leave it,
ship it, I accept the risk."

**Root cause**: `hitl/interrupts.py::_act_on_product_reply` had exactly two
branches: `_REJECT_WORD` (retire the product) or, for any other non-empty
reply, treat it as guidance and queue another patch attempt. A deferral
reply doesn't contain "reject," so it fell straight into the guidance
path — a real business decision got fed to the coding agent as if it were
a bug report.

**Affected**: `src/factory/hitl/interrupts.py` (`_act_on_product_reply`,
new `_DEFER_WORD`), `src/factory/engines/development/graph.py` (new
`deploy_accepting_risk`), `src/factory/engines/development/deploy.py`
(`deploy_requires_approval`/`deploy` now accept `council: CouncilResult |
None` plus `override_reason`).

**Fix**: Added a genuine third path. `DEFER` in a reply now bypasses Bug
Council's approval gate for that one product, on that one human's explicit
say-so (`council=None`, `override_reason=<their reply>` — never a
fabricated approval, so `council.approved` stays a truthful historical
record everywhere else it's read), deploys the current workspace exactly
as it stands, and records the accepted risk as a durable `PREFERENCE`
memory note. Updated the escalation's own `reply_hint` to actually document
the option going forward.

**Verification**: New `tests/integration/test_hitl_defer_reply.py` (4
tests) plus 4 new `deploy_requires_approval`/override tests in
`test_bug_council.py`. Full suite green (1231 tests) on scratch Postgres.
Applied live to the one real product affected: it deployed successfully
with the accepted risk noted, `heal_attempts` reset to 0, and — the whole
bug this closes — no patch task queued.

**Status**: Fixed, deployed, commit `3453459`.

---

## 6. Discovery's raw sources: HN contributed nothing, GitHub surfaced the wrong things

**Found**: 2026-08-20, digging into why Discovery's generator kept producing
zero candidates even after bug #7's crash fix was live and cycles were
running clean — the crash wasn't the whole story.

**Severity**: Real, structural — not a crash, a silent quality problem.
Discovery had effectively been mining startup ideas out of Steam/Proton
compatibility bug trackers, with its one source that might have surfaced
founder-relevant discussion contributing nothing at all.

**Root cause, two independent bugs found together**:
(a) `HackerNewsSource.fetch()` set `Signal.engagement` from the Algolia
hit's `points` field — but `points` is a story-level field, `null` on
every `tags=comment` hit (confirmed live: 100% of a real fetch). Every HN
signal scored `engagement=0`, and `cluster_signals()`'s singleton-keep floor
(`engagement >= 25`) made an HN singleton's survival structurally
impossible no matter how resonant the comment. 36 real signals fetched per
cycle, 0 ever reached a kept cluster.
(b) `GitHubIssuesSource`'s search queries had a floor on `comments` but no
ceiling, sorted by `comments` descending — which always surfaces the same
handful of all-time-record megathreads (Cyberpunk/GTA V Proton
compatibility, PyTorch op-coverage tracking) that permanently outrank
anything a real, smaller-scale business pain point could ever accumulate.
Confirmed live: an entire real cycle's top-12 clusters were 100%
consumer/gaming/OSS threads, zero resembling a B2B pain point.

**Affected**: `src/factory/engines/discovery/sources/hackernews.py`
(`fetch`), `src/factory/engines/discovery/sources/github.py` (`_QUERIES`),
`src/factory/engines/discovery/cluster.py` (`_SINGLETON_ENGAGEMENT_FLOOR`,
new).

**Fix**: HN now uses reply count (`len(hit["children"])`) as engagement —
the one real per-comment signal this endpoint returns. GitHub's queries
gained an upper bound (`comments:11..60` / `comments:6..60`), confirmed
live to replace an entire cycle's Cyberpunk/PyTorch results with issues
like a CI build-timeout limit and a billing-panel integration request.
`cluster_signals()`'s singleton floor was lowered from a flat, uncalibrated
25 to 10 — chosen from a real 120-comment HN sample, where only the two
loudest replies (15, 11) cleared it, keeping the floor selective rather
than a rubber stamp for whatever scale nothing could reach. Also added
`discovery_verbose_logging` (off by default, temporary diagnostic), which
logs the real top-12 clusters and every scored idea's niche/score/rejection
reason each cycle.

**Verification**: New `test_discovery_sources.py` (4 tests),
`test_cluster_signals.py` (3 tests), `test_discovery_verbose_logging.py` (4
tests). Full suite green (899 tests) on scratch Postgres. Live proof this
mattered, not just correctness in isolation: the very next real cycle after
deploy generated its first-ever surviving idea (score 0.669, cleared the
0.55 threshold) from the exact GitHub cluster this fix was designed to
surface ("Increase build timeout from 60 minutes") — the first surviving
idea since production went live.

**Status**: Fixed, deployed, commit `b0d8140`.

---

## 7. Gemini: unbounded thinking budget crowds out the JSON answer in `parse()`

**Found**: 2026-08-19, routine periodic check-in — `llm_calls` showed a
`schema_violation` on an `analysis`/`discovery` call from a few hours
earlier, right after the factory's own supervisor had already run a
maintenance task at 00:04 titled "Review recent schema_violation and
refusal outcomes... and tighten the prompts that produced them" without
fully resolving it.

**Severity**: Real, intermittent, and directly relevant to why Discovery
had produced zero surviving ideas after its first full day live — this
crashed the discriminator step outright (`engine.failed`) on some fraction
of small-batch scoring calls, rather than merely scoring the batch low.

**Root cause**: `GeminiProvider.parse()` never set a `thinking_config`, so
Gemini used its default dynamic/unbounded thinking budget — and thinking
tokens share the *same* `max_output_tokens` ceiling as the visible JSON
answer (confirmed via `usage_metadata.thoughts_token_count`, counted
separately from `candidates_token_count` but against the same cap).
`discovery/score.py::_scaled_discriminator_tokens()` scales the ceiling down
for small batches (as low as ~2236 tokens for a single candidate idea), and
on some inputs thinking alone consumed nearly all of it. Real failure
reproduced live: a call with `max_tokens=2236` hit `finish_reason=MAX_TOKENS`
after emitting only 106 tokens of visible answer — `SchemaViolation`, fatal
for structured output (unlike `converse()`, where a `MAX_TOKENS` finish is
merely a short turn the agent loop can continue from, so this class of bug
is specific to `parse()`). Confirmed directly: an unbounded live repro call
against the same prompt used `thoughts_token_count=860` against
`candidates_token_count=178` — thinking spend is real and highly variable,
not a fixed small overhead.

**Affected**: `src/factory/llm/providers/gemini_provider.py` (`parse`, new
`_thinking_budget` helper).

**Fix**: Added `_thinking_budget(max_tokens) -> int`, returning
`max(128, min(1024, max_tokens // 4))`, passed as `thinking_config` on every
`parse()` call. `thinking_budget=0` (fully disabled) was tried first and
rejected outright by `gemini-pro-latest` — `"Budget 0 is invalid. This model
only works in thinking mode."` — so this reserves a small, capped share
instead: verified live that a bounded budget (512–1024) still completes the
same schema successfully, using less than the cap when the model doesn't
need it (guarantees answer headroom without forcing extra thinking spend on
the easy case). Deliberately scoped to `parse()` only — `converse()` is left
with its default dynamic thinking, since a truncated agentic turn there
isn't fatal the way a truncated JSON blob is.

**Verification**: `test_gemini_provider.py::test_thinking_budget_is_a_quarter_of_max_tokens_within_bounds`,
`test_thinking_budget_never_drops_below_the_floor_on_a_tiny_ceiling`,
`test_thinking_budget_always_leaves_headroom_for_the_answer`,
`test_parse_sets_a_bounded_thinking_config_on_every_request`. Full local
suite (888 tests) green in a genuine scratch Postgres container before
deploying. Reproduced the exact real failure shape live pre-fix (unbounded
repro showing `thoughts_token_count=860`), then re-ran the exact previously
-failing call (`max_tokens=2236`, single-idea batch) 5x live post-fix — all
5 succeeded, two scored above the `SCORE_THRESHOLD=0.55` survival bar.

**Status**: Fixed, deployed, commit `1668906`.

---

## 8. Gemini: missing `thought_signature` breaks every multi-turn tool call

**Found**: 2026-08-18, prompted by Jesse noticing the dashboard's provider-health
panel showing `gemini: down`.

**Severity**: Real, recurring, previously invisible (masked by bug #9's fix,
which made every occurrence fail over successfully instead of crashing).

**Root cause**: Gemini's newer models attach an opaque `thought_signature`
(bytes) to every `function_call` part in a response, and require it echoed
back verbatim when that same call is replayed into a later turn — otherwise
a 400: `"Function call is missing a thought_signature in functionCall
parts."` This codebase never captured or replayed it. Explains the exact
observed pattern: the *first* call in a run (no history to replay yet)
always succeeded; *every* subsequent call in the same run failed,
unconditionally, not intermittently.

**Affected**: `src/factory/llm/providers/gemini_provider.py`
(`_to_gemini_content`, `converse`'s response-parsing loop),
`src/factory/llm/providers/base.py` (`ToolCall`).

**Fix**: Added `ToolCall.thought_signature: bytes | None = None` to the
provider-neutral type. `GeminiProvider` populates it from the real response
`Part` on the way in, and replays it in the `function_call` part dict on the
way out — omitted entirely when `None` (Anthropic/DeepSeek never set it).

**Known residual gap, not fixed**: `LLMClient.converse()` re-resolves the
primary provider fresh every step, not once per run — so a single agent run
can interleave providers (Gemini fails a step, Anthropic serves it, the next
step tries Gemini again). A signature-less call from that Anthropic-served
step, if replayed into a later Gemini turn, will 400 the same way for that
one part — still a clean, absorbed failover though, not a crash. Rare enough
not worth the added complexity.

**Verification**: `test_gemini_provider.py::test_a_replayed_function_call_carries_its_thought_signature`
and `test_a_call_with_no_thought_signature_omits_the_field_entirely`.
Reproduced the exact real failure live against the API pre-fix, then
re-reproduced the identical scenario post-fix and watched it succeed
end-to-end, including the model continuing to a genuine second tool call.

**Status**: Fixed, deployed, commit `2f7815d`.

---

## 9. Anthropic: `effort` param unsupported by the Haiku 4.5 fallback model

**Found**: 2026-08-18, routine "anything weird?" check-in — found by querying
`llm_calls` for the day's `rejected` outcomes.

**Severity**: Real, silently defeated the entire Gemini-outage failover
safety net (Anthropic is the last candidate — nothing to fall through to
after it fails too).

**Root cause**: `AnthropicProvider._effort()` always returned a real value
and every `parse()`/`converse()` call unconditionally included it in
`output_config`, regardless of which model actually served the request.
`model_fallback_anthropic` (`claude-haiku-4-5`, the cheap stand-in used only
when a Gemini-routed call fails over) does not support the parameter at
all: `"This model does not support the effort parameter."` Three real
incidents the same day — Gemini 400s correctly triggering failover, the
failover call *also* 400ing.

**Affected**: `src/factory/llm/providers/anthropic_provider.py` (`_effort`,
`parse`, `converse`).

**Fix**: `_effort()` now takes the model string too and returns `None`
(field omitted entirely, not a guessed value) specifically when the model
is `model_fallback_anthropic`.

**Verification**: New file `tests/unit/test_anthropic_provider.py` — the
first direct unit test of `AnthropicProvider`'s own request-shaping logic
(previously only ever exercised indirectly via `FakeProvider` substitution,
which is exactly how this shipped unnoticed). Verified live: a real
`converse()` call to the fallback model that would have 400'd now succeeds.

**Status**: Fixed, deployed, commit `aa16f62`.

---

## 10. Dashboard container reports unhealthy despite working correctly

**Found**: 2026-08-18, same check-in as #6 — `docker ps` showed
`dashboard: unhealthy`.

**Severity**: Cosmetic only. Real traffic through Caddy worked all day
(confirmed via direct HTTPS checks); only the container's own internal
healthcheck probe was wrong.

**Root cause**: two independent issues, both in `dashboard/Dockerfile`.
(a) Next.js standalone's `server.js` was binding only to the container's own
interface IP, never loopback, because `HOSTNAME` was never set. (b) Even
after fixing that, `wget`'s resolution of the literal string `localhost`
tried `::1` (IPv6) first, which a `0.0.0.0` (IPv4-only) bind refuses, and
never fell back to the working IPv4 address.

**Affected**: `dashboard/Dockerfile`.

**Fix**: Added `ENV HOSTNAME=0.0.0.0`; changed the healthcheck command to
probe `http://127.0.0.1:3000/login` instead of `http://localhost:3000/login`.

**Verification**: `docker ps` / `docker inspect ...State.Health` confirmed
`healthy` after redeploy.

**Status**: Fixed, deployed, commit `aa16f62`.

---

## 11. DeepSeek could accidentally serve as fallback for unrelated tasks

**Found**: 2026-08-18, while investigating bug #13 (checking `llm_calls` for
pricing accuracy turned up an unrelated anomalous row).

**Severity**: Real, live risk — not just theoretical. One real production
call already hit it.

**Root cause**: `ProviderRegistry.alternatives()` returns every other
*registered* provider on failover, with no concept of task-type fitness.
Once DeepSeek became a third registered provider (for `CODE_REPAIR`, see
bug #12's context), any Gemini-routed, failover-eligible task type
(`ANALYSIS`, `ORCHESTRATION`, `COMPLIANCE`, `DISCOVERY`, `MARKETING_COPY`)
could fail over to DeepSeek and send it a model name it had never heard of.
Confirmed live: `provider=deepseek, model=gemini-pro-latest,
task_type=analysis, outcome=rejected` — caught safely only because
DeepSeek's endpoint rejected the unknown model name. A more lenient
endpoint could have silently served a compliance or orchestration decision
on the wrong model.

**Affected**: `src/factory/llm/router.py` (new `GENERAL_FALLBACK_PROVIDERS`),
`src/factory/llm/client.py` (`parse()`/`converse()`'s candidate-building).

**Fix**: Added `GENERAL_FALLBACK_PROVIDERS = frozenset({Provider.GEMINI,
Provider.ANTHROPIC})`; both failover-candidate lists in `client.py` now
filter through it. DeepSeek can only ever be reached as `CODE_REPAIR`'s own
primary, never as anyone else's fallback.

**Verification**: `tests/unit/test_llm_client.py::test_deepseek_is_never_an_accidental_failover_target`.
Full unit + integration suite (1205 tests) against a genuine scratch
Postgres before deploying.

**Status**: Fixed, deployed, commit `aa16f62`.

---

## 12. DeepSeek sent Gemini-shaped tool schemas, not Anthropic-shaped

**Found**: 2026-08-18, the very first real production `CODE_REPAIR` call
after routing DeepSeek in — caught before it affected a real repair
attempt, via direct end-to-end verification of the new deploy.

**Severity**: Would have broken 100% of real DeepSeek repair calls with no
exception.

**Root cause**: `client.py::_tools_payload()` only special-cased
`Provider.ANTHROPIC` for Anthropic-shaped tool schemas; everything else,
including the new `Provider.DEEPSEEK`, silently fell through to Gemini's
shape. DeepSeek is served over `DeepSeekProvider(AnthropicProvider)` — the
same Anthropic-compatible endpoint and message format — so it needs the
Anthropic shape, not Gemini's. Real 400: `"Invalid schema for function
'list_workspace_files': null is not of types 'boolean', 'object'"`.

**Affected**: `src/factory/llm/client.py` (`_tools_payload`).

**Fix**: `_tools_payload()` now treats `Provider.DEEPSEEK` the same as
`Provider.ANTHROPIC`.

**Verification**: `tests/unit/test_llm_client.py::test_deepseek_gets_anthropics_tool_schema_shape_not_geminis`.
Re-ran the real production repair-agent path end-to-end post-fix: 5 clean
steps, correct diagnosis, correct fix, independently verified passing.

**Status**: Fixed, deployed, commit `8d21de8`.

---

## 13. Gemini and DeepSeek spend silently mispriced

**Found**: 2026-08-18, Jesse asked directly: "is LLM spend accurate?"

**Severity**: Real, financial — also fed directly into the
`DAILY_LLM_SPEND_CAP_USD` circuit breaker's calculation, so the cap could
trip earlier than intended too.

**Root cause**: `PRICE_TABLE`'s Gemini keys were the dated model names
(`gemini-2.5-pro`/`gemini-2.5-flash`); production's `MODEL_GENERAL`/
`MODEL_FAST` are configured to Google's rolling aliases
(`gemini-pro-latest`/`gemini-flash-latest`), which is what `LLMCall.model`
actually logs — every real Gemini call fell through to `_DEFAULT_PRICE`
(Claude Sonnet 5's rate, $3.00/$15.00 per M). `deepseek-v4-pro` was never
added to the table at all once it started serving `CODE_REPAIR`, so every
real DeepSeek call was *also* priced at Sonnet 5's rate — roughly 14x
DeepSeek's real cost ($0.435/$0.87 per M, verified against deepseek.ai's
own pricing page, not a marked-up reseller).

**Affected**: `src/factory/observability/costs.py` (`PRICE_TABLE`).

**Fix**: Added the real alias keys and the `deepseek-v4-pro` entry.

**Known residual gap, not fixed**: DeepSeek's real cache-hit discount
(~99.2% off) is far steeper than this file's blanket 10%
`_CACHE_READ_MULTIPLIER` (calibrated for Anthropic) — a smaller, separate
overstatement on DeepSeek's cached input tokens specifically. Historical
`llm_calls.cost_usd` rows already written were not retroactively corrected
either (recomputing at read time would rewrite history to a price never
actually charged, per this file's own design) — acceptable since the
affected total was a few cents.

**Verification**: Live check post-deploy: `estimate_usd('gemini-pro-latest',
1_000_000, 1_000_000, 0, 0)` → `$11.25` (was `$18.00`);
`estimate_usd('deepseek-v4-pro', ...)` → `$1.305` (was `$18.00`).

**Status**: Fixed, deployed, commit `aa16f62`.

---

## 14. Real code generation was mocked in production

**Found**: 2026-08-18, go-live day, a few hours after `FACTORY_DRY_RUN` was
flipped off — not caught as part of the go-live checklist itself.

**Severity**: High while it lasted — every product built during the gap
was a fixed, generic template, not real AI-generated code for the specific
idea.

**Root cause**: `STAGING_MOCK_EXPENSIVE_CALLS` was still `1` in production.
This flag swaps the Anthropic provider slot for `StagingCodeProvider`, a
deterministic stand-in that returns a genuinely functional but fixed MVP
fixture — built specifically to let the rest of the pipeline (Discovery,
sandbox install/test cycle, deploy) be validated during the dry run without
paying for real Opus/Sonnet calls on every idea.

**Affected**: `/opt/superagent/.env` (config only, no code bug).

**Fix**: `STAGING_MOCK_EXPENSIVE_CALLS=1` → `0`.

**Verification**: Confirmed via `/health` and subsequent real `llm_calls`
rows showing genuine Anthropic-served `CODE_GENERATION`/`CODE_REVIEW`
outcomes instead of the mock's synthetic ones.

**Status**: Fixed, live. No code changed, no commit.
