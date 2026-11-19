"""UI tests for the code panel docked under the canvas."""

import os

import pytest
from panel.tests.util import wait_until

from tests.test_code_panel import make_editor
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

VIEWPORT = {"width": 1440, "height": 900}
SHOTS = os.environ.get("FLOWDASH_CODE_PANEL_SHOTS")


def _open(page, editor, query):
    page.set_viewport_size(VIEWPORT)
    serve_component(page, editor)
    page.locator(f".react-flow__node[data-id='{query}']").click()
    page.locator(".fd-code-expand").first.click(timeout=8000)
    panel = page.locator(".fd-code-panel")
    expect(panel).to_be_visible(timeout=8000)
    code = page.locator(".fd-code-panel-editor")
    expect(code.locator(".ace_content")).to_contain_text("p_year > 2014", timeout=8000)
    return panel, code


def _type_at_end(page, code, text):
    code.locator(".ace_content").click()
    page.keyboard.press("Control+End")
    page.keyboard.type(text, delay=60)


def _shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, f"{name}.png"))


def test_opened_from_the_inspector_under_the_canvas(page):
    editor, query = make_editor()
    panel, _code = _open(page, editor, query)
    assert editor.code_panel_open
    canvas = page.locator(".react-flow").first.bounding_box()
    box = panel.bounding_box()
    assert box["y"] >= canvas["y"] + canvas["height"] - 1
    assert abs(box["x"] - canvas["x"]) <= 1 and abs(box["width"] - canvas["width"]) <= 1
    assert box["y"] + box["height"] <= page.viewport_size["height"]
    wait_until(lambda: editor._flow.selection["nodes"] == [query], timeout=8000)
    expect(page.locator(".flowdash-inspector, .fd-code-expand").first).to_be_visible()
    expect(page.locator(".fd-code-dialog .fd-code-field-large")).to_have_count(0)
    expect(page.locator(".fd-code-table")).to_contain_text("2016", timeout=8000)
    _shot(page, "open")


def test_typing_is_held_until_run(page):
    editor, query = make_editor()
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, " and p_cap > 2")
    expect(page.locator(".fd-code-panel-result")).to_contain_text(
        "Edited since the last run", timeout=8000
    )
    page.wait_for_timeout(500)
    assert editor._component_instances[query].expr == "p_year > 2014"
    page.locator(".fd-code-run").click()
    wait_until(
        lambda: editor._component_instances[query].expr == "p_year > 2014 and p_cap > 2",
        timeout=8000,
    )
    expect(page.locator(".fd-code-panel-result")).to_contain_text("2 rows", timeout=8000)


def test_ctrl_enter_runs(page):
    editor, query = make_editor()
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, "5")
    page.keyboard.press("Control+Enter")
    wait_until(lambda: editor._component_instances[query].expr == "p_year > 20145", timeout=8000)
    expect(page.locator(".fd-code-panel-result")).to_contain_text("0 rows", timeout=8000)


def test_a_check_error_shows_under_its_line_and_keeps_the_result(page):
    editor, query = make_editor()
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, "\nand p_yaer < 2016")
    page.keyboard.press("Control+Enter")
    message = code.locator(".fd-code-diagnostic")
    expect(message).to_contain_text("There is no column p_yaer.", timeout=8000)
    expect(code.locator(".fd-ace-error")).to_have_count(1)
    expect(code.locator(".ace_gutter-cell.ace_error")).to_have_count(1)
    line = code.locator(".ace_line").nth(1).bounding_box()
    assert message.bounding_box()["y"] >= line["y"] + line["height"] - 1
    result = page.locator(".fd-code-panel-result")
    expect(result).to_contain_text("Not run, the check failed")
    expect(result.locator(".fd-code-result-stale")).to_have_count(1)
    expect(result).to_contain_text("5 rows")
    assert editor._component_instances[query].expr == "p_year > 2014"
    _shot(page, "error")
    code.locator(".fd-code-fix").click()
    page.keyboard.press("Control+Enter")
    wait_until(
        lambda: editor._component_instances[query].expr == "p_year > 2014\nand p_year < 2016",
        timeout=8000,
    )
    expect(message).to_have_count(0, timeout=8000)


