"""Reading a file must never leave an agent unsure whether it has the whole thing.

This is the bug that cost more than any other single thing in this codebase, and it
came back after being "fixed":

  * `MAX_RESULT_CHARS` was 6_000 against a 20_277-char `app/main.py`. The read came
    back with its middle removed, the agent distrusted it, and rebuilt the file with
    `sed -n '1,130p'`, `'130,280p'`, ... until its step budget was gone. Three live
    repairs failed that way.
  * It was raised to 24_000. Every regeneration adds code, the file reached 34_604,
    and the identical failure returned -- four consecutive repair attempts paging one
    file with seven `sed` calls each and changing nothing, while the fallback
    (regenerating the whole product) made the file bigger still.

The lesson is not "pick a bigger number". A file has no signal at its ends the way a
pytest dump does, so middle-elision produces something that looks complete and is
not. These tests pin the two properties that fix it for good: a read is either
whole, or it says exactly what it is missing and how to get it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.engines.development.repair import MAX_RESULT_CHARS, _result_budget
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import Settings
from factory.tools.workspace import build_workspace_tools


@pytest.fixture
def workspace(tmp_path: Path) -> Settings:
    (tmp_path / "product-1" / "app").mkdir(parents=True)
    return Settings(gemini_api_key="k", workspace_root=tmp_path)


def _tools(settings: Settings, *, max_file_chars: int = 60_000):  # type: ignore[no-untyped-def]
    return build_workspace_tools(
        workspace="product-1",
        settings=settings,
        sandbox=DisabledSandbox(),
        test_command="pytest -q",
        max_file_chars=max_file_chars,
    )


async def test_a_file_that_fits_comes_back_whole(workspace: Settings) -> None:
    body = "\n".join(f"line {i}" for i in range(200))
    (Path(workspace.workspace_root) / "product-1" / "app" / "main.py").write_text(body)

    result = await _tools(workspace).get("read_workspace_file").handler(path="app/main.py")

    assert result["content"] == body
    assert result["complete"] is True
    assert "next_offset" not in result


async def test_a_file_too_large_pages_instead_of_eliding(workspace: Settings) -> None:
    """The core property. The old behaviour returned the head and the tail with the
    middle silently gone; an agent could not tell that from a complete file."""
    body = "\n".join(f"line {i}" for i in range(1000))
    (Path(workspace.workspace_root) / "product-1" / "app" / "main.py").write_text(body)

    result = await _tools(workspace, max_file_chars=2_000).get("read_workspace_file").handler(
        path="app/main.py"
    )

    assert result["complete"] is False
    assert "next_offset" in result
    assert "more line(s)" in result["more"]
    # Contiguous from the top, never a head-and-tail splice.
    assert result["content"].startswith("line 0")
    assert "line 999" not in result["content"]


async def test_paging_reaches_the_end_and_loses_nothing(workspace: Settings) -> None:
    body = "\n".join(f"line {i}" for i in range(1000))
    (Path(workspace.workspace_root) / "product-1" / "app" / "main.py").write_text(body)
    read = _tools(workspace, max_file_chars=2_000).get("read_workspace_file").handler

    collected = ""
    offset, pages = 0, 0
    while True:
        page = await read(path="app/main.py", offset=offset)
        collected += page["content"]
        pages += 1
        if "next_offset" not in page:
            break
        offset = page["next_offset"]
        assert pages < 50, "paging did not terminate"

    assert collected == body
    assert pages > 1


async def test_every_page_says_where_it_is(workspace: Settings) -> None:
    """An agent must never have to infer its position. The `sed` fallback happened
    precisely because the read did not say."""
    body = "\n".join(f"line {i}" for i in range(500))
    (Path(workspace.workspace_root) / "product-1" / "app" / "main.py").write_text(body)

    first = await _tools(workspace, max_file_chars=1_000).get("read_workspace_file").handler(
        path="app/main.py"
    )
    assert first["lines"].startswith("1-")
    assert first["lines"].endswith("of 500")


class TestResultBudget:
    def test_it_is_derived_from_the_transcript_not_chosen(self) -> None:
        """A flat constant outgrew itself twice. Half the transcript budget cannot be
        outgrown by a file, because the only real constraint on one result is that it
        must not crowd out the rest of the run."""
        assert _result_budget(120_000) == 60_000
        assert _result_budget(300_000) == 150_000

    def test_the_file_that_broke_four_repairs_now_fits(self) -> None:
        assert 34_604 <= _result_budget(120_000)

    def test_a_role_with_no_transcript_ceiling_still_gets_a_floor(self) -> None:
        assert _result_budget(None) == MAX_RESULT_CHARS
