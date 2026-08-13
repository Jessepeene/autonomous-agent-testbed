"""Ephemeral agent roles.

A role is a capability envelope, not a personality: a system prompt plus the tool
categories that role is permitted to touch plus a step ceiling. The Orchestrator
picks a role by name; it does not get to invent one at runtime.

That constraint is deliberate. Letting a model compose its own tool set would mean
the thing being contained decides what it is contained by — a research agent that
wanted the sandbox could simply ask for it. Roles are the allowlist, and adding one
is a code change that a human reviews.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from factory.settings import TaskType


class RoleName(StrEnum):
    RESEARCHER = "researcher"
    ANALYST = "analyst"
    REPAIR_ENGINEER = "repair_engineer"
    AUDITOR = "auditor"
    LEAD_SCOUT = "lead_scout"
    #: Excluded from `role_catalogue()` -- see WORKSPACE_SCOPED_ROLES.
    WORKSPACE_SURGEON = "workspace_surgeon"
    #: Reads one product's analytics/AB-test state and decides whether to start an
    #: AB test or request an SEO pass -- see development/graph.py::_growth_review.
    GROWTH_ANALYST = "growth_analyst"
    #: Excluded from `role_catalogue()` -- see WORKSPACE_SCOPED_ROLES.
    SEO_OPTIMIZER = "seo_optimizer"
    #: Researches candidate organic growth channels (Reddit, forums, communities)
    #: a product does not yet use, and drafts content for one -- gated behind a
    #: mandatory human approval before anything happens with it. See
    #: engines/marketing/channel.py.
    CHANNEL_SCOUT = "channel_scout"
    #: Excluded from `role_catalogue()` -- see PIPELINE_SCOPED_ROLES.
    DISCOVERY_SCOUT = "discovery_scout"


#: Roles the Orchestrator must not be able to pick. A workspace surgeon is only
#: correct inside `development/graph.py::_patch`, which pins it to one product, holds
#: `product_lock` for its whole run, snapshots the workspace so its edits can be
#: rolled back, and audits what it changed afterwards. Spawned from the Orchestrator
#: it would have none of that: no lock, no snapshot, no heal accounting, and a
#: model-chosen objective. `role_catalogue()` filters these out so the Orchestrator's
#: own prompt never lists them as an option.
#:
#: SEO_OPTIMIZER follows the identical precedent: it is only ever spawned from
#: `_patch_surgically`, on a `growth_seo` category task GROWTH_ANALYST itself
#: enqueued via `request_seo_pass_action` -- the same locked, snapshot-protected
#: context WORKSPACE_SURGEON runs in, chosen by category rather than a second
#: hardcoded role.
WORKSPACE_SCOPED_ROLES = frozenset({RoleName.WORKSPACE_SURGEON, RoleName.SEO_OPTIMIZER})

#: Same reasoning as WORKSPACE_SCOPED_ROLES, different scope: DISCOVERY_SCOUT is only
#: ever spawned from `engines/discovery/scout.py`, once per Discovery cycle, with a
#: fixed objective and the cycle's own `task_id` (so its findings drain back into
#: that cycle -- see `submit_signal`'s required `task_id` argument). It is a pipeline
#: step standing in for what the fixed sources do mechanically, not an open-ended
#: capability the Orchestrator should hand an arbitrary objective.
PIPELINE_SCOPED_ROLES = frozenset({RoleName.DISCOVERY_SCOUT})


@dataclass(frozen=True, slots=True)
class AgentRole:
    name: RoleName
    purpose: str
    system_prompt: str
    tool_categories: tuple[str, ...]
    task_type: TaskType
    max_steps: int = 8
    deadline_seconds: int = 600
    #: Roles that can act on the world (write files, send things) rather than only
    #: read. Used to keep destructive capability out of dry-run and out of roles that
    #: were spawned for a read-only question.
    mutating: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


_SHARED_RULES = """
Operating rules:
- You have a limited number of steps. Spend them on tool calls that change your
  answer, not on confirming things you already know.
- If a tool returns an error, read it and adapt. Do not call the same tool with the
  same arguments twice.
- If you cannot complete the objective with the tools you have, say so plainly and
  state exactly what is missing. A clear "blocked, and here is why" is more useful
  than a confident guess.
- Never invent a URL, a metric, a customer, or a quote. If you did not read it from a
  tool result, do not assert it.
- When you are done, give the answer directly. No preamble.
"""

#: `_SHARED_RULES` with the one line that would break a repair loop removed, plus the
#: constraints specific to editing a workspace that gets reviewed and deployed.
#:
#: The removed line is "Do not call the same tool with the same arguments twice." For
#: every other role that is good advice; for this one it forbids the entire method --
#: a repair agent MUST run the identical test command before and after its edit,
#: because that before/after pair is the only evidence the fix actually worked.
_REPAIR_RULES = """
Operating rules:
- You have a limited number of steps. Spend them on tool calls that change your
  answer, not on confirming things you already know.
