"""Design engine runner.

Deliberately thin, and the shape is the point. Development's `run()` is a dispatch
table because somebody else already decided what needed doing; Design's entry
point is mostly one task kind -- the manager's tick -- and the work it performs
exists because that manager chose to commission it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.state.orm import Task
from factory.state.schemas import TaskKind
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    kind = TaskKind(task.kind)

    if kind is TaskKind.ANSWER_QUESTION:
        from factory.answering import answer_question_task

        return await answer_question_task(ctx, session, task)

    if kind is TaskKind.DESIGN_MANAGE:
        from factory.engines.design.manager import run_manager_tick

        return await run_manager_tick(ctx, session, task)

    if kind is TaskKind.WRITE_DESIGN_GUIDELINES:
        from factory.engines.design.work import write_guidelines_task

        return await write_guidelines_task(ctx, session, task)

    if kind is TaskKind.REVIEW_PRODUCT_DESIGN:
        from factory.engines.design.work import review_design_task

        return await review_design_task(ctx, session, task)

    return f"design: nothing to do for task kind {task.kind}"
