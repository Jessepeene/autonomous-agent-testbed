"""The `/status` and `/issue` commands' message-matching regexes -- no DB
needed for this part."""

from __future__ import annotations

from factory.hitl.commands import ISSUE_COMMAND, STATUS_COMMAND


def test_matches_a_bare_status_command() -> None:
    assert STATUS_COMMAND.match("/status")


def test_matches_case_insensitively() -> None:
    assert STATUS_COMMAND.match("/STATUS")
    assert STATUS_COMMAND.match("/Status")


def test_matches_with_a_bot_username_suffix() -> None:
    """Telegram appends this in a group chat: "/status@MyFactoryBot"."""
    assert STATUS_COMMAND.match("/status@MyFactoryBot")


def test_matches_with_trailing_text() -> None:
    assert STATUS_COMMAND.match("/status please")


def test_does_not_match_a_longer_command_with_the_same_prefix() -> None:
    assert not STATUS_COMMAND.match("/statuses")


def test_does_not_match_a_tagged_reply() -> None:
    assert not STATUS_COMMAND.match("#57242379 retry")


def test_does_not_match_plain_text_mentioning_status() -> None:
    assert not STATUS_COMMAND.match("what's the status")


def test_issue_matches_a_tag_with_a_hash() -> None:
    match = ISSUE_COMMAND.match("/issue #2406ca12")
    assert match is not None
    assert match.group(2) == "2406ca12"


def test_issue_matches_a_tag_without_a_hash() -> None:
    match = ISSUE_COMMAND.match("/issue 2406ca12")
    assert match is not None
    assert match.group(2) == "2406ca12"


def test_issue_matches_case_insensitively() -> None:
    match = ISSUE_COMMAND.match("/ISSUE #2406CA12")
    assert match is not None
    assert match.group(2) == "2406CA12"


def test_issue_matches_with_a_bot_username_suffix() -> None:
    match = ISSUE_COMMAND.match("/issue@MyFactoryBot #2406ca12")
    assert match is not None
    assert match.group(2) == "2406ca12"


def test_issue_does_not_match_without_a_tag() -> None:
    assert not ISSUE_COMMAND.match("/issue")


def test_issue_does_not_match_a_short_or_long_tag() -> None:
    assert not ISSUE_COMMAND.match("/issue #2406ca1")
    assert not ISSUE_COMMAND.match("/issue #2406ca123")
