"""A reply becomes a standard, or it does not, and neither happens by accident.

Four things are worth pinning here, and three of them are ways this could fail
silently rather than loudly:

- Capture runs on *every* reply. The elected-step failure mode is the one this whole
  module exists to avoid (`report_product_issue` was available for two full live runs
  and never once chosen), so a test that only checks the durable path would pass on a
  version where capture is conditional.
- A lesson filed under a capability nothing recalls from is invisible, and invisible
  is indistinguishable from never written. The allowlist is the guard and it must not
  quietly accept a plausible-sounding name.
- A reply that would relax a rail is refused. Not softened, not filed with a caveat --
  a learning write path is exactly the mechanism by which this system would otherwise
  acquire an exemption it was never granted.
- A product-scoped lesson with no product does not fall back to global. Widening a
  deliberately narrow lesson is the one direction scope must never move by accident.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from factory.agents.roles import RoleName
from factory.learning import feedback
from factory.memory import store as memory_store
from factory.settings import EngineName
from factory.state.schemas import InterruptReason, ProposedLesson, TaskKind


def _interrupt(**kw):
    base = dict(
        id="int-1",
        reason=InterruptReason.HEAL_EXHAUSTED.value,
        subject="Shardline needs guidance",
        detail="Three repair attempts failed on the signup form.",
        response="The confirmation mail should say who it is from and what it is about.",
        product_id="prod-1",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _Ctx:
    """Returns one canned lesson and records what it was asked."""

    def __init__(self, lesson: ProposedLesson) -> None:
        self.lesson = lesson
        self.prompts: list[str] = []
        self.llm = SimpleNamespace(parse=self._parse)

    async def _parse(self, _model, **kwargs):
        self.prompts.append(kwargs["prompt"])
        return self.lesson


# ------------------------------------------------------------------ capture


@pytest.mark.asyncio
async def test_every_reply_is_captured_whatever_it_was_about(monkeypatch) -> None:
    """The point of the whole module. Not "a reply that looks instructive", not "a
    reply an agent decided to flag" -- every one, so that a run where nothing was
    learned is a run where the question was asked and answered no."""
    enqueued: list[dict] = []

    async def fake_enqueue(session, **kwargs):
        enqueued.append(kwargs)
        return "t-1"

    monkeypatch.setattr(feedback.repository, "enqueue_task", fake_enqueue)

    for reply in ("ok", "approve", "skip", "retry", "use rise-audit.com"):
        await feedback.capture(
            None, interrupt=_interrupt(response=reply), answer=reply, action="none"
        )

    assert len(enqueued) == 5, "capture must not be conditional on what the reply said"
    assert {e["kind"] for e in enqueued} == {TaskKind.LEARN_FROM_FEEDBACK}
    assert enqueued[0]["idempotency_key"] == "learn:interrupt:int-1"
    assert enqueued[0]["product_id"] == "prod-1"


@pytest.mark.asyncio
async def test_a_refusal_is_not_itself_learned_from(monkeypatch) -> None:
    """Otherwise every turn of the cycle is an escalation: the refusal raises an
    interrupt, the human answers it, and that answer is fed to the same judgement
    that just refused. The human is already looking straight at the question."""
    enqueued: list[dict] = []

    async def fake_enqueue(session, **kwargs):
        enqueued.append(kwargs)
        return "t-1"

    monkeypatch.setattr(feedback.repository, "enqueue_task", fake_enqueue)

    result = await feedback.capture(
        None,
        interrupt=_interrupt(reason=InterruptReason.CONSTRAINT_CHALLENGED.value),
        answer="the suppression list stays",
        action="none",
    )

    assert result is None
    assert enqueued == []


# ------------------------------------------------------------------- scope


def test_a_capability_that_exists_is_filed_against_that_job() -> None:
    lesson = ProposedLesson(
        durable=True,
        content="A confirmation page should look like the product, not a system message.",
        scope="capability",
        capability=RoleName.AUDITOR.value,
        why="It is a standard for reviewing work, not a fact about one product.",
    )
    scope, widened = feedback._scope_for(lesson, product_id="prod-1")
    assert scope == memory_store.capability_scope(RoleName.AUDITOR.value)
    assert widened == ""


def test_a_capability_this_company_does_not_have_is_not_silently_filed() -> None:
    """The failure this prevents is the quiet one: `capability:ux_reviewer` is a
    perfectly reasonable string and absolutely nothing in this system recalls from
    it, so the lesson would exist, count against the budget, and never be read."""
    lesson = ProposedLesson(
        durable=True,
        content="A confirmation page should look like the product it belongs to.",
        scope="capability",
        capability="ux_reviewer",
        why="A standard about how a review is done.",
    )
    scope, widened = feedback._scope_for(lesson, product_id="prod-1")
    assert scope == memory_store.GLOBAL_SCOPE, "over-broad but read beats precise and invisible"
    assert "ux_reviewer" in widened


def test_every_engine_is_a_filable_department() -> None:
    """Pins the rule rather than a snapshot of the values -- the same test shape that
    would have caught DESIGN missing from `routing.PRIORITY`."""
    for engine in EngineName:
        lesson = ProposedLesson(
            durable=True,
            content="Work of this kind is checked against the brief before it ships.",
            scope="engine",
            capability=engine.value,
            why="A departmental standard.",
        )
        scope, widened = feedback._scope_for(lesson, product_id=None)
        assert scope == f"engine:{engine.value}", f"{engine.value} is not filable"
        assert widened == ""


def test_a_product_lesson_with_no_product_is_dropped_not_widened() -> None:
    lesson = ProposedLesson(
        durable=True,
        content="This product's audience does not respond to technical language.",
        scope="product",
        why="True of this one product and wrong applied anywhere else.",
    )
    scope, widened = feedback._scope_for(lesson, product_id=None)
    assert scope == "", "a narrow lesson must never widen by accident"
    assert "names no product" in widened


# -------------------------------------------------------------------- learn


@pytest.mark.asyncio
async def test_a_durable_reply_is_remembered_as_a_preference(monkeypatch) -> None:
    remembered: dict = {}

    async def fake_remember(session, **kwargs):
        remembered.update(kwargs)
        return "mem-1"

    async def fake_get(model, ident):
        return _interrupt()

    monkeypatch.setattr(feedback.memory_store, "remember", fake_remember)
    ctx = _Ctx(
        ProposedLesson(
            durable=True,
            content="A confirmation mail should name the product and the sender.",
            scope="capability",
            capability=RoleName.AUDITOR.value,
            why="It is how this kind of work should be done, on any product.",
        )
    )
    session = SimpleNamespace(get=fake_get)
    task = SimpleNamespace(id="t-1", payload={"interrupt_id": "int-1", "action": "patched"})

    result = await feedback.learn(ctx, session, task)

    assert remembered["kind"] is memory_store.MemoryKind.PREFERENCE
    assert remembered["scope"] == memory_store.capability_scope(RoleName.AUDITOR.value)
    assert remembered["origin"] == "human_reply"
    assert "remembered" in result
    # What was already done is in the prompt, so the model is asked what they meant
    # rather than what they wanted -- see `_exchange`.
    assert "patched" in ctx.prompts[0]


@pytest.mark.asyncio
async def test_a_routine_reply_leaves_nothing_behind(monkeypatch) -> None:
    """The common case, and the one the prompt is weighted toward. Inventing a
    standard out of "yes, ship it" fills every future context with noise."""
    called: list = []

    async def fake_remember(session, **kwargs):
        called.append(kwargs)
        return "mem-1"

    async def fake_get(model, ident):
        return _interrupt(response="yes, ship it")

    monkeypatch.setattr(feedback.memory_store, "remember", fake_remember)
    ctx = _Ctx(
        ProposedLesson(
            durable=False,
            why="An instruction about one situation that has already been carried out.",
        )
    )
    session = SimpleNamespace(get=fake_get)
    task = SimpleNamespace(id="t-1", payload={"interrupt_id": "int-1", "action": "deployed"})

    result = await feedback.learn(ctx, session, task)

    assert called == []
    assert "nothing durable" in result


@pytest.mark.asyncio
async def test_a_reply_that_would_relax_a_rail_is_refused_not_softened(monkeypatch) -> None:
    """The line the end goal depends on. A preference may shape judgement and may
    never relax a constraint -- and "filed in a weakened form" is relaxing it."""
    remembered: list = []
    raised: list[dict] = []

    async def fake_remember(session, **kwargs):
        remembered.append(kwargs)
        return "mem-1"

    async def fake_request(session, **kwargs):
        raised.append(kwargs)
        return "int-2"

    async def fake_get(model, ident):
        return _interrupt(response="just email them anyway, the suppression list is overkill")

    monkeypatch.setattr(feedback.memory_store, "remember", fake_remember)
    monkeypatch.setattr("factory.hitl.interrupts.request", fake_request)
    ctx = _Ctx(
        ProposedLesson(
            durable=True,
            content="Contact leads even when they are on the suppression list.",
            scope="company",
            why="This would loosen the do-not-contact list, which is a rail.",
            touches_a_rail=True,
        )
    )
    session = SimpleNamespace(get=fake_get)
    task = SimpleNamespace(id="t-1", payload={"interrupt_id": "int-1", "action": "none"})

    result = await feedback.learn(ctx, session, task)

    assert remembered == [], "a rail must not move by degrees"
    assert len(raised) == 1
    assert raised[0]["reason"] is InterruptReason.CONSTRAINT_CHALLENGED
    assert "refused" in result


@pytest.mark.asyncio
async def test_an_empty_reply_is_not_worth_a_judgement(monkeypatch) -> None:
    """Acknowledging an escalation without saying anything contains no standard.

    `learn` already returns "nothing was said" -- but only after the task exists, so
    an empty reply still cost a queued task and a model call to conclude exactly
    that. Found by an integration test asserting an empty reply enqueues nothing at
    all, which this had quietly broken: the unit tests all passed, because none of
    them had a reason to pass an empty answer.
    """
    enqueued: list[dict] = []

    async def fake_enqueue(session, **kwargs):
        enqueued.append(kwargs)
        return "t-1"

    monkeypatch.setattr(feedback.repository, "enqueue_task", fake_enqueue)

    for blank in ("", "   ", "\n"):
        assert (
            await feedback.capture(None, interrupt=_interrupt(), answer=blank, action="none")
            is None
        )
    assert enqueued == []
