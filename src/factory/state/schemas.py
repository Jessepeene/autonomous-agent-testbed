"""Agent contracts.

Every model call in the factory returns one of these. They are the reason routing is
deterministic: the Supervisor never parses prose, it reads a validated field and looks
the destination up in a dict.

``extra="forbid"`` renders as ``additionalProperties: false`` in the emitted JSON
Schema, which both providers enforce server-side.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from factory.settings import EngineName


class Contract(BaseModel):
    """Base for anything an LLM is asked to produce.

    A note on `max_length`, learned the same way three times: do not put one on a
    prose field a model writes. `ReviewVerdict.summary` at 200 discarded a real
    review of a real defect; `details` at 2000 discarded the retry; `evidence` at 600
    discarded a gap report. Every limit chosen here has been wrong, which is evidence
    against the practice rather than against the numbers.

    They also bound nothing. The database columns behind these are `Text`, every
    consumer truncates at the point of use, and the genuine ceiling on a response is
    `max_tokens` -- enforced by the API rather than by hope. A `max_length` is a
    second, redundant bound whose only distinct effect is to convert a verbose but
    correct answer into total loss.

    `min_length` is the opposite and stays: an empty summary really is useless, and
    that is a defect worth failing on. Caps on *list* lengths stay too -- a report
    with five hundred findings is broken in a way a long sentence is not.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- enums


class NextAction(StrEnum):
    ROUTE = "ROUTE"
    RETRY = "RETRY"
    HOLD_DISCOVERY = "HOLD_DISCOVERY"
    ESCALATE = "ESCALATE"
    IDLE_BACKLOG = "IDLE_BACKLOG"
    #: Create an ephemeral agent for work no fixed engine covers. The role is chosen
    #: from a fixed catalogue — see `factory.agents.roles`.
    SPAWN_AGENT = "SPAWN_AGENT"
    #: The daily LLM spend circuit breaker has tripped. Deterministic, never chosen by
    #: the model — see `factory.supervisor.orchestrator.decide`.
    BUDGET_PAUSED = "BUDGET_PAUSED"


class TaskKind(StrEnum):
    """A unit of durable work. Distinct from `settings.TaskType`, which is a model role."""

    DISCOVER_OPPORTUNITIES = "discover_opportunities"
    ASSESS_COMPLIANCE = "assess_compliance"
    BUILD_MVP = "build_mvp"
    PATCH_PRODUCT = "patch_product"
    REVIEW_BUILD = "review_build"
    DEPLOY_PRODUCT = "deploy_product"
    HARVEST_LEADS = "harvest_leads"
    SEND_OUTREACH = "send_outreach"
    RECONCILE_REVENUE = "reconcile_revenue"
    MAINTENANCE = "maintenance"
    #: Builds and deploys a cheap pitch-only page for an idea, before any real MVP
    #: work happens -- see the validation gate in development/graph.py.
    BUILD_LANDING_PAGE = "build_landing_page"
    #: Tallies a validation run's interest signals against a threshold and either
    #: enqueues BUILD_MVP or retires the product.
    EVALUATE_VALIDATION = "evaluate_validation"
    #: Tallies a running AB test's per-variant outcomes and concludes it once
    #: either a winner is statistically significant or its time budget expires.
    #: A pure DB decision, no code generation -- see EVALUATE_VALIDATION, same shape.
    EVALUATE_AB_TEST = "evaluate_ab_test"
    #: Spawns GROWTH_ANALYST for one product -- reads its analytics/AB-test state
    #: and decides whether to start an AB test or request an SEO pass. See
    #: agents/roles.py and development/graph.py::_growth_review.
    GROWTH_REVIEW = "growth_review"
    #: Spawns CHANNEL_SCOUT for one product -- researches candidate organic growth
    #: channels (Reddit, forums) it does not yet use. See marketing/graph.py.
    CHANNEL_SCOUT_REVIEW = "channel_scout_review"
    #: Tallies referrer hits for an approved channel_experiment and concludes it.
    #: A pure DB decision, no LLM call -- see EVALUATE_AB_TEST, same shape.
    MEASURE_CHANNEL_EXPERIMENT = "measure_channel_experiment"
    #: Attaches a human-supplied hostname to a product's Vercel project and relays
    #: the required DNS records back. See development/domain.py.
    ATTACH_DOMAIN = "attach_domain"
    #: Polls Vercel for DNS verification status on an attached domain. A pure API
    #: check, no LLM call -- see EVALUATE_AB_TEST, same shape.
    VERIFY_DOMAIN = "verify_domain"
    #: Generates and caches a `DesignBrief` for one already-live product that
    #: doesn't have one yet. Enqueued by the dashboard (`api/dashboard.py`),
    #: not the tick loop itself -- the API only ever writes intent, the loop
    #: does the real work. See development/design_brief.py.
    GENERATE_DESIGN_BRIEF = "generate_design_brief"
    #: Spawns AUDITOR against one LIVE product with a real browser -- it opens the
    #: page, uses it the way a visitor would (submits the form, follows the call to
    #: action), and commissions a patch for anything genuinely wrong. Distinct from
    #: the deterministic `audit-live-products` maintenance slug, which only asks
    #: "does the URL answer" and reads markup: a form that silently fails on submit
    #: is invisible to every check that never presses the button. See
    #: development/graph.py::_review_live_product.
    REVIEW_LIVE_PRODUCT = "review_live_product"


class TaskStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ESCALATED = "escalated"


class ProductStatus(StrEnum):
    #: A landing page is live and gathering real interest signals; no MVP code
    #: exists yet. Exits to either PLANNED (validated, proceed to build_mvp) or
    #: RETIRED (not enough interest).
    VALIDATING = "validating"
    PLANNED = "planned"
    BUILDING = "building"
    REVIEWING = "reviewing"
    PATCHING = "patching"
    LIVE = "live"
    RETIRED = "retired"


class InterruptReason(StrEnum):
    """The only situations a human may be asked about. Enforced, not advisory: the
    enum makes anything else impossible to express, so widening this set is always a
    deliberate, reviewed code change rather than a call site improvising a new reason.

    Started as exactly two (missing credential, legal uncertainty). Three more were
    added deliberately: BUDGET_EXCEEDED (the financial circuit breaker),
    CAMPAIGN_APPROVAL (the mandatory pre-send authorisation gate), and
    DELIVERABILITY_FROZEN (the spam-complaint circuit breaker) are none of the
    original two — two operational hard limits and a standing policy sign-off — so
    folding them into LEGAL_UNCERTAINTY would have been a dishonest fit.
    """

    MISSING_CREDENTIAL = "missing_credential"
    LEGAL_UNCERTAINTY = "legal_uncertainty"
    BUDGET_EXCEEDED = "budget_exceeded"
    CAMPAIGN_APPROVAL = "campaign_approval"
    DELIVERABILITY_FROZEN = "deliverability_frozen"
    #: Sign-off on a CHANNEL_SCOUT-proposed organic channel (a specific subreddit,
    #: forum, community) before any real action is taken there. Distinct from
    #: CAMPAIGN_APPROVAL: platform-posting risk (bans, reputational damage) is a
    #: different kind of judgement than GDPR-gated cold email, and this factory
    #: never posts automatically even once approved -- see engines/marketing/channel.py.
    CHANNEL_APPROVAL = "channel_approval"
    #: A product just went live and has no custom domain yet -- asked once per
    #: product, never blocking (the Vercel subdomain already works). See
    #: development/domain.py.
    DOMAIN_REQUIRED = "domain_required"
    #: A structural assumption an Orchestrator-facing snapshot metric silently
    #: relies on has stopped holding -- e.g. an idea marked 'approved' with no
    #: product at all, which `build_snapshot`'s `approved_ideas_waiting` query
    #: would just quietly undercount rather than ever flag. Not urgent (nothing
    #: crashes, no money moves), but worth a human's attention rather than
    #: discovery weeks later by someone noticing a number looks off. See
    #: supervisor/audit.py.
    DATA_INTEGRITY = "data_integrity"
    #: A product failed `MAX_HEAL_CYCLES` repair attempts and needs a human's
    #: guidance or a decision to retire it. Previously raised under
    #: LEGAL_UNCERTAINTY, which is what a human actually saw in the Telegram
    #: `[LEGAL_UNCERTAINTY]` prefix for what is a technical repair failure, not a
    #: legal question -- confusing enough on its own to be worth a real reason.
    #: See development/graph.py::_heal.
    HEAL_EXHAUSTED = "heal_exhausted"
    #: A validating product's lead sourcing found nothing, and one automatic
    #: retry (`scheduler.py`'s stalled-validation sweep) *also* found nothing --
    #: e.g. a lead-provider credential that is present but being rejected (a
    #: 403, not MISSING_CREDENTIAL's "absent entirely"), or a niche too narrow
    #: for either provider. Before this, a validation stuck this way had no
    #: automatic path forward at all: HARVEST_LEADS is a one-shot enqueue, so
    #: nothing ever asked again and nothing ever told a human -- observed live.
    #: See marketing/graph.py and supervisor/scheduler.py.
    LEAD_SOURCING_STALLED = "lead_sourcing_stalled"
    #: A model provider itself is rejecting requests (quota/credit exhaustion, a
    #: permanent 4xx, a sustained 5xx) -- a billing/infrastructure problem, never
    #: a legal or content question. Previously fell through to the generic
    #: exhausted-retries handler in `supervisor/graph.py::_run_engine`, which
    #: raised it as LEGAL_UNCERTAINTY with a subject that embedded the failing
    #: task's id -- meaning every fresh task hitting the *same* persistent
    #: provider outage got its own "new" subject, defeating `interrupts.request`'s
    #: duplicate-open-subject collapse entirely. Observed live: 40 separate
    #: LEGAL_UNCERTAINTY interrupts over 14 days, all the same Anthropic credit
    #: exhaustion, several with `plain_language_error`'s own explanation call
    #: failing too (every provider was down at once) and dumping a raw,
    #: unedited exception at the human instead. See PROVIDER_OUTAGE's own
    #: handler for why its subject is built from the provider/status instead.
    PROVIDER_OUTAGE = "provider_outage"
    #: A task failed `max_task_attempts` times for a reason with no more specific
    #: handler -- the generic last-resort escalation in
    #: `supervisor/graph.py::_run_engine`. Previously raised as LEGAL_UNCERTAINTY,
    #: which is what a human actually saw in the Telegram `[LEGAL_UNCERTAINTY]`
    #: prefix for what is nearly always a technical failure. Same complaint, and
    #: same fix, as HEAL_EXHAUSTED and PROVIDER_OUTAGE above: the two remaining
    #: genuine uses of LEGAL_UNCERTAINTY (a flagged idea in `compliance/graph.py`,
    #: an uncertain LIA in `compliance/lia.py`) are real legal questions, and
    #: diluting that label with technical failures is what made a human stop
    #: trusting it.
    TASK_EXHAUSTED = "task_exhausted"


