"""`verification.grounding` -- the shared "does an LLM's claim actually
appear in its real source" checks (Tier 1 finding #4, Tier 2 finding #9,
system-wide verification plan).
"""

from __future__ import annotations

from factory.verification.grounding import claim_in_source, claim_loosely_in_source

_PAGE = "Ada Lovelace, Head of Finance at Acme Books. Contact: ada@acme-books.test"


# --- claim_in_source (exact substring) --------------------------------------------


def test_a_real_substring_is_found() -> None:
    assert claim_in_source("Ada Lovelace", _PAGE) is True


def test_a_fabricated_value_is_not_found() -> None:
    assert claim_in_source("Ada Byron", _PAGE) is False


def test_matching_is_case_and_whitespace_insensitive() -> None:
    assert claim_in_source("ada   lovelace", _PAGE.upper()) is True


def test_an_empty_claim_never_matches() -> None:
    """An empty string is technically a substring of everything -- that must
    never be mistaken for "the claim was verified"."""
    assert claim_in_source("", _PAGE) is False
    assert claim_in_source("   ", _PAGE) is False


def test_a_paraphrased_value_is_correctly_rejected_by_the_exact_check() -> None:
    """The whole reason `claim_loosely_in_source` exists as a separate
    function: an exact check is the *correct*, stricter behavior for a value
    that's supposed to be copied verbatim."""
    assert claim_in_source("Financial Lead", _PAGE) is False


# --- claim_loosely_in_source (at least one distinctive word) ----------------------


def test_a_paraphrased_title_with_a_shared_distinctive_word_passes() -> None:
    assert claim_loosely_in_source("Finance Lead", _PAGE) is True


def test_an_exact_title_still_passes_the_loose_check() -> None:
    assert claim_loosely_in_source("Head of Finance", _PAGE) is True


def test_a_title_sharing_no_real_word_with_the_page_fails() -> None:
    assert claim_loosely_in_source("Chief Marketing Officer", _PAGE) is False


def test_short_words_below_the_length_floor_are_ignored() -> None:
    """"CEO" is 3 characters -- below the default floor -- so a claim made up
    entirely of short words has nothing distinctive to check and must fail
    closed, not silently pass because nothing could be checked."""
    assert claim_loosely_in_source("CEO", _PAGE) is False


def test_a_lower_length_floor_can_admit_a_short_word() -> None:
    assert claim_loosely_in_source("CEO", "The CEO signed off on this.", min_word_length=3) is True


def test_an_empty_claim_never_matches_loosely_either() -> None:
    assert claim_loosely_in_source("", _PAGE) is False
