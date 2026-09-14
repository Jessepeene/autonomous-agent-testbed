"""A landing page must stay a landing page through its whole life.

`_verify_and_ship` took `is_validation` as a parameter and two of its four call
sites -- both repair paths -- never passed it. So a validation page verified
correctly when it was built, and the moment it needed any repair it was re-verified
as a full MVP. The codegen contract then demanded STRIPE_API_KEY,
PRODUCT_PRICE_CENTS and client_reference_id on a page whose own requirements list
payment as out of scope.

The repair agent built the checkout it was told to build. That checkout had no
product behind it, so it returned 422. A reviewer found the broken checkout, a
repair was commissioned, and that ran for 25 days. The product became an MVP
because the gate kept insisting it was one.

A fact that four call sites must remember is a fact waiting to be dropped, and it
was. It lives on the product now.
"""

from __future__ import annotations

from factory.engines.development.contract import missing_contract_requirements

_PRODUCT_ID = "p" * 32
_PITCH_PAGE = {
    "public/index.html": (
        "<html><body><h1>iOS test sharding</h1>"
        f'<form action="/interest/{_PRODUCT_ID}"></form>'
        f'<script>fetch("/analytics/{_PRODUCT_ID}")</script>'
        "</body></html>"
    ),
    "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
}


def _stripe_demands(missing: list[str]) -> list[str]:
    return [m for m in missing if "STRIPE" in m or "PRODUCT_PRICE_CENTS" in m or "client_ref" in m]


def test_a_validation_page_is_never_asked_for_payment_wiring() -> None:
    """Its own requirements list payment as out of scope. A gate demanding the
    opposite is the contradiction that produced 25 days of repair."""
    missing = missing_contract_requirements(
        _PITCH_PAGE, product_id=_PRODUCT_ID, is_validation=True
    )
    assert _stripe_demands(missing) == []


def test_a_real_mvp_still_is() -> None:
    """The rail that does not move: a product that takes money must read the key,
    the price and the attribution id from the environment. Nothing here weakens
    that -- it only stops it being applied to pages that take no money."""
    missing = missing_contract_requirements(
        _PITCH_PAGE, product_id=_PRODUCT_ID, is_validation=False
    )
    assert len(_stripe_demands(missing)) == 3


def test_the_kind_defaults_to_validation() -> None:
    """Every product genuinely starts as a landing page: idea, page, real interest,
    then build_mvp. Defaulting to `validation` is the lifecycle, not a guess -- and
    a product only becomes `mvp` where `_build_mvp` actually runs."""
    from factory.state.orm import Product

    assert Product.__table__.c.build_kind.default.arg == "validation"


def test_verify_and_ship_cannot_be_told_the_wrong_kind() -> None:
    """The shape of the fix. It is not that the callers were corrected -- it is that
    there is no longer a parameter to get wrong."""
    import inspect

    from factory.engines.development import graph

    params = inspect.signature(graph._verify_and_ship).parameters
    assert "is_validation" not in params