class ComplianceStatus(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FLAG_FOR_REVIEW = "FLAG_FOR_REVIEW"


# ------------------------------------------------------------------- orchestrator


class OrchestratorDecision(Contract):
    """Supervisor routing. `target_engine` is looked up in a dict, never interpreted."""

    next_action: NextAction
    target_engine: EngineName
    tool_call: str | None = Field(
        default=None,
        description="Name of a registered MCP tool to invoke, or null for none.",
    )
    agent_role: str | None = Field(
        default=None,
        description=("With SPAWN_AGENT, the role to spawn from the catalogue. Null otherwise."),
    )
    agent_objective: str | None = Field(
        default=None,
        description="With SPAWN_AGENT, the single concrete objective for that agent.",
    )
    rationale: str = Field(min_length=1, max_length=600)


# ----------------------------------------------------------------------- discovery


class GeneratedIdea(Contract):
    problem_statement: str = Field(min_length=20, max_length=800)
    target_niche: str = Field(min_length=3, max_length=200)
    source_url: HttpUrl


class IdeaBatch(Contract):
    """Object-wrapped because a JSON Schema root must be an object, not an array."""

    ideas: list[GeneratedIdea] = Field(default_factory=list, max_length=25)


class PlatformDependency(StrEnum):
    """How much the idea's core function depends on one third-party platform --
    informational only in Phase 4, never gated or weighted. Surfaced so the
    distribution can be observed for a cycle before anyone hard-codes a rule
    about it; `unknown` represents "the evidence genuinely doesn't say", never
    a forced guess."""

    UNKNOWN = "unknown"
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class ReplacementMode(StrEnum):
    """Whether the idea displaces an existing tool/process, sits alongside one,
    or only observes -- informational only, same reasoning as
    `PlatformDependency` above."""

    UNKNOWN = "unknown"
    REPLACES = "replaces"
    AUGMENTS = "augments"
    OBSERVES = "observes"


class ScoredIdea(Contract):
    #: The historical fixed weights, kept as the default for `.score` so every
    #: existing caller that never heard of the learning loop keeps behaving exactly
    #: as before. `discovery/graph.py` is the only caller that fetches tuned weights
    #: and calls `weighted_score()` directly instead. Sums to 1.0 -- `SCORE_THRESHOLD`
    #: (`discovery/score.py`) is a bare constant calibrated against that, not derived
    #: from the weights, so a vector that doesn't sum to 1.0 silently changes what the
    #: threshold means.
    _DEFAULT_WEIGHTS: ClassVar[dict[str, float]] = {
        "pain_severity": 0.25,
        "willingness_to_pay": 0.25,
        "adoption_ease": 0.20,
        "build_feasibility": 0.12,
        "defensibility": 0.18,
    }

    #: Single source of truth for which fields are weighted-scored, in what order
    #: they're summed. `weighted_score` loops over this instead of a hardcoded sum
    #: so a future dimension change touches one place, not three (the field list,
    #: the prompt, and the formula used to silently drift apart before this).
    SCORE_DIMENSIONS: ClassVar[tuple[str, ...]] = (
        "pain_severity",
        "willingness_to_pay",
        "build_feasibility",
        "adoption_ease",
        "defensibility",
    )

    problem_statement: str = Field(min_length=20, max_length=800)
    target_niche: str = Field(min_length=3, max_length=200)
    source_url: HttpUrl
    monetization_hypothesis: str = Field(min_length=20, max_length=800)
    pain_severity: Annotated[float, Field(ge=0.0, le=1.0)]
    willingness_to_pay: Annotated[float, Field(ge=0.0, le=1.0)]
    build_feasibility: Annotated[float, Field(ge=0.0, le=1.0)]
    #: How easily a buyer in this niche can actually adopt self-serve software,
    #: independent of pain or price -- 1.0 means they already discover and buy
    #: similar tools online unassisted; 0.0 means the industry's procurement
    #: culture, compliance sign-off, or low tech adoption makes a self-serve sale
    #: implausible regardless of how strong the other three scores are (construction,
    #: healthcare, and government are common low-scoring examples). Required, not
    #: defaulted, like the other three original score dimensions: it needs the
    #: discriminator to actually reason about it for every idea, not silently skip it.
    adoption_ease: Annotated[float, Field(ge=0.0, le=1.0)]
    #: Placed immediately before `defensibility` so the model commits its reasoning
    #: to the context window (proprietary data, workflow lock-in, switching cost)
    #: before the constrained numeric token -- auto-regressive generation is
    #: strictly left-to-right, so a bare float with no preceding justification is
    #: more likely to collapse to a safe average under constrained decoding.
    #: Defaulted, like `defensibility` itself, for the same reason: a stale served
    #: prompt that doesn't ask for it must not turn into a hard schema failure.
    defensibility_reasoning: str = Field(default="", max_length=800)
    #: The 5th weighted dimension. Defaulted (unlike `adoption_ease` above) rather
    #: than required: `learning/prompts.py::get_prompt` serves whatever's in the DB
    #: with no re-sync path, so a stale served prompt that never asks for this field
    #: would otherwise throw `SchemaViolation` on every discriminate() call --
    #: Discovery dies silently. Defaulted, a stale prompt just degrades every idea's
    #: defensibility to 0.0, which fails safe (almost nothing clears the threshold)
    #: instead of failing hard.
    defensibility: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    #: Informational, never gated or weighted -- see `PlatformDependency`/
    #: `ReplacementMode`'s own docstrings for why.
    platform_dependency: PlatformDependency = PlatformDependency.UNKNOWN
    replacement_mode: ReplacementMode = ReplacementMode.UNKNOWN
    rejection_reason: str | None = None
    #: Self-serve / PLG constraint inputs (see `discovery.score.passes_plg_constraint`).
    #: Defaulted rather than required so every existing caller that predates this
    #: constraint — including callers that construct a `ScoredIdea` directly in a
    #: test — keeps validating without having to know about it.
    requires_human_sales: bool = False
    requires_human_onboarding: bool = False
    self_serve_price_usd: Annotated[float, Field(ge=0.0, le=100_000.0)] = 0.0
    #: Holistic veto, independent of the five scored dimensions above: an idea can
    #: score adequately on every individual axis while being, taken as a whole,
    #: obviously not a real business (an experienced operator would object on
    #: sight even though nothing above technically scored low). See
    #: `discovery.score.passes_gut_check`. Defaulted to True (no veto), same
    #: precedent and same reasoning as `defensibility` above:
    #: `learning/prompts.py::get_prompt` can serve a stale DISCRIMINATOR_SYSTEM
    #: that predates this field, and a missing gut check must never silently
    #: veto every idea -- it must do nothing until the served prompt actually
    #: asks for it.
    gut_check_passed: bool = True
    gut_check_reasoning: str = Field(default="", max_length=500)

    @classmethod
    def default_weights(cls) -> dict[str, float]:
        return dict(cls._DEFAULT_WEIGHTS)

    @property
    def score(self) -> float:
        return self.weighted_score(self._DEFAULT_WEIGHTS)

    def weighted_score(self, weights: dict[str, float]) -> float:
        return round(
            sum(float(getattr(self, dim)) * weights.get(dim, 0.0) for dim in self.SCORE_DIMENSIONS),
            4,
        )


class DiscriminatedIdeas(Contract):
    ideas: list[ScoredIdea] = Field(default_factory=list, max_length=25)


class ValidatedIdea(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    problem_statement: str
    target_niche: str
    source_url: str
    monetization_hypothesis: str
    score: float
    evidence_count: int = 0
    #: The discriminator's own self-serve price target (`ScoredIdea.self_serve_price_usd`),
    #: carried through so codegen can actually charge it instead of the pitch and the
    #: real Checkout amount silently disagreeing. Defaulted, matching every other
    #: field the discriminator produces -- an idea scored before this field existed
    #: has no opinion, not a fabricated one.
    self_serve_price_usd: float = 0.0
    created_at: datetime | None = None


# ---------------------------------------------------------------------- compliance


class LIAVerdict(Contract):
    """GDPR Art. 6(1)(f) legitimate interest assessment.

    All three limbs must be argued explicitly. A model that cannot articulate the
    role-to-utility link has not established a lawful basis for contact.
    """

    role_utility_link: str = Field(min_length=20, max_length=1000)
    necessity: str = Field(min_length=20, max_length=1000)
    balancing_test: str = Field(min_length=20, max_length=1000)
    passed: bool
    #: Holistic sign-off, independent of the three limbs above and of `passed`
    #: itself: a justification can be well-grounded in the right facts (the
    #: three limbs correctly reference the real job_title/company) while still
    #: not being a decision a careful compliance officer would actually stand
    #: behind. Tier 0 of the system-wide verification plan -- the same
    #: distinction `bug_council.py`'s QUALITY reviewer draws between "grounded
    #: in the right file" and "actually a good idea," applied here to a legal
    #: judgement instead of a code review. Required, not defaulted, unlike
    #: `ScoredIdea.gut_check_passed`: this prompt is never tunable (see
    #: `learning/allowlist.py::MUTABLE_KEYS`, which excludes every LIA prompt
    #: by omission), so there is no stale-served-prompt risk to default around.
    would_a_careful_reviewer_approve: bool
    reviewer_concern: str = Field(default="", max_length=500)


class ComplianceAssessment(Contract):
    status: ComplianceStatus
    justification: str = Field(min_length=20, max_length=2000)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    lia_verdict: LIAVerdict | None = None

    @property
    def approved(self) -> bool:
        return self.status is ComplianceStatus.APPROVED


class LeadAssessment(Contract):
    """One prospect's LIA within a batched call.

    ``ref`` echoes the caller-minted, per-batch token (``L1``, ``L2``, ...) rather
    than a list position or a database id. A position is silently wrong the moment
    the model drops, reorders, or duplicates an entry; an echoed token makes all
    three mechanically detectable. A real id would give a third-party model provider
    a stable handle to correlate the same prospect across separate batches, which the
    LIA prompt deliberately never gives it in the first place.
    """

    ref: str = Field(min_length=1, max_length=8)
    status: ComplianceStatus
    justification: str = Field(min_length=20, max_length=2000)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    lia_verdict: LIAVerdict | None = None

    def to_assessment(self) -> ComplianceAssessment:
        return ComplianceAssessment(
            status=self.status,
            justification=self.justification,
            confidence=self.confidence,
            lia_verdict=self.lia_verdict,
        )


class LeadAssessmentBatch(Contract):
    """Object-wrapped, matching `IdeaBatch`/`DiscriminatedIdeas`."""

    assessments: list[LeadAssessment] = Field(default_factory=list, max_length=10)


class EntityType(StrEnum):
    """What kind of data subject sits behind a company domain.

    The distinction that matters under GDPR: a CORPORATE entity's staff are
    contacted in their professional capacity; a SOLE_PROPRIETOR_OR_INDIVIDUAL is, or
    may be indistinguishable from, a protected natural person acting outside any
    organisation. UNKNOWN is the fail-closed default — the agent found no clear
    evidence either way, which is treated identically to a negative result.
    """

    CORPORATE = "corporate"
    SOLE_PROPRIETOR_OR_INDIVIDUAL = "sole_proprietor_or_individual"
    UNKNOWN = "unknown"


class EntityVerdict(Contract):
    """Whether a company domain belongs to a registered corporate entity.

    Deliberately a judgement about the *organisation*, never the person: the prompt
    that produces this is given a company name and email domain only, the same
    restraint already applied to the LIA.
    """

    entity_type: EntityType
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    evidence: str = Field(min_length=10, max_length=1500)


# --------------------------------------------------------------------------- leads

# GDPR data minimisation: this tuple is the whole permitted PII surface. The `leads`
# table has no other personal columns, so widening it requires a migration and a
# conscious decision — not an accidental dict spread.
ALLOWED_LEAD_FIELDS: frozenset[str] = frozenset(
    {"first_name", "last_name", "job_title", "company", "work_email"}
)


class LeadRecord(BaseModel):
    """A contactable prospect. Exactly five PII fields, plus mandatory provenance."""

    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    job_title: str = Field(min_length=1, max_length=200)
    company: str = Field(min_length=1, max_length=200)
    work_email: str = Field(min_length=5, max_length=320)

    # Proof-of-origin. Non-optional here and NOT NULL in the database.
    source_url: str = Field(min_length=8, max_length=2000)
    scraped_at: datetime

    @field_validator("work_email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        email = value.strip().lower()
        if email.count("@") != 1 or email.startswith("@") or email.endswith("@"):
            raise ValueError("work_email must be a single well-formed address")
        local, _, domain = email.partition("@")
        if not local or "." not in domain:
            raise ValueError("work_email must contain a domain with a dot")
        return email

    @field_validator("source_url")
    @classmethod
    def _require_url(cls, value: str) -> str:
        url = value.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("source_url must be an absolute http(s) URL for provenance")
        return url

    @property
    def email_domain(self) -> str:
        return self.work_email.partition("@")[2]


# --------------------------------------------------------------------- development


class ReviewCategory(StrEnum):
    SECURITY = "security"
    PERFORMANCE = "performance"
    DEPENDENCIES = "dependencies"
    PRIVILEGE = "privilege"
    SECRETS = "secrets"
    #: The one reviewer with a holistic "would I show this to a customer"
    #: mandate rather than a narrow technical one -- see bug_council.py's
    #: REVIEWER_PROMPTS entry for why this category exists at all (a
    #: hardcoded stale copyright year trips none of the other five).
    QUALITY = "quality"


class ReviewFinding(Contract):
    category: ReviewCategory
    severity: Literal["low", "medium", "high", "critical"]
    file: str = Field(max_length=500)
    line: int | None = Field(default=None, ge=1)
    summary: str = Field(min_length=10, max_length=500)
    failure_scenario: str = Field(min_length=10, max_length=1500)
    remediation: str = Field(min_length=10, max_length=1500)


class ReviewVerdict(Contract):
    """One Bug Council reviewer's output."""

    category: ReviewCategory
    approved: bool
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=40)


class HealingInstructions(Contract):
    """What the dev agent must change before the build is re-reviewed."""

    summary: str = Field(min_length=10, max_length=1000)
    ordered_steps: list[str] = Field(min_length=1, max_length=25)
    blocking_findings: list[ReviewFinding] = Field(default_factory=list, max_length=40)


#: Severities that stop a deploy outright, whatever the reviewer's category.
BLOCKING_SEVERITIES = frozenset({"critical", "high"})

#: Categories where `medium` blocks too: the three that can leak data, credentials
#: or access. This system handles real payments and real personal data, every other
#: guard in it fails closed, and "ship a known hole and track it" is a human's call
#: to make, not an autonomous factory's.
#:
#: `performance`, `dependencies` and `quality` are not here on purpose. Those are
#: the ones that trapped a working checkout fix for a day -- real findings, worth
#: recording, never worth holding a broken payment path hostage to.
STRICT_CATEGORIES = frozenset({"security", "secrets", "privilege"})


def _blocks_deploy(finding: ReviewFinding) -> bool:
    severity = str(finding.severity).lower()
    if severity in BLOCKING_SEVERITIES:
        return True
    return severity == "medium" and str(finding.category).lower() in STRICT_CATEGORIES


class CouncilResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    verdicts: list[ReviewVerdict]
    healing: HealingInstructions | None = None

    @property
    def blocking(self) -> list[ReviewFinding]:
        """Every finding from every reviewer that withheld approval.

        The full set, used for *recording*. `deploy_blocking` is the subset that
        actually holds a ship.
        """
        return [f for v in self.verdicts if not v.approved for f in v.findings]

    @property
    def deploy_blocking(self) -> list[ReviewFinding]:
        """The findings serious enough to stop a deploy.

        Every finding in `blocking` used to hold the product, and the reviewer
        prompt sets `approved=false` at "medium severity or above" -- so one medium
        nit in a file was enough to block everything else in it indefinitely.

        Observed live, and the reason this property exists: a correct fix for a
        broken `POST /api/billing/checkout` was written on 12 September and never
        shipped. It sat behind 8 unrelated performance/quality/security findings in
        the same file while the live product kept returning 422 to every paying
        customer for another day, with three heal attempts exhausting themselves
        trying to clear the backlog before they could ship the one fix that mattered.

        A human team ships the hotfix and files the rest. That is only safe if the
        rest genuinely *are* filed, which is what `issues` now guarantees -- nothing
        here is dropped, it is recorded and tracked (`_record_council_findings`).

        Security is deliberately stricter: a medium security finding still blocks.
        Everywhere else in this codebase the security posture fails closed, and
        "we shipped a known hole and tracked it" is not a trade this system gets to
        make on its own.
        """
        return [f for f in self.blocking if _blocks_deploy(f)]


class GeneratedFile(Contract):
    path: str = Field(min_length=1, max_length=400)
    contents: str


class ScaffoldPlan(Contract):
    product_name: str = Field(min_length=2, max_length=120)
    summary: str = Field(min_length=20, max_length=2000)
    files: list[GeneratedFile] = Field(min_length=1, max_length=80)
    test_command: str = Field(min_length=1, max_length=400)


class AcceptanceCriterion(Contract):
    """One checkable statement about finished work.

    `criterion` has to be verifiable by looking at the product -- "a visitor can
    submit an email and sees a confirmation message" rather than "good UX". A
    criterion nobody can check is a wish, and it makes every downstream judgement
    that references it unfalsifiable too.
    """

    criterion: str = Field(min_length=10)
    #: Why this matters, so a release decision can weigh it rather than only tick
    #: it. A reviewer that knows *why* a criterion exists can tell a near-miss from
    #: a real failure; one that only has the rule cannot.
    why: str = Field(min_length=10)


class WorkRequirements(Contract):
    """What a piece of work must achieve, before anyone starts it.

    The three lists are the point. A single flat list of requirements is just a
    longer blocking gate, and the old deploy gate already proved where that leads:
    "done" meant "no blocking findings", which is unbounded, because there is always
    another finding.

    - `must` is non-negotiable. Failing one means not done.
    - `should` is valuable and not blocking -- what a release judgement *weighs*
      rather than obeys, and what makes "done, with known issues accepted" sayable.
    - `out_of_scope` stops a reviewer flagging the absence of things nobody intended
      to build, and makes scope creep visible as a change to this record rather than
      an argument nobody wrote down.
    """

    goal: str = Field(min_length=20)
    must: list[AcceptanceCriterion] = Field(min_length=1, max_length=12)
    should: list[AcceptanceCriterion] = Field(default_factory=list, max_length=12)
    out_of_scope: list[str] = Field(default_factory=list, max_length=12)


class ReleaseDecision(Contract):
    """A judgement about whether to put this in front of a real person.

    Replaces `if not deploy_blocking: ship`. The four options exist because a human
    team has four; a system with one has a retry loop, which is what this factory
    had -- Council blocks, heal, regenerate, repeat, until a counter runs out.

    `accepted_issues` is what makes "done" reachable. Shipping with known flaws,
    named and on the record, is a normal engineering decision; the old gate could
    only express "nothing blocking remains", which is unbounded because there is
    always another finding.
    """

    decision: Literal["ship", "fix_first", "rethink", "abandon"]
    rationale: str = Field(min_length=30)
    #: MUST criteria this build does not meet. Non-empty is close to decisive: a
    #: product that does not do what it had to do is not shippable, whatever else is
    #: true of it.
    unmet_must: list[str] = Field(default_factory=list, max_length=12)
    #: Known problems being shipped with, deliberately. Recorded so the acceptance
    #: is a decision someone made rather than something nobody noticed.
    accepted_issues: list[str] = Field(default_factory=list, max_length=20)
    #: For `fix_first`: the fewest changes that would make this shippable. Not every
    #: finding -- a long list here is how a product never ships.
    fix_first: list[str] = Field(default_factory=list, max_length=10)


class RequirementGap(Contract):
    """One acceptance criterion the product does not meet.

    A *gap*, deliberately, not a finding. The difference is what makes "done"
    reachable: gaps are the distance between a product and a stated standard, so
    there are finitely many and fixing one removes it. Findings are whatever a
    reviewer happens to notice, and there is always another one -- which is why a
    gate defined as "no outstanding findings" could never be satisfied, and why 19
    issues became 28 in a single cycle.
    """

    criterion: str = Field(min_length=10)
    tier: Literal["must", "should"]
    #: What in the product shows this is unmet. Required, because a gap asserted
    #: without evidence is an opinion, and the whole point of comparing against a
    #: written standard is that the answer is checkable.
    evidence: str = Field(min_length=10)


class GapReport(Contract):
    """The comparison of one build against its acceptance criteria."""

    gaps: list[RequirementGap] = Field(default_factory=list, max_length=20)
    #: Criteria confirmed met, by the same pass. Present so that "done" is a positive
    #: statement backed by evidence rather than the absence of complaints -- and so a
    #: reviewer that looked at nothing and reported nothing is distinguishable from
    #: one that checked and found the product sound.
    met: list[str] = Field(default_factory=list, max_length=20)
    #: What the reviewer could not determine from what it was given. Stated rather
    #: than silently folded into "met", because a criterion nobody could check is not
    #: a criterion that passed -- the instrumentation principle, applied to the
    #: comparison itself.
    undetermined: list[str] = Field(default_factory=list, max_length=20)
