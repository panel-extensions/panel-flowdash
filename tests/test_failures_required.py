"""Guards the CI environment that exercises failure propagation.

Tests marked ``failures`` are skipped when param cannot carry failures along
edges. The ``test-param-pre`` pixi environment sets ``FLOWDASH_REQUIRE_FAILURES``
so that a param resolve without ``ReactiveError`` fails the job instead of
silently skipping them.
"""

import os

import param
import pytest

from panel_flowdash.util import ReactiveError


@pytest.mark.skipif(
    not os.environ.get("FLOWDASH_REQUIRE_FAILURES"),
    reason="Only enforced where FLOWDASH_REQUIRE_FAILURES is set.",
)
def test_param_can_carry_failures():
    assert ReactiveError is not None, (
        f"FLOWDASH_REQUIRE_FAILURES is set but param {param.__version__} has no "
        "ReactiveError, so the failure propagation tests would be skipped."
    )
