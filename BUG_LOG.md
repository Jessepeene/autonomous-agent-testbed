# Bug Log

Real bugs found in production, one entry per bug: what broke, why, exactly
what was touched, and how the fix was verified. Ordered most-recent-first.
This is a reference for coming back to later, not a deploy narrative — see
`DEPLOYMENT_CHECKLIST.md` for the full blow-by-blow of any entry below.

**Convention going forward: every real bug found gets an entry here at the
time it's fixed, not batched up afterward.**

---

## Index

1. [Discovery generated the same idea 5 times from the same source, undetected](#1-discovery-generated-the-same-idea-5-times-from-the-same-source-undetected)
2. [Bug-Council-rejected products always burned a full blind regeneration, even for one-line fixes](#2-bug-council-rejected-products-always-burned-a-full-blind-regeneration-even-for-one-line-fixes)
3. [Four products in a row failed billing the same way: the codegen prompt never named the Stripe key's env var](#3-four-products-in-a-row-failed-billing-the-same-way-the-codegen-prompt-never-named-the-stripe-keys-env-var)
4. [Generated apps crashed on every route once deployed: an unconditional static-file mount](#4-generated-apps-crashed-on-every-route-once-deployed-an-unconditional-static-file-mount)
5. [The one real live product was invisible behind Vercel's own SSO wall](#5-the-one-real-live-product-was-invisible-behind-vercels-own-sso-wall)
6. [Vercel deployments never actually built the Python backend](#6-vercel-deployments-never-actually-built-the-python-backend)
7. [A "defer" reply to heal-exhaustion silently became patch guidance instead](#7-a-defer-reply-to-heal-exhaustion-silently-became-patch-guidance-instead)
8. [Discovery's raw sources: HN contributed nothing, GitHub surfaced the wrong things](#8-discoverys-raw-sources-hn-contributed-nothing-github-surfaced-the-wrong-things)
9. [Gemini: unbounded thinking budget crowds out the JSON answer in `parse()`](#9-gemini-unbounded-thinking-budget-crowds-out-the-json-answer-in-parse)
10. [Gemini: missing `thought_signature` breaks every multi-turn tool call](#10-gemini-missing-thought_signature-breaks-every-multi-turn-tool-call)
11. [Anthropic: `effort` param unsupported by the Haiku 4.5 fallback model](#11-anthropic-effort-param-unsupported-by-the-haiku-45-fallback-model)
12. [Dashboard container reports unhealthy despite working correctly](#12-dashboard-container-reports-unhealthy-despite-working-correctly)
13. [DeepSeek could accidentally serve as fallback for unrelated tasks](#13-deepseek-could-accidentally-serve-as-fallback-for-unrelated-tasks)
14. [DeepSeek sent Gemini-shaped tool schemas, not Anthropic-shaped](#14-deepseek-sent-gemini-shaped-tool-schemas-not-anthropic-shaped)
15. [Gemini and DeepSeek spend silently mispriced](#15-gemini-and-deepseek-spend-silently-mispriced)
16. [Real code generation was mocked in production](#16-real-code-generation-was-mocked-in-production)
17. [Contract check false-positived on `os.environ.get(...)`, forcing needless heal cycles](#17-contract-check-false-positived-on-osenvironget-forcing-needless-heal-cycles)
18. [Dashboard credential-write endpoint failed closed with a real `PermissionError`](#18-dashboard-credential-write-endpoint-failed-closed-with-a-real-permissionerror)
19. [A tunable prompt's live DB row silently never gets a code-side rewrite once bootstrapped](#19-a-tunable-prompts-live-db-row-silently-never-gets-a-code-side-rewrite-once-bootstrapped)
20. [40 identical provider-outage escalations mislabeled as legal review, spamming past the dedup guard](#20-40-identical-provider-outage-escalations-mislabeled-as-legal-review-spamming-past-the-dedup-guard)
21. [Live-product auditing only ran when the factory was idle](#21-live-product-auditing-only-ran-when-the-factory-was-idle)
22. [An issue filed `open` never closed, so convergence could only ever read "not converging"](#22-an-issue-filed-open-never-closed-so-convergence-could-only-ever-read-not-converging)
23. [`max_length` on model-written prose silently discarded whole responses, for the 4th and 5th time](#23-max_length-on-model-written-prose-silently-discarded-whole-responses-for-the-4th-and-5th-time)
24. [The release decision inherited an 8,192-token budget that thinking ate before it could answer](#24-the-release-decision-inherited-an-8192-token-budget-that-thinking-ate-before-it-could-answer)
25. [A cosmetic finding the release judge had accepted twice counted as a wall and escalated the product](#25-a-cosmetic-finding-the-release-judge-had-accepted-twice-counted-as-a-wall-and-escalated-the-product)
26. [A static landing page shipped `requirements.txt`, so Vercel built it as a Python app and failed](#26-a-static-landing-page-shipped-requirementstxt-so-vercel-built-it-as-a-python-app-and-failed)
27. [The repair agent was told `readyState=ERROR` and nothing else](#27-the-repair-agent-was-told-readystateerror-and-nothing-else)
28. [`converging` read zero blockers as a wall, escalating a build that had just been approved to ship](#28-converging-read-zero-blockers-as-a-wall-escalating-a-build-that-had-just-been-approved-to-ship)
29. [Every new product ships behind Vercel's SSO login wall — the residual gap from #5, as predicted](#29-every-new-product-ships-behind-vercels-sso-login-wall--the-residual-gap-from-5-as-predicted)
30. [No CORS anywhere: every interest form on every page ever shipped failed in the browser](#30-no-cors-anywhere-every-interest-form-on-every-page-ever-shipped-failed-in-the-browser)
31. [Validation pages never got a design brief — the call sat below the early return](#31-validation-pages-never-got-a-design-brief--the-call-sat-below-the-early-return)
32. [The landing page published its own internal provenance to customers](#32-the-landing-page-published-its-own-internal-provenance-to-customers)
33. [Convergence compared the current state against itself, so every `fix_first` walled on attempt one](#33-convergence-compared-the-current-state-against-itself-so-every-fix_first-walled-on-attempt-one)
34. [Six repair failures, one cause: nothing knew whether the workspace matched what was deployed](#34-six-repair-failures-one-cause-nothing-knew-whether-the-workspace-matched-what-was-deployed)
35. [The repair agent had no way to make a small edit, and the logs could not say what it ran](#35-the-repair-agent-had-no-way-to-make-a-small-edit-and-the-logs-could-not-say-what-it-ran)
36. [The live reviewer filed fake signups into the demand signal it was measuring](#36-the-live-reviewer-filed-fake-signups-into-the-demand-signal-it-was-measuring)
37. [The analytics beacon never worked in any browser, and the gate that saw it recorded a pass](#37-the-analytics-beacon-never-worked-in-any-browser-and-the-gate-that-saw-it-recorded-a-pass)

---

## 22. An issue filed `open` never closed, so convergence could only ever read "not converging"

**Found**: 2026-09-14, from a `heal_exhausted` escalation on product
`06829d03` — its second that morning — on a landing page that was passing
every deterministic gate.

**Severity**: Silent and total. Every product that ever had one blocking
issue filed against it would escalate on every heal attempt from then on,
forever, regardless of whether the defect was fixed. Two escalations on this
product; the mechanism was general.

**Root cause**: Issues are filed with status `open`. The only thing that ever
closed one was `verify_unseen_issues`, which by design only acts on issues
already marked `fixed` — and nothing marks a gap issue fixed. A repair agent
rewrites a file, the defect genuinely goes away, and the row stays `open` for
good.

`product_progress.blocking_now` counts open blocking issues, so it could rise
and never fall. `converging` is `blocking_now < blocking_at_last_attempt`, so
after the first attempt it was permanently `False`. `_heal` had just been
changed to stop on a wall rather than a counter — `walled = progress.converging
is False or bool(progress.stuck)` — which made the broken signal load-bearing:
it escalated on attempt two, every time.

Concretely: this product carried exactly one `high` issue, from
`contract_check`, with `times_seen = 1`. The contract check reported **zero**
violations on every run after the one that filed it. The workspace was correct
— `index.html` defined `INTEREST_URL` and posted the form to it — and the
release judgement was told, by the stale row, that it was not.

The previous stopping mechanism (a heal counter) hid this: it escalated for its
own blind reason before the progress signal was ever consulted.

**Fix** (`3faed10`): `repository.close_resolved_issues` — a check that examines
everything every run closes what it no longer finds. Wired into the three
checks for which that is sound: the codegen contract and SEO checks in
`development/graph.py`, and the requirements comparison in
`development/gaps.py`. All three look at the whole product and report all of
it, so absence from their output is real evidence.

Deliberately **not** wired into Bug Council. It judges six categories by model,
and its not mentioning something is not the same as its having looked and found
nothing — the same reasoning that keeps `verify_unseen_issues` narrow.

Self-healing for the stale row that caused this: the next contract check run
closes it with no manual intervention.

**Verified**: `tests/integration/test_issue_closure.py` (3 tests, real
Postgres) pins the behaviour that matters — `blocking_now` reaching zero after
a clean run — plus the two failure modes that would be worse than the bug: a
still-present defect must stay open, and one check must never close another's
findings. `tests/unit/test_seo_gate.py` asserts a clean gate pass runs a close
pass for both deterministic checks with an empty found-set.

---

## 23. `max_length` on model-written prose silently discarded whole responses, for the 4th and 5th time

**Found**: 2026-09-14, reading `/srv/factory-logs/factory.jsonl` after noticing
4 `release.undecided` out of 9 release judgements.

**Severity**: Total loss of the affected call, silently, with a retry that fails
the same way. Two fired in production the same morning; the audit found the same
cap on **30 fields across 12 more schemas**, every one of them armed.

**Root cause**: `RequirementGap.evidence` capped at 600 and
`ReleaseDecision.rationale` capped at 1500. The model is never told the limit
exists, so a verbose but entirely correct answer becomes a validation error and
the whole response is thrown away:

```
deepseek output failed ReleaseDecision validation: 1 validation error
rationale  String should have at most 1500 characters
```

That discarded a fully-reasoned release decision — the highest-judgement call in
the company — for being 1,501 characters long.

These caps bound nothing real. The columns behind them are `Text`, every consumer
truncates at the point of use, and the actual ceiling is `max_tokens`, enforced
by the API.

**The real root cause is that the rule was a docstring.** `Contract` has said
"do not put a `max_length` on a prose field a model writes" since the third
occurrence (`ReviewVerdict.summary` at 200, `details` at 2000). Both schemas that
failed here were written *after* that docstring. A rule you have to remember at
the moment you add a field is not a control.

**Fix**: `tests/unit/test_prose_fields_uncapped.py` — enumerates every `Contract`
subclass and fails on any bare-`str` field carrying a `max_length`. 30 caps
stripped from `schemas.py` to make it pass. Deliberately still allowed: list caps
(they bound item count, which is a real cost control) and `min_length` (an empty
rationale is a genuine defect worth failing on). One allowlisted structural
field, `LeadAssessment.ref`, a footnote marker like "[3]".

**Verified**: the rule test is parametrised per model, so the failure message
names the offending model and fields; 1,257 unit tests pass.

---

## 24. The release decision inherited an 8,192-token budget that thinking ate before it could answer

**Found**: 2026-09-14, same log sweep as #23 — the other half of the 4
`release.undecided`.

**Severity**: The deciding call in the build silently fails and the caller falls
back to its previous deterministic behaviour, so a product does not ship and
nothing says why.

**Root cause**: `decide_release` called `ctx.llm.parse` without `max_tokens`,
inheriting `TaskType.ANALYSIS`'s 8,192 default. DeepSeek's thinking budget is
`max(1024, max_tokens // 2)`, so reasoning consumed the budget before the tool
call was ever emitted:

```
deepseek ran out of tokens before emitting the ReleaseDecision tool call
(max_tokens=8192, content_block_types=['thinking'])
```

This is the same per-category-constant problem that `find_requirement_gaps`
already fixed for itself — and that fix is 30 lines above this call site, with a
comment explaining exactly why. The release judgement weighs the requirements,
every open issue and the convergence history, then writes a rationale a human
has to act on; 8,192 was never sized for it.

**Fix**: `max_tokens=32_000` at the call site, where the caller knows the shape
of the call, matching `find_requirement_gaps`.

**Verified**: shipped alongside #22 and #23; the release judgement is the thing
being watched on the next live build.

---

## 25. A cosmetic finding the release judge had accepted twice counted as a wall and escalated the product

**Found**: 2026-09-14, while verifying that the fix for #22 would actually let
product `06829d03` proceed. It would not have.

**Severity**: Would have silently defeated #22's fix on the very product it was
written for. Any product accumulating one recurring cosmetic finding escalates
forever, regardless of whether anything is actually blocked.

**Root cause**: `_heal` stops on a wall:

```python
walled = progress.converging is False or bool(progress.stuck)
```

`blocking_now` is severity-filtered to `critical`/`high`. `stuck` was not:

```python
stuck = [i.title for i in open_issues if i.times_seen >= 3]
```

So *any* issue seen three times became a wall. This product carried
`bug_council:quality`, **medium**, `times_seen = 4`: a hardcoded copyright year
in the footer. Not deploy-blocking, not counted in `blocking_now` — and enough
on its own to make `walled` true on every attempt.

The inversion is the point. The release judgement had weighed that exact finding
**twice** and explicitly accepted it — *"Cosmetic, invisible to the demand
signal, and cheap to fix later"* — and recorded that acceptance in
`release.decided`. An issue recurring precisely **because** a judge decided not
to fix it was then read as evidence that the approach was failing. The system
punished itself for honouring its own decision.

**Fix**: filter `stuck` by the same severity set as `blocking_now`. "We keep
failing at the same thing" is only a wall when the thing blocks; a recurring
accepted trade is evidence the judgement is being honoured.

**Verified**: `tests/integration/test_issue_closure.py` — a medium finding seen
four times yields `stuck == []`, and a `high` one seen three times still yields
a wall, so the signal the filter must not destroy is pinned.

---

## 26. A static landing page shipped `requirements.txt`, so Vercel built it as a Python app and failed

**Found**: 2026-09-14, by pulling the Vercel build log by hand after the first
run that ever reached a deploy.

**Severity**: Terminal for every validation landing page. No landing page could
deploy, and nothing in the system could say why.

**Root cause**: Vercel's zero-config detection classifies a deployment as Python
when it sees `requirements.txt`, then demands an entrypoint:

```
Error: No python entrypoint found. Set "tool.vercel.entrypoint" in pyproject.toml
or define an entrypoint in one of: app.py, index.py, server.py, main.py, ...
```

The page is pure static HTML by design — `LANDING_PAGE_SYSTEM` forbids backend
logic — so that entrypoint will never exist. It carried a 15-byte
`requirements.txt` holding `pytest`, for the `test_validation_page.py` beside
it. Both exist so the build can be tested locally; neither has anything to do
with what gets served. All 7 workspace files were uploaded.

**Fix**: `_has_fastapi_entrypoint` already draws exactly the right line, and
nothing acted on it. A deployment with no recognised entrypoint is static, so it
now drops `requirements.txt`, `pyproject.toml`, `Pipfile`, `setup.py` and
`test_*`. A real MVP keeps all of them — that is how Vercel learns what to
install, and stripping it there would be a far worse failure than this one.

**Verified**: two unit tests, one per direction — a static page ships only its
HTML, an MVP with `app/main.py` still ships its `requirements.txt`.

---

## 27. The repair agent was told `readyState=ERROR` and nothing else

**Found**: 2026-09-14, same deploy failure as #26 — I had to query the Vercel
API by hand to learn what the system could not tell me.

**Severity**: Unfixable-by-construction. This detail becomes the *entire* brief
a repair agent receives (`"Deployment failed: {detail}"`), so no amount of
repair budget could ever have closed the gap.

**Root cause**: `deploy()` returned only the terminal state name. `ERROR` does
not distinguish a syntax error from a missing entrypoint from an out-of-memory
build. This is the instrumentation principle from `ARCHITECTURE_ROADMAP.md`
failing in the most direct way available: *before giving an agent judgement over
a domain, check the system can show it what an expert looks at*. A human
deploying to Vercel reads the build log. The agent got a status word.

**Fix**: fetch `/v3/deployments/{id}/events` on failure and carry the tail of the
build output back in `detail`. Best-effort throughout — a deploy that already
failed must not also fail to report, so every error degrades to the state name
the caller already had.

**Verified**: the endpoint and payload shape were confirmed live against the
failed deployment before the code was written; it is what produced the quoted
error in #26.

---

## 28. `converging` read zero blockers as a wall, escalating a build that had just been approved to ship

**Found**: 2026-09-14, in the first run where every earlier fix held.

**Severity**: Blocked the one outcome the whole loop exists to produce, and
guaranteed that no deploy failure was ever retried even once.

**Root cause**: `converging` is `blocking_now < blocking_at_last_attempt`. With
both at zero, `0 < 0` is False, which `_heal` reads as "not converging" and
treats as a wall. The live sequence:

```
12:51:43  gaps.recorded      gaps=0 blocking=0 met=9
12:51:50  release.decided    decision=ship unmet_must=0 accepted=4
12:52:02  heal_stopped       reason=not_converging blocking_now=0 blocking_before=0 stuck=0
12:52:05  hitl.raised        heal_exhausted
```

Holding at zero is *done*, not stuck. Worse, `_heal` also handles deploy
failures, which are not issues and never appear in these counts — so
`blocking_now` is structurally zero on that path, and every deploy failure
walled on its first attempt without a single retry.

**Fix**: `converging` returns `None` (no signal) when `blocking_now == 0`,
alongside the existing `None` for a first attempt.

**Verified**: `tests/integration/test_issue_closure.py` — zero blockers never
reads as a wall, while one-before-and-one-now still does and 3→1 still reads as
converging.

---

## 29. Every new product ships behind Vercel's SSO login wall — the residual gap from #5, as predicted

**Found**: 2026-09-14, by curling the first URL this factory ever successfully
deployed end to end. It returned 200 — of `<title>Login – Vercel</title>`.

**Severity**: Defeats the entire purpose of a validation page. It exists to
measure whether strangers want the idea; behind a login it measures nothing, and
the `harvest_leads` task that runs seconds after the deploy read zero from a page
no visitor could open.

**Root cause**: Not new. Entry #5 found this on 2026-08-20, fixed it by hand on
one project, and recorded the gap exactly:

> this was disabled on one project only ... Every future product's Vercel project
> will need the same manual step (or a real custom domain, which is exempt by
> design) **until this is automated in `deploy.py` itself.**

It then recurred on the very next project this factory ever created. Vercel sets
`ssoProtection: {"deploymentType": "all_except_custom_domains"}` on new projects,
so every `*.vercel.app` URL 302s to `vercel.com/sso-api`.

The lesson worth keeping is not about Vercel. A known defect with a known
one-line fix, written down in the place the team reads, still shipped — because
"someone will remember to do this next time" is not a control. Same shape as #23,
where a rule in a docstring did not stop two schemas from breaking it.

**Fix**: `_disable_sso_protection` — `PATCH /v9/projects/{name}` with
`{"ssoProtection": null}` on every successful deploy. Best-effort: it logs and
continues on failure, because a page that is up but gated still beats failing a
deploy that otherwise worked.

The tradeoff is the one taken deliberately in #5: this disables protection for
all deployments on the project, previews included. Each project here hosts
exactly one product whose whole purpose is public reachability, so protection was
guarding nothing. A real custom domain is exempt by design and remains the better
end state — see `domain.ensure_requested`.

**Verified**: applied live to `shardrunner` first, which took the page from a
Vercel login form to `<title>ShardRunner — iOS UI Test Sharding for CI Teams</title>`
with `fetch(INTEREST_URL)` wired to `/interest/06829d037f...`. Two unit tests: a
successful deploy issues the PATCH with the right body, and a deploy still
succeeds when that call fails.

**Also fixed here**: the new call runs on the success path of every deploy, and
its forgiving error handling made a real, unmocked request to api.vercel.com
invisible inside the unit suite — the call went out, 403'd on a fake token,
logged a warning, and the test passed anyway. Caught by timing it. An autouse
fixture now keeps `httpx.AsyncClient.patch` off the network for the whole file.

---

## 30. No CORS anywhere: every interest form on every page ever shipped failed in the browser

**Found**: 2026-09-14, by Jesse, who opened the first product this factory shipped
and tried the form. "Form doesn't work."

**Severity**: The whole point of the validation stage. A landing page exists to
collect interest; the interest signal gates every build-or-scrap decision; and it
could never arrive from a browser. Every page this factory has ever deployed.

**Root cause**: The FastAPI app had no CORS middleware at all. Every landing page
is served from a `*.vercel.app` origin and posts to `/interest/{product_id}` on
the factory's own domain, so every submission is cross-origin:

```
OPTIONS /interest/{id}  ->  405 Method Not Allowed, no CORS headers
POST    /interest/{id}  ->  200 {"received":true}, no Access-Control-Allow-Origin
```

`Content-Type: application/json` makes the request non-simple, so the browser
*must* preflight; a 405 means the fetch is never sent. And even past that, no
`Access-Control-Allow-Origin` on the response means the page cannot read it. Two
independent failures, either fatal.

**Why nothing caught it — the important part.** Every judge in the pipeline reads
source. The gap check compares files to acceptance criteria, Bug Council reviews
code in six categories, the generated `test_validate.py` asserts against the HTML
on disk, and the contract and SEO checks grep for strings. All five passed,
correctly: the markup *is* right. `curl` passed too, because `curl` sends no
`Origin` and needs no preflight. The defect existed only in the gap between
correct source and a working product, and nothing in the pipeline stood there.

**Fix**: `CORSMiddleware`, scoped as narrowly as the two public beacons need —
POST/OPTIONS only (no GET is ever cross-origin readable, so `/dashboard/*`,
`/metrics` and `/health` gain nothing), `allow_credentials=False`, and
`allow_origins=["*"]` because each product deploys to a fresh random subdomain
that cannot be known in advance. Abuse stays bounded by `abuse.rate_limit`.

**And the structural fix**: `engines/development/preship.py` — see below.

**Verified**: live preflight 405 → 200 with the right headers; then a real
Playwright browser on the real deployed page filled the form, clicked, and
recorded `POST /interest/{id} -> 200`, `FAILURES: 0`.

---

## 31. Validation pages never got a design brief — the call sat below the early return

**Found**: 2026-09-14, by Jesse. "No design brief."

**Severity**: Cosmetic in effect, total in scope — the dashboard's design panel
was empty for every product this factory has ever shipped.

**Root cause**: `_verify_and_ship` calls `build_design_brief` after deploy. The
`is_validation` branch returns at line 1753; the call sat at 1782. Validation
pages are the only thing this factory currently ships, so the one product type
that existed was the one type that never reached the call.

**Fix**: moved above the branch. Still never blocks shipping — it degrades to
`None` on any failure and no-ops when one is already cached.

---

## 32. The landing page published its own internal provenance to customers

**Found**: 2026-09-14, by Jesse, reading the live page: *"This pain is real and
public — read the corroborating discussion in this App Center issue: GitHub:
microsoft/appcenter issue #427. This is a bit weird to have on your landing page
right?"*

**Severity**: Credibility, and it would have recurred on every page. A prospect
sees the page linking the forum thread the idea was scraped from.

**Root cause**: The codegen prompt hands the model:

```
- evidence: {idea.source_url}
```

with nothing saying what the field is for. It is internal Discovery provenance. A
field labelled "evidence", handed to a copywriter with no framing, is material —
the model was not wrong to use it. Same shape as #23 and #29: the thing that was
missing was never in the model, it was in what the model was told.

**Fix**: the field is now labelled as internal provenance explicitly not for the
page, plus a hard constraint that nothing internal to the factory — provenance,
issue trackers, research sources, the validation process itself — may appear.

**Related, same commit** (Jesse's decision, not a bug): validation pages now
describe the product in the present tense like any company describes what it
sells, rather than announcing they are not real, since a page that says the
product does not exist measures nothing. Two limits written into the prompt as
firm: never invent evidence (testimonials, logos, usage counts, press), and never
imply the visitor can buy now, because the page only ever collects an email.

---

## 33. Convergence compared the current state against itself, so every `fix_first` walled on attempt one

**Found**: 2026-09-14, auditing my own day's work after Jesse asked *"is
everything that needs to be still judgement based? And are there no quick fix
shortcuts that only fix one issue?"*

**Severity**: Every `fix_first` release decision escalated instead of repairing,
on the first attempt, always. Two of the day's escalations were this. It also
makes this the most instructive entry in the log, because the morning's fix for
it was itself the shortcut the question was asking about.

**Root cause**: `product_progress` read `blocking_at_last_attempt` from the most
recent `release.decided` task event. `_verify_and_ship` *writes* that event and
then calls `_heal`, which lands in `product_progress` and reads it back:

```
decide_release()  -> writes release.decided with open_blocking = N
                  -> returns fix_first
_heal()           -> product_progress() reads the latest release.decided
                  -> blocking_before = N, written seconds earlier, same pass
                  -> N < N is False -> "not converging" -> wall
```

Both numbers came from the same moment. The comparison was against itself and
therefore false unconditionally.

**Why the morning fix was not a fix.** Entry #28 made `converging` return `None`
when `blocking_now == 0`, after watching a build with zero gaps and a `ship`
decision escalate on `0 < 0`. That was a real observation and the wrong
conclusion: zero was simply where a universal fault happened to be *visible*. The
non-zero case kept self-comparing, which is why the very next escalation looked
identical with `blocking_now=1 blocking_before=1`. Fixing the manifestation you
can see, and stopping there, is the whole failure mode.

**Fix**: convergence is a claim about *repair attempts*, so it now anchors on one.
`HealAttempt.blocking_at_attempt` (migration 0037) records how much was blocking
when each repair was commissioned, and `product_progress` compares against the
last attempt rather than the last decision. A first attempt has nothing to compare
against and correctly yields `None`.

The zero guard stays, correctly demoted to what it always was: `_heal` also
handles deploy failures, which produce no issues at all, so `blocking_now` is
structurally zero there and a count comparison is meaningless.

**Three more rule-shaped patches found in the same audit**, all from the same
day's work:

- **Any failed request blocked the ship** (`preship.py`). A 404 on a favicon would
  have held a release with no judgement applied -- the unbounded-findings gate the
  release judgement exists to replace, reintroduced one layer down. Only a failure
  of the interest endpoint blocks now; that is a stated requirement failing, not a
  defect to be weighed.
- **`credibility` findings blocked deterministically**, overruling the release
  judgement built to weigh exactly that. Only `broken` blocks from here.
- **The static-deploy filter was a denylist of four filenames**, which fixed the
  file that broke that day and nothing else -- `poetry.lock`, `setup.cfg`,
  `Dockerfile`, `.python-version` would each have been a fresh outage with an
  identical cause. Now an allowlist of what a browser can request. Its own test
  immediately caught that `.txt` is genuinely ambiguous (`robots.txt` vs
  `requirements.txt`), so served `.txt` files are named -- a short closed list of
  web conventions rather than an enumeration of build files.

**Verified**: 1,279 unit tests, 9 integration tests against real Postgres, and
migration 0037 applied live.

---

## 34. Six repair failures, one cause: nothing knew whether the workspace matched what was deployed

**Found**: 2026-09-14, after six consecutive repair attempts on one product failed
for six different reasons. Jesse named the cause: *"isn't this something to do with
the workspaces being out of sync? Or the latest version not being deployed?"*

**Severity**: Two of the six failures directly, a third aggravated. It also nearly
caused real damage -- see the postscript.

**Root cause**: The repair agent judges the *workspace*; its briefs describe the
*deployed page*. Those agree only while everything has shipped, and they diverge
exactly when it matters most: a repair applies a fix and the deploy never happens.

Once diverged, every path misreads it. A repair opened the workspace, correctly saw
the work already done, reported NOT_NEEDED -- and the handler closed both issues as
invalid and promoted a validation landing page to LIVE while both defects were
still on the page. A later repair, handed a brief describing defects absent from
the files, spent its budget searching the whole container for a page that only
exists on Vercel. The system recorded that as the agent failing.

**Fix, in two goes, and the first was the weaker kind.** `workspace_undeployed`
(migration 0038) recorded it as a *remembered* fact -- set by one code path,
cleared by another -- and drifted immediately: every edit made before the column
existed was invisible to it, so a workspace genuinely holding an unshipped fix
reported false and had to be corrected by hand. That is the same failure as
everything else that day: state the system believes rather than state it can check.

`deployed_content_hash` (migration 0039) makes it checkable. Deploy records a
digest of exactly the bundle it uploaded; asking whether the workspace is shipped
means hashing the workspace the same way and comparing, so both sides are computed
from reality at the moment the question is asked. Length-prefixed rather than
delimiter-separated, because a delimiter that can occur inside base64 lets two
different bundles collide. Plus: NOT_NEEDED with undeployed changes now verifies
and ships instead of concluding there is nothing to do, and no longer forces LIVE.

**Postscript, and the reason this entry matters most.** I re-queued that repair six
times against a workspace I had last inspected properly hours earlier. Between
attempts I checked one thing -- whether the copy defects were gone from index.html
-- and carried that forward as "the workspace is fine". It was not: it had lost
privacy.html, robots.txt, sitemap.xml, requirements.txt and its test file. I was
one command from deploying a page whose privacy link 404s, replacing a
stale-but-working page with a broken one. Jesse stopped it and called the right
move: clear everything and rebuild from requirements.

Verified narrowly, concluded broadly -- the identical mistake I had spent the day
fixing in the system.

---

## 35. The repair agent had no way to make a small edit, and the logs could not say what it ran

**Found**: 2026-09-14, watching a repair for two copy defects exhaust its 16-step
budget.

**Severity**: Every small repair was expensive and likely to fail, and the failure
was attributed to the agent.

**Root cause**: The workspace tools were `read_workspace_file`,
`write_workspace_file` and `run_workspace_command` -- nothing in between. Changing
two sentences in a 15KB page meant reproducing all 15KB or driving sed through the
shell. The agent chose the shell, spent its whole budget there, never called
`write_workspace_file` once, and was recorded as "used its entire budget without
finishing". It was not being careless; it had no better move.

**And the logs actively misled the diagnosis.** `run_workspace_command` wraps every
command as `export PATH=...; {command}`, and `sandbox.execute` logs
`shlex.split(cmd)[:1]` -- so every command any agent has ever run appears in the
record as `export`. I read that as the agent running a no-op seven times, said so,
and had to correct it. Same failure as a deploy reporting readyState=ERROR: the
system did something and could not say what.

**Fix**: `edit_workspace_file(path, old_string, new_string)` -- exact-match and
unique-match, the contract a human editor works under. A snippet appearing twice is
refused rather than guessed, because guessing is how a repair silently changes the
wrong line. And the real command is logged before the prefix is added.

**Verified**: the next repair used it (`workspace.file_edited`, twice) and applied
its changes in 13 steps.

---

## 36. The live reviewer filed fake signups into the demand signal it was measuring

**Found**: 2026-09-14, checking why `validation_signals` was non-empty after being
cleared: `qa.tester.lead@example-agency.test`.

**Severity**: Corrupts the one number a build-or-scrap decision is gated on, and
had just been made recurring.

**Root cause**: The periodic AUDITOR review uses a browser and is told to "actually
try to do that thing yourself". On a landing page the thing is leaving an email
address, so it invented one and it landed in the real table. `preship` already had
a reserved `@factory-selftest.invalid` address the endpoint answers identically but
never records or mails; AUDITOR did not. Extending that sweep to VALIDATING
products the same day turned a latent problem into one signup per page per sweep.

**Fix**: AUDITOR is given the same reserved address. A review that corrupts what it
is measuring is worse than no review.

---

## 37. The analytics beacon never worked in any browser, and the gate that saw it recorded a pass

**Found**: 2026-09-14, in the first clean end-to-end run's pre-ship browser review:
`network_failures=8`, all `POST /analytics/{id} -> FAILED (net::ERR_FAILED)`.

**Severity**: Every generated page's analytics silently collected nothing. The
endpoint itself was fine -- curl returned 200 with correct CORS headers.

**Root cause**: The page used `navigator.sendBeacon(url, new Blob([payload],
{type: 'application/json'}))`. `application/json` is not CORS-safelisted, so the
request requires a preflight, and `sendBeacon` cannot make preflighted requests.
Worse, it returns true for *queued* rather than *delivered*, so the usual
`if (sendBeacon(...)) {} else { fetch(...) }` fallback never ran and the failure
was silent.

Nothing that reads source could catch this, and neither could curl -- which is
precisely what the browser review exists for, and it worked.

**Then the gate recorded a pass anyway.** `preship.reviewed blocking=0
action_succeeded=False`: it blocked only when the primary action was attempted
*and* failed, so a review that never tried the form at all cleared it. The same
shape as ran=False -- a check that did not happen must never be indistinguishable
from a check that passed.

**Fix**: the codegen prompt specifies `fetch(..., {keepalive: true})`, which
survives unload without the restriction, and names why sendBeacon is wrong here.
Not-attempted and attempted-but-failed now both block the release.

---

## 1. Discovery generated the same idea 5 times from the same source, undetected

**Found**: 2026-08-21, Jesse noticed it directly, looking at the pipeline
once the dashboard finally became reachable: "basically the same idea 5
times."

**Severity**: Real waste, not a crash — 4 of the 5 products this factory
has ever built were near-duplicates of each other, each one paying for a
full real build (scaffold generation, Bug Council, deploy attempts) for
what was, underneath the paraphrasing, the same opportunity already built.

**Root cause**: `upsert_idea`'s dedup is a SHA256 fingerprint over the
*generated prose* (`problem_statement` + `target_niche`). An LLM naturally
rephrases the same input differently on every call, so this fingerprint
almost never collides even when the underlying evidence is identical — it
only catches a literal byte-for-byte resubmission, which near-never
happens. Confirmed live: all 5 real ideas in the database carry the exact
same `source_url`
(`https://github.com/microsoft/appcenter/issues/427`) — the same GitHub
issue won the top-ranked cluster slot 5 discovery cycles running (nothing
about a GitHub issue's comment count changes meaningfully hour to hour),
and the generator produced a fresh paraphrase from it each time, every one
passing the fingerprint check and becoming a separate product.

**Affected**: `src/factory/state/repository.py` (new
`used_idea_source_urls`), `src/factory/engines/discovery/graph.py` (new
`_exclude_already_mined`, wired into `run()`).

**Fix**: `source_url` is the one fact the generator is required to copy
verbatim rather than paraphrase ("Never invent one" — `GENERATOR_SYSTEM`),
which makes it the one reliable "have we already mined this" signal
available *before* ever paying for a generation call. `run()` now fetches
every `source_url` any idea has ever used and drops any cluster containing
a signal whose URL is already claimed, before clustering even reaches the
generator — cheaper than the prose-fingerprint check too, since it skips
the LLM call entirely for evidence already spent, rather than paying for a
generation and discrimination call only to have the fingerprint (probably)
not catch the duplicate anyway.

**Verification**: New `tests/unit/test_discovery_dedup.py` (6 tests) and 3
new repository-level tests for `used_idea_source_urls` in
`test_repository.py`. Full suite green (1260 tests) on scratch Postgres
before deploying. Not yet confirmed against a real live cycle producing a
correctly-excluded duplicate (the next real cycle after deploy will
naturally exercise this, since `appcenter#427` is still the top-ranked
cluster).

**Status**: Fixed, deployed, commit `bc893b3`.

---

## 2. Bug-Council-rejected products always burned a full blind regeneration, even for one-line fixes

**Found**: 2026-08-21, Jesse's own framing after entry #3: "in the future
these types of easy fixes shouldn't result in human escalation... it's a
smart system so it should easily be able to do stuff like this" — asked
directly to look into why, structurally, not just fix the one bug.

**Severity**: Not a correctness bug — a standing inefficiency that made
entry #3 (and any future well-described Council finding) more expensive
and more escalation-prone than it needed to be. Every one of the 4 stuck
products burned 3 full blind regenerations before a human ever saw them,
for a finding Council itself had already stated precisely.

**Root cause**: `_should_repair_surgically` excluded every
`council_rejection:*` category from targeted, in-place repair, forcing a
full workspace regeneration instead — reasoning, per the original comment,
that "the agent cannot re-run the Council, so it would be editing blind to
the only opinion that decides the outcome." That reasoning doesn't
actually hold up: a full regeneration is exactly as blind to Council's
live opinion mid-attempt as a surgical edit is, since *both* paths only
find out whether they worked via the same downstream `_verify_and_ship`
(tests + a fresh Council pass) afterward. Meanwhile Council's own
rejection already carries `healing.summary`/`ordered_steps` — the same
quality of actionable guidance a human replying to an escalation would
give, which is exactly what already makes the `guided` category reliable.
A second, smaller gap compounded it: `attempt_repair`'s objective framing
hard-coded "The test suite for this product is failing" for every category
except `growth_seo` — false for a Council rejection (the suite already
passed by the time Council runs), and false for `guided` too, risking the
repair agent hunting for a nonexistent failure instead of acting on the
real, precise guidance it was actually given.

**Affected**: `src/factory/engines/development/graph.py`
(`_should_repair_surgically`, `SURGICAL_CATEGORIES`'s docstring),
`src/factory/engines/development/repair.py` (`attempt_repair`'s objective
framing).

**Fix**: `council_rejection:*` categories (checked via `.startswith()`,
since the category string is dynamic — e.g.
`"council_rejection:pricing,security"`) are now eligible for surgical
repair, reusing the same safety net already proven for every other
category: the last autonomous cycle before the heal-cycle ceiling always
falls back to a full regeneration regardless of category, so a genuinely
structural Council finding a targeted edit can't fix still gets one full
rebuild attempt before escalating, same as today. `dependency_timeout`/
`dependency_failure` (no sandbox network to retry an install) and
`deploy_failure`/`repair_rejected` stay excluded — their exclusion reasoning
was sound and unrelated to the one being fixed here. Added dedicated
objective framing for `council_rejection:*` and `guided`, so neither is
ever told the test suite is failing when it already passed.

**Verification**: New tests in `test_surgical_repair.py`:
`test_a_council_rejection_routes_to_a_surgical_repair` (both a single
category and the real comma-joined multi-category shape),
`test_a_council_rejection_still_regenerates_on_the_last_autonomous_cycle`
(safety net preserved), `test_a_council_rejection_is_never_told_the_test_suite_is_failing`,
`test_a_guided_patch_is_never_told_the_test_suite_is_failing`, and a
regression guard confirming `test_failure`'s original framing survives
unchanged. Full suite green (1260 tests) on scratch Postgres before
deploying. Not yet confirmed against a real live Council rejection (none
has occurred since deploy) — the next one will be the first real test of
this path end-to-end.

**Status**: Fixed, deployed, commit `bc893b3`.

---

## 3. Four products in a row failed billing the same way: the codegen prompt never named the Stripe key's env var

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

## 4. Generated apps crashed on every route once deployed: an unconditional static-file mount

**Found**: 2026-08-20, live end-to-end QA of the first product this factory
ever deployed with a working backend (bug #6's fix made the backend
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

## 5. The one real live product was invisible behind Vercel's own SSO wall

**Found**: 2026-08-20, immediately after bug #6's fix — the deployed
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

## 6. Vercel deployments never actually built the Python backend

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

## 7. A "defer" reply to heal-exhaustion silently became patch guidance instead

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

## 8. Discovery's raw sources: HN contributed nothing, GitHub surfaced the wrong things

**Found**: 2026-08-20, digging into why Discovery's generator kept producing
zero candidates even after bug #9's crash fix was live and cycles were
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

## 9. Gemini: unbounded thinking budget crowds out the JSON answer in `parse()`

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

## 10. Gemini: missing `thought_signature` breaks every multi-turn tool call

**Found**: 2026-08-18, prompted by Jesse noticing the dashboard's provider-health
panel showing `gemini: down`.

**Severity**: Real, recurring, previously invisible (masked by bug #11's fix,
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

## 11. Anthropic: `effort` param unsupported by the Haiku 4.5 fallback model

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

## 12. Dashboard container reports unhealthy despite working correctly

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

## 13. DeepSeek could accidentally serve as fallback for unrelated tasks

**Found**: 2026-08-18, while investigating bug #15 (checking `llm_calls` for
pricing accuracy turned up an unrelated anomalous row).

**Severity**: Real, live risk — not just theoretical. One real production
call already hit it.

**Root cause**: `ProviderRegistry.alternatives()` returns every other
*registered* provider on failover, with no concept of task-type fitness.
Once DeepSeek became a third registered provider (for `CODE_REPAIR`, see
bug #14's context), any Gemini-routed, failover-eligible task type
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

## 14. DeepSeek sent Gemini-shaped tool schemas, not Anthropic-shaped

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

## 15. Gemini and DeepSeek spend silently mispriced

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

## 16. Real code generation was mocked in production

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

---

## 17. Contract check false-positived on `os.environ.get(...)`, forcing needless heal cycles

**Found**: 2026-08-22, while investigating why 4 products that had already
been hand-guided to fix their Stripe wiring still failed the very same
check on the next heal attempt. The raw (unparaphrased) `heal_attempts.
healing_text` insisted `PRODUCT_PRICE_CENTS`/`STRIPE_API_KEY` were "never
read" — directly contradicting the real pulled source file, which read both
correctly.

**Severity**: High-frequency, silent waste — every product whose generated
backend happened to use `os.environ.get(...)` (a fully correct, common
idiom) instead of `os.environ[...]`/`os.getenv(...)` burned an unnecessary
heal cycle on a bug that was never there, and enough of them in a row hit
the autonomous heal ceiling and escalated to a human over nothing.

**Root cause**: `contract.py`'s `_PRICE_ENV_READ_RE`/`_STRIPE_KEY_ENV_READ_RE`
only matched two of the three real Python idioms for reading an environment
variable (`os.environ["X"]`, `os.getenv("X")`) — `os.environ.get("X")`,
equally correct and the one the model actually wrote, silently never
matched.

**Affected**: `src/factory/engines/development/contract.py`.

**Fix**: Refactored into a shared `_env_read_pattern(name)` helper covering
all three idioms.

**Verification**: Reproduced the false positive locally against the real
pulled workspace file (`missing: [...]` before, `missing: []` after).
Parametrized regression tests added
(`tests/unit/test_codegen_contract.py`). Full local + scratch-Postgres
suites green before deploy.

**Status**: Fixed, deployed. No commit (per standing practice, not
committed without being asked).

---

## 18. Dashboard credential-write endpoint failed closed with a real `PermissionError`

**Found**: 2026-08-23, live smoke test of the new `/dashboard/integrations`
credential-write endpoint immediately after its first deploy — caught
before any real operator hit it through the UI.

**Severity**: Would have 500'd on every real write attempt; no data was
ever at risk (fails closed, nothing partially written), but the whole
"add/update credentials from the dashboard" feature would have looked
completely broken the first time anyone tried it.

**Root cause**: The factory container runs as a fixed non-root user (uid
10001) — the same reason `WORKSPACE_ROOT`/`BLACKLIST_PATH` already require
a one-time host `chown`. The newly bind-mounted `.env` (`docker-compose.yml`,
`./.env:/app/.env`) was root-owned on the host, exactly like every `.env`
created by a root shell or a root-run deploy step — readable by the
container (world-readable, `0644`) but not writable by uid 10001. The write
endpoint's `write_env_value()` correctly raised, FastAPI correctly turned it
into a 500, and nothing was corrupted — but the feature did not work.

**Affected**: Host file ownership only, no code bug (`/opt/superagent/.env`).

**Fix**: `chown 10001:10001 .env` on the host, once. Documented next to
`ENV_FILE_PATH` in `.env.example` and in `docker-compose.yml`'s own comment
so a future new-host deploy doesn't rediscover this the same way.

**Verification**: Confirmed via `ls -la` inside the container
(`-rw-r--r-- 1 10001 10001 ... /app/.env`) and via the local integration
test (`test_write_credential_saves_to_the_configured_env_file`) covering
the identical code path against a writable file. Live end-to-end
verification via a real write was blocked by Claude Code's own auto-mode
safety classifier (writing to a production credential file is treated as a
sensitive action) — flagged to the user rather than routed around.

**Status**: Fixed, deployed. No commit.

---

## 19. A tunable prompt's live DB row silently never gets a code-side rewrite once bootstrapped

**Found**: 2026-08-24, not from a production symptom but from the test
suite itself catching real drift: changing `DISCRIMINATOR_SYSTEM`'s text
(adding the Tier 0 gut-check paragraph, system-wide verification plan)
broke `test_idea_defensibility_migration.py::
test_new_discriminator_system_matches_the_live_code_constant` — a test that
exists precisely to compare the migration-seeded DB text against the live
Python constant.

**Severity**: Silent and permanent, not a crash. Every one of this
factory's tunable prompts (`learning/prompts.py::get_prompt`, gated to a
`MUTABLE_KEYS` allowlist) is only ever written to the database *once* — the
first time `get_prompt` is called for that key with no `active` row yet
(`store.bootstrap_active`). Every later call just serves whatever text is
already sitting in that row. Editing the Python constant changes what a
brand-new, never-bootstrapped database will serve; it does nothing at all
for a database that bootstrapped that key any time in the past — which, in
production, is every database that has been running for more than one
tick. Discovered only because `0023_idea_defensibility.py` had already set
the precedent of a migration that rewrites the live row to match, and the
test enforcing that precedent is what actually caught this specific
instance; without that test, `DISCRIMINATOR_SYSTEM`'s new gut-check
instruction and outreach's new tone-check instruction would have shipped in
the container image and never once reached the model actually running
against the production database.

**Root cause**: `get_prompt`/`get_scoring_weights` (`learning/prompts.py`)
have no code path that re-syncs an already-bootstrapped row to a changed
default — by design, since a *tuned* row (promoted by a trial) must never
be silently overwritten by a code deploy. But that same "never touch a
live row automatically" property means an *un-tuned*, still-default row is
indistinguishable from a tuned one to this code, and a source-level edit to
the default text has no effect on it either.

**Affected**: `src/factory/engines/discovery/score.py`
(`DISCRIMINATOR_SYSTEM`), `src/factory/engines/marketing/outreach.py`
(outreach `SYSTEM_PROMPT`) — both edited in the same change as this
session's Tier 0 holistic-check work.

**Fix**: `alembic/versions/0028_tier0_prompt_appendices.py`, following
`0023_idea_defensibility.py`'s exact precedent — appends the new paragraph
to whatever `active` tunable_parameters row already exists for each key,
idempotently (`endswith()` check, safe to run twice). No code-side fix is
possible or desirable here: the correct general fix is procedural, not a
bug in `get_prompt` itself — **any future change to a tunable prompt
constant needs its own migration to reach an already-bootstrapped
database**, the same way a schema change needs a migration to reach
already-existing rows.

---

## 20. 40 identical provider-outage escalations mislabeled as legal review, spamming past the dedup guard

**Found**: 2026-09-10, Jesse asked to start a "quality testing" cycle
evaluating the system's autonomy — specifically, errors that "should be
caught by a supervisor as a human supervisor would." A direct query
against production's `interrupts` table, run to ground that conversation
in real data rather than a hypothetical, turned up 40 `legal_uncertainty`
interrupts in the trailing 14 days — every one of them, on inspection, the
identical Anthropic "credit balance too low" 400 error.

**Severity**: Real, ongoing noise, not a crash. A human reading
`[LEGAL_UNCERTAINTY]` in Telegram reasonably expects a compliance judgment
call; getting the same billing failure under that label 40 times both
wastes their attention and actively erodes trust in every *other*
`legal_uncertainty` interrupt, which are genuine and need real judgment
(see `compliance/graph.py`'s and `compliance/lia.py`'s uses of the same
reason, both correctly classified and correctly deduped by content). Also
compounded by a second failure: the same handler tries to write a
human-friendly explanation via an LLM call (`interrupts.plain_language_error`),
which — since both configured providers were out of credits at once — also
failed, so several of the 40 interrupts showed the human a raw, unedited
`ProviderRequestRejected(...)` repr instead of any explanation at all.

**Root cause**: `supervisor/graph.py::_run_engine`'s generic
`except Exception` handler (the last-resort bucket for an exhausted task)
raises every uncategorized failure as `InterruptReason.LEGAL_UNCERTAINTY`
with `subject=f"{engine.value} task {task_id} exhausted its retries"`. That
subject embeds the failing task's own id, so `interrupts.request`'s
duplicate-open-subject collapse (added specifically to fix a *different*
version of this exact bug class — see entry-free comment in
`supervisor/graph.py::_escalate` calling out "the same bug class as the
generic per-engine task-failure subjects above", and `test_interrupt_
subject_uniqueness.py`) never engages: a fresh `discover_opportunities` task
enqueued on every tick fails the same way and gets its own "unique" subject
every time, because the uniqueness comes from the task id, not the actual
cause. `ProviderUnavailable`/`ProviderRequestRejected` (`llm/providers/
base.py`) were never given their own branch, so they fell all the way
through the specific handlers above (`ProviderRefusal`, `DevOpsError`,
`MarketingFrozen`, `CampaignApprovalPending`, `ValidationCapacityExceeded`,
`RepairBlocked`, `SandboxUnavailable`) into the generic one.

**Affected**: `src/factory/state/schemas.py` (new
`InterruptReason.PROVIDER_OUTAGE`), `src/factory/supervisor/graph.py`
(new `except ProviderUnavailable` branch in `_run_engine`, before the
generic `except Exception`).

**Fix**: A provider failure now gets its own branch and its own reason,
with a subject built from the provider and HTTP status (`f"{provider} is
rejecting requests (HTTP {status})"`) instead of the task id — so every
task hitting the same persistent outage collapses into the one open
interrupt, exactly like `compliance/graph.py`'s niche-keyed and
`compliance/lia.py`'s email-keyed subjects already do correctly. The
explanation is built deterministically from the exception's own structured
`provider`/`status`/message fields rather than calling
`plain_language_error` — which needs a working LLM call to explain an LLM
call failing, the exact single point of failure that left several of the
40 real interrupts with no explanation at all.

**Verification**: New `tests/integration/test_interrupt_subject_
uniqueness.py::test_two_tasks_hitting_the_same_provider_outage_collapse_
into_one_interrupt` — two different tasks failing with the identical
`ProviderRequestRejected` now produce exactly one open interrupt
(`PROVIDER_OUTAGE`, correct subject/detail, no LLM call registered on the
fake provider), proving both the collapse and the no-LLM-dependency
property. Full local unit suite (1037 passed, 2 pre-existing unrelated
Windows path-separator failures) and the relevant integration suite
(`test_interrupt_subject_uniqueness.py`, `test_campaign_approval.py`,
`test_surgical_patch.py` — 12 passed) green on scratch Postgres.

**Status**: Fixed, deployed 2026-09-11. Not yet committed to git.

---

## 21. Live-product auditing only ran when the factory was idle

**Found**: 2026-09-11, while scoping the department-manager architecture. Jesse
reported that a live product's landing page had a broken signup/email-capture
form and a wrong year, unnoticed for weeks; tracing *why* nothing caught it
turned up two independent causes, of which this is the structural one.

**Severity**: Silent and structural. `audit-live-products` is the only
recurring check a shipped product ever gets, and it stops running exactly when
the factory is busiest -- which is precisely when a defect is most likely to
ship unnoticed and least likely to be spotted by a human watching the queue.

**Root cause**: the maintenance catalogue (`supervisor/backlog.py::CATALOGUE`,
which holds `audit-live-products` on a nominal 4h cadence) is enqueued from
exactly one place: `supervisor/graph.py::_backlog`. That node is only reached on
an `IDLE_BACKLOG` or `HOLD_DISCOVERY` routing decision (or as a fallback for an
unknown engine/role). So the "4h cadence" is really "4h cadence, but only on a
tick where there was nothing revenue-bearing to do." Everything else hanging off
that catalogue inherits the same property -- including `prune-memory`,
`audit-snapshot-integrity` and `remind-stale-interrupts`.

A second, unrelated cause compounded it for this specific product: the content
half of that audit (`_content_freshness_issue`) routes through `CODE_REVIEW`,
which is Anthropic-only and in `router.NO_FAILOVER`, and Anthropic had been out
of credits for weeks -- so even on the ticks it did run, it returned `None`
(fail-quiet by design, so a provider outage never looks like a content problem).

**Affected**: `src/factory/supervisor/scheduler.py` (new live-review sweep in
`_schedule_periodic`), `src/factory/settings.py`
(`live_review_interval_days`), plus the new reviewer itself (see below).

**Fix**: product oversight moved onto `scheduler._schedule_periodic`, which runs
unconditionally every tick regardless of load, bucketed daily per live product.
The new check is also materially stronger than the one it backstops: AUDITOR
(`agents/roles.py`, previously defined but entirely unused) now has a real
browser (`tools/browser.py`) and *uses* the product -- submits the form, follows
the call to action -- rather than reading its markup. A form that silently fails
on submit is invisible to every check that never presses the button, which is
why both existing checks reported this product healthy for three weeks. It
commissions fixes through the existing `PATCH_PRODUCT` lever
(`tools/product_review.py`), so nothing bypasses the repair → Bug Council →
deploy pipeline.

The old deterministic `audit-live-products` slug is deliberately left in place
for now, to run alongside the reviewer for a shadow period before its LLM-backed
content half is retired -- its cheap HTTP status check is strictly better at the
uptime question and stays either way.

**Verification**: `tests/unit/test_browser_tools.py` (18 tests -- origin
pinning, SSRF refusals incl. cloud-metadata and sibling compose services,
call-site role scoping), `tests/integration/test_live_product_review.py` (7
tests -- dispatch guards, a finding queues exactly one guided patch, a clean
review queues nothing, dry-run commissions nothing, no same-day patch
stacking), two new scheduler sweep tests. Image built on the real host and
Chromium confirmed launching as the non-root uid-10001 user; `BrowserSession`
exercised end-to-end in that image, including a real navigation against the
live product (HTTP 200, title and form fields correctly extracted).

**Status**: Fixed, deployed 2026-09-12. Not yet committed to git.

**Postscript — the reviewer's own first two runs both failed, and the fix
generalises.** v1 cast AUDITOR as "a demanding manager on a walkthrough" and gave
it a browser returning only visible page text. Asked whether a form worked, it had
no way to observe whether submitting fired a request, so it re-filled and
re-clicked exactly like a confused visitor; both runs hit `step_limit` without
ever calling their reporting tool, and because neither emitted a final text turn
`AgentResult.answer` was empty too, so nothing was recorded either. Two real
reviews performed and discarded.

Three changes, all in this deploy: network/console instruments surfaced on every
browser action (`tools/browser.py`); the role reframed from manager-on-a-walkthrough
to senior QA engineer with an explicit method; and the verdict now *extracted* by a
separate always-run structured call rather than self-reported through a tool the
agent has to remember to use (`tools/product_review.py::queue_review_patch`, the
same investigate-then-extract split `learning/maintenance.py` uses). On the first
run after, it found `POST /api/billing/checkout -> 422` -- the live product's
payment path, broken since launch -- in 11 steps, and stopped on its own.

The transferable lesson, recorded in `ARCHITECTURE_ROADMAP.md`: before giving an
agent judgment over a domain, check the system can actually show it what an expert
in that domain looks at. Access without instruments produces confident flailing.

**Verification**: `tests/unit/test_tier0_prompt_appendices_migration.py`
(new) covers the migration's content and idempotency directly;
`test_idea_defensibility_migration.py`'s existing test was updated to
compose both migrations' text and re-passed. Full local unit suite and a
scratch-Postgres `alembic upgrade head` + integration run both green.

**Status**: Fixed, part of the Tier 0-3 system-wide verification change.
Not yet deployed. No commit (per standing practice, not committed without
being asked).

## 22. The Orchestrator escalated daily because nothing told it what `spend_limit_cents` meant

**Found**: 2026-09-13, by the escalation monitor — the sixth instance of the same
false alarm in two days.

**Symptom**: a `task_exhausted` escalation every day, reading:

> `spend_limit_cents` is 0, blocking all work. A missing budget credential or
> configuration error must be resolved by a human before the factory can resume
> autonomously.

**It was false.** In the six hours around the escalation the factory completed 15
tasks (`reconcile_revenue`, `maintenance`, `discover_opportunities`,
`patch_product`) and made 60 model calls. Nothing was blocked.

**Cause**: `spend_limit_cents` is the **reinvestment pool** —
`budget.get_spend_limit` → `available_cents` → `repository.current_balance`, which
reads the latest `ledger` row and returns `(0, 0)` when the table is empty. The
ledger is empty because this business has not yet earned revenue. Zero is the
correct, expected value.

The Orchestrator could not know that. `_ask_model` hands it
`json.dumps(asdict(snapshot))` — a bare JSON blob, no legend — while the system
prompt says escalation is *"permitted only for a missing credential or genuine
legal uncertainty."* Given an unlabelled zero named like a budget and exactly one
sanctioned escalation category, "the budget credential is missing" is the obvious
inference. The prompt supplied the category and the snapshot supplied the zero; the
model joined them. Its wording even mirrors the prompt back.

Two things made it recur rather than get noticed: the dedup key is day-bucketed
(`Supervisor requested human review ({day})`), so it renews every midnight instead
of colliding; and `needs_reply=False` meant each one was closed by a human glance
rather than investigated.

**Fix**: a legend in the Orchestrator's system prompt for the snapshot fields whose
*name alone misleads* — that `spend_limit_cents` is retained revenue, that zero is
normal before the first paying customer, that it does not gate model work (the
separate daily-spend breaker does, and says so explicitly when it trips), and that
it is never grounds to escalate. Escalation also now requires naming the specific
missing credential or open legal question: if the model cannot name one, it is not
an escalation.

**Not fixed by**: telling the model to be more careful. The number was accessible
and its meaning was not — the instrumentation principle
(`ARCHITECTURE_ROADMAP.md`) applied to the Supervisor itself. This is the same
failure class as bug #21, one level up: there an agent could see a page but not
what an expert looks at; here the Supervisor could see a balance but not what it
measures.

**Verification**: `tests/unit/test_routing.py` — one test pins the legend, and one
is a drift guard asserting every `*_cents` field on `FactorySnapshot` appears in
the prompt, so the next money field added cannot reach the model unlabelled.

**Status**: Fixed locally, not yet deployed.

## 23. Every defect was a first encounter — one bug filed four times in 24 days

**Found**: 2026-09-13, while answering Jesse's question "an issue got made about
this somewhere I assume?"

**Symptom**: the same broken checkout on one product, filed repeatedly as
unrelated work items:

```
attempt 4  09-12 06:43   subscription checkout fails with a 422...
attempt 5  09-12 06:43   checkout flow is broken, returning a 422...
attempt 6  09-12 13:38   subscription checkout fails with a 422...
attempt 7  (later)       subscription checkout fails with a 422...
review     09-13 15:28   $199/mo subscribe/checkout fails      ← found again
```

Attempts 4 and 5 were filed **inside the same minute** — two descriptions of one
defect, as two separate items. The defect was first found on 20 August and was
still live 24 days later.

**Cause**: there was no such thing as an issue. A review produced a
`ReviewVerdict`, `queue_review_patch` turned it into a `PATCH_PRODUCT` task with a
`healing` text blob, and that was the entire record. Three consequences, each
independently sufficient:

- **No identity.** Nothing could tell two reports apart from two defects. Matching
  on the text was never possible — an LLM never writes the same sentence twice,
  which is exactly how BUG_LOG #20's escalation dedup failed.
- **No state.** A task is pending/running/done. A defect is open, being fixed,
  fixed, or verified gone, and those are different questions.
- **No closure.** `done` meant *the patch ran*. Nothing ever re-checked the
  symptom, so a failed repair and a successful one were indistinguishable.

The system was never failing to fix this bug. It was failing to notice it was the
same bug.

**Fix**: an `issues` table (migration `0033`) plus `factory/issues.py`.
`fingerprint` = product + kind + normalised location, with `jaccard` wording
similarity at `memory/store.py`'s existing 0.6 threshold as a second pass when the
reviewer describes a location rather than naming one. `status` runs
detected→triaged→fixing→fixed→verified→closed. **`verified_at` is deliberately
separate from `fixed_at`**: a deploy sets `fixed` (a claim about work); only a
later review failing to find the defect sets `verified` and closes it. A review
that reports it again reopens it, because a defect surviving a claimed repair is
evidence the repair failed. Re-finding a *closed* issue reopens rather than
duplicating — a regression is one row with a history.

**Verification**: `tests/unit/test_issues.py` (21 tests) pins identity in both
directions — the same defect reworded must match, two different defects must not.
`tests/integration/test_issues_lifecycle.py` pins the state machine, including the
one that matters most: a defect still being reported is never closed.

**Status**: Fixed and deployed 2026-09-13.

## 24. The circuit breaker paused a healthy provider for failing schema validation

**Found**: 2026-09-13, about an hour into the DeepSeek POC, by the escalation
monitor reporting `provider_outage: deepseek is in a sustained outage`.

**It was not an outage.** DeepSeek was returning HTTP 200 to every call:

```
orchestration -> deepseek [schema_violation]           x3
orchestration -> deepseek [schema_violation_repaired]  x4
analysis      -> deepseek [schema_violation]           x2
analysis      -> deepseek [schema_violation_repaired]  x2
```

**Cause**: `llm/health.py::tripped_providers` counted successes as
`outcome == "ok"` and treated everything else as failure. A schema violation is
the opposite kind of evidence — the provider accepted the request, billed for it,
and returned a complete response that happened not to satisfy a Pydantic model.

The consequence was a **deadlock**, not a pause. The breaker suspended DeepSeek —
which under POC routing serves nearly every task type — and waiting could never
clear it, because the model would miss the same schemas on resume. The factory
stopped with a perfectly reachable provider.

**Fix**: `NON_TRANSPORT_OUTCOMES` (`schema_violation`, `schema_violation_repaired`,
`refusal`) are excluded from the reachability population entirely rather than
counted as failures. They also must not pad `MIN_CALLS`, or two transport errors
buried in thirty schema failures would read as an outage on a quiet provider.

**The separate, real finding**: `deepseek-flash` genuinely struggles with
structured output on `orchestration` and `analysis` — roughly 3 in 4 attempts
missed the schema. That is the same limitation that kept DeepSeek scoped to
`CODE_REPAIR` (tool-calling only) in the first place, now confirmed to extend
beyond code tasks. It is a routing decision, not a breaker problem, and is
deliberately left open for Jesse rather than silently worked around.

**Verification**: `tests/integration/test_provider_health_classification.py` — a
real outage still trips; schema violations never do; a genuine outage underneath
schema noise is still caught; schema failures do not dilute the evidence
threshold.

**Status**: Fixed and deployed 2026-09-13.

## 25. A null `content` field on an HTTP 200 stopped every engine for fifteen minutes

**Found**: 2026-09-14, live, as a total stall — no engine ran at all and nothing
in the dashboard said why.

DeepSeek returned a successful 200 whose `content` field was `null`. Every path
that read a response iterated it directly:

```python
for block in response.content:   # TypeError: 'NoneType' object is not iterable
```

**The missing content was not the damage; the kind of error was.** A provider
returning nothing usable is an ordinary provider failure, and `SchemaViolation`
is the existing shape for "answered, but not with what was asked for" — retry,
failover and the circuit breaker all know what to do with it. A `TypeError`
escapes every one of those and kills the caller instead.

The caller here was the Orchestrator's own `parse()` — the one call every tick
makes. So every tick died at the same line, and the factory did not degrade, it
stopped.

**Fix**: `AnthropicProvider._content_blocks()`, raising `SchemaViolation` when
`content` is absent. Guarded in the shared base class rather than at the call
sites: `DeepSeekProvider` extends `AnthropicProvider`, so both inherit it, and
fixing the five sites individually would have left the sixth one somebody adds
next.

An **empty** list is deliberately passed through. Each caller already has a far
better message for it — naming the stop reason and the block types it did get,
which is what distinguishes "thinking consumed the whole budget" from "ignored a
forced `tool_choice`". A generic guard would have replaced a real diagnosis with
nothing. The first version of this fix made exactly that mistake.

**Verification**: `tests/unit/test_anthropic_provider.py` — a null `content`
raises `SchemaViolation` rather than `TypeError`; an empty list still reaches the
caller's own message.

**Status**: Fixed 2026-09-14, commit `b75c350`. Not yet deployed.

## 26. A department manager that asked a question and commissioned nothing dropped the question

**Found**: 2026-09-14, while generalising the comms channel — not in production,
because the ask path had not shipped yet.

`run_manager_tick` returned early on `if not decision.actions`, before raising
the questions the manager had said it needed answered:

```python
if not decision.actions:
    return f"design: nothing worth doing -- {decision.assessment[:200]}"

asked = await comms.raise_decision_questions(...)   # unreachable in that case
```

**"I cannot establish whether this matters, so I am asking" is the likeliest
shape of a tick with questions, not an edge case** — it is exactly what the
Design manager did on its first live run. So the branch that dropped the question
was the one that would fire almost every time asking mattered.

Nothing would have errored. The department would have logged `nothing worth
doing` — in a tone that reads as a judgement it made — and waited indefinitely
for a fact nobody was ever asked for.

**A second bug in the same function**: the answers a parked tick was resuming on
were fetched into `answered_block` and never interpolated into the prompt. The
round trip would have bought nothing: released by an answer, the tick would have
asked the identical question again, every six hours, forever.

**Fix**: raise the questions before the empty-actions check, and put the answers
in the prompt. Both are now general — `comms.raise_decision_questions` and
`comms.answers_for` — so the next manager cannot reintroduce either.

**Third, found by the same pass**: `DESIGN` was missing from `comms.ANSWERABLE`,
so no department could ask Design anything. A question routed to a department
whose runner drops it is worse than having no channel: it is recorded, delivered,
and waited on forever, which from outside looks exactly like a hang. A test now
pins that every answerable department's runner actually handles
`ANSWER_QUESTION`.

**Verification**: `tests/unit/test_design_manager.py` — a manager with only a
question still asks it and parks; asking does not cancel work it was already sure
about; a manager with nothing to do is not parked; an answer reaches the prompt.
`tests/unit/test_comms.py` drives the whole path with a department that does not
exist, which is what "general" has to mean.

**Status**: Fixed 2026-09-14, commit `b75c350`. Not yet deployed.

## 27. The Design department's first real work was generated, rejected by the database, and regenerated twice more

**Found**: 2026-09-15, minutes after the Design manager commissioned work for the
first time ever — by an escalation monitor, not by any test.

`record_design_guidelines` stores guidelines in `requirements` under
`subject_kind='design'`, deliberately: they are the same kind of thing as
acceptance criteria — a standard written before the work, that the builder works
to and a reviewer judges against, superseded rather than rewritten.

Migration `0034_requirements` constrained that column:

```sql
subject_kind IN ('product','task','issue','campaign')
```

So every design guideline ever produced was rejected on insert:

```
CheckViolationError: new row for relation "requirements" violates
check constraint "ck_requirements_requirement_subject_kind_valid"
```

**The agent had already done the work.** The rejected row contained a real
palette, a real typographic scale, and a stated design intent. The task retried,
regenerated it from scratch, and had it destroyed again — three times — then
escalated with nothing kept. Three paid generations, three discarded.

**Why nothing caught it.** The mismatch is between a Python string literal and a
SQL constraint, and neither file mentions the other — unreadable from either side.
And the write was unreachable until this week: the Design manager reported
"nothing worth doing" on every tick it had ever run, first because it had no way
to ask the question it needed answered (entry 26), then because its domain query
only saw products that had already shipped. **A department that never acts cannot
fail**, and its silence looked identical to having nothing to do.

**Fix**: `0041_design_requirements` widens the constraint, and the ORM matches.

**The guard is not "design is allowed"**:
`tests/unit/test_requirement_subject_kinds.py` parses every literal
`subject_kind=` the codebase writes and every kind the CHECK constraint permits,
and asserts the two agree — whichever kind somebody adds next. Verified it fails
against the old constraint rather than assumed.

**Second, smaller bug, found deploying the first**: migration 0041 passed the
already-expanded constraint name to `drop_constraint`, and alembic applied this
project's `ck_%(table_name)s_%(constraint_name)s` convention to it a second time,
looking for `ck_requirements_ck_requirements_requirement_subject_kind_valid`. The
migration takes the bare name.

**Verification**: after the fix the same work ran once, succeeded on the first
attempt, and persisted. The department's own domain moved with it — the product
left `without_guidelines` and entered `unreviewed`, which is the state machine
advancing for the first time.

**Status**: Fixed and deployed 2026-09-15, commits `5fdab67` and the naming
follow-up.

## 28. A workspace name an agent invented became a real directory, so guessing worked

**Found**: 2026-09-15, listing production workspaces — 17 directories for 2
products.

`resolve_workspace()` ended with `path.mkdir(parents=True, exist_ok=True)` on
*every* call. So a workspace name an agent made up became a real empty directory
the moment it was mentioned: the command ran, succeeded, returned nothing useful,
and taught the agent nothing — so it guessed again.

Fourteen of those directories were empty and were never anything: `api`, `app`,
`asf`, `autonomous-saas-factory`, `autonomous_software_factory`, `default`,
`factory`, `live`, `main`, `product`, `repair`, `repo`, `saas-factory`,
`workspace` — plus one named **`nonexistent-xyz-123`**, which is a probe the
system answered by creating it.

**Fix**: creating is explicit and the default is not to. The two places that
genuinely bring a workspace into existence — `clear_workspace` and `write_plan`,
both on a first build — pass `create=True`. Everywhere else a missing workspace
raises `UnknownWorkspace`, which says the useful thing: *there is no workspace by
that name, use the one you were given, do not guess.* An error is actionable; an
empty directory is a plausible lie.

`UnknownWorkspace` is deliberately distinct from `UnsafeWorkspace`. They mean
opposite things — one is an attempt to leave the sandbox, the other is a guess —
and conflating them would report an invented name as a security event while
burying the thing worth saying.

**Three silent degradations became honest failures** as a side effect: a repair
against a workspace that no longer exists used to read an empty dict and "repair"
nothing; a deploy could ship an empty site; a test run could pass against nothing.

**Verification**: `tests/unit/test_router_and_sandbox.py` — an unknown name
raises, a traversal attempt still raises the *other* error, and an explicitly
created workspace resolves normally afterwards.

**Status**: Fixed and deployed 2026-09-15.

## 29. A daily maintenance task that could not succeed by construction

**Found**: 2026-09-15, while tracing the source of entry 28.

`improve-test-coverage` asked a REPAIR_ENGINEER to *"find guardrails in the
codebase that lack a direct regression test and write one. Prioritise the
compliance, sandbox, budget and blacklist paths."*

Those are **this factory's own** guardrails. The factory's source is deliberately
unreachable from a sandboxed agent — and the maintenance spawn carries no
`product_id` and no workspace at all (`SpawnRequest` in `engines/maintenance.py`),
so the agent had nowhere to work even in principle.

It ran daily and **every recorded run ended `stopped: step_limit`** — the full
step budget spent hunting for a codebase it was never given, including
`find / -maxdepth 3 -type d -name '*workspace*'`. It is also what produced the
fourteen invented directories in entry 28. Real cost, daily, 0% success rate, by
construction.

**Fix**: retired, with the reasoning left at the site so it is not re-added.

**Reviving it needs a decision the item cannot make for itself: which codebase.**
Pointed at a product it would be a real job — but choosing *which* product
deserves the spend is a judgement nothing in the system owns yet. Pointed at the
factory it is a request for self-modification, which is a far larger decision
than a backlog slot.

Its removal also leaves every remaining maintenance item read-only, which is the
honest state: a maintenance spawn that can *change* things needs somewhere
specific to change them, and this path gives an agent neither.

**Status**: Retired 2026-09-15.

## 30. Parking a task marked it done a line later, which would have silently disabled the whole comms channel

**Found**: 2026-09-15, while fixing the `LockUnavailable` handlers — and it is a
defect in work shipped earlier the same day.

`_run_engine` calls `complete_task` unconditionally the moment a runner returns:

```python
note = await runner(ctx, session, claimed)
await repository.complete_task(session, task_id)
```

`complete_task` had no status guard, so it set `DONE` whatever the runner had
done. A runner that **parks its own work** — defers itself to `PENDING` with a
future `not_before` because it is waiting on an answer — also returns normally.
So "the runner returned" and "the work is finished" were one fact. They are not.

**Completing a parked task is worse than a no-op, it is a silent loss.** Three
things follow, none of them visible:

1. The task is `DONE`, so it never resumes.
2. `answer_question` then declines to release it — releasing only applies to work
   that is `pending` or `deferred` — so the release is a no-op too.
3. The answer somebody was asked for arrives for a task that will never read it.

**Why it had never bitten**: every `defer_task` caller lived in the supervisor's
*scheduling* path, outside the runner, where the conflict cannot arise.
`comms.park_until_answered` is the first to defer from **inside** one — so this
arrived with the comms channel and would have quietly broken both users of it:
the blocked-repair path and the Design manager's park.

It was never observed live only because no question has yet been asked in
production (see entry 26 — the manager reasons its way to the question and does
not populate the field), so nothing has parked yet. A latent bug hidden behind
another bug, again.

**Fix**: `complete_task` only completes a task still `CLAIMED` or `RUNNING`, and
returns whether it did. A database-level invariant rather than a rule every
runner has to remember — the next runner that parks itself inherits it.

**Verification**: `tests/integration/test_repository.py` — a parked task survives
the completion call with its `not_before` intact, and an ordinary task still
completes. **Confirmed the test fails with the guard removed** rather than
assuming it: `assert True is False`.

**Status**: Fixed and deployed 2026-09-15.
