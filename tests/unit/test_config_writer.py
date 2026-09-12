"""`write_env_value`/`read_env_value` -- the dashboard's credential
endpoints' only actual filesystem interaction. Pure unit tests against
`tmp_path`; no app, no settings, no DB.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.config_writer import InvalidEnvValue, read_env_value, write_env_value


def test_appends_a_new_key_to_an_empty_file(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("", encoding="utf-8")

    write_env_value(target, "HUNTER_API_KEY", "abc123")

    assert target.read_text(encoding="utf-8") == "HUNTER_API_KEY=abc123\n"


def test_creates_the_file_if_it_does_not_exist(tmp_path: Path) -> None:
    target = tmp_path / ".env"

    write_env_value(target, "HUNTER_API_KEY", "abc123")

    assert target.read_text(encoding="utf-8") == "HUNTER_API_KEY=abc123\n"


def test_replaces_an_existing_keys_line_in_place(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text(
        "GEMINI_API_KEY=old\nHUNTER_API_KEY=stale\nSTRIPE_API_KEY=untouched\n", encoding="utf-8"
    )

    write_env_value(target, "HUNTER_API_KEY", "fresh")

    assert target.read_text(encoding="utf-8") == (
        "GEMINI_API_KEY=old\nHUNTER_API_KEY=fresh\nSTRIPE_API_KEY=untouched\n"
    )


def test_preserves_comments_and_blank_lines(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("# a comment\n\nHUNTER_API_KEY=old\n", encoding="utf-8")

    write_env_value(target, "HUNTER_API_KEY", "new")

    assert target.read_text(encoding="utf-8") == "# a comment\n\nHUNTER_API_KEY=new\n"


def test_a_bare_mention_inside_a_comment_does_not_count_as_the_keys_line(tmp_path: Path) -> None:
    """`# HUNTER_API_KEY=disabled` must not be mistaken for the real line -- only
    a line that actually *starts* with `KEY=` is a match."""
    target = tmp_path / ".env"
    target.write_text("# HUNTER_API_KEY=disabled\n", encoding="utf-8")

    write_env_value(target, "HUNTER_API_KEY", "new")

    assert target.read_text(encoding="utf-8") == (
        "# HUNTER_API_KEY=disabled\nHUNTER_API_KEY=new\n"
    )


def test_rejects_a_value_containing_a_newline(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("", encoding="utf-8")

    with pytest.raises(InvalidEnvValue):
        write_env_value(target, "HUNTER_API_KEY", "abc\nGITHUB_TOKEN=hijacked")

    assert target.read_text(encoding="utf-8") == ""


def test_read_env_value_round_trips_with_write(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    write_env_value(target, "MY_CUSTOM_KEY", "abc123")

    assert read_env_value(target, "MY_CUSTOM_KEY") == "abc123"


def test_read_env_value_returns_none_for_a_missing_key(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("OTHER_KEY=x\n", encoding="utf-8")

    assert read_env_value(target, "MY_CUSTOM_KEY") is None


def test_read_env_value_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    assert read_env_value(tmp_path / "does-not-exist.env", "MY_CUSTOM_KEY") is None


def test_read_env_value_ignores_a_bare_mention_inside_a_comment(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("# MY_CUSTOM_KEY=disabled\nMY_CUSTOM_KEY=real\n", encoding="utf-8")

    assert read_env_value(target, "MY_CUSTOM_KEY") == "real"
