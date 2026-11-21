"""Holds fixtures and configuration for the test suite."""

import os

import param
import pytest
from panel.config import panel_extension
from panel.io.reload import _local_modules, _modules, _watched_files
from panel.io.state import state
from panel.theme import Design

from panel_flowdash.util import ReactiveError

optional_markers = {
    "ui": {
        "help": "Runs UI related tests",
        "marker-descr": "UI test marker",
        "skip-reason": "Test only runs with the --ui option.",
    },
}


def pytest_addoption(parser):
    """Add extra command line options."""
    for marker, info in optional_markers.items():
        parser.addoption(f"--{marker}", action="store_true", default=False, help=info["help"])
    parser.addoption("--repeat", action="store", help="Number of times to repeat each test")
    parser.addoption(
        "--shard",
        action="store",
        default=None,
        metavar="I/N",
        help="Run only the I-th of N interleaved slices of the collected tests (1-based).",
    )


def pytest_configure(config):
    """Add extra markers."""
    for marker, info in optional_markers.items():
        config.addinivalue_line("markers", "{}: {}".format(marker, info["marker-descr"]))

    config.addinivalue_line("markers", "internet: mark test as requiring an internet connection")
    config.addinivalue_line(
        "markers", "failures: needs param.ReactiveError to carry failures along edges"
    )


def _select_shard(config, items):
    shard = config.getoption("--shard")
    if not shard:
        return
    try:
        index, count = (int(part) for part in shard.split("/"))
    except ValueError:
        raise pytest.UsageError(f"--shard expects I/N, got {shard!r}") from None
    if not 1 <= index <= count:
        raise pytest.UsageError(f"--shard index must be between 1 and {count}, got {index}")
    # Interleaving spreads each module's tests over every shard, so the slow
    # modules do not pile up in one shard and the shards take about as long.
    keep, drop = [], []
    for position, item in enumerate(items):
        (keep if position % count == index - 1 else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def pytest_collection_modifyitems(config, items):
    """Keep the --shard slice and skip optional tests and failure tests param cannot run."""
    _select_shard(config, items)
    for marker, info in optional_markers.items():
        if config.getoption(f"--{marker}"):
            continue
        skip = pytest.mark.skip(reason=info["skip-reason"])
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)
    if ReactiveError is not None:
        return
    if os.environ.get("FLOWDASH_REQUIRE_FAILURES"):
        raise pytest.UsageError(
            "FLOWDASH_REQUIRE_FAILURES is set but param "
            f"{param.__version__} cannot carry failures along edges (needs >= 2.5)."
        )
    skip = pytest.mark.skip(reason="Carrying failures along edges needs param >= 2.5.")
    for item in items:
        if "failures" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def module_cleanup():
    """Cleanup Panel extensions after each test."""
    from bokeh.core.has_props import _default_resolver
    from panel.reactive import ReactiveMetaBase

    to_reset = list(panel_extension._imports.values())
    _default_resolver._known_models = {
        name: model
        for name, model in _default_resolver._known_models.items()
        if not any(model.__module__.startswith(tr) for tr in to_reset)
    }
    ReactiveMetaBase._loaded_extensions = set()


@pytest.fixture(autouse=True)
def server_cleanup():
    """Clean up server state after each test."""
    try:
        yield
    finally:
        state.reset()
        _watched_files.clear()
        _modules.clear()
        _local_modules.clear()


@pytest.fixture(autouse=True)
def cache_cleanup():
    """Clean up cache."""
    state.clear_caches()
    # Panel 1.11.0rc1 moved the lru_cache to _resolve_modifiers_cached; rc0 keeps it on _resolve_modifiers.
    getattr(Design, "_resolve_modifiers_cached", Design._resolve_modifiers).cache_clear()
    Design._cache.clear()
