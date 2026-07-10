"""Core tests - basic import and version check."""

import panel_flowdash


async def test_import():
    assert panel_flowdash.__version__
