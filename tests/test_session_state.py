"""Tests for the per-session shared state built from the registry."""

from panel_flowdash.registry import PanelAppMetadata, RegistryEntry
from panel_flowdash.session_state import build_session_state_class


def _registry(requires):
    metadata = PanelAppMetadata(requires=requires)
    entry = RegistryEntry(
        app_id="x", section="S", name="x", page_path="/x", module_name="m", metadata=metadata
    )
    return {"x": entry}


def test_fallback_is_the_default():
    cls = build_session_state_class(_registry([{"key": "tickers", "fallback": ["AAPL"]}]))
    assert cls().tickers == ["AAPL"]


def test_mutable_fallback_is_not_shared_between_sessions():
    fallback = ["AAPL"]
    cls = build_session_state_class(_registry([{"key": "tickers", "fallback": fallback}]))
    first, second = cls(), cls()

    first.tickers.append("MSFT")

    assert second.tickers == ["AAPL"]
    assert fallback == ["AAPL"]
