"""Tests for normalizing programmatic component declarations into a registry."""

import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.component_library import normalize_components
from panel_flowdash.component_spec import build_component_specs
from panel_flowdash.registry import RegistryEntry, build_registry


@register(page=False, component=True, provides=[{"key": "ticker", "type": "str"}])
def ticker_select(config):
    return "selector"


@register(page=False, component=True, requires=[{"key": "ticker", "type": "str"}])
def price_chart(config):
    return "chart"


class Upper(Viewer):
    """An undecorated Viewer, which should still register as a component."""

    text = param.String()

    @param.output(param.String)
    def shouted(self):
        return self.text.upper()

    def __panel__(self):
        return self.text


class TestNormalizeComponents:
    async def test_single_function(self):
        registry = normalize_components(ticker_select)
        (entry,) = registry.values()
        assert entry.app_id in registry
        assert entry.metadata.component
        assert not entry.metadata.page

    async def test_list_of_components(self):
        registry = normalize_components([ticker_select, price_chart])
        assert len(registry) == 2
        assert all(e.metadata.component for e in registry.values())

    async def test_undecorated_viewer_defaults_to_component(self):
        registry = normalize_components(Upper)
        (entry,) = registry.values()
        assert entry.metadata.component
        assert not entry.metadata.page

    async def test_load_is_a_noop_for_live_objects(self):
        registry = normalize_components(ticker_select)
        (entry,) = registry.values()
        assert entry.app is ticker_select
        assert entry.load() is ticker_select

    async def test_explicit_ids_from_mapping(self):
        registry = normalize_components({"Custom/a": ticker_select, "Custom/b": Upper})
        assert sorted(registry) == ["Custom/a", "Custom/b"]
        assert registry["Custom/a"].section == "Custom"
        assert registry["Custom/a"].name == "a"
        assert registry["Custom/a"].page_path == "/Custom/a"

    async def test_registry_entry_passes_through(self):
        entry = RegistryEntry.from_app(ticker_select, app_id="Passed/through")
        registry = normalize_components(entry)
        assert registry["Passed/through"] is entry

    async def test_existing_registry_mapping_passes_through(self, tmp_path):
        _write_project(tmp_path)
        scanned = build_registry(tmp_path)
        assert normalize_components(scanned) == scanned

    async def test_duplicate_ids_are_suffixed_not_dropped(self):
        registry = normalize_components([ticker_select, ticker_select])
        assert len(registry) == 2
        assert all(e.app is ticker_select for e in registry.values())

    async def test_mixed_list_of_every_supported_kind(self, tmp_path):
        _write_project(tmp_path)
        registry = normalize_components(
            [
                tmp_path,
                ticker_select,
                {"Custom/upper": Upper},
                RegistryEntry.from_app(price_chart, app_id="Custom/chart"),
            ]
        )
        assert "Analytics/selector" in registry
        assert "Custom/upper" in registry
        assert "Custom/chart" in registry

    async def test_non_directory_string_is_rejected(self):
        with pytest.raises(TypeError, match="paths to an existing project directory"):
            normalize_components("not-a-directory")

    async def test_specs_build_from_normalized_registry(self):
        specs = build_component_specs(normalize_components([ticker_select, price_chart, Upper]))
        by_ports = {
            spec.title: ([p.name for p in spec.inputs], [p.name for p in spec.outputs])
            for spec in specs.values()
        }
        assert by_ports["ticker select"] == ([], ["ticker"])
        assert by_ports["price chart"] == (["ticker"], [])
        assert by_ports["Upper"] == (["text"], ["shouted"])


class TestIdDerivation:
    async def test_app_convention_matches_directory_scan(self, tmp_path, monkeypatch):
        """A module exporting `app` must get the same id whichever route is used.

        Saved dashboards key components by id, so the programmatic and scanned
        paths have to agree or a dashboard authored one way cannot load the other.
        """
        # A section name unique to this test: importing it caches a module bound
        # to this tmp_path for the rest of the session, which would break any
        # other test that builds a project with the same section name.
        _write_project(tmp_path, section="LibIdSection")
        monkeypatch.syspath_prepend(str(tmp_path))
        scanned = build_registry(tmp_path)

        from LibIdSection import selector

        entry = RegistryEntry.from_app(selector.app)
        assert entry.app_id in scanned
        assert entry.section == "LibIdSection"
        assert entry.name == "selector"

    async def test_section_falls_back_when_module_is_uninformative(self):
        def bare(config):
            return "x"

        bare.__module__ = "__main__"
        entry = RegistryEntry.from_app(bare)
        assert entry.section == "Components"
        assert entry.name == "bare"


def _write_project(tmp_path, section="Analytics"):
    section = tmp_path / section
    section.mkdir()
    (section / "__init__.py").write_text("")
    (section / "selector.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=False, component=True, provides=['company'])\n"
        "def app(config):\n"
        "    return 'selector'\n"
    )
