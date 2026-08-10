"""MVP scaffolding.

Claude writes the product; this module writes it to disk safely. Every generated path
is resolved and checked against the workspace root before a single byte is written,
because a model-supplied path like ``../../.env`` is a plausible output and must be a
rejected write rather than an exfiltrated credential.
"""

from __future__ import annotations

from pathlib import Path

from factory.logging import get_logger
from factory.sandbox.runner import resolve_workspace
from factory.settings import EngineName, Settings, TaskType
from factory.state.schemas import ScaffoldPlan, ValidatedIdea
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

SYSTEM_PROMPT = """\
You are the engineering agent of an autonomous SaaS factory. You produce a complete, \
runnable MVP for one validated idea.

The MVP must include, as actual files with real contents:
- A FastAPI backend with a /health endpoint and the core feature endpoints.
- A minimal single-page frontend (plain HTML/CSS/JS, no build step).
- Token-based authentication with password hashing. Never store plaintext passwords.
- Stripe Checkout billing wiring, reading the key from an environment variable. Every
  Checkout Session you create must set client_reference_id to the exact value of the
  PRODUCT_ID environment variable, read at request time. Never hard-code it, never
  omit it — it is the only way a payment can be attributed back to this product.
- A pytest suite covering the core feature and the auth boundary.
- requirements.txt pinned to major versions, and a README with run instructions.

Hard constraints:
- Every file must be complete. No TODOs, no placeholders, no "implement this".
- Read every secret from os.environ. Never hard-code a key, password, or token, not \
even a fake one.
- Do not write files outside the project directory. All paths are relative, with no \
leading slash and no "..".
- No unbounded loops, no unbounded recursion, no retry loop without a maximum, and \
no sleep longer than 5 seconds. This code runs unattended in a metered container.
- test_command must be a single shell command that runs the suite.

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
platform to avoid the failure, do only that.\
"""


class UnsafeGeneratedPath(ValueError):
    """A generated file path tried to escape the product workspace."""


async def generate_scaffold(
    ctx: FactoryContext, idea: ValidatedIdea, *, task_id: str | None = None
) -> ScaffoldPlan:
    return await ctx.llm.parse(
        ScaffoldPlan,
        task_type=TaskType.CODE_GENERATION,
        system=SYSTEM_PROMPT,
        prompt=(
            f"Validated idea:\n"
            f"- problem: {idea.problem_statement}\n"
            f"- target niche: {idea.target_niche}\n"
            f"- monetization: {idea.monetization_hypothesis}\n"
            f"- evidence: {idea.source_url}\n\n"
            "Produce the complete MVP."
        ),
        engine=EngineName.DEVELOPMENT,
        task_id=task_id,
    )


def safe_target(root: Path, relative: str) -> Path:
    """Resolve a generated path inside ``root`` or refuse it."""
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise UnsafeGeneratedPath(f"generated path escapes workspace: {relative!r}")
    return candidate


def write_plan(plan: ScaffoldPlan, workspace: str, settings: Settings | None = None) -> list[str]:
    """Materialise the plan into the product workspace. Returns the paths written."""
    root = resolve_workspace(workspace, settings)
    written: list[str] = []
    for generated in plan.files:
        target = safe_target(root, generated.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated.contents, encoding="utf-8")
        written.append(str(target.relative_to(root)))
    log.info("scaffold.written", workspace=workspace, files=len(written))
    return written


def read_workspace(workspace: str, settings: Settings | None = None) -> dict[str, str]:
    """Read back the generated source for review. Skips binaries and vendored trees."""
    root = resolve_workspace(workspace, settings)
    # .deps: where _verify_and_ship's install step puts dependencies (see graph.py)
    # so they survive into the separate test-run container. Without this, Bug
    # Council reviews third-party library source as if it were the model's own
    # code -- observed live: pytest/sqlalchemy/pydantic's legitimate internal
    # eval()/exec()/shell=True usage tripped the privilege scanner and blocked
    # every deploy.
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".deps"}
    sources: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            sources[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return sources
