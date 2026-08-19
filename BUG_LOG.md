# Bug Log

Real bugs found in production, one entry per bug: what broke, why, exactly
what was touched, and how the fix was verified. Ordered most-recent-first.
This is a reference for coming back to later, not a deploy narrative — see
`DEPLOYMENT_CHECKLIST.md` for the full blow-by-blow of any entry below.

**Convention going forward: every real bug found gets an entry here at the
time it's fixed, not batched up afterward.**

---

## Index

1. [Gemini: unbounded thinking budget crowds out the JSON answer in `parse()`](#1-gemini-unbounded-thinking-budget-crowds-out-the-json-answer-in-parse)
2. [Gemini: missing `thought_signature` breaks every multi-turn tool call](#2-gemini-missing-thought_signature-breaks-every-multi-turn-tool-call)
3. [Anthropic: `effort` param unsupported by the Haiku 4.5 fallback model](#3-anthropic-effort-param-unsupported-by-the-haiku-45-fallback-model)
4. [Dashboard container reports unhealthy despite working correctly](#4-dashboard-container-reports-unhealthy-despite-working-correctly)
5. [DeepSeek could accidentally serve as fallback for unrelated tasks](#5-deepseek-could-accidentally-serve-as-fallback-for-unrelated-tasks)
6. [DeepSeek sent Gemini-shaped tool schemas, not Anthropic-shaped](#6-deepseek-sent-gemini-shaped-tool-schemas-not-anthropic-shaped)
7. [Gemini and DeepSeek spend silently mispriced](#7-gemini-and-deepseek-spend-silently-mispriced)
8. [Real code generation was mocked in production](#8-real-code-generation-was-mocked-in-production)

---

## 1. Gemini: unbounded thinking budget crowds out the JSON answer in `parse()`

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

## 2. Gemini: missing `thought_signature` breaks every multi-turn tool call

**Found**: 2026-08-18, prompted by Jesse noticing the dashboard's provider-health
panel showing `gemini: down`.

**Severity**: Real, recurring, previously invisible (masked by bug #3's fix,
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

## 3. Anthropic: `effort` param unsupported by the Haiku 4.5 fallback model

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

## 4. Dashboard container reports unhealthy despite working correctly

**Found**: 2026-08-18, same check-in as #3 — `docker ps` showed
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

## 5. DeepSeek could accidentally serve as fallback for unrelated tasks

**Found**: 2026-08-18, while investigating bug #7 (checking `llm_calls` for
pricing accuracy turned up an unrelated anomalous row).

**Severity**: Real, live risk — not just theoretical. One real production
call already hit it.

**Root cause**: `ProviderRegistry.alternatives()` returns every other
*registered* provider on failover, with no concept of task-type fitness.
Once DeepSeek became a third registered provider (for `CODE_REPAIR`, see
bug #6's context), any Gemini-routed, failover-eligible task type
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

## 6. DeepSeek sent Gemini-shaped tool schemas, not Anthropic-shaped

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

## 7. Gemini and DeepSeek spend silently mispriced

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

## 8. Real code generation was mocked in production

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
