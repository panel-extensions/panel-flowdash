"""The wind energy example's Raw Data Export view builds without warnings on every supported Panel."""

import sys
from pathlib import Path

import pandas as pd
import panel_material_ui as pmui
import pytest

EXAMPLE = Path(__file__).parents[1] / "examples" / "wind_energy"


@pytest.fixture
def raw_export(monkeypatch):
    monkeypatch.setattr(sys, "path", [str(EXAMPLE), *sys.path])
    from Views import raw_export

    yield raw_export
    for name in list(sys.modules):
        if name.split(".")[0] == "Views":
            del sys.modules[name]


def test_download_is_an_outlined_button(raw_export):
    view = raw_export.app(turbines=pd.DataFrame({"t_cap": [1500, 2000]}))
    (download,) = view.__panel__().select(pmui.FileDownload)
    assert download.variant == "outlined"
    assert download.visible
