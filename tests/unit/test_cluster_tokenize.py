"""`tokenize`'s `include_numeric` parameter.

Regression coverage for a bug found in the 2026-08-10 full-codebase audit: the
default tokenizer only matches tokens starting with a letter, so it can never
produce a purely-numeric token. `memory/store.py` reuses this tokenizer for
near-duplicate detection, where a fact's number is frequently the entire point --
two facts differing only in a number hashed to identical token sets and were
merged as near-duplicates. Discovery clustering deliberately keeps the
letter-only default (see `cluster.py`'s `_TOKEN_WITH_NUMERIC` docstring for why).
"""

from __future__ import annotations

from factory.engines.discovery.cluster import jaccard, tokenize


def test_default_tokenize_never_produces_a_purely_numeric_token() -> None:
    tokens = tokenize("the retry cap is 40 percent")
    assert "40" not in tokens
    assert "cap" in tokens


def test_include_numeric_captures_bare_digit_runs() -> None:
    tokens = tokenize("the retry cap is 40 percent", include_numeric=True)
    assert "40" in tokens
    assert "cap" in tokens


def test_include_numeric_captures_single_digit_numbers() -> None:
    tokens = tokenize("max_concurrent_builds is 2", include_numeric=True)
    assert "2" in tokens


def test_facts_differing_only_by_number_are_distinguishable_with_include_numeric() -> None:
    without_numeric_a = tokenize("Timeout is 40 seconds.")
    without_numeric_b = tokenize("Timeout is 60 seconds.")
    assert without_numeric_a == without_numeric_b  # the bug, demonstrated directly

    with_numeric_a = tokenize("Timeout is 40 seconds.", include_numeric=True)
    with_numeric_b = tokenize("Timeout is 60 seconds.", include_numeric=True)
    assert with_numeric_a != with_numeric_b
    assert jaccard(with_numeric_a, with_numeric_b) < 0.6


def test_include_numeric_still_respects_stopwords_and_case() -> None:
    tokens = tokenize("The THING is 5 things", include_numeric=True)
    assert "the" not in tokens  # stopword
    assert "thing" not in tokens  # stopword, and case-folded
    assert "5" in tokens