def test_a_failing_run_keeps_the_last_good_result(page):
    editor, query = make_editor()
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, " >")
    page.keyboard.press("Control+Enter")
    result = page.locator(".fd-code-panel-result")
    expect(result.locator(".fd-code-run-error")).to_contain_text("SyntaxError", timeout=8000)
    expect(result.locator(".fd-code-table")).to_contain_text("2016")


def test_columns_are_completed(page):
    editor, query = make_editor()
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, " and p_c")
    popup = page.locator(".ace_autocomplete")
    expect(popup).to_be_visible(timeout=8000)
    expect(popup).to_contain_text("p_cap")
    expect(popup).to_contain_text("number")
    _shot(page, "completion")
    page.keyboard.press("Enter")
    page.keyboard.press("Control+Enter")
    wait_until(
        lambda: editor._component_instances[query].expr == "p_year > 2014 and p_cap", timeout=8000
    )


def test_resize_collapse_and_close(page):
    editor, query = make_editor()
    panel, _code = _open(page, editor, query)
    before = panel.bounding_box()["height"]
    handle = page.locator(".fd-dock-handle")
    box = handle.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] - 150, steps=8)
    page.mouse.up()
    wait_until(lambda: panel.bounding_box()["height"] > before + 100, timeout=8000)
    resized = panel.bounding_box()["height"]
    wait_until(lambda: editor._code_dock_height > 500, timeout=8000)
    handle.focus()
    page.keyboard.press("ArrowDown")
    wait_until(lambda: panel.bounding_box()["height"] < resized - 10, timeout=8000)
    page.locator(".fd-code-collapse").click()
    wait_until(lambda: panel.bounding_box()["height"] < 60, timeout=8000)
    canvas = page.locator(".react-flow").first.bounding_box()
    assert canvas["y"] + canvas["height"] <= panel.bounding_box()["y"] + 1
    page.locator(".fd-code-collapse").click()
    wait_until(lambda: panel.bounding_box()["height"] > before + 50, timeout=8000)
    page.locator(".fd-code-close").click()
    expect(page.locator(".fd-code-panel")).to_be_hidden(timeout=8000)
    assert not editor.code_panel_open


def test_narrow_windows_use_the_dialog(page):
    editor, query = make_editor()
    page.set_viewport_size({"width": 900, "height": 800})
    serve_component(page, editor)
    page.locator(f".react-flow__node[data-id='{query}']").click()
    page.locator(".fd-code-expand").first.click(timeout=8000)
    expect(page.locator(".fd-code-field-large")).to_have_count(1, timeout=8000)
    assert not editor.code_panel_open


def test_viewers_get_no_panel(page):
    editor, query = make_editor()
    editor.open_code_panel(query, "expr")
    editor.editable = False
    page.set_viewport_size(VIEWPORT)
    serve_component(page, editor)
    page.wait_for_timeout(500)
    expect(page.locator(".fd-code-panel")).to_be_hidden()


@pytest.mark.skipif(not SHOTS, reason="Screenshots for comparing with the design")
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_screenshots(page, scheme):
    import panel_material_ui as pmui

    editor, query = make_editor()
    page.set_viewport_size(VIEWPORT)
    serve_component(page, pmui.Page(main=[editor], dark_theme=scheme == "dark"))
    page.locator(f".react-flow__node[data-id='{query}']").click()
    page.locator(".fd-code-expand").first.click(timeout=8000)
    code = page.locator(".fd-code-panel-editor")
    expect(code.locator(".ace_content")).to_contain_text("p_year > 2014", timeout=8000)
    _type_at_end(page, code, "\nand p_yaer < 2016")
    page.keyboard.press("Control+Enter")
    expect(code.locator(".fd-code-diagnostic")).to_have_count(1, timeout=8000)
    page.wait_for_timeout(500)
    _shot(page, f"error-{scheme}")
    page.keyboard.press("Control+End")
    for _ in range(len("p_yaer < 2016")):
        page.keyboard.press("Backspace")
    page.keyboard.type("p_c", delay=60)
    expect(page.locator(".ace_autocomplete")).to_contain_text("p_cap", timeout=8000)
    page.wait_for_timeout(300)
    _shot(page, f"completion-{scheme}")
