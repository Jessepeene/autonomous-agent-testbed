"""MVP scaffolding.

Claude writes the product; this module writes it to disk safely. Every generated path
is resolved and checked against the workspace root before a single byte is written,
because a model-supplied path like ``../../.env`` is a plausible output and must be a
rejected write rather than an exfiltrated credential.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from factory.engines.development.requirements import (
    HOUSE_STYLE,
    render_design_for_prompt,
)
from factory.logging import get_logger
from factory.sandbox.runner import DevOpsError, resolve_workspace
from factory.settings import EngineName, Settings, TaskType
from factory.state.schemas import DesignGuidelines, ScaffoldPlan, ValidatedIdea
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

SYSTEM_PROMPT = """\
You are the engineering agent of an autonomous SaaS factory. You produce a complete, \
runnable MVP for one validated idea.

The MVP must include, as actual files with real contents:
- A FastAPI backend with a /health endpoint and the core feature endpoints.
- A minimal single-page frontend (plain HTML/CSS/JS, no build step).
- Token-based authentication with password hashing. Never store plaintext passwords.
- Stripe Checkout billing wiring, reading the secret key from the STRIPE_API_KEY
  environment variable — that exact name, not STRIPE_SECRET_KEY or any other guess.
  Observed live, four separate times: STRIPE_API_KEY is the only name the deploy
  pipeline ever actually sets, so a plausible-sounding but wrong name (e.g. the name
  Stripe's own docs use) reads as empty at runtime and every checkout attempt fails
  closed. Every Checkout Session you create must set client_reference_id to the exact value of the
  PRODUCT_ID environment variable, read at request time. Never hard-code it, never
  omit it — it is the only way a payment can be attributed back to this product. The
  Checkout Session's price must be read from the PRODUCT_PRICE_CENTS environment
  variable (an integer number of US cents), also read at request time. Never
  hard-code a price, and never create your own Stripe Price or Product object with a
  baked-in amount — the target price given below is context for what to build, not a
  literal number to embed; the environment variable is the only source of truth for
  what a customer is actually charged.
- An analytics beacon: a small inline script on the frontend's page load that POSTs
  JSON {"path": ..., "referrer": ...} to the exact analytics URL given below.
  Fire-and-forget — never block page rendering on it, and never surface a failure
  to the visitor.
- A standalone privacy.html page describing what data is collected (account details,
  anything the signup or checkout form asks for) and how it's used, linked from the
  signup or account page.
- SEO hygiene on the frontend page: a non-empty <title>, a <meta name="description"
  content="..."> summarising what the product does, Open Graph tags (og:title,
  og:description), a <link rel="canonical"> pointing at the product's own root
  URL, and semantic landmark elements (<header>, <main>, <nav> as applicable)
  rather than an unstructured soup of <div>s. Also produce two standalone files:
  sitemap.xml (listing the root URL) and robots.txt (allowing all crawlers).
- A pytest suite covering the core feature and the auth boundary.
- requirements.txt with a minimum-version lower bound per package (e.g. \
"pydantic>=2.9,<3"), and a README with run instructions. The sandbox installs \
nothing beyond this file, so requirements.txt must list pytest itself, not just \
the packages the tests import.

Hard constraints:
- Every file must be complete. No TODOs, no placeholders, no "implement this".
- Read every secret from os.environ. Never hard-code a key, password, or token, not \
even a fake one.
- Do not write files outside the project directory. All paths are relative, with no \
leading slash and no "..".
- No unbounded loops, no unbounded recursion, no retry loop without a maximum, and \
no sleep longer than 5 seconds. This code runs unattended in a metered container.
- The analytics beacon's fetch/XHR target must be the EXACT analytics URL given in \
the prompt, verbatim. Do not invent your own endpoint path or guess at one.
- test_command must be a single shell command that runs the suite.
- requirements.txt runs on Python 3.13 in a container with no compiler toolchain and \
no writable-and-executable temp directory (/tmp is mounted noexec). Never pin an \
exact old version of a package with a compiled extension (pydantic, cryptography, \
orjson, and similar) — an old exact pin can predate the prebuilt wheel for Python \
3.13, which forces pip to compile from source, which cannot succeed here. Observed \
live: "pydantic==2.6.4" forced a from-source build of pydantic-core that failed \
trying to install a Rust toolchain. Use a lower-bound range instead, so pip resolves \
a release recent enough to ship a Python 3.13 wheel.
- This deploys to Vercel, which serves everything under public/** (index.html, \
privacy.html, sitemap.xml, robots.txt) automatically from its own CDN, separately \
from the FastAPI function — never app.mount("/", StaticFiles(directory=...)) to \
serve them yourself. The deployed function's bundle does not necessarily contain \
public/ at the same relative path your source tree has it, so an unconditional \
mount raises RuntimeError at import time, which takes down every route, not just \
requests for a static file. Observed live: `RuntimeError: Directory \
'/var/task/public' does not exist`, breaking /health along with the entire app. If \
the pytest suite needs the app to also serve those files locally, guard the mount \
so it only runs where the directory actually exists (e.g. `if STATIC_DIR.is_dir():`).
- Never write a literal year (a copyright notice, "as of 2024", etc.) anywhere in \
generated HTML or Python — training data has a cutoff, so a hard-coded year is \
wrong the moment it's read, on every product. A footer copyright line either omits \
the year entirely ("© Acme Inc.") or computes it, e.g. a tiny inline \
`<script>document.getElementById("year").textContent = new Date().getFullYear()</script>` \
for a static page, or `datetime.now().year` read at request time for anything the \
backend renders.

Request/response validation:
- Define an explicit Pydantic model for every request body and response — never a \
bare dict. Give every field a type and, where it matters, a Field(...) constraint \
(min_length, ge/le, a regex) so bad input is rejected by the framework before your \
handler code ever runs.
- Do not silently catch pydantic.ValidationError to paper over bad input. Let FastAPI \
return its own 422 with the validation detail — a caller needs to see exactly which \
field failed, not a generic 400 or a 500.
- Test the validation boundary explicitly: at least one test per endpoint asserting \
that malformed input is rejected with 422, not merely that valid input succeeds.

If you are patching an existing product because a previous build failed, the failure
text may include raw stderr from the sandbox. Two categories, and they call for
different fixes:
- Python tracebacks, assertion failures, and HTTP status mismatches are your code.
  Fix the logic.
- Lines like "permission denied", "command not found", "no space left on device", \
"cannot connect to the docker daemon", or a bare exit code with no Python traceback \
at all are the *environment*, not your code — rewriting application logic will not \
fix a missing binary or a host permission. If everything you are shown is one of \
these, say so plainly in your summary rather than guessing at a code change: change \
nothing risky, and if a dependency genuinely needs pinning to a specific version or \
platform to avoid the failure, do only that.

If you are shown the CURRENT CONTENTS of an existing workspace, this is a repair
pass on real, already-written code — not a blank slate. Keep every existing file
path and module name exactly as given wherever the fix allows it, and edit those
files in place. Do not invent a second, differently-named copy of the app (e.g.
a new "backend/" tree alongside an existing "app/") to work around a problem —
that produces two divergent implementations of the same bugs, which is worse than
either one. Only add a new file for functionality that genuinely does not exist
yet.\
"""


#: Mutable via the Learning Loop (see learning/allowlist.py) -- unlike SYSTEM_PROMPT
#: above, which is permanently excluded from mutation.
LANDING_PAGE_SYSTEM = """\
You are the validation agent of an autonomous SaaS factory. Before any real product \
is built, you produce a single, cheap pitch page that measures whether real people \
actually want it.

The page must include, as actual files with real contents:
- One static HTML page (plain HTML/CSS/JS, no build step, no framework) that pitches \
the idea clearly: the problem it solves, who it's for, and why it's worth paying for. \
Visual polish matters directly here: this page's only job is to earn a stranger's \
trust in the few seconds before they decide whether to leave an email address, and a \
page that looks machine-generated suppresses genuine interest regardless of how good \
the idea is — a low response rate must reflect the idea, not the page.

  **Design it for this specific product, and say so before you build.** Open your \
plan with three lines: a palette of 4-6 named hex values, the two typefaces you are \
pairing and what each is for, and one sentence describing the layout idea. Then build \
to that. A page assembled from defaults is the single most common reason a generated \
page reads as generated.

  Ground the look in the buyer's own world. A tool for CI engineers can borrow from \
terminal output, build logs, monospace timings, a pipeline diagram; a tool for \
accountants borrows from ledgers and reconciliation. Something specific and slightly \
unexpected beats something tasteful and anonymous.

  Typography carries it. Link real typefaces from Google Fonts \
(`<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=...&display=swap">`) \
with a full fallback stack — a system-font-only page reads as unstyled. Pair a \
characterful display face with a comfortable body face, set a type scale and hold to \
it, keep running text near 65 characters wide.

  **Do not ship any of these**, because they are what a generated page looks like and \
every one is a default rather than a decision: the Tailwind slate/gray ramps \
(#f8fafc, #64748b, #e2e8f0, #cbd5e1, #0f172a, #1e293b) or any other framework's \
stock palette; a dark navy or purple-to-blue gradient hero, or any other stock \
gradient treatment; `-apple-system`/Inter/Space Grotesk or any other 'safe default' \
sans as the only typeface; emoji as section markers; every section centered; \
the same rounded corner and card treatment repeated down the page; three feature \
cards in a row with an icon above each, or any other generic feature-grid layout.

  Pick your neutrals rather than inheriting them — a grey nudged slightly toward the \
accent hue reads as chosen, a stock mid-grey reads as untouched. Spend boldness in \
one place and keep everything around it quiet. Real, specific copy throughout, never \
lorem-ipsum, and a mobile-responsive layout.
- An interest-capture form (email, and an optional short message) that POSTs as JSON \
to the exact interest URL given below. On success, replace the form with a short \
message telling the visitor to check their email and click the confirmation link to \
finish registering their interest -- submitting the form is not the final step, the \
confirmation is. On failure, show an error and leave the form intact so the visitor \
can retry.
- An analytics beacon: a small inline script that runs on page load and POSTs JSON \
{"path": ..., "referrer": ...} to the exact analytics URL given below, using \
`fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: \
JSON.stringify(...), keepalive: true})`. Fire-and-forget — never block page \
rendering on it, and never surface a failure to the visitor.
  Do NOT use `navigator.sendBeacon` here. The endpoint requires \
`Content-Type: application/json`, which is not CORS-safelisted, so the request \
needs a preflight and `sendBeacon` cannot make preflighted requests — it fails with \
`net::ERR_FAILED`. It also returns `true` for *queued*, not *delivered*, so the \
usual `if (navigator.sendBeacon(...)) {} else { fetch(...) }` fallback never runs \
and the failure is silent. A generated page shipped exactly that and its beacon \
never worked in any browser; `keepalive: true` gives the same survives-unload \
behaviour without the restriction.
- A standalone privacy.html page stating that the visitor's submitted email is used \
only to notify them about this product and nothing else, linked from the \
interest-capture form's page.
- SEO hygiene: a non-empty <title>, a <meta name="description" content="..."> \
summarising the pitch, Open Graph tags (og:title, og:description), a \
<link rel="canonical"> pointing at the page's own root URL, and semantic landmark \
elements (<header>, <main>) rather than an unstructured soup of <div>s. Also \
produce two standalone files: sitemap.xml (listing the root URL) and robots.txt \
(allowing all crawlers) — a page nobody can find gathers no interest signal at all.
- A pytest suite that serves the page (e.g. via FastAPI's StaticFiles, or an \
equivalent minimal static server) and asserts the page loads and contains the pitch \
and the form.
- requirements.txt with a minimum-version lower bound per package (see the pinning \
rule below), and a README with run instructions. The sandbox installs nothing \
beyond this file, so requirements.txt must list pytest itself, not just the \
packages the tests import.

How to write it: as a product page, not an experiment report.

The page is a demand test, but it must never read like one. Describe the product in \
the present tense, the way any company describes something it sells: what it does, \
who it is for, what it costs. Do not tell the visitor the product does not exist \
yet, do not describe the page as a test or a validation, and do not hedge what the \
product does. A page that announces it is not a real product measures nothing, \
because nobody signs up for something that is not there.

Two things you must never do, and they are firm:

- **Never invent evidence.** No testimonials, no customer names or logos, no \
"trusted by 400 engineering teams", no case studies, no review scores, no press \
mentions, no usage numbers. Each of those is a fabricated claim about real people \
and none of them is yours to make. Persuade with the problem and the solution, \
which are real.
- **Never take payment or imply the visitor can buy now.** State the price plainly \
as what it will cost. The only thing this page ever collects is an email address, \
so a button promising a purchase or a trial that the visitor cannot actually start \
is a lie told at the most important moment on the page. "Get early access" is \
honest and converts; "Start your free trial" leading to an email box is not.

Build no more than the page needs:
- No authentication, no user accounts, no database beyond nothing.
- No Stripe or any billing integration.
- No backend business logic of any kind beyond serving the static page. If a minimal \
backend is needed to serve the page, its only job is that.

Hard constraints:
- Every file must be complete. No TODOs, no placeholders, no "implement this".
- Do not write files outside the project directory. All paths are relative, with no \
leading slash and no "..".
- test_command must be a single shell command that runs the suite.
- Nothing internal to this factory may appear on the page. The prompt gives you \
context about where the idea came from and how it was assessed — that is background \
for you, never material for the visitor. No links to issue trackers, forums, \
discussions or research sources, no "this problem is real, see X", no mention of \
the factory, the validation process, or the idea's provenance. A visitor should see \
a company describing its product, with no trace of how the page came to exist.
- requirements.txt runs on Python 3.13 in a container with no compiler toolchain and \
no writable-and-executable temp directory (/tmp is mounted noexec). Never pin an \
exact old version of a package with a compiled extension — use a minimum-version \
lower bound instead (e.g. "fastapi>=0.115,<1"), so pip resolves a release recent \
enough to ship a Python 3.13 wheel.
- The interest form's fetch/XHR target must be the EXACT interest URL given in the \
prompt, verbatim. Do not invent your own endpoint path or guess at one.
- The analytics beacon's fetch/XHR target must be the EXACT analytics URL given in \
the prompt, verbatim. Do not invent your own endpoint path or guess at one.\
"""


#: Appended to `LANDING_PAGE_SYSTEM`'s prompt only when a live AB test is running
#: for this product (see `development/ab_testing.py`). Deliberately a client-side
#: split, not two deployments behind an edge router: it fits the existing "single
#: static deploy, no build step" constraint with no new hosting mechanism, and it
#: reuses the interest-capture and analytics endpoints exactly as they already
#: exist -- only one new field (`variant`) is added to each POST body.
_AB_TEST_INSTRUCTIONS = """\

This page is running a live AB test. Render BOTH variants below in the SAME page \
as two separate content blocks, exactly `<div id="variant-a">...</div>` and \
`<div id="variant-b">...</div>`, each a complete, fully-styled pitch on its own \
(not a diff against the other) built from its own brief:

Variant A brief: {variant_a_brief}

Variant B brief: {variant_b_brief}

Add a small inline script, run before either block is shown, that:
1. Reads `localStorage.getItem('ab_variant')`. If empty, picks "a" or "b" with \
equal probability and stores it with `localStorage.setItem('ab_variant', ...)` -- \
so a returning visitor always sees the same variant they saw before.
2. Shows only the chosen variant's block (`hidden = false`) and hides the other \
(`hidden = true`).
3. Includes `variant: <the chosen value>` in the JSON body of BOTH the interest-form \
POST and the analytics beacon POST -- this is the only way results can be told \
apart per arm afterward.\
"""


class UnsafeGeneratedPath(ValueError):
    """A generated file path tried to escape the product workspace."""


class WorkspaceNotCleared(DevOpsError):
    """The workspace could not be emptied, so a fresh scaffold must not be written.

    A host/environment failure rather than a code defect -- the usual cause is a
    file the host uid cannot unlink because a sandbox uid created the directory
    holding it. Callers route it accordingly (`is_infra_failure`), because handing
    it to a repair agent as a code bug would waste a heal cycle on something no
    edit to the product can fix.
    """


async def generate_scaffold(
    ctx: FactoryContext,
    idea: ValidatedIdea,
    *,
    product_id: str,
    task_id: str | None = None,
    existing_files: dict[str, str] | None = None,
    #: The standard this build is held to, resolved by
    #: `requirements.py::effective_design` -- this product's own guidelines when
    #: Design wrote any, the house style when it did not. Defaulted so every
    #: existing caller keeps its previous behaviour exactly.
    design: DesignGuidelines = HOUSE_STYLE,
    #: The acceptance criteria, rendered by `requirements.py::render_for_prompt`.
    #: Its sibling `generate_landing_page` has taken these since criteria existed;
    #: this did not, so a full regeneration was judged against a standard it was
    #: never shown -- and a release decision duly reported five of six MUSTs unmet,
    #: for the honest reason that nobody had told it what they were.
    requirements: str | None = None,
) -> ScaffoldPlan:
    """``existing_files`` (from `read_workspace`) turns this into a repair pass:
    the model sees exactly what is already on disk instead of regenerating a whole
    MVP blind to its own previous attempt. Without this, two generations for the
    same product routinely disagree on file layout (one calls its package `app/`,
    the next `backend/`), and since `write_plan` only ever adds files, both
    versions -- and both sets of bugs -- pile up in the workspace side by side.
    Observed live: Bug Council rejected a build for exactly this, having found the
    same N+1 query bug duplicated across `app/main.py` and `backend/main.py`.

    ``product_id`` is required (unlike a real backend, which reads PRODUCT_ID from
    its own environment at request time for Stripe attribution) because the
    frontend's analytics beacon is static JS with no server-side templating and no
    env var to read -- the exact URL has to be composed here and handed to the
    model, the same reason `generate_landing_page` already does this for its
    interest-capture URL.
    """
    analytics_url = f"https://{ctx.settings.public_domain}/analytics/{product_id}"
    prompt = (
        f"Validated idea:\n"
        f"- problem: {idea.problem_statement}\n"
        f"- target niche: {idea.target_niche}\n"
        f"- monetization: {idea.monetization_hypothesis}\n"
        f"- target self-serve price: ${idea.self_serve_price_usd:.2f}/month (context "
        f"for what to build -- the actual Checkout amount comes from "
        f"PRODUCT_PRICE_CENTS at request time, see the system prompt)\n"
        # `source_url` is deliberately not passed. It was, labelled as internal and
        # explicitly not for the page, and a generated page cited it anyway: "This
        # pain is real and public -- read the corroborating discussion in this App
        # Center issue", linking the GitHub thread Discovery had scraped the idea
        # from. Jesse's call, and the right shape of fix: not a better warning, just
        # don't hand it over. The page describes a product; where the idea came from
        # cannot inform that, so passing it only creates a way to get this wrong.
        "\n"
        # The same guidelines the gap check will judge the result against. One
        # rendering shared by builder and reviewer, so they cannot work to two
        # different readings of the same standard -- which is why this is the
        # resolved `design`, not the constant: the reviewer resolves it the same way.
        + render_design_for_prompt(design)
        + "\n\n"
        + f'Analytics beacon URL (POST JSON {{"path": ..., "referrer": ...}} here, '
        f"exactly, verbatim): {analytics_url}\n\n"
    )
    if requirements:
        # Ahead of the workspace dump for the same reason the landing page builder
        # puts it ahead of its instruction: this is the standard the result is judged
        # against, not a hint. A regeneration that cannot see the criteria cannot
        # meet them, and will be told afterwards that it did not.
        prompt += (
            "ACCEPTANCE CRITERIA this product was built to meet -- it will be "
            "reviewed against exactly these, and anything listed out of scope is "
            "absent on purpose and must not be built:\n\n" + requirements + "\n\n"
        )
    if existing_files:
        prompt += (
            "CURRENT CONTENTS of the workspace (this is a repair pass -- see the "
            "system prompt for how to treat these):\n\n"
            + "\n\n".join(
                f"--- {path} ---\n{contents}" for path, contents in existing_files.items()
            )
            + "\n\n"
        )
    prompt += "Produce the complete MVP."
    # Building a product from nothing and rebuilding one that already exists are
    # different jobs, and `existing_files` is exactly what separates them: with it,
    # the model is editing a known product; without it, inventing one from a brief.
    # They were one task type, so a routine feature iteration was priced, budgeted
    # and routed as the one-shot whole-product build.
    task_type = TaskType.CODE_ITERATION if existing_files else TaskType.CODE_GENERATION
    return await ctx.llm.parse(
        ScaffoldPlan,
        task_type=task_type,
        system=SYSTEM_PROMPT,
        prompt=prompt,
        engine=EngineName.DEVELOPMENT,
        task_id=task_id,
    )


async def generate_landing_page(
    ctx: FactoryContext,
    idea: ValidatedIdea,
    *,
    product_id: str,
    system: str = LANDING_PAGE_SYSTEM,
    tunable_parameter_id: str | None = None,
    task_id: str | None = None,
    existing_files: dict[str, str] | None = None,
    ab_variants: tuple[str, str] | None = None,
    #: This page's own acceptance criteria, rendered by
    #: `development/requirements.py::render_for_prompt`. Optional because a
    #: build with none is exactly what happened before they existed -- a
    #: degradation, never a reason to refuse to build.
    requirements: str | None = None,
    #: What the Design department decided *for this product*, when it has decided
    #: anything. Deliberately `None` rather than defaulted to `HOUSE_STYLE`: the
    #: system prompt above already carries the general direction in prose, so the
    #: house style here would be a second copy of guidance the model already has.
    #: A product's own guidelines are different in kind -- specific decisions rather
    #: than instructions to decide -- and only those are worth the tokens.
    design: DesignGuidelines | None = None,
) -> ScaffoldPlan:
    """A cheap, pitch-only page for the idea validation gate -- see
    `development/graph.py::_build_landing_page`. Unlike `generate_scaffold` (whose
    SYSTEM_PROMPT is permanently excluded from the Learning Loop), the caller is
    expected to have already resolved `system`/`tunable_parameter_id` via
    `learning_prompts.get_prompt(key=..., default=LANDING_PAGE_SYSTEM)` -- this
    prompt's copy is meant to self-improve over time.

    The interest-capture URL is baked into the prompt, not left for the model to
    guess: a static page has no server-side env var to read PRODUCT_ID from the way
    a real backend does (see `deploy.py::_runtime_env`), so `settings.public_domain`
    + `product_id` are composed here into the one exact URL the model must use. The
    analytics beacon URL is composed the same way, for the same reason.

    ``ab_variants``, when given (the two variant briefs, "a" then "b"), switches
    the prompt into the dual-variant pattern -- see `_AB_TEST_INSTRUCTIONS`. Comes
    from `repository.get_running_ab_test`, checked by the caller before every
    landing-page (re)generation, not threaded through the task payload -- so a
    repair pass mid-test still regenerates both variants correctly without
    needing to know a test was involved.
    """
    interest_url = f"https://{ctx.settings.public_domain}/interest/{product_id}"
    analytics_url = f"https://{ctx.settings.public_domain}/analytics/{product_id}"
    prompt = (
        f"Validated idea:\n"
        f"- problem: {idea.problem_statement}\n"
        f"- target niche: {idea.target_niche}\n"
        f"- monetization: {idea.monetization_hypothesis}\n"
        f"- target self-serve price: ${idea.self_serve_price_usd:.2f}/month -- the "
        f"pitch should state this price\n"
        # `source_url` is deliberately not passed. It was, labelled as internal and
        # explicitly not for the page, and a generated page cited it anyway: "This
        # pain is real and public -- read the corroborating discussion in this App
        # Center issue", linking the GitHub thread Discovery had scraped the idea
        # from. Jesse's call, and the right shape of fix: not a better warning, just
        # don't hand it over. The page describes a product; where the idea came from
        # cannot inform that, so passing it only creates a way to get this wrong.
        "\n"
        f'Interest capture URL (POST JSON {{"email": ..., "message": ...}} here, '
        f"exactly, verbatim): {interest_url}\n\n"
        f'Analytics beacon URL (POST JSON {{"path": ..., "referrer": ...}} here, '
        f"exactly, verbatim): {analytics_url}\n\n"
    )
    if ab_variants is not None:
        variant_a_brief, variant_b_brief = ab_variants
        prompt += _AB_TEST_INSTRUCTIONS.format(
            variant_a_brief=variant_a_brief, variant_b_brief=variant_b_brief
        )
    if existing_files:
        prompt += (
            "CURRENT CONTENTS of the workspace (this is a repair pass -- see the "
            "system prompt for how to treat these):\n\n"
            + "\n\n".join(
                f"--- {path} ---\n{contents}" for path, contents in existing_files.items()
            )
            + "\n\n"
        )
    if requirements:
        # Ahead of the instruction, because it is the standard the result is judged
        # against -- not a hint. The generic requirements in LANDING_PAGE_SYSTEM say
        # what every page needs; these say what *this* one is for.
        prompt += (
            "ACCEPTANCE CRITERIA for this specific page -- it will be reviewed "
            "against exactly these:\n\n" + requirements + "\n\n"
        )
    if design is not None:
        # The half of the loop that was missing. Design could write a standard for a
        # specific product and store it; nothing read it, so the page it was written
        # for was built to the general house style instead.
        #
        # Placed after the system prompt's general direction and marked as replacing
        # it, because the two are different in kind: "choose 4-6 named hex values" is
        # an instruction to decide, and "the palette is #E74C3C because it is the
        # build-failed state this audience lives in" is the decision. Left ambiguous,
        # a model asked to decide something already decided will decide it again.
        prompt += (
            "DESIGN GUIDELINES decided by the design department for this specific "
            "product. Where these are more specific than the general direction "
            "above, these win -- they are decisions already made, not suggestions "
            "to weigh:\n\n" + render_design_for_prompt(design) + "\n\n"
        )
    prompt += "Produce the complete validation page."
    return await ctx.llm.parse(
        ScaffoldPlan,
        # Not split by `existing_files` the way the MVP path is: a landing page is a
        # page with a form whether it is the first draft or the fifth, and nothing
        # about regenerating one is harder than writing it.
        task_type=TaskType.LANDING_PAGE,
        system=system,
        prompt=prompt,
        engine=EngineName.DEVELOPMENT,
        task_id=task_id,
        tunable_parameter_id=tunable_parameter_id,
    )


def safe_target(root: Path, relative: str) -> Path:
    """Resolve a generated path inside ``root`` or refuse it."""
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise UnsafeGeneratedPath(f"generated path escapes workspace: {relative!r}")
    return candidate


def clear_workspace(workspace: str, settings: Settings | None = None) -> None:
    """Wipe everything under the workspace root before `write_plan` writes a fresh
    scaffold.

    `write_plan` only ever adds files -- it never removes ones that existed from a
    previous attempt. Every heal cycle, and every `build_mvp` retried after a crash
    (both reuse the same product-scoped workspace directory), call `generate_scaffold`
    again, and two generations rarely agree on exact file layout. Without clearing
    first, stale files from the previous attempt silently persist alongside the new
    ones regardless of how well the model reuses the existing structure -- this is
    the deterministic guarantee; the prompt guidance in `generate_scaffold` is the
    complementary attempt to avoid needing it in the first place.
    """
    # The other of the two creators -- see `resolve_workspace`. This runs before
    # `write_plan` on a first build, so it is the first thing to touch the directory
    # and must be allowed to bring it into existence rather than failing on a product
    # that has never been built.
    root = resolve_workspace(workspace, settings, create=True)

    def _log_delete_failure(func: object, path: str, exc: BaseException) -> None:
        # ignore_errors=True previously swallowed every deletion failure (a
        # permission error, a file locked by another process, a symlink) with no
        # trace at all -- exactly the "stale files silently persist" failure this
        # function exists to prevent, just one layer deeper: the delete itself
        # silently failing instead of the write silently not clearing first.
        log.warning(
            "scaffold.clear_workspace_failed",
            workspace=workspace,
            path=path,
            detail=str(exc)[:200],
        )

    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, onexc=_log_delete_failure)
        else:
            child.unlink(missing_ok=True)

    # The docstring above calls this "the deterministic guarantee". It was not one:
    # every deletion failure was logged and then `write_plan` wrote a fresh scaffold
    # over whatever survived, producing exactly the hybrid workspace this function
    # exists to prevent -- new source beside the previous generation's files.
    #
    # Seen live: sandbox-created `app/__pycache__` (uid 10001, not removable by the
    # host uid) survived, and the next repair agent spent six of sixteen steps
    # working out whether that bytecode matched the source.
    #
    # Refusing is the honest outcome. A caller that cannot get a clean workspace
    # should stop, not build on an unknown mixture -- and `PYTHONDONTWRITEBYTECODE`
    # in the sandbox now removes the one cause seen in practice, so this should be
    # unreachable rather than routine.
    leftovers = sorted(child.name for child in root.iterdir())
    if leftovers:
        raise WorkspaceNotCleared(
            f"{workspace} still holds {len(leftovers)} entr(ies) after clearing "
            f"({', '.join(leftovers[:8])}) -- refusing to write a scaffold over them"
        )


def write_plan(plan: ScaffoldPlan, workspace: str, settings: Settings | None = None) -> list[str]:
    """Materialise the plan into the product workspace. Returns the paths written."""
    # One of exactly two places a workspace is brought into existence -- see
    # `resolve_workspace`. A first build has nowhere to write yet.
    root = resolve_workspace(workspace, settings, create=True)
    written: list[str] = []
    for generated in plan.files:
        target = safe_target(root, generated.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated.contents, encoding="utf-8")
        written.append(str(target.relative_to(root)))
    log.info("scaffold.written", workspace=workspace, files=len(written))
    return written


#: Shared by every reader of a generated workspace -- `read_workspace` (what Bug
#: Council reviews) and `deploy._collect_files` (what actually ships) must exclude
#: exactly the same directories, or something can ship that was never reviewed by
#: anyone. `.deps`: where _verify_and_ship's install step puts dependencies (see
#: graph.py) so they survive into the separate test-run container -- without this,
#: Bug Council reviews third-party library source as if it were the model's own
#: code (pytest/sqlalchemy/pydantic's legitimate internal eval()/exec()/shell=True
#: usage tripped the privilege scanner and blocked every deploy). `.pytest_cache`:
#: sorts early (dot-prefixed) and is pure post-test-run artifact, never source.
#: `.factory`: the durable per-product knowledge file a repair agent reads before it
#: starts (see `development/repair.py`). It is notes *about* the product, not part of
#: it -- reviewing it would spend Council tokens on prose no user runs, and shipping
#: it would publish the factory's internal commentary alongside the product.
WORKSPACE_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        ".deps",
        ".pytest_cache",
        ".factory",
    }
)


def read_workspace(workspace: str, settings: Settings | None = None) -> dict[str, str]:
    """Read back the generated source for review. Skips binaries and vendored trees."""
    root = resolve_workspace(workspace, settings)
    sources: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in WORKSPACE_SKIP_DIRS for part in path.parts):
            continue
        try:
            sources[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return sources
