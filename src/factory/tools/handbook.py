"""The `docs` tool category — looking things up, and writing things down.

Two halves, because Jesse's framing has two halves. Reading: *"I can read
documentation, I know the purpose of the product so I can gauge why it would be
important"* — `list_docs`/`read_doc`/`read_product_dossier`, plain reads on the
global registry, available to any role whose envelope includes `docs`. Writing:
*"I document what I do and justify my actions"* — `document_work`, which cannot
be a global tool because it must know **which** employee is writing, and an agent
asked to name its own role would simply be trusted on it.

So `document_work` is built per-run and bound to the real `RoleName` the spawner
resolved, via `ToolRegistry.extending()`. The agent supplies what it did and why;
the identity is supplied by the code that spawned it. Same split as everywhere
else here: *an agent decides the inputs, deterministic code decides the record.*

Pull, not push, for the reads. A prompt that carries the whole handbook costs
tokens on every run and gets skimmed; a tool call happens when the employee has an
actual question, which is also the honest signal that it had one.
"""

from __future__ import annotations

from typing import Any

from factory.logging import get_logger
from factory.settings import EngineName, get_settings
from factory.tools.registry import REGISTRY, ToolRegistry

log = get_logger(__name__)

#: Name -> (one-line summary, loader). The summary is what `list_docs` shows, so
#: it has to be enough to decide whether the document is worth a second call.
_DOCS: dict[str, str] = {
    "org": "Who works at this company, what each employee does, and what tools they hold.",
    "handoffs": "How work moves between departments, and which department owns what.",
    "policy": "The rules that bind every employee, and which are enforced in code.",
}


@REGISTRY.tool(
    name="list_docs",
    description=(
        "List the company documentation you can read. Use this when you are unsure "
        "how something here works, who owns a problem, or why a rule exists -- "
        "before guessing or working around it."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    category="docs",
)
async def list_docs() -> dict[str, Any]:
    return {"docs": [{"name": name, "about": about} for name, about in _DOCS.items()]}


@REGISTRY.tool(
    name="read_doc",
    description=(
        "Read one company document by name. Call list_docs first if you do not know "
        "the names."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": sorted(_DOCS),
                "description": "Which document to read.",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    category="docs",
)
async def read_doc(name: str) -> dict[str, Any]:
    from factory import handbook

    if name == "org":
        return {"name": name, "content": handbook.org_directory()}
    if name == "handoffs":
        return {"name": name, "content": handbook.HANDOFF_MAP}
    if name == "policy":
        return {"name": name, "content": handbook.policy_brief(get_settings())}
    return {"error": f"no document named {name!r}; call list_docs to see what exists"}


@REGISTRY.tool(
    name="read_product_dossier",
    description=(
        "Read everything the company knows about one product: what problem it "
        "solves, who it is for, how it is meant to make money, what has happened to "
        "it so far, and which repairs have already been tried. Read this before "
        "changing a product -- knowing what it is for is how you judge whether a "
        "change actually helps."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "product_id": {
                "type": "string",
                "description": "The exact product id given to you at the start of this run.",
            }
        },
        "required": ["product_id"],
        "additionalProperties": False,
    },
    category="docs",
)
async def read_product_dossier(product_id: str) -> dict[str, Any]:
    from factory import handbook
    from factory.state.db import session_scope

    async with session_scope(get_settings()) as session:
        dossier = await handbook.product_dossier(session, product_id)
    return {"product_id": product_id, "dossier": dossier}


def build_handbook_tools(
    *,
    role: str,
    task_id: str | None,
    product_id: str | None,
    engine: EngineName | None,
    base: ToolRegistry | None = None,
) -> ToolRegistry:
    """A private registry carrying `base`'s tools plus the handbook.

    The binding is the point. `role` here is the `RoleName` the caller already
    resolved and authorised, not a string the model chose, so a note can never be
    filed under an employee that did not write it.

    `base` composes with the two builders that replace the registry rather than
    extend it — `build_workspace_tools` and `BrowserSession`. Without it, the three
    roles reachable only through those (WORKSPACE_SURGEON, SEO_OPTIMIZER, AUDITOR)
    would read the shared rules promising them a handbook and find no such tools,
    which is precisely the confident-flailing failure this whole layer exists to
    end. Neither builder registers a `docs` name, so extending never collides.
    """
    registry = ToolRegistry.extending(base or REGISTRY)
    for descriptor in REGISTRY.all():
        if descriptor.category == "docs" and registry.get(descriptor.name) is None:
            registry.register(descriptor)

    @registry.tool(
        name="document_work",
        description=(
            "Record what you did and why you did it, for whoever picks this up next "
            "-- quite possibly you, weeks from now, with no memory of today. Write "
            "the reasoning, not just the change: what you found, what you decided, "
            "and what you rejected and why. If you were blocked or had to assume "
            "something, say so here. Call this once, near the end of your work."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "what_i_did": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": "The change or conclusion, concretely.",
                },
                "why": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": (
                        "The reasoning. Why this was the right thing to do, what you "
                        "considered instead, and anything you had to assume."
                    ),
                },
            },
            "required": ["what_i_did", "why"],
            "additionalProperties": False,
        },
        category="docs",
    )
    async def document_work(what_i_did: str, why: str) -> dict[str, Any]:
        from factory.memory import store as memory_store
        from factory.state import repository
        from factory.state.db import session_scope

        note = f"{what_i_did.strip()}\n\nWhy: {why.strip()}"
        async with session_scope(get_settings()) as session:
            # Two destinations because they answer different questions. The event
            # trail is *what happened to this product, in order* -- read by the
            # dossier, so the next employee on this product sees the reasoning.
            # Capability memory is *how this job tends to go* -- recalled on every
            # future spawn of this same role, across every product.
            await repository.record_task_event(
                session,
                event="agent.documented",
                task_id=task_id,
                product_id=product_id,
                engine=engine,
                detail={"role": role, "what": what_i_did[:1000], "why": why[:1000]},
            )
            await memory_store.remember(
                session,
                content=note,
                scope=memory_store.capability_scope(role),
                source_task_id=task_id,
                origin=f"agent:{role}",
            )
            await session.commit()

        log.info("handbook.documented", role=role, task_id=task_id, product_id=product_id)
        return {"recorded": True}

    return registry
