"""A short, cached, per-product visual design summary for the dashboard.

Generated once from the product's real deployed page -- not a guess from the
idea text -- and cached on `Product.design_brief` (JSON), the same shape as
`marketing/leads.py::build_qualification_brief` caches `QualificationBrief`.
Triggered two ways: automatically the moment a build first ships
(`development/graph.py::_verify_and_ship`), or on demand for a product that
was already live before this existed (`api/dashboard.py`'s
generate-design-brief endpoint enqueues `TaskKind.GENERATE_DESIGN_BRIEF`,
handled by `development/graph.py::run`).
"""

from __future__ import annotations

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.maintenance import check_deploy_url
from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.schemas import Contract
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

_DESIGN_BRIEF_SYSTEM = """\
You are a design reviewer describing the visual identity of a real, live web page \
from its actual rendered HTML -- not inventing one.

Read the HTML and report what is actually there:
- palette: 2-6 hex color codes genuinely used for backgrounds, text, or accents \
(from inline styles, <style> blocks, or class names that name a color), primary/\
most prominent color first. Do not invent colors that aren't evidenced in the markup.
- typography: one or two sentences on the actual font choices and type scale in use \
(e.g. "a bold sans-serif headline over a smaller serif body", or "system UI font \
throughout, no custom typography") -- describe what's there, including "no custom \
typography" if that's the truth.
- tone: one or two sentences on the voice/mood the page's actual copy and layout \
convey (e.g. "minimal and technical," "warm and consumer-friendly") -- read from the \
real headline and body text, not assumed from the product category.\
"""


class DesignBrief(Contract):
    """A factual summary of one product's real, deployed visual design --
    used as a dashboard reference for "does this still look consistent with
    what we shipped," not a prescriptive style guide the model invented."""

    palette: list[str] = Field(min_length=2, max_length=6)
    typography: str = Field(min_length=10, max_length=300)
    tone: str = Field(min_length=10, max_length=300)


async def build_design_brief(
    ctx: FactoryContext,
    session: AsyncSession,
    *,
    product_id: str,
    deploy_url: str,
    task_id: str | None = None,
) -> DesignBrief | None:
    product = await repository.get_product(session, product_id)
    if product is not None and product.design_brief:
        try:
            return DesignBrief.model_validate_json(product.design_brief)
        except ValueError:
            log.warning("design_brief.corrupt", product_id=product_id)

    degraded_reason, body = await check_deploy_url(deploy_url)
    if degraded_reason is not None or not body:
        log.info("design_brief.fetch_failed", product_id=product_id, reason=degraded_reason)
        return None

    try:
        brief = await ctx.llm.parse(
            DesignBrief,
            task_type=TaskType.ANALYSIS,
            system=_DESIGN_BRIEF_SYSTEM,
            prompt=f"Live page HTML:\n{body[:8000]}",
            engine=EngineName.DEVELOPMENT,
            task_id=task_id,
        )
    except LLMError as exc:
        log.warning("design_brief.generation_failed", product_id=product_id, detail=str(exc)[:200])
        return None

    await repository.set_design_brief(session, product_id, brief.model_dump_json())
    return brief
