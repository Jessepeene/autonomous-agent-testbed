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


async def test_a_small_change_to_a_large_file_costs_one_call(workspace: Settings) -> None:
    """The tool whose absence made every small repair expensive.

    A repair agent could read a file and write a file, with nothing in between, so
    changing two sentences in a 15KB page meant reproducing all 15KB or driving
    `sed` through the shell. Observed live: a repair commissioned for two copy
    defects spent its entire 16-step budget on shell commands, never called
    `write_workspace_file` once, and was recorded as "used its entire budget
    without finishing". It was not being careless -- it had no better move.
    """
    page = (
        "<html>\n<body>\n"
        + "<p>filler</p>\n" * 400
        + "<p>ShardRunner is not available yet.</p>\n</body>\n</html>"
    )
    (workspace.workspace_root / "product-1" / "index.html").write_text(page, encoding="utf-8")
    tools = _tools(workspace)

    result = await tools.get("edit_workspace_file").handler(
        path="index.html",
        old_string="<p>ShardRunner is not available yet.</p>",
        new_string="<p>ShardRunner shards your iOS test suite across CI runners.</p>",
    )

    assert result == {"path": "index.html", "replaced": 1, "chars_now": len(page) + 24}
    updated = (workspace.workspace_root / "product-1" / "index.html").read_text(encoding="utf-8")
    assert "not available yet" not in updated
    assert "shards your iOS test suite" in updated
    assert updated.count("<p>filler</p>") == 400, "nothing else may change"


async def test_an_ambiguous_snippet_is_refused_rather_than_guessed(
    workspace: Settings,
) -> None:
    """Guessing which of two matches was meant is how a repair edits the wrong line."""
    (workspace.workspace_root / "product-1" / "index.html").write_text(
        "<p>© 2026</p>\n<p>© 2026</p>\n", encoding="utf-8"
    )
    tools = _tools(workspace)

    result = await tools.get("edit_workspace_file").handler(
        path="index.html", old_string="<p>© 2026</p>", new_string="<p>© 2027</p>"
    )

    assert "appears 2 times" in result["error"]
    # And nothing was written.
    body = (workspace.workspace_root / "product-1" / "index.html").read_text(encoding="utf-8")
    assert body.count("2026") == 2


async def test_a_snippet_that_does_not_match_says_so_usefully(workspace: Settings) -> None:
    """The error has to tell the agent what to do next, or it burns steps guessing."""
    (workspace.workspace_root / "product-1" / "index.html").write_text(
        "<p>a</p>\n", encoding="utf-8"
    )
    tools = _tools(workspace)

    result = await tools.get("edit_workspace_file").handler(
        path="index.html", old_string="<p>b</p>", new_string="<p>c</p>"
    )

    assert "was not found" in result["error"]
    assert "read the file again" in result["error"]


async def test_the_edit_tool_cannot_escape_the_workspace(workspace: Settings) -> None:
    tools = _tools(workspace)

    result = await tools.get("edit_workspace_file").handler(
        path="../../etc/passwd", old_string="root", new_string="pwned"
    )

    assert "outside the workspace" in result["error"]