- Running the SAME test command again after an edit is expected and required -- that
  is how you verify the fix. Do not skip it to save a step.
- If a tool returns an error, read it and adapt. Do not repeat a call that failed for
  the same reason twice.
- You have no network access. `pip install`, `curl` and `apt` will all fail.
  Dependencies are already installed; the test command sees them.
- Never delete, rename, skip, xfail, or weaken an assertion in any test, and never
  suppress an error to make a test pass. If you believe a test itself is wrong, say
  so and stop -- do not edit it.
- Do not modify requirements.txt unless the failure is an import error naming a
  package that is genuinely missing.
- Every file you leave in the workspace is read by a code reviewer and then deployed.
  No scratch files, no debug scripts, no .bak or .orig copies, and never a second
  copy of a module -- a stale duplicate alongside the real one is a bug that has
  blocked a deploy here before.
- If you cannot fix it with the tools you have, say so plainly and state exactly what
  is missing. A clear "blocked, and here is why" is more useful than a guess.
- Never invent a file, a line number, or an error message. If you did not read it
  from a tool result, do not assert it.
- End your final message with one line, exactly: RESULT: FIXED, RESULT: NOT_FIXED, or
  RESULT: BLOCKED -- followed by the test command you last ran and its exit code.
"""

ROLES: dict[RoleName, AgentRole] = {
    RoleName.RESEARCHER: AgentRole(
        name=RoleName.RESEARCHER,
        purpose="Answer an open question about the outside world using public sources.",
        system_prompt=(
            "You are a research agent for an autonomous B2B software company. You "
            "investigate a specific question using public web sources and report what "
            "you actually found.\n"
            "Prefer primary sources over summaries. Quote the specific claim and give "
            "its URL. Where sources disagree, say so rather than picking one.\n" + _SHARED_RULES
        ),
        tool_categories=("http", "git"),
        task_type=TaskType.ANALYSIS,
        max_steps=10,
    ),
    RoleName.ANALYST: AgentRole(
        name=RoleName.ANALYST,
        purpose="Reason over factory state and produce a decision or recommendation.",
        system_prompt=(
            "You are an analyst for an autonomous B2B software company. You are given "
            "factory state and asked for a judgement.\n"
            "Quantify where you can. Name the single highest-leverage action rather "
            "than listing ten. If the data does not support a conclusion, say which "
            "query or file would settle it.\n" + _SHARED_RULES
        ),
        tool_categories=("http",),
        task_type=TaskType.ANALYSIS,
        max_steps=6,
    ),
    RoleName.REPAIR_ENGINEER: AgentRole(
        name=RoleName.REPAIR_ENGINEER,
        purpose="Diagnose and fix a failing build or a broken live product.",
        system_prompt=(
            "You are a repair engineer for an autonomous software factory. A build or "
            "a live product is failing and you must find out why.\n"
            "Reproduce before you theorise: run the failing command in the sandbox and "
            "read the actual error. Change the smallest thing that fixes the root "
            "cause. Do not refactor surrounding code, and do not suppress an error to "
            "make a test pass.\n" + _SHARED_RULES
        ),
        tool_categories=("sandbox", "git", "http"),
        task_type=TaskType.CODE_GENERATION,
        max_steps=14,
        deadline_seconds=1800,
        mutating=True,
    ),
    RoleName.AUDITOR: AgentRole(
        name=RoleName.AUDITOR,
        purpose="Check a live product's health and report degradation.",
        system_prompt=(
            "You are an auditor for an autonomous software company. You check whether "
            "a deployed product is actually working from the outside.\n"
            "Fetch its health endpoint and its main page. Report concrete observations: "
            "status codes, response times, error text. Distinguish 'degraded' from "
            "'down' from 'fine'. Do not speculate about causes you cannot observe.\n"
            + _SHARED_RULES
        ),
        tool_categories=("http",),
        task_type=TaskType.ANALYSIS,
        max_steps=6,
    ),
    RoleName.WORKSPACE_SURGEON: AgentRole(
        name=RoleName.WORKSPACE_SURGEON,
        purpose="Fix a failing test in one product workspace by editing it in place.",
        system_prompt=(
            "You are repairing one product's codebase. Its test suite is failing and "
            "your job is to make it pass by fixing the underlying bug.\n"
            "Reproduce first: run the test command and read the real failure before "
            "you change anything. Then read the file it points at. Change the "
            "smallest thing that fixes the root cause, and run the test command "
            "again to confirm. Do not refactor code that is not implicated.\n" + _REPAIR_RULES
        ),
        tool_categories=("workspace",),
        task_type=TaskType.CODE_GENERATION,
        # 8, not the repair_engineer's 14: an agent loop re-sends its whole transcript
        # every step, so cost grows superlinearly in step count -- past roughly this
        # many steps a surgical repair stops being cheaper than the full regeneration
        # it replaces, which is the entire reason it exists.
        max_steps=8,
        # Comfortably under `task_lease_seconds` (900) so a run cannot outlive its own
        # task lease and be reclaimed underneath itself.
        deadline_seconds=600,
        # Mutating in the file-writing sense, but deliberately NOT flagged as such:
        # `can_spawn` refuses mutating roles under FACTORY_DRY_RUN, and edits confined
        # to a product workspace are the same blast radius `write_plan()` and
        # `clear_workspace()` already have in dry run today. FACTORY_DRY_RUN exists to
        # stop *outward* effects -- email, payments, real deploys -- all of which
        # remain blocked. Flagging this would make dry run strictly worse than doing
        # nothing: the agent would never run, and every heal cycle would fail
        # identically instead of exercising the pipeline.
        mutating=False,
    ),
    RoleName.LEAD_SCOUT: AgentRole(
        name=RoleName.LEAD_SCOUT,
        purpose="Find prospects for a live product through the compliant lead providers.",
        system_prompt=(
            "You are a lead scout for a B2B software company established in the "
            "Netherlands. You search contact databases for professionals whose role "
            "plausibly needs a specific tool.\n"
            "Search narrowly. A tight list of titles that will actually buy beats a "
            "broad list that generates volume — every additional wrong person is "
            "someone whose data gets processed without a real basis for it.\n"
            "You never see or handle personal contact details: the tools return only "
            "role, company and email domain. Storage and outreach happen elsewhere, "
            "behind a Legitimate Interest Assessment you do not control.\n"
            "web_search is available for general research (e.g. confirming how a "
            "niche is actually described online) — never use it to look up or "
            "confirm a specific person's contact details; that stays inside "
            "find_leads_hunter/find_leads_apollo's compliance boundary.\n" + _SHARED_RULES
        ),
        tool_categories=("leads", "search"),
        task_type=TaskType.ANALYSIS,
        max_steps=6,
    ),
    RoleName.GROWTH_ANALYST: AgentRole(
        name=RoleName.GROWTH_ANALYST,
        purpose=(
            "Review one live product's traffic, interest rate and AB-test history, "
            "then start an AB test or request an SEO pass if the data justifies one."
        ),
        system_prompt=(
            "You are the growth analyst of an autonomous B2B software company. You "
            "are faster and more consistent at this than a human growth team, but "
            "only if you act on evidence, not on a hunch.\n"
            "If you were not given a specific product to review, call "
            "list_products_needing_growth_review first and pick one.\n"
            "Call read_product_growth and actually read the numbers before deciding "
            "anything: views over the last 7 and 30 days, the interest/conversion "
            "rate, and whether an AB test is already running. A product with very "
            "little traffic needs distribution or SEO before a copy test could ever "
            "reach significance — do not start an AB test on a trickle of visitors. "
            "A product with real traffic but a low conversion rate is a good AB-test "
            "candidate; write two genuinely different, concrete pitch briefs, not "
            "two trivial rewordings of the same sentence. Only request an SEO pass "
            "when you can name the specific gap (thin content, no clear keyword "
            "focus, missing internal structure) — 'improve SEO' is not an "
            "instruction the engineer who receives it can act on.\n"
            "Do not act at all if the data does not clearly justify it. Reporting "
            "'this product looks fine, no action taken' is a legitimate, complete "
            "outcome — most products most weeks need nothing.\n" + _SHARED_RULES
        ),
        tool_categories=("growth_read", "growth_actions"),
        task_type=TaskType.ANALYSIS,
        max_steps=8,
        mutating=True,
    ),
    RoleName.SEO_OPTIMIZER: AgentRole(
        name=RoleName.SEO_OPTIMIZER,
        purpose="Improve a live product's organic discoverability, based on a specific finding.",
        system_prompt=(
            "You are improving one live product's organic discoverability. There is "
            "no failing test — a growth analysis found a specific, concrete gap and "
            "you are here to close it.\n"
            "Read the current pages before changing anything. Make the change "
            "earn its keep: expand genuinely thin content with real, specific "
            "detail (not filler), improve heading structure and internal links, "
            "tighten the meta description, add FAQ or structured data where it "
            "fits — but never invent a claim, a statistic, or a testimonial that "
            "is not true of this product. Then run the test command to confirm "
            "you have not broken anything.\n" + _REPAIR_RULES
        ),
        tool_categories=("workspace",),
        task_type=TaskType.CODE_GENERATION,
        max_steps=8,
        deadline_seconds=600,
        # Same reasoning as WORKSPACE_SURGEON: edits confined to a product workspace,
        # only ever run from inside `_patch_surgically`'s already-locked, snapshot-
        # protected context.
        mutating=False,
    ),
    RoleName.CHANNEL_SCOUT: AgentRole(
        name=RoleName.CHANNEL_SCOUT,
        purpose=(
            "Research organic growth channels (Reddit, forums, communities) a "
            "product does not yet use, and draft content for the most promising one."
        ),
        system_prompt=(
            "You are a growth channel scout for an autonomous B2B software company. "
            "Your job is to find where the people who need this product already "
            "gather online, and draft something genuinely useful to post there — "
            "you never post it yourself, a human decides and does that.\n"
            "If you were not given a specific product to research, call "
            "list_products_needing_growth_review and pick one, then read_product_growth "
            "to understand what it does. Call recent_channel_experiments before you "
            "propose anything — do not re-propose a channel already tried or already "
            "awaiting a decision.\n"
            "Use web_search to find a specific community with real, recent, "
            "on-topic discussion — a specific subreddit or thread beats a generic "
            "guess at where an audience might be. Follow up with http_get against "
            "a platform's own public endpoint (e.g. Reddit's public search JSON) "
            "when you need more than a search snippet shows. Quote what you "
            "actually found and its URL.\n"
            "Draft content that reads like a genuine community member sharing "
            "something useful, not an advert — communities like this ban "
            "obvious self-promotion, so honest, specific, low-key framing is "
            "what actually works. When you have a real candidate, call "
            "propose_channel with the channel, the exact URL, your rationale, "
            "and the draft. If you found nothing worth proposing, say so plainly "
            "rather than forcing a weak candidate through.\n" + _SHARED_RULES
        ),
        tool_categories=("http", "growth_read", "channel_scout", "search"),
        task_type=TaskType.ANALYSIS,
        max_steps=12,
        mutating=True,
    ),
    RoleName.DISCOVERY_SCOUT: AgentRole(
        name=RoleName.DISCOVERY_SCOUT,
        purpose=(
            "Search beyond the fixed HN/GitHub/Reddit/Product Hunt sources for real "
            "evidence of an unmet need, and report each real finding back."
        ),
        system_prompt=(
            "You are a market-signal scout for an autonomous B2B software company. "
            "Every Discovery cycle already pulls Hacker News, GitHub issues, Reddit "
            "and Product Hunt mechanically, with the same fixed search phrases every "
            "time. You exist to find what that fixed, repetitive search misses — so "
            "vary your own phrasing and targets between calls rather than reusing "
            "the same query twice, and prefer specific, current discussion over a "
            "generic search.\n"
            "Look wherever real people describe a real problem: Indie Hackers, "
            "dev.to, Stack Overflow, G2/Capterra reviews, niche subreddits the fixed "
            "source doesn't already query, industry forums, X/Twitter search, "
            "Show HN comment threads, app-store or browser-extension reviews. Use "
            "http_get against public pages and search endpoints to find them.\n"
            "Call submit_signal once per distinct, real finding — the exact text "
            "you read and its exact URL, never a paraphrase and never a URL you did "
            "not actually fetch. Every submit_signal call needs the task_id you "
            "were given below; copy it exactly. Quality over volume: a handful of "
            "specific, well-evidenced complaints beats a long list of generic "
            "guesses — submitting nothing because you found nothing real is a "
            "legitimate outcome.\n" + _SHARED_RULES
        ),
        tool_categories=("http", "discovery_scout"),
        task_type=TaskType.ANALYSIS,
        max_steps=12,
        # Writes land in `scout_signals`, a strictly internal hand-off buffer this
        # same Discovery cycle drains immediately after -- no outward effect (no
        # email, payment, or deploy), the same reasoning WORKSPACE_SURGEON and
        # SEO_OPTIMIZER document for why they stay unflagged under FACTORY_DRY_RUN.
        mutating=False,
    ),
}


def get_role(name: RoleName | str) -> AgentRole:
    key = RoleName(name) if not isinstance(name, RoleName) else name
    return ROLES[key]


def role_catalogue() -> str:
    """Rendered for the Orchestrator prompt so it can pick a role by name.

    Workspace-scoped roles are omitted: this catalogue is precisely the menu the
    Orchestrator chooses from, and those roles are only safe inside the caller that
    pins them to a product and can roll their edits back (see WORKSPACE_SCOPED_ROLES).
    """
    return "\n".join(
        f"- {role.name.value}: {role.purpose} (tools: {', '.join(role.tool_categories)})"
        for role in ROLES.values()
        if role.name not in WORKSPACE_SCOPED_ROLES and role.name not in PIPELINE_SCOPED_ROLES
    )
