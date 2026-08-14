"""`api/abuse.py::hash_ip` -- the pure, no-database half of the abuse controls.

The fixed-window counter (`hit`) and the FastAPI dependency (`rate_limit`) both
need real Postgres to exercise meaningfully (the atomicity of the upsert is the
whole point) -- see `tests/integration/test_validation_gate.py` for those. This
file covers only what can be verified without a database: what IP a request is
judged to come from, and the one property that must never be violated no matter
what -- an unconfigured secret must never produce a hash of anything.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from factory.api.abuse import hash_ip
from factory.settings import Settings


def _request(*, xff: str | None = None, client_host: str | None = "203.0.113.7") -> MagicMock:
    request = MagicMock()
    request.headers = {"x-forwarded-for": xff} if xff is not None else {}
    request.client = MagicMock(host=client_host) if client_host is not None else None
    return request


def test_no_secret_configured_means_no_hash_at_all(settings: Settings) -> None:
    """The load-bearing property: `abuse_ip_hash_secret` unset must mean "record
    no IP", never "record a weaker one". Plain `sha256(ip)` is reversible in
    seconds for an IPv4 address (2**32 preimages) -- falling back to it here
    would silently defeat the entire point of hashing in the first place."""
    unset = settings.model_copy(update={"abuse_ip_hash_secret": None})

    assert hash_ip(_request(xff="203.0.113.7"), unset) is None


def test_hash_is_deterministic_for_the_same_ip_and_secret(settings: Settings) -> None:
    from pydantic import SecretStr

    configured = settings.model_copy(update={"abuse_ip_hash_secret": SecretStr("test-secret")})

    first = hash_ip(_request(xff="203.0.113.7"), configured)
    second = hash_ip(_request(xff="203.0.113.7"), configured)

    assert first is not None
    assert first == second


def test_different_ips_hash_differently(settings: Settings) -> None:
    from pydantic import SecretStr

    configured = settings.model_copy(update={"abuse_ip_hash_secret": SecretStr("test-secret")})

    a = hash_ip(_request(xff="203.0.113.7"), configured)
    b = hash_ip(_request(xff="198.51.100.9"), configured)

    assert a != b


def test_the_same_ip_hashes_differently_under_a_different_secret(settings: Settings) -> None:
    """Confirms the hash actually depends on the secret, not just the IP -- a
    plain unsalted sha256 would pass every other test in this file too."""
    from pydantic import SecretStr

    secret_a = settings.model_copy(update={"abuse_ip_hash_secret": SecretStr("secret-a")})
    secret_b = settings.model_copy(update={"abuse_ip_hash_secret": SecretStr("secret-b")})

    assert hash_ip(_request(xff="203.0.113.7"), secret_a) != hash_ip(
        _request(xff="203.0.113.7"), secret_b
    )


def test_takes_only_the_first_forwarded_address(settings: Settings) -> None:
    """The Caddyfile overwrites X-Forwarded-For with a single value
    ({remote_host}), but this must still behave sanely if that config ever
    drifts or a request arrives with no proxy in front at all (e.g. a local
    docker-compose run) and a comma-joined chain shows up."""
    from pydantic import SecretStr

    configured = settings.model_copy(update={"abuse_ip_hash_secret": SecretStr("test-secret")})

    chained = hash_ip(_request(xff="203.0.113.7, 10.0.0.1, 10.0.0.2"), configured)
    direct = hash_ip(_request(xff="203.0.113.7"), configured)

    assert chained == direct


def test_falls_back_to_the_asgi_peer_address_with_no_proxy_header(settings: Settings) -> None:
    from pydantic import SecretStr

    configured = settings.model_copy(update={"abuse_ip_hash_secret": SecretStr("test-secret")})

    via_header = hash_ip(_request(xff="203.0.113.7", client_host=None), configured)
    via_peer = hash_ip(_request(xff=None, client_host="203.0.113.7"), configured)

    assert via_header == via_peer


def test_no_ip_available_at_all_returns_none(settings: Settings) -> None:
    from pydantic import SecretStr

    configured = settings.model_copy(update={"abuse_ip_hash_secret": SecretStr("test-secret")})

    assert hash_ip(_request(xff=None, client_host=None), configured) is None
